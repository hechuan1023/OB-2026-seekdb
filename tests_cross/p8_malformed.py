"""独立交叉验证 P8：畸形输入与鲁棒性（第八组，纯本地黑盒）。

预期从官方要求推导：非法输入必须返回错误且不能留下部分修改；进程必须继续
服务后续语句；合法但极端的输入必须被正确接受。本组同时回归两个已修复缺陷
（VARCHAR 超长谓词、深嵌套 JSON 向量），防止回退。

  R1 缺陷回归：超长谓词（=、<、UPDATE、DELETE）；深嵌套向量（500/1000/2000 层、
     事务内、多行语句内）
  R2 畸形向量字面量全谱（20 种）
  R3 极端数值（BIGINT 千位、1e309/1e400、次正规、-0、+5、巨大 LIMIT）
  R4 字符串鲁棒性（NUL、Tab、换行、反斜杠、引号、万字符、反引号中文表名、长标识符）
  R5 批量与事务压力（1000 行单语句原子性、100 条事务语句、20 表重启）
  R6 连续错误后恢复（500 条错误语句后仍可用）
"""
import json
import math
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASSED = []
FAILED = []


def check(name, condition, detail=''):
    (PASSED if condition else FAILED).append(name)
    print(('PASS ' if condition else 'FAIL ') + name + ((' :: ' + str(detail)) if detail and not condition else ''))


def f32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def parse_float(text):
    got = float(text)
    assert math.isfinite(got), text
    return got


def close_enough(actual, expected, tol=1e-5):
    return abs(actual - expected) <= tol + tol * abs(expected)


class Pipe:
    def __init__(self, directory, extra=(), ready=True):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        env.pop('MINIVECTORDB_FAILPOINT', None)
        self.process = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'minivectordb', '--data-dir', str(directory), *extra],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', bufsize=1)
        self.lines = queue.Queue()
        self.stderr_lines = []
        for target, sink in ((self.process.stdout, self.lines), (self.process.stderr, self.stderr_lines)):
            threading.Thread(target=self._pump, args=(target, sink), daemon=True).start()
        if ready:
            assert self.line(timeout=120) == 'READY'

    def _pump(self, stream, sink):
        for line in stream:
            if isinstance(sink, queue.Queue):
                sink.put(line.rstrip('\r\n'))
            else:
                sink.append(line.rstrip('\r\n'))
        if isinstance(sink, queue.Queue):
            sink.put(None)

    def line(self, timeout=120):
        value = self.lines.get(timeout=timeout)
        assert value is not None, f'unexpected exit; stderr={self.stderr_lines[-3:]}'
        return value

    def execute(self, sql, timeout=120):
        self.process.stdin.write(sql + ';\n')
        self.process.stdin.flush()
        first = self.line(timeout)
        if first.startswith('RESULT\t'):
            rows = []
            while True:
                row = self.line(timeout)
                if row.startswith('END\t'):
                    return first, rows
                rows.append(row.split('\t'))
        return first, None

    def alive(self):
        return self.process.poll() is None

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        self.process.wait(timeout=120)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def fresh():
    temp = tempfile.TemporaryDirectory(prefix='cross-p8-')
    return temp, Path(temp.name)


def rows_of(pipe, sql):
    head, rows = pipe.execute(sql)
    assert head.startswith('RESULT\t'), (sql, head)
    return rows


def expect_ok(pipe, name, sql, affected=None):
    head, _ = pipe.execute(sql)
    ok = head.startswith('OK\t') and (affected is None or head == f'OK\t{affected}')
    check(name, ok, head)
    return ok


def expect_error(pipe, name, sql):
    head, _ = pipe.execute(sql)
    check(name, head.startswith('ERROR\t'), head)
    return head


def main():
    # ================= R1：两个已修复缺陷的黑盒回归 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'R1a1 建表', 'CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(4))', 0)
            expect_ok(pipe, 'R1a2 数据', "INSERT INTO t VALUES (1,'ab'),(2,'cd')", 2)
            long_literal = 'x' * 10000
            head, rows = pipe.execute(f"SELECT id FROM t WHERE s='{long_literal}'")
            check('R1a3 超长等值谓词成功且返回空集',
                  head.startswith('RESULT\t') and rows == [], (head, rows))
            head, rows = pipe.execute(f"SELECT id FROM t WHERE s < '{long_literal}' ORDER BY id")
            check('R1a4 超长小于谓词返回全部', [r[0] for r in rows] == ['1', '2'], rows)
            head, _ = pipe.execute(f"UPDATE t SET s='zz' WHERE s='{long_literal}'")
            check('R1a5 超长谓词 UPDATE 影响 0 行', head == 'OK\t0', head)
            head, _ = pipe.execute(f"DELETE FROM t WHERE s='{long_literal}'")
            check('R1a6 超长谓词 DELETE 影响 0 行', head == 'OK\t0', head)
            rows = rows_of(pipe, 'SELECT id FROM t ORDER BY id')
            check('R1a7 超长谓词后数据不变', [r[0] for r in rows] == ['1', '2'], rows)
            expect_error(pipe, 'R1a8 插入超长仍被拒绝', "INSERT INTO t VALUES (3,'toolong')")
            check('R1a9 进程存活', pipe.alive())

            expect_ok(pipe, 'R1b1 向量表', 'CREATE TABLE v (id BIGINT PRIMARY KEY, x VECTOR(2))', 0)
            for depth in (500, 1000, 2000):
                nested = '[' * depth + '0' + ']' * depth
                head, _ = pipe.execute(f"INSERT INTO v VALUES ({depth},'[0,{nested}]')")
                check(f'R1b2 深嵌套 {depth} 层返回错误且进程存活',
                      head.startswith('ERROR\t') and pipe.alive(), head)
            # 事务内深嵌套后仍可提交
            expect_ok(pipe, 'R1b3 BEGIN', 'BEGIN', 0)
            expect_ok(pipe, 'R1b4 事务内合法插入', "INSERT INTO v VALUES (1,'[1,1]')", 1)
            nested = '[' * 1500 + '0' + ']' * 1500
            expect_error(pipe, 'R1b5 事务内深嵌套报错', f"INSERT INTO v VALUES (2,'[0,{nested}]')")
            expect_ok(pipe, 'R1b6 事务仍可提交', 'COMMIT', 0)
            rows = rows_of(pipe, 'SELECT id FROM v')
            check('R1b7 提交后只保留合法行', [r[0] for r in rows] == ['1'], rows)
            # 多行语句内深嵌套：整条失败无残留
            expect_error(pipe, 'R1b8 多行语句内深嵌套整条失败',
                         f"INSERT INTO v VALUES (3,'[1,1]'),(4,'[0,{nested}]')")
            rows = rows_of(pipe, 'SELECT id FROM v')
            check('R1b9 多行失败无残留', [r[0] for r in rows] == ['1'], rows)
            check('R1b10 全程进程存活', pipe.alive())
    finally:
        temp.cleanup()

    # ================= R2：畸形向量字面量全谱 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'R2a1 建表', 'CREATE TABLE v (id BIGINT PRIMARY KEY, x VECTOR(2))', 0)
            malformed = [
                ('尾逗号', '[1,2,]'), ('前导零', '[01,2]'), ('缺逗号', '[1 2]'),
                ('加号', '[+1,2]'), ('裸点', '[.5,2]'), ('单点', '[.,2]'),
                ('裸负号', '[-,2]'), ('指数缺位', '[1e,2]'), ('null', '[null,2]'),
                ('字符串', '["1",2]'), ('对象', '[{},2]'), ('布尔', '[true,2]'),
                ('嵌套', '[[1],2]'), ('NaN', '[NaN,2]'), ('Infinity', '[Infinity,2]'),
                ('负 Infinity', '[-Infinity,2]'), ('溢出', '[1e400,2]'),
                ('空数组', '[]'), ('维度多', '[1,2,3]'), ('维度少', '[1]'),
                ('单引号包裹', "'[1,2]'"), ('未闭合', '[1,2'), ('空串', ''),
            ]
            failures = []
            for label, literal in malformed:
                head, _ = pipe.execute(f"INSERT INTO v VALUES (9,'{literal}')")
                if not head.startswith('ERROR\t'):
                    failures.append((label, head))
            check('R2a2 23 种畸形向量全部报错', not failures, failures)
            check('R2a3 畸形输入后进程存活', pipe.alive())
            rows = rows_of(pipe, 'SELECT id FROM v')
            check('R2a4 畸形输入无残留', rows == [], rows)
            expect_ok(pipe, 'R2a5 畸形输入后仍可插入', "INSERT INTO v VALUES (1,'[1,2]')", 1)
    finally:
        temp.cleanup()

    # ================= R3：极端数值 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'R3a1 BIGINT 表', 'CREATE TABLE b (id BIGINT PRIMARY KEY, n BIGINT)', 0)
            expect_error(pipe, 'R3a2 1000 位整数拒绝', 'INSERT INTO b VALUES (1,' + '9' * 1000 + ')')
            expect_ok(pipe, 'R3a3 +5 接受', 'INSERT INTO b VALUES (2,+5)', 1)
            expect_ok(pipe, 'R3a4 -0 接受', 'INSERT INTO b VALUES (3,-0)', 1)
            rows = rows_of(pipe, 'SELECT n FROM b ORDER BY id')
            check('R3a5 +5/-0 值正确', rows == [['5'], ['0']], rows)
            expect_ok(pipe, 'R3a6 巨大 LIMIT 接受', 'CREATE TABLE l (id BIGINT PRIMARY KEY)', 0)
            expect_ok(pipe, 'R3a7 插入三行', 'INSERT INTO l VALUES (1),(2),(3)', 3)
            rows = rows_of(pipe, 'SELECT id FROM l ORDER BY id LIMIT 999999999999999999')
            check('R3a8 巨大 LIMIT 返回全部', [r[0] for r in rows] == ['1', '2', '3'], rows)
            # 向量数值极端
            expect_ok(pipe, 'R3b1 向量表', 'CREATE TABLE v (id BIGINT PRIMARY KEY, x VECTOR(1))', 0)
            expect_ok(pipe, 'R3b2 次正规 1e-45', "INSERT INTO v VALUES (1,'[1e-45]')", 1)
            expect_ok(pipe, 'R3b3 float64 最小次正规 5e-324', "INSERT INTO v VALUES (2,'[5e-324]')", 1)
            expect_ok(pipe, 'R3b4 -0.0', "INSERT INTO v VALUES (3,'[-0.0]')", 1)
            head, rows = pipe.execute("SELECT id, L2_DISTANCE(x, '[0]') AS d FROM v ORDER BY d, id")
            # 1e-45 是 float32 最小次正规（约 1.4e-45），5e-324 下溢为 0，-0.0 为 0
            expected = [('2', 0.0), ('3', 0.0), ('1', f32(1e-45))]
            check('R3b5 次正规距离按存储值计算且并列按主键升序',
                  [r[0] for r in rows] == [pk for pk, _ in expected]
                  and all(close_enough(parse_float(r[1]), d) for r, (_, d) in zip(rows, expected)),
                  rows)
            expect_error(pipe, 'R3b6 1e309 拒绝', "INSERT INTO v VALUES (4,'[1e309]')")
            expect_error(pipe, 'R3b7 1e400 拒绝', "INSERT INTO v VALUES (4,'[1e400]')")
            check('R3b8 进程存活', pipe.alive())
    finally:
        temp.cleanup()

    # ================= R4：字符串鲁棒性 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'R4a1 建表', 'CREATE TABLE s (id BIGINT PRIMARY KEY, s VARCHAR(32))', 0)
            cases = [
                ('NUL', 'a\x00b'), ('Tab', 'a\tb'), ('换行', 'a\nb'), ('回车', 'a\rb'),
                ('反斜杠', 'a\\b'), ('双引号', 'a"b'), ('单引号', "a''b"), ('分号', 'a;b'),
                ('注释符', 'a--b'), ('块注释符', 'a/*b*/c'), ('反引号', 'a`b'),
                ('表情', '😀👨‍👩‍👧'), ('组合字符', 'e\u0301e\u0301'),
            ]
            failures = []
            for index, (label, value) in enumerate(cases, 1):
                escaped = value.replace("'", "''")
                head, _ = pipe.execute(f"INSERT INTO s VALUES ({index},'{escaped}')")
                if not head.startswith('OK\t'):
                    failures.append((label, head))
            check('R4a2 13 种特殊字符串全部接受', not failures, failures)
            def unescape(text):
                mapping = {'t': '\t', 'n': '\n', 'r': '\r', '\\': '\\'}
                out, i = [], 0
                while i < len(text):
                    if text[i] == '\\' and i + 1 < len(text):
                        out.append(mapping.get(text[i + 1], text[i + 1]))
                        i += 2
                    else:
                        out.append(text[i])
                        i += 1
                return ''.join(out)
            failures = []
            for index, (label, value) in enumerate(cases, 1):
                expected = 'a\nb' if (label == '回车' and os.name == 'nt') else value
                head, rows = pipe.execute(f'SELECT s FROM s WHERE id = {index}')
                if len(rows) != 1 or unescape(rows[0][0]) != expected:
                    failures.append((label, rows, expected))
            check('R4a3 特殊字符串往返一致（反解协议转义）', not failures, failures[:3])
            # 万字符谓词（超长但合法的比较）
            expect_ok(pipe, 'R4a4 建长串表', 'CREATE TABLE big (id BIGINT PRIMARY KEY, s VARCHAR(4))', 0)
            expect_ok(pipe, 'R4a5 插入短串', "INSERT INTO big VALUES (1,'ab')", 1)
            head, rows = pipe.execute("SELECT id FROM big WHERE s = '" + 'y' * 10000 + "'")
            check('R4a6 万字符谓词成功且空集', head.startswith('RESULT\t') and rows == [], (head, rows))
            # 反引号中文表名与长标识符
            expect_ok(pipe, 'R4a7 反引号中文表名', 'CREATE TABLE `中文表` (id BIGINT PRIMARY KEY)', 0)
            expect_ok(pipe, 'R4a8 中文表插入', 'INSERT INTO `中文表` VALUES (1)', 1)
            rows = rows_of(pipe, 'SELECT id FROM `中文表`')
            check('R4a9 中文表查询', rows == [['1']], rows)
            long_name = 't_' + 'a' * 100
            expect_ok(pipe, f'R4a10 长标识符（{len(long_name)} 字符）',
                      f'CREATE TABLE {long_name} (id BIGINT PRIMARY KEY)', 0)
            expect_ok(pipe, 'R4a11 长标识符插入', f'INSERT INTO {long_name} VALUES (1)', 1)
            check('R4a12 进程存活', pipe.alive())
    finally:
        temp.cleanup()

    # ================= R5：批量与事务压力 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'R5a1 建表', 'CREATE TABLE b (id BIGINT PRIMARY KEY, n BIGINT)', 0)
            # 1000 行单语句，末尾重复主键 → 整条失败
            values = ','.join(f'({i},{i})' for i in range(1, 1001))
            expect_error(pipe, 'R5a2 1000 行单语句末尾重复整条失败',
                         'INSERT INTO b VALUES ' + values + ',(1,0)')
            rows = rows_of(pipe, 'SELECT id FROM b')
            check('R5a3 失败语句无残留', rows == [], rows)
            expect_ok(pipe, 'R5a4 1000 行重试成功', 'INSERT INTO b VALUES ' + values, 1000)
            rows = rows_of(pipe, 'SELECT id FROM b ORDER BY id DESC LIMIT 1')
            check('R5a5 1000 行全部写入', rows == [['1000']], rows)
            # 100 条语句事务：50 插入 + 25 更新 + 25 删除，回滚
            expect_ok(pipe, 'R5b1 BEGIN', 'BEGIN', 0)
            for batch in range(5):
                expect_ok(pipe, f'R5b2 事务内插入批 {batch + 1}',
                          'INSERT INTO b VALUES ' + ','.join(
                              f'({2000 + batch * 10 + i},{i})' for i in range(10)), 10)
            for step in range(25):
                expect_ok(pipe, f'R5b3 事务内更新 {step + 1}',
                          f'UPDATE b SET n = {step + 1} WHERE id = {step + 1}', 1)
            for step in range(25):
                expect_ok(pipe, f'R5b4 事务内删除 {step + 1}',
                          f'DELETE FROM b WHERE id = {step + 1}', 1)
            expect_ok(pipe, 'R5b5 ROLLBACK', 'ROLLBACK', 0)
            rows = rows_of(pipe, 'SELECT id FROM b ORDER BY id DESC LIMIT 1')
            check('R5b6 回滚后恢复 1000 行', rows == [['1000']], rows)
            rows = rows_of(pipe, 'SELECT id FROM b WHERE id >= 2000')
            check('R5b7 回滚后无事务内插入', rows == [], rows)
            # 重复一次并提交
            expect_ok(pipe, 'R5b8 BEGIN', 'BEGIN', 0)
            expect_ok(pipe, 'R5b9 事务内插入', 'INSERT INTO b VALUES (2000,0)', 1)
            expect_ok(pipe, 'R5b10 事务内删除', 'DELETE FROM b WHERE id = 1000', 1)
            expect_ok(pipe, 'R5b11 COMMIT', 'COMMIT', 0)
            rows = rows_of(pipe, 'SELECT id FROM b ORDER BY id DESC LIMIT 1')
            check('R5b12 提交后状态正确', rows == [['2000']], rows)
        # 20 表重启
        with Pipe(directory) as pipe:
            for index in range(20):
                expect_ok(pipe, f'R5c1 建表 {index}', f'CREATE TABLE m{index} (id BIGINT PRIMARY KEY, n BIGINT)', 0)
                expect_ok(pipe, f'R5c2 插入 {index}', f'INSERT INTO m{index} VALUES (1,{index})', 1)
        with Pipe(directory) as pipe:
            failures = []
            for index in range(20):
                head, rows = pipe.execute(f'SELECT n FROM m{index} WHERE id = 1')
                if rows != [[str(index)]]:
                    failures.append((index, rows))
            check('R5c3 20 表重启后全部正确', not failures, failures[:3])
    finally:
        temp.cleanup()

    # ================= R6：连续错误后恢复 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'R6a1 建表', 'CREATE TABLE t (id BIGINT PRIMARY KEY, n BIGINT)', 0)
            failures = 0
            for _ in range(500):
                head, _ = pipe.execute('SELECT nope FROM t')
                if not head.startswith('ERROR\t'):
                    failures += 1
            check('R6a2 500 条错误语句全部报错', failures == 0, failures)
            check('R6a3 连续错误后进程存活', pipe.alive())
            expect_ok(pipe, 'R6a4 错误后仍可写入', 'INSERT INTO t VALUES (1,1)', 1)
            rows = rows_of(pipe, 'SELECT n FROM t')
            check('R6a5 写入正确', rows == [['1']], rows)
            check('R6a6 正常操作 stderr 为空', pipe.stderr_lines == [], pipe.stderr_lines[:3])
    finally:
        temp.cleanup()

    print(f'\nSUMMARY passed={len(PASSED)} failed={len(FAILED)}')
    if FAILED:
        print('FAILED:', *FAILED, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
