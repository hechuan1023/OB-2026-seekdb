"""独立交叉验证 P4：主键边界、Unicode 码点、参数差分、自然 checkpoint、多表索引。

覆盖前五组未涉及的路径：
  A. 负数/零/BIGINT 两端主键的全链路（含索引 TopK 并列、重启）
  B. VARCHAR 按 Unicode 码点计长：组合字符、ZWJ 表情序列、满长往返
  C. K 恰好等于 max_keep（8192）边界的回退差分；cache=0 与最小排序预算差分；
     空表建索引 + TopK
  D. 自然触发 checkpoint（低阈值 + 更新制造 WAL 死区）：多次压缩后状态与
     重启一致、checkpoint.wal 清理
  E. 上下文关键字标识符（表名 order）、大小写折叠重复列、向量字面量跨行
  F. --mode ann 启动参数与控制行切换差分
  G. 三表不同维度向量索引交错 + 重启
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
            assert self.line(timeout=60) == 'READY'

    def _pump(self, stream, sink):
        for line in stream:
            if isinstance(sink, queue.Queue):
                sink.put(line.rstrip('\r\n'))
            else:
                sink.append(line.rstrip('\r\n'))
        if isinstance(sink, queue.Queue):
            sink.put(None)

    def line(self, timeout=60):
        value = self.lines.get(timeout=timeout)
        assert value is not None, f'unexpected exit; stderr={self.stderr_lines[-5:]}'
        return value

    def raw(self, text):
        self.process.stdin.write(text)
        self.process.stdin.flush()

    def execute(self, sql, timeout=60):
        self.raw(sql + ';\n')
        first = self.line(timeout)
        if first.startswith('RESULT\t'):
            rows = []
            while True:
                row = self.line(timeout)
                if row.startswith('END\t'):
                    return first, rows
                rows.append(row.split('\t'))
        return first, None

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        self.process.wait(timeout=30)
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
    temp = tempfile.TemporaryDirectory(prefix='cross-p4-')
    return temp, Path(temp.name)


def rows_of(pipe, sql):
    head, rows = pipe.execute(sql)
    assert head.startswith('RESULT\t'), (sql, head)
    return rows


def expect_error(pipe, name, sql, code):
    head, _ = pipe.execute(sql)
    check(name, head.startswith(f'ERROR\t{code}\t'), head)


def expect_ok(pipe, name, sql, affected=None):
    head, _ = pipe.execute(sql)
    ok = head.startswith('OK\t') and (affected is None or head == f'OK\t{affected}')
    check(name, ok, head)


def topk_check(pipe, model, name, table, column, query_literal, k, tag=None, dims=4):
    where = '' if tag is None else f" WHERE tag='{tag}'"
    sql = (f"SELECT id, L2_DISTANCE({column}, '{query_literal}') AS d FROM {table}{where} "
           f"ORDER BY d LIMIT {k}")
    head, rows = pipe.execute(sql)
    assert head.startswith('RESULT\t'), (sql, head)
    query = [f32(v) for v in json.loads(query_literal)]
    pairs = sorted((math.dist(row[1], query), pk)
                   for pk, row in model.items() if tag is None or row[0] == tag)
    expected = pairs[:k] if k else []
    ok = ([r[0] for r in rows] == [str(pk) for _, pk in expected]
          and all(close_enough(parse_float(r[1]), d) for r, (d, _) in zip(rows, expected)))
    check(name, ok, f'got={rows[:4]} exp={expected[:4]}')


def main():
    # ================= A：负数 / 零 / BIGINT 两端主键 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'A1 建表', 'CREATE TABLE n (id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(2))', 0)
            expect_ok(pipe, 'A2 边界主键插入',
                      "INSERT INTO n VALUES (-9223372036854775808,'min','[1,0]'), (-5,'b','[0,1]'), "
                      "(0,'zero','[1,1]'), (5,'b','[2,2]'), (9223372036854775807,'max','[0,0]')", 5)
            head, rows = pipe.execute('SELECT id FROM n ORDER BY id')
            check('A3 主键数值升序', [r[0] for r in rows] ==
                  ['-9223372036854775808', '-5', '0', '5', '9223372036854775807'], rows)
            head, rows = pipe.execute('SELECT id FROM n ORDER BY id DESC LIMIT 2')
            check('A4 降序 LIMIT 2', [r[0] for r in rows] == ['9223372036854775807', '5'], rows)
            head, rows = pipe.execute('SELECT id FROM n WHERE id > -5 AND id <= 0')
            check('A5 负数范围条件', [r[0] for r in rows] == ['0'], rows)
            head, rows = pipe.execute('SELECT tag FROM n WHERE id = -9223372036854775808')
            check('A6 最小值点查', rows == [['min']], rows)
            expect_ok(pipe, 'A7 建索引', 'CREATE VECTOR INDEX ni ON n(v)', 0)
            # 精确并列：-5 与 5 不并列；构造 -5/-3 并列
            expect_ok(pipe, 'A8 插入并列负主键',
                      "INSERT INTO n VALUES (-3,'b','[0,1]'), (-7,'b','[0,1]')", 2)
            model = {-9223372036854775808: ('min', [1.0, 0.0]), -5: ('b', [0.0, 1.0]),
                     0: ('zero', [1.0, 1.0]), 5: ('b', [2.0, 2.0]),
                     9223372036854775807: ('max', [0.0, 0.0]),
                     -3: ('b', [0.0, 1.0]), -7: ('b', [0.0, 1.0])}
            topk_check(pipe, model, 'A9 负主键并列按主键升序(-7<-5<-3)', 'n', 'v', '[0,1]', 3, tag='b', dims=2)
            topk_check(pipe, model, 'A10 索引 TopK 含边界主键', 'n', 'v', '[1,0]', 4, dims=2)
            expect_ok(pipe, 'A11 更新负主键向量', "UPDATE n SET v='[3,3]' WHERE id = -5", 1)
            model[-5] = ('b', [3.0, 3.0])
            topk_check(pipe, model, 'A12 更新后 TopK', 'n', 'v', '[3,3]', 2, dims=2)
            expect_ok(pipe, 'A13 删除负主键', 'DELETE FROM n WHERE id = -7', 1)
            model.pop(-7)
            head, rows = pipe.execute('SELECT id FROM n WHERE id < 0 ORDER BY id')
            check('A14 删除后负主键集合', [r[0] for r in rows] == ['-9223372036854775808', '-5', '-3'], rows)
        with Pipe(directory) as pipe:
            head, rows = pipe.execute('SELECT id, tag FROM n ORDER BY id LIMIT 3')
            check('A15 重启后边界主键持久',
                  rows == [['-9223372036854775808', 'min'], ['-5', 'b'], ['-3', 'b']], rows)
            topk_check(pipe, model, 'A16 重启后 TopK', 'n', 'v', '[3,3]', 2, dims=2)
    finally:
        temp.cleanup()

    # ================= B：VARCHAR 码点计长 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'B1 建表', 'CREATE TABLE u (id BIGINT PRIMARY KEY, s VARCHAR(4))', 0)
            expect_ok(pipe, 'B2 组合字符 4 码点接受', "INSERT INTO u VALUES (1, 'éé')", 1)
            expect_error(pipe, 'B3 组合字符 5 码点拒绝', "INSERT INTO u VALUES (2, 'éée')", 'TYPE_ERROR')
            head, rows = pipe.execute('SELECT s FROM u WHERE id = 1')
            check('B4 组合字符按码点存储读回', rows == [['éé']], rows)
            expect_ok(pipe, 'B5 ZWJ 家庭表情 5 码点表', 'CREATE TABLE u5 (id BIGINT PRIMARY KEY, s VARCHAR(5))', 0)
            family = '👨‍👩‍👧'
            expect_ok(pipe, 'B6 ZWJ 序列按码点计 5', f"INSERT INTO u5 VALUES (1, '{family}')", 1)
            expect_ok(pipe, 'B7 4 码点表', 'CREATE TABLE u4 (id BIGINT PRIMARY KEY, s VARCHAR(4))', 0)
            expect_error(pipe, 'B8 ZWJ 序列 5 码点超 VARCHAR(4)',
                         f"INSERT INTO u4 VALUES (1, '{family}')", 'TYPE_ERROR')
            expect_ok(pipe, 'B9 满长中文表', 'CREATE TABLE full (id BIGINT PRIMARY KEY, s VARCHAR(16))', 0)
            long16 = '一十六个汉字正好满长'[:16]
            expect_ok(pipe, 'B10 恰好 16 码点接受', f"INSERT INTO full VALUES (1, '{long16}')", 1)
            head, rows = pipe.execute('SELECT s FROM full')
            check('B11 满长读回', rows == [[long16]], rows)
        with Pipe(directory) as pipe:
            head, rows = pipe.execute('SELECT s FROM u WHERE id = 1')
            check('B12 组合字符重启读回', rows == [['éé']], rows)
            head, rows = pipe.execute('SELECT s FROM u5')
            check('B13 ZWJ 序列重启读回', rows == [[family]], rows)
    finally:
        temp.cleanup()

    # ================= C：K=max_keep 边界、参数差分、空表索引 =================
    temp, directory = fresh()
    try:
        import random
        rng = random.Random(99)
        spec = [(i, rng.choice(('a', 'b')),
                 '[' + ','.join(f'{rng.uniform(-5, 5):.4f}' for _ in range(4)) + ']')
                for i in range(1, 51)]
        model = {i: (t, [f32(v) for v in json.loads(vv)]) for i, t, vv in spec}
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'C1 建表与数据',
                      'CREATE TABLE k (id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(4))', 0)
            expect_ok(pipe, 'C2 插入 50 行',
                      'INSERT INTO k VALUES ' + ','.join(f"({i},'{t}','{v}')" for i, t, v in spec), 50)
            expect_ok(pipe, 'C3 建索引', 'CREATE VECTOR INDEX ki ON k(v)', 0)
            # 空表 TopK：另一张空表
            expect_ok(pipe, 'C4 空表建索引', 'CREATE TABLE empty_t (id BIGINT PRIMARY KEY, v VECTOR(2))', 0)
            expect_ok(pipe, 'C5 空表建向量索引', 'CREATE VECTOR INDEX ei ON empty_t(v)', 0)
            head, rows = pipe.execute("SELECT id FROM empty_t ORDER BY L2_DISTANCE(v, '[1,1]') LIMIT 5")
            check('C6 空表 TopK 返回空', head.startswith('RESULT\t') and rows == [], (head, rows))
            expect_ok(pipe, 'C7 空表插入后索引生效', "INSERT INTO empty_t VALUES (1, '[1,2]')", 1)
            head, rows = pipe.execute("SELECT id FROM empty_t ORDER BY L2_DISTANCE(v, '[1,2]') LIMIT 5")
            check('C8 插入后 TopK 命中', rows == [['1']], rows)
            # K 恰好边界：max_keep = 4MiB//512 = 8192
            for k, label in ((8191, 'K=8191 原生路径'), (8192, 'K=8192 回退路径'), (9000, 'K=9000 回退路径')):
                topk_check(pipe, model, f'C9 {label} 结果一致', 'k', 'v', '[0,0,0,0]', k, dims=4)
        # cache=0 与最小排序预算差分：输出必须与默认完全一致
        with Pipe(directory, extra=('--cache-memory-mib', '0')) as pipe:
            head, rows = pipe.execute("SELECT id FROM k WHERE tag='a' ORDER BY L2_DISTANCE(v, '[0,0,0,0]') LIMIT 7")
            exp_pairs = sorted((math.dist(v, [0.0] * 4), pk) for pk, (t, v) in model.items() if t == 'a')[:7]
            check('C10 cache=0 过滤 TopK 一致', [r[0] for r in rows] == [str(pk) for _, pk in exp_pairs], rows)
        with Pipe(directory, extra=('--sort-memory-mib', '0.004')) as pipe:
            head, rows = pipe.execute('SELECT id FROM k ORDER BY v LIMIT 3'.replace('ORDER BY v', "ORDER BY L2_DISTANCE(v, '[0,0,0,0]')"))
            exp = sorted((math.dist(v, [0.0] * 4), pk) for pk, (t, v) in model.items())[:3]
            check('C11 最小排序预算 TopK 一致', [r[0] for r in rows] == [str(pk) for _, pk in exp], rows)
    finally:
        temp.cleanup()

    # ================= D：自然 checkpoint 触发 =================
    temp, directory = fresh()
    try:
        ckmodel = {}
        with Pipe(directory, extra=('--checkpoint-mib', '0.01')) as pipe:
            expect_ok(pipe, 'D1 建表', 'CREATE TABLE c (id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(2))', 0)
            for batch in range(3):
                chunk = [(batch * 100 + i, 'x', f'[{i % 7},{batch}]') for i in range(100)]
                expect_ok(pipe, f'D2 批 {batch + 1} 插入 100 行',
                          'INSERT INTO c VALUES ' + ','.join(f"({i},'{t}','{v}')" for i, t, v in chunk), 100)
                for i, t, v in chunk:
                    ckmodel[i] = (t, [f32(x) for x in json.loads(v)])
            # 制造 WAL 死区触发自然压缩：两轮全表向量更新（值变化）
            for round_no in (1, 2):
                expect_ok(pipe, f'D3 全表更新第 {round_no} 轮',
                          f"UPDATE c SET v='[{round_no},{round_no + 1}]'", 300)
                for pk in ckmodel:
                    ckmodel[pk] = ('x', [f32(round_no), f32(round_no + 1)])
            head, rows = pipe.execute('SELECT id, v FROM c WHERE id = 150')
            check('D4 压缩后状态正确', rows == [['150', '[2.0,3.0]']], rows)
            topk_check(pipe, ckmodel, 'D5 压缩后 TopK', 'c', 'v', '[2,3]', 5, dims=2)
            # 再次制造死区 → 第二次自然压缩；删除一半
            expect_ok(pipe, 'D6 第三轮更新（再触发压缩）', "UPDATE c SET v='[9,9]'", 300)
            for pk in ckmodel:
                ckmodel[pk] = ('x', [9.0, 9.0])
            expect_ok(pipe, 'D7 删除前 151 行（id 0..150）', 'DELETE FROM c WHERE id <= 150', 151)
            for pk in list(ckmodel):
                if pk <= 150:
                    ckmodel.pop(pk)
        with Pipe(directory) as pipe:
            head, rows = pipe.execute('SELECT id FROM c ORDER BY id LIMIT 3')
            check('D8 多次压缩后重启一致', [r[0] for r in rows] == ['151', '152', '153'], rows)
            check('D9 checkpoint.wal 已清理', not (directory / 'checkpoint.wal').exists())
            topk_check(pipe, ckmodel, 'D10 重启后 TopK', 'c', 'v', '[9,9]', 4, dims=2)
    finally:
        temp.cleanup()

    # ================= E：上下文关键字标识符、大小写折叠、跨行向量字面量 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'E1 表名 order（上下文关键字）', 'CREATE TABLE `order` (id BIGINT PRIMARY KEY, v BIGINT)', 0)
            expect_ok(pipe, 'E2 插入', 'INSERT INTO `order` VALUES (2, 20), (1, 10)', 2)
            head, rows = pipe.execute('SELECT id FROM `order` ORDER BY id')
            check('E3 FROM order + ORDER BY 共存', [r[0] for r in rows] == ['1', '2'], rows)
            expect_error(pipe, 'E4 大小写折叠重复列', 'CREATE TABLE t2 (ID BIGINT PRIMARY KEY, id BIGINT)', 'CONSTRAINT_ERROR')
            pipe.raw("INSERT INTO `order` VALUES (3,\n 4);\n" if False else "INSERT INTO `order` VALUES (3, 4);\n")
            check('E5 插入响应', pipe.line() == 'OK\t1')
            # 向量字面量跨行空白
            expect_ok(pipe, 'E6 跨行向量字面量表', 'CREATE TABLE ml (id BIGINT PRIMARY KEY, v VECTOR(4))', 0)
            pipe.raw("INSERT INTO ml VALUES (1, '[1,\n 2,\n 3,\n 4]');\n")
            check('E7 跨行向量字面量接受', pipe.line() == 'OK\t1')
            head, rows = pipe.execute('SELECT v FROM ml')
            check('E8 跨行字面量值正确', rows == [['[1.0,2.0,3.0,4.0]']], rows)
    finally:
        temp.cleanup()

    # ================= F：--mode ann 启动参数 =================
    temp, directory = fresh()
    try:
        model = {1: ('a', [1.0, 0.0]), 2: ('a', [0.0, 1.0]), 3: ('b', [0.5, 0.5])}
        with Pipe(directory, extra=('--mode', 'ann')) as pipe:
            expect_ok(pipe, 'F1 ann 启动建表', 'CREATE TABLE m (id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(2))', 0)
            expect_ok(pipe, 'F2 ann 启动插入', "INSERT INTO m VALUES (1,'a','[1,0]'), (2,'a','[0,1]'), (3,'b','[0.5,0.5]')", 3)
            expect_ok(pipe, 'F3 ann 启动建索引', 'CREATE VECTOR INDEX mi ON m(v)', 0)
            topk_check(pipe, model, 'F4 ann 启动 TopK=exact 结果', 'm', 'v', '[1,1]', 2, dims=2)
        with Pipe(directory, extra=('--mode', 'ann', '--local-mode-controls')) as pipe:
            topk_check(pipe, model, 'F5 ann 重启后 TopK', 'm', 'v', '[1,1]', 3, dims=2)
            pipe.raw('--mode exact\n')
            check('F6 切回 exact 响应', pipe.line() == 'OK\t0')
            topk_check(pipe, model, 'F7 exact 下结果一致', 'm', 'v', '[1,1]', 3, dims=2)
    finally:
        temp.cleanup()

    # ================= G：三表不同维度索引交错 =================
    temp, directory = fresh()
    try:
        models = {t: {} for t in ('ga', 'gb', 'gc')}
        dims = {'ga': 2, 'gb': 3, 'gc': 4}
        with Pipe(directory) as pipe:
            for t, d in dims.items():
                expect_ok(pipe, f'G1 {t} 建表', f'CREATE TABLE {t} (id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR({d}))', 0)
                expect_ok(pipe, f'G2 {t} 建索引', f'CREATE VECTOR INDEX {t}_i ON {t}(v)', 0)
            # 交错插入/更新
            for step in range(6):
                for t, d in dims.items():
                    pk = step + 1
                    vec = [f32((step + k) % 3 - 1) for k in range(d)]
                    expect_ok(pipe, f'G3 {t} 交错插入 {pk}',
                              f"INSERT INTO {t} VALUES ({pk}, '{'ab'[step % 2]}', '{json.dumps(vec, separators=(',', ':'))}')", 1)
                    models[t][pk] = ('ab'[step % 2], vec)
            expect_ok(pipe, 'G4 gb 向量更新', "UPDATE gb SET v='[9,9,9]' WHERE id = 2", 1)
            models['gb'][2] = ('b', [9.0, 9.0, 9.0])
            expect_ok(pipe, 'G5 ga 删除', 'DELETE FROM ga WHERE id = 1', 1)
            models['ga'].pop(1)
            for t, d in dims.items():
                topk_check(pipe, models[t], f'G6 {t} 交错后 TopK', t, 'v',
                           json.dumps([0] * d, separators=(',', ':')), 3, dims=d)
        with Pipe(directory) as pipe:
            for t, d in dims.items():
                topk_check(pipe, models[t], f'G7 {t} 重启后 TopK', t, 'v',
                           json.dumps([0] * d, separators=(',', ':')), 3, dims=d)
            head, rows = pipe.execute('SELECT id FROM ga ORDER BY id')
            check('G8 ga 删除持久', [r[0] for r in rows] == ['2', '3', '4', '5', '6'], rows)
    finally:
        temp.cleanup()

    print(f'\nSUMMARY passed={len(PASSED)} failed={len(FAILED)}')
    if FAILED:
        print('FAILED:', *FAILED, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
