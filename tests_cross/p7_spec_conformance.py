"""独立交叉验证 P7：规格一致性黑盒测试（第七组）。

预期只从官方赛题要求推导（docs/minivectordb-requirements.md，2026-11-10 与
官方页面核对一致），不参考实现内部行为，不使用实现自带的 failpoint 或测试
工具。错误断言只要求"返回错误且状态不变"，不绑定本地错误码（官方未冻结）。

覆盖：
  S1 未指定 ORDER BY 按集合比较；SELECT * 列序
  S2 过滤 TopK 对抗场景（全表最近记录被过滤掉；并列；空集合；K 超候选）
  S3 并列与排序（大量等距向量、普通 ORDER BY 并列按主键升序、DESC）
  S4 类型与向量语义（VARCHAR 码点计长与比较、BIGINT 边界、float32 舍入并列、
     非法向量拒绝且无残留）
  S5 事务语义（自动提交、读自身修改、语句原子性、UPDATE 原子性、回滚全恢复、
     提交持久化）
  S6 崩溃恢复（确认提交后强杀、未提交强杀、自动提交强杀、恢复途中黑盒强杀）
  S7 协议契约（同进程、stdout 仅协议、stderr 干净、响应及时、EOF 回滚）
  S8 官方 benchmark 完整形状（10,000×128、1,000 查询、K=10、一半过滤、
     两组 Recall@10 分别 ≥0.95）
"""
import json
import math
import os
import queue
import random
import struct
import subprocess
import sys
import tempfile
import threading
import time
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
        self.transcript = []
        for target, sink in ((self.process.stdout, self.lines), (self.process.stderr, self.stderr_lines)):
            threading.Thread(target=self._pump, args=(target, sink), daemon=True).start()
        if ready:
            assert self.line(timeout=120) == 'READY'

    def _pump(self, stream, sink):
        for line in stream:
            text = line.rstrip('\r\n')
            if isinstance(sink, queue.Queue):
                self.transcript.append(text)
                sink.put(text)
            else:
                sink.append(text)
        if isinstance(sink, queue.Queue):
            sink.put(None)

    def line(self, timeout=120):
        value = self.lines.get(timeout=timeout)
        assert value is not None, f'unexpected exit; stderr={self.stderr_lines[-5:]}'
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

    def kill(self):
        self.process.kill()
        return self.process.wait(timeout=60)

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
    temp = tempfile.TemporaryDirectory(prefix='cross-p7-')
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
    """官方只要求"返回约定错误、不能静默执行"，不绑定错误码。"""
    head, _ = pipe.execute(sql)
    check(name, head.startswith('ERROR\t'), head)
    return head


def topk(pipe, table, query, k, where='', column='v'):
    sql = (f"SELECT id, L2_DISTANCE({column}, '{query}') AS d FROM {table}{where} "
           f"ORDER BY d LIMIT {k}")
    head, rows = pipe.execute(sql)
    assert head.startswith('RESULT\t'), (sql, head)
    return [(r[0], parse_float(r[1])) for r in rows]


def main():
    # ================= S1：未指定 ORDER BY 按集合比较；SELECT * 列序 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S1a1 建表', 'CREATE TABLE s (id BIGINT PRIMARY KEY, tag VARCHAR(4), n BIGINT)', 0)
            expect_ok(pipe, 'S1a2 乱序插入', "INSERT INTO s VALUES (3,'b',30),(1,'a',10),(4,'b',40),(2,'a',20)", 4)
            rows = rows_of(pipe, 'SELECT id FROM s')
            check('S1a3 未指定 ORDER BY 按集合比较', sorted(int(r[0]) for r in rows) == [1, 2, 3, 4], rows)
            head, rows = pipe.execute('SELECT * FROM s')
            check('S1a4 SELECT * 列序为定义序', head == 'RESULT\tid\ttag\tn', head)
            check('S1a5 SELECT * 行集合正确',
                  sorted(tuple(r) for r in rows) == [('1', 'a', '10'), ('2', 'a', '20'),
                                                     ('3', 'b', '30'), ('4', 'b', '40')], rows)
            rows = rows_of(pipe, "SELECT id, tag FROM s WHERE tag='a'")
            check('S1a6 投影与过滤集合正确', sorted(tuple(r) for r in rows) == [('1', 'a'), ('2', 'a')], rows)
    finally:
        temp.cleanup()

    # ================= S2：过滤 TopK 对抗场景 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S2a1 建表', 'CREATE TABLE f (id BIGINT PRIMARY KEY, tag VARCHAR(8), v VECTOR(2))', 0)
            values = []
            for i, x in ((1, 0.0), (2, 0.1), (3, 0.2)):
                values.append(f"({i},'near','[{x},0]')")
            for i in range(4, 11):
                values.append(f"({i},'far','[{10 + i - 4},0]')")
            values.append("(11,'far','[10,0]')")
            values.append("(12,'far','[10,0]')")
            expect_ok(pipe, 'S2a2 插入 12 行', 'INSERT INTO f VALUES ' + ','.join(values), 12)
            got = topk(pipe, 'f', '[0,0]', 3, " WHERE tag='far'")
            check('S2a3 过滤 TopK 先过滤后取 K（全表最近被排除）',
                  [pk for pk, _ in got] == ['4', '11', '12'], got)
            check('S2a4 过滤 TopK 距离对应记录',
                  all(close_enough(d, 10.0) for _, d in got), got)
            got = topk(pipe, 'f', '[0,0]', 20, " WHERE tag='far'")
            check('S2a5 K 超过滤候选数返回全部且有序',
                  [pk for pk, _ in got] == ['4', '11', '12', '5', '6', '7', '8', '9', '10'],
                  [pk for pk, _ in got])
            got = topk(pipe, 'f', '[0,0]', 3, " WHERE tag='none'")
            check('S2a6 空过滤集合返回空结果', got == [], got)
            got = topk(pipe, 'f', '[0,0]', 1)
            check('S2a7 无过滤 TopK 取全表最近', got == [('1', 0.0)], got)
    finally:
        temp.cleanup()

    # ================= S3：并列与排序 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S3a1 建表', 'CREATE TABLE t (id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(2))', 0)
            rng = random.Random(7)
            ids = list(range(1, 51))
            rng.shuffle(ids)
            values = [f"({i},'{'x' if i % 2 == 0 else 'y'}','[1,1]')" for i in ids]
            expect_ok(pipe, 'S3a2 乱序插入 50 行等距向量',
                      'INSERT INTO t VALUES ' + ','.join(values), 50)
            got = topk(pipe, 't', '[1,1]', 10)
            check('S3a3 等距 TopK 按主键升序', [pk for pk, _ in got] == [str(i) for i in range(1, 11)],
                  [pk for pk, _ in got])
            rows = rows_of(pipe, 'SELECT id FROM t ORDER BY tag')
            expected = [str(i) for i in range(2, 51, 2)] + [str(i) for i in range(1, 50, 2)]
            check('S3a4 普通 ORDER BY 并列按主键升序', [r[0] for r in rows] == expected, rows[:5])
            rows = rows_of(pipe, 'SELECT id FROM t ORDER BY tag DESC')
            expected = [str(i) for i in range(1, 50, 2)] + [str(i) for i in range(2, 51, 2)]
            check('S3a5 DESC 并列仍按主键升序', [r[0] for r in rows] == expected, rows[:5])
            rows = rows_of(pipe, 'SELECT id FROM t ORDER BY id DESC LIMIT 3')
            check('S3a6 降序 LIMIT', [r[0] for r in rows] == ['50', '49', '48'], rows)
    finally:
        temp.cleanup()

    # ================= S4：类型与向量语义 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S4a1 VARCHAR(4) 表', 'CREATE TABLE u (id BIGINT PRIMARY KEY, s VARCHAR(4))', 0)
            expect_ok(pipe, 'S4a2 恰好 4 个汉字接受', "INSERT INTO u VALUES (1,'四个汉字')", 1)
            expect_error(pipe, 'S4a3 5 个汉字拒绝', "INSERT INTO u VALUES (2,'五个汉字啊')")
            rows = rows_of(pipe, 'SELECT id FROM u')
            check('S4a4 超长插入无残留', rows == [['1']], rows)
            expect_ok(pipe, 'S4a5 码点比较数据', "INSERT INTO u VALUES (3,'Z'),(4,'a'),(5,'中')", 3)
            rows = rows_of(pipe, "SELECT id FROM u WHERE s > 'Z'")
            check('S4a6 字符串比较按码点序（四/a/中 > Z）',
                  sorted(int(r[0]) for r in rows) == [1, 4, 5], rows)
            rows = rows_of(pipe, 'SELECT s FROM u ORDER BY s')
            check('S4a7 ORDER BY 字符串按码点序', [r[0] for r in rows] == ['Z', 'a', '中', '四个汉字'], rows)
            # BIGINT 边界
            expect_ok(pipe, 'S4b1 BIGINT 表', 'CREATE TABLE b (id BIGINT PRIMARY KEY, n BIGINT)', 0)
            expect_ok(pipe, 'S4b2 2^63-1 接受', 'INSERT INTO b VALUES (1,9223372036854775807)', 1)
            expect_ok(pipe, 'S4b3 -2^63 接受', 'INSERT INTO b VALUES (2,-9223372036854775808)', 1)
            expect_error(pipe, 'S4b4 2^63 拒绝', 'INSERT INTO b VALUES (3,9223372036854775808)')
            expect_error(pipe, 'S4b5 -2^63-1 拒绝', 'INSERT INTO b VALUES (3,-9223372036854775809)')
            expect_ok(pipe, 'S4b6 前导零接受', 'INSERT INTO b VALUES (4,0009)', 1)
            rows = rows_of(pipe, 'SELECT n FROM b WHERE id = 4')
            check('S4b7 前导零值正确', rows == [['9']], rows)
            # float32 舍入与并列
            expect_ok(pipe, 'S4c1 VECTOR(1) 表', 'CREATE TABLE v1 (id BIGINT PRIMARY KEY, v VECTOR(1))', 0)
            expect_ok(pipe, 'S4c2 16777217 与 16777216 插入',
                      'INSERT INTO v1 VALUES (1,\'[16777217]\'),(2,\'[16777216]\')', 2)
            got = topk(pipe, 'v1', '[16777216]', 2)
            check('S4c3 float32 舍入后等距，按主键升序',
                  [pk for pk, _ in got] == ['1', '2'] and all(d == 0.0 for _, d in got), got)
            got = topk(pipe, 'v1', '[16777218]', 2)
            check('S4c4 距离按存储后的 float32 值计算',
                  all(close_enough(d, 2.0) for _, d in got), got)
            # 非法向量拒绝且无残留
            for label, literal in (('NaN', '[NaN]'), ('Infinity', '[Infinity]'),
                                   ('float32 溢出', '[1e40]'), ('维度多', '[1,2]'),
                                   ('维度少', '[]'), ('非数值', '[true]'), ('嵌套', '[[1]]')):
                expect_error(pipe, f'S4c5 拒绝 {label}', f"INSERT INTO v1 VALUES (9,'{literal}')")
            rows = rows_of(pipe, 'SELECT id FROM v1')
            check('S4c6 非法向量无残留（集合比较）',
                  sorted(r[0] for r in rows) == ['1', '2'], rows)
    finally:
        temp.cleanup()

    # ================= S5：事务语义 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S5a1 建表', 'CREATE TABLE t (id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(2))', 0)
            expect_ok(pipe, 'S5a2 初始数据', "INSERT INTO t VALUES (1,'a','[9,9]'),(2,'a','[1,0]')", 2)
            # 自动提交
            expect_ok(pipe, 'S5a3 自动提交插入', "INSERT INTO t VALUES (3,'b','[2,2]')", 1)
            expect_ok(pipe, 'S5a4 自动提交更新', "UPDATE t SET v='[3,3]' WHERE id = 3", 1)
            expect_ok(pipe, 'S5a5 自动提交删除', 'DELETE FROM t WHERE id = 2', 1)
            pipe.kill()
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT id, v FROM t ORDER BY id')
            check('S5a6 自动提交强杀后全部保留',
                  rows == [['1', '[9.0,9.0]'], ['3', '[3.0,3.0]']], rows)
        # 读自身修改 + 回滚
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S5b1 建索引', 'CREATE VECTOR INDEX ti ON t(v)', 0)
            expect_ok(pipe, 'S5b2 BEGIN', 'BEGIN', 0)
            expect_ok(pipe, 'S5b3 事务内更新向量', "UPDATE t SET v='[0,0]' WHERE id = 1", 1)
            expect_ok(pipe, 'S5b4 事务内插入', "INSERT INTO t VALUES (4,'a','[0.1,0.1]')", 1)
            rows = rows_of(pipe, 'SELECT v FROM t WHERE id = 1')
            check('S5b5 普通查询读到自身修改', rows == [['[0.0,0.0]']], rows)
            got = topk(pipe, 't', '[0,0]', 1)
            check('S5b6 向量检索读到自身修改', got == [('1', 0.0)], got)
            expect_ok(pipe, 'S5b7 ROLLBACK', 'ROLLBACK', 0)
            rows = rows_of(pipe, 'SELECT id, v FROM t ORDER BY id')
            check('S5b8 回滚恢复内容与可见集合',
                  rows == [['1', '[9.0,9.0]'], ['3', '[3.0,3.0]']], rows)
            got = topk(pipe, 't', '[0,0]', 1)
            check('S5b9 回滚恢复向量检索', got == [('3', 4.242640687119285)], got)
        # 语句原子性：多行插入中途重复
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S5c1 BEGIN', 'BEGIN', 0)
            expect_ok(pipe, 'S5c2 成功修改', "UPDATE t SET v='[5,5]' WHERE id = 1", 1)
            expect_error(pipe, 'S5c3 多行插入中途重复报错',
                         "INSERT INTO t VALUES (5,'x','[1,1]'),(1,'dup','[1,1]'),(6,'x','[1,1]')")
            rows = rows_of(pipe, 'SELECT id FROM t ORDER BY id')
            check('S5c4 失败语句无部分修改（无 5/6）', [r[0] for r in rows] == ['1', '3'], rows)
            expect_ok(pipe, 'S5c5 失败后仍可提交', 'COMMIT', 0)
            pipe.kill()
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT id, v FROM t ORDER BY id')
            check('S5c6 提交后重启只保留成功修改',
                  rows == [['1', '[5.0,5.0]'], ['3', '[3.0,3.0]']], rows)
        # UPDATE 原子性：多行更新中值超出 VARCHAR 长度
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S5d1 建表', 'CREATE TABLE w (id BIGINT PRIMARY KEY, s VARCHAR(4))', 0)
            expect_ok(pipe, 'S5d2 初始数据', "INSERT INTO w VALUES (1,'aa'),(2,'bb'),(3,'cc')", 3)
            expect_error(pipe, 'S5d3 多行 UPDATE 值超长报错', "UPDATE w SET s='toolong' WHERE id >= 1")
            rows = rows_of(pipe, 'SELECT id, s FROM w ORDER BY id')
            check('S5d4 UPDATE 失败无部分修改',
                  rows == [['1', 'aa'], ['2', 'bb'], ['3', 'cc']], rows)
            expect_ok(pipe, 'S5d5 失败后可继续修改', "UPDATE w SET s='dddd' WHERE id = 2", 1)
            rows = rows_of(pipe, 'SELECT s FROM w WHERE id = 2')
            check('S5d6 后续修改生效', rows == [['dddd']], rows)
    finally:
        temp.cleanup()

    # ================= S6：崩溃恢复（黑盒强杀） =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S6a1 建表', 'CREATE TABLE t (id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(2))', 0)
            expect_ok(pipe, 'S6a2 初始数据', "INSERT INTO t VALUES (1,'a','[1,0]'),(2,'a','[0,1]'),(3,'b','[2,2]')", 3)
            expect_ok(pipe, 'S6a3 建索引', 'CREATE VECTOR INDEX ti ON t(v)', 0)
            expect_ok(pipe, 'S6a4 BEGIN', 'BEGIN', 0)
            expect_ok(pipe, 'S6a5 事务内更新', "UPDATE t SET v='[5,5]' WHERE id = 1", 1)
            expect_ok(pipe, 'S6a6 COMMIT', 'COMMIT', 0)
            pipe.kill()
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT id, tag, v FROM t ORDER BY id')
            check('S6a7 确认提交后强杀：内容保留',
                  rows == [['1', 'a', '[5.0,5.0]'], ['2', 'a', '[0.0,1.0]'], ['3', 'b', '[2.0,2.0]']], rows)
            rows = rows_of(pipe, "SELECT id FROM t WHERE tag='a' ORDER BY id")
            check('S6a8 确认提交后强杀：过滤查询正确', [r[0] for r in rows] == ['1', '2'], rows)
            got = topk(pipe, 't', '[5,5]', 1)
            check('S6a9 确认提交后强杀：向量检索正确', got == [('1', 0.0)], got)
        # 未提交事务强杀
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S6b1 BEGIN', 'BEGIN', 0)
            expect_ok(pipe, 'S6b2 未提交更新', "UPDATE t SET v='[9,9]' WHERE id = 2", 1)
            expect_ok(pipe, 'S6b3 未提交插入', "INSERT INTO t VALUES (4,'c','[0,0]')", 1)
            pipe.kill()
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT id, v FROM t ORDER BY id')
            check('S6b4 未提交事务强杀：完整回滚',
                  rows == [['1', '[5.0,5.0]'], ['2', '[0.0,1.0]'], ['3', '[2.0,2.0]']], rows)
        # 自动提交确认后强杀
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S6c1 自动提交更新', "UPDATE t SET v='[7,7]' WHERE id = 3", 1)
            pipe.kill()
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT v FROM t WHERE id = 3')
            check('S6c2 自动提交确认后强杀：保留', rows == [['[7.0,7.0]']], rows)
    finally:
        temp.cleanup()

    # ================= S6d：恢复途中黑盒强杀（不用内部 failpoint） =================
    temp, directory = fresh()
    try:
        # 构造较大 WAL，使恢复耗时足以命中窗口
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S6d1 建表', 'CREATE TABLE big (id BIGINT PRIMARY KEY, n BIGINT)', 0)
            for batch in range(50):
                values = ','.join(f'({batch * 1000 + i},{i})' for i in range(1000))
                head, _ = pipe.execute('INSERT INTO big VALUES ' + values)
                if head != 'OK\t1000':
                    check('S6d2 批量写入失败', False, (batch, head))
                    break
            else:
                check('S6d2 写入 50,000 行', True)
        # 测量一次完整恢复耗时
        started = time.perf_counter()
        with Pipe(directory) as pipe:
            recovery_seconds = time.perf_counter() - started
            rows = rows_of(pipe, 'SELECT id FROM big WHERE id = 49999')
            check('S6d3 首次恢复后数据完整', rows == [['49999']], rows)
        # 在恢复窗口内强杀两次：延迟取恢复耗时的三分之一（下限 30ms）
        delay = max(0.03, recovery_seconds / 3)
        killed_before_ready = 0
        for attempt in (1, 2):
            process = subprocess.Popen(
                [sys.executable, '-X', 'utf8', '-m', 'minivectordb', '--data-dir', str(directory)],
                cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', bufsize=1)
            time.sleep(delay)
            process.kill()
            out = process.communicate(timeout=60)[0]
            if 'READY' not in out:
                killed_before_ready += 1
            print(f'  [S6d] 第 {attempt} 次强杀：delay={delay*1000:.0f}ms，'
                  f'READY 已输出={"是" if "READY" in out else "否"}')
        check('S6d4 至少一次强杀发生在 READY 之前（恢复途中）', killed_before_ready >= 1,
              f'killed_before_ready={killed_before_ready}, recovery={recovery_seconds:.2f}s')
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT id FROM big ORDER BY id DESC LIMIT 2')
            check('S6d5 恢复途中两次强杀后仍完整恢复',
                  [r[0] for r in rows] == ['49999', '49998'], rows)
            expect_ok(pipe, 'S6d6 恢复后可继续写入', 'INSERT INTO big VALUES (50000,0)', 1)
    finally:
        temp.cleanup()

    # ================= S7：协议契约 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S7a1 建表', 'CREATE TABLE p (id BIGINT PRIMARY KEY, n BIGINT)', 0)
            pid = pipe.process.pid
            latencies = []
            ok = True
            for i in range(100):
                started = time.perf_counter()
                head, _ = pipe.execute(f'SELECT id FROM p WHERE id = {i}')
                latencies.append(time.perf_counter() - started)
                if not head.startswith('RESULT\t'):
                    ok = False
                    break
            check('S7a2 同进程连续 100 条语句', ok and pipe.process.pid == pid
                  and pipe.process.poll() is None)
            in_result, illegal = False, []
            for line in pipe.transcript:
                if line.startswith('RESULT\t'):
                    in_result = True
                elif line.startswith('END\t'):
                    in_result = False
                elif in_result:
                    continue
                elif not (line == 'READY' or line.startswith(('OK\t', 'ERROR\t'))):
                    illegal.append(line)
            check('S7a3 stdout 只含协议内容', not illegal, illegal[:3])
            check('S7a4 正常操作 stderr 为空', pipe.stderr_lines == [], pipe.stderr_lines[:3])
            check(f'S7a5 响应及时（最大 {max(latencies)*1000:.1f}ms < 2000ms）',
                  max(latencies) < 2.0, max(latencies))
            expect_error(pipe, 'S7a6 错误后进程仍可用', 'SELECT nope FROM p')
            rows = rows_of(pipe, 'SELECT id FROM p')
            check('S7a7 错误后查询正常', rows == [], rows)
            expect_ok(pipe, 'S7a8 BEGIN', 'BEGIN', 0)
            expect_ok(pipe, 'S7a9 未提交插入', 'INSERT INTO p VALUES (1,1)', 1)
            code = pipe.process.stdin.close() or pipe.process.wait(timeout=120)
            check('S7a10 EOF 正常退出（exit=0）', pipe.process.returncode == 0, pipe.process.returncode)
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT id FROM p')
            check('S7a11 EOF 回滚未提交事务', rows == [], rows)
    finally:
        temp.cleanup()

    # ================= S8：官方 benchmark 完整形状 =================
    rows_count, dimensions, queries_count, k = 10_000, 128, 1_000, 10
    seed = 20261110
    rng = random.Random(seed)
    data = {i: (f'g{i % 5}', [f32(rng.uniform(-1, 1)) for _ in range(dimensions)])
            for i in range(1, rows_count + 1)}
    queries = []
    for i in range(queries_count):
        query = [f32(rng.uniform(-1, 1)) for _ in range(dimensions)]
        queries.append((query, None if i % 2 == 0 else f'g{i % 5}'))
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S8a1 建表',
                      f'CREATE TABLE docs (id BIGINT PRIMARY KEY, tag VARCHAR(8), v VECTOR({dimensions}))', 0)
            started = time.perf_counter()
            for offset in range(1, rows_count + 1, 100):
                chunk = [(pk, data[pk][0], data[pk][1])
                         for pk in range(offset, min(offset + 100, rows_count + 1))]
                head, _ = pipe.execute('INSERT INTO docs VALUES ' + ','.join(
                    f"({pk},'{tag}','{json.dumps(vec)}')" for pk, tag, vec in chunk))
                if head != f'OK\t{len(chunk)}':
                    check('S8a2 每批 100 条写入失败', False, (offset, head))
                    break
            else:
                check('S8a2 每批 100 条写入全部成功（100 批）', True)
            write_seconds = time.perf_counter() - started
            expect_ok(pipe, 'S8a3 建向量索引', 'CREATE VECTOR INDEX docs_vec ON docs(v)', 0)
            filtered_recall, unfiltered_recall = [], []
            invalid = []
            started = time.perf_counter()
            for index, (query, tag) in enumerate(queries):
                where = '' if tag is None else f" WHERE tag='{tag}'"
                head, result = pipe.execute(
                    f"SELECT id, L2_DISTANCE(v, '{json.dumps(query)}') AS d FROM docs{where} "
                    f'ORDER BY d LIMIT {k}')
                if not head.startswith('RESULT\t'):
                    invalid.append((index, 'query failed', head))
                    continue
                truth = sorted((math.dist(row[1], query), pk)
                               for pk, row in data.items() if tag is None or row[0] == tag)[:k]
                truth_ids = [pk for _, pk in truth]
                ids = [int(r[0]) for r in result]
                if len(ids) != len(set(ids)):
                    invalid.append((index, 'duplicate', ids))
                for row in result:
                    pk = int(row[0])
                    if pk not in data:
                        invalid.append((index, 'unknown id', pk))
                        continue
                    if tag is not None and data[pk][0] != tag:
                        invalid.append((index, 'filter violated', pk))
                    if not close_enough(parse_float(row[1]), math.dist(data[pk][1], query)):
                        invalid.append((index, 'distance mismatch', pk))
                denominator = min(k, len(truth_ids))
                if denominator:
                    recall = len(set(ids) & set(truth_ids)) / denominator
                    (filtered_recall if tag is not None else unfiltered_recall).append(recall)
            query_seconds = time.perf_counter() - started
            check('S8a4 1,000 查询全部合法（ID/去重/过滤/距离）', not invalid, invalid[:5])
            f_avg = sum(filtered_recall) / len(filtered_recall) if filtered_recall else 0.0
            u_avg = sum(unfiltered_recall) / len(unfiltered_recall) if unfiltered_recall else 0.0
            check(f'S8a5 过滤组平均 Recall@10={f_avg:.4f} ≥ 0.95', f_avg >= 0.95, f_avg)
            check(f'S8a6 无过滤组平均 Recall@10={u_avg:.4f} ≥ 0.95', u_avg >= 0.95, u_avg)
            print(f'  [S8] 写入 {write_seconds:.2f}s，查询 {query_seconds:.2f}s，'
                  f'行数 {rows_count}，维度 {dimensions}，查询数 {queries_count}')
        # 重启后持久性
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT id FROM docs ORDER BY id DESC LIMIT 1')
            check('S8a7 重启后数据完整', rows == [[str(rows_count)]], rows)
            got = topk(pipe, 'docs', json.dumps(data[1][1]), 1)
            check('S8a8 重启后向量检索命中自身', got and got[0][0] == '1', got)
    finally:
        temp.cleanup()

    print(f'\nSUMMARY passed={len(PASSED)} failed={len(FAILED)}')
    if FAILED:
        print('FAILED:', *FAILED, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
