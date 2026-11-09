"""独立交叉验证 P0（二）：模式切换、过滤/索引缓存一致性、独立真值 TopK。

真值模型完全独立于被测代码：float32 舍入用 struct.pack/unpack('f')，
距离用 Python 双精度 math.dist，排序键 (distance, primary_key)。
不复用被测实现的距离、过滤或排序函数。
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


def truth_vector(literal):
    values = json.loads(literal)
    return [f32(v) for v in values]


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
            assert self.line(timeout=30) == 'READY'

    def _pump(self, stream, sink):
        for line in stream:
            if isinstance(sink, queue.Queue):
                sink.put(line.rstrip('\r\n'))
            else:
                sink.append(line.rstrip('\r\n'))
        if isinstance(sink, queue.Queue):
            sink.put(None)

    def line(self, timeout=20):
        value = self.lines.get(timeout=timeout)
        assert value is not None, f'unexpected exit; stderr={self.stderr_lines}'
        return value

    def raw(self, text):
        self.process.stdin.write(text)
        self.process.stdin.flush()

    def execute(self, sql, timeout=20):
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

    def control(self, line):
        """发送本地模式控制行（不带分号）。"""
        self.raw(line + '\n')
        return self.line()

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        self.process.wait(timeout=20)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass


def rows_of(pipe, sql):
    head, rows = pipe.execute(sql)
    assert head.startswith('RESULT\t'), (sql, head)
    return rows


def fresh():
    temp = tempfile.TemporaryDirectory(prefix='cross-vec-')
    return temp, Path(temp.name)


def close_enough(actual, expected, tol=1e-5):
    return abs(actual - expected) <= tol + tol * abs(expected)


def parse_float(text):
    got = float(text)
    assert math.isfinite(got), text
    return got


class Model:
    """独立真值：内存字典，仅用于小数据对照。"""

    def __init__(self):
        self.rows = {}  # pk -> (tag, [f32...])

    def insert(self, pk, tag, vec_literal):
        assert pk not in self.rows, 'truth model duplicate'
        self.rows[pk] = (tag, truth_vector(vec_literal))

    def update(self, pk, tag=None, vec_literal=None):
        tag0, vec0 = self.rows[pk]
        self.rows[pk] = (tag if tag is not None else tag0,
                         truth_vector(vec_literal) if vec_literal is not None else vec0)

    def delete(self, pk):
        self.rows.pop(pk, None)

    def topk(self, query_literal, k, tag=None):
        query = truth_vector(query_literal)
        pairs = sorted((math.dist(vec, query), pk)
                       for pk, (t, vec) in self.rows.items() if tag is None or t == tag)
        return pairs[:k] if k else []

    def ids(self, tag=None):
        return sorted(pk for pk, (t, _) in self.rows.items() if tag is None or t == tag)


def compare_topk(pipe, model, name, query, k, tag=None, timeout=20):
    where = '' if tag is None else f" WHERE tag='{tag}'"
    sql = (f"SELECT id, L2_DISTANCE(embedding, '{query}') AS d FROM v{where} "
           f"ORDER BY d LIMIT {k}")
    head, rows = pipe.execute(sql, timeout)
    assert head.startswith('RESULT\t'), (sql, head)
    expected = model.topk(query, k, tag)
    got_ids = [r[0] for r in rows]
    exp_ids = [str(pk) for _, pk in expected]
    ok = got_ids == exp_ids
    # rows 为 [id, 距离文本]；期望元组为 (距离, 主键)，逐位对齐比较
    dist_ok = all(close_enough(parse_float(got[1]), exp_d)
                  for got, (exp_d, _) in zip(rows, expected)) if ok else False
    check(f'{name}: TopK id 序列', ok, f'got={got_ids} exp={exp_ids}')
    check(f'{name}: TopK 距离值(1e-5)', dist_ok,
          f'got={[r[1] for r in rows]} exp={[d for d, _ in expected]}')
    return ok and dist_ok


def compare_topk_f(pipe, model, name, query, k, tag=None, timeout=20):
    """与 compare_topk 相同，但表名为 f（场景 F 使用）。"""
    where = '' if tag is None else f" WHERE tag='{tag}'"
    sql = (f"SELECT id, L2_DISTANCE(embedding, '{query}') AS d FROM f{where} "
           f"ORDER BY d LIMIT {k}")
    head, rows = pipe.execute(sql, timeout)
    assert head.startswith('RESULT\t'), (sql, head)
    expected = model.topk(query, k, tag)
    got_ids = [r[0] for r in rows]
    exp_ids = [str(pk) for _, pk in expected]
    ok = got_ids == exp_ids
    dist_ok = all(close_enough(parse_float(got[1]), exp_d)
                  for got, (exp_d, _) in zip(rows, expected)) if ok else False
    check(f'{name}: TopK id 序列', ok, f'got={got_ids} exp={exp_ids}')
    check(f'{name}: TopK 距离值(1e-5)', dist_ok,
          f'got={[r[1] for r in rows]} exp={[d for d, _ in expected]}')
    return ok and dist_ok


def seed_table(pipe, model, spec):
    pipe.execute("CREATE TABLE v (id BIGINT PRIMARY KEY, tag VARCHAR(8), embedding VECTOR(4))")
    for pk, tag, vec in spec:
        head, _ = pipe.execute(f"INSERT INTO v VALUES ({pk}, '{tag}', '{vec}')")
        assert head.startswith('OK\t'), head
        model.insert(pk, tag, vec)


def main():
    # ---------- 场景 A：事务中切换 ann/exact（交接文档 §5 P0）----------
    temp, directory = fresh()
    try:
        pipe = Pipe(directory, extra=('--local-mode-controls',))
        try:
            pipe.execute("CREATE TABLE v (id BIGINT PRIMARY KEY, embedding VECTOR(2))")
            pipe.execute("INSERT INTO v VALUES (1, '[9,9]'), (2, '[1,0]')")
            pipe.execute("CREATE VECTOR INDEX vi ON v(embedding)")
            pipe.execute('BEGIN')
            pipe.execute("UPDATE v SET embedding = '[0,0]' WHERE id = 1")
            pid_before = pipe.process.pid
            head = pipe.control('--mode ann')
            check('模式控制行响应 OK\\t0', head == 'OK\t0', head)
            rows = rows_of(pipe, "SELECT id FROM v ORDER BY L2_DISTANCE(embedding, '[0,0]') LIMIT 1")
            check('ann 事务内查询读到自身修改(id=1)', rows == [['1']], rows)
            check('模式切换未替换进程', pipe.process.pid == pid_before and pipe.process.poll() is None)
            pipe.execute('ROLLBACK')
            pipe.control('--mode exact')
            rows = rows_of(pipe, "SELECT id FROM v ORDER BY L2_DISTANCE(embedding, '[0,0]') LIMIT 1")
            check('回滚后 exact 查询恢复(id=2)', rows == [['2']], rows)
            check('切换后进程仍为同一进程', pipe.process.pid == pid_before and pipe.process.poll() is None)
            # 事务内双向切换再提交
            pipe.execute('BEGIN')
            pipe.execute("UPDATE v SET embedding = '[2,0]' WHERE id = 2")
            pipe.control('--mode ann')
            rows = rows_of(pipe, "SELECT id FROM v ORDER BY L2_DISTANCE(embedding, '[2,0]') LIMIT 1")
            check('第二次 ann 事务内查询(id=2)', rows == [['2']], rows)
            pipe.control('--mode exact')
            head, _ = pipe.execute('COMMIT')
            check('切换后仍可提交', head == 'OK\t0', head)
            rows = rows_of(pipe, "SELECT id FROM v ORDER BY L2_DISTANCE(embedding, '[2,0]') LIMIT 1")
            check('提交后 exact 查询(id=2)', rows == [['2']], rows)
        finally:
            pipe.close()
        again = Pipe(directory)
        try:
            rows = rows_of(again, "SELECT id, embedding FROM v ORDER BY id")
            check('重启后提交内容保留', rows == [['1', '[9.0,9.0]'], ['2', '[2.0,0.0]']], rows)
        finally:
            again.close()
    finally:
        temp.cleanup()

    # ---------- 场景 B：过滤缓存刷新（先建缓存再改过滤列）----------
    temp, directory = fresh()
    try:
        pipe = Pipe(directory)
        model = Model()
        try:
            seed_table(pipe, model, [
                (1, 'x', '[1,0,0,0]'), (2, 'x', '[0,1,0,0]'), (3, 'y', '[0.5,0.5,0,0]'),
                (4, 'y', '[0,0,1,0]'), (5, 'z', '[0,0,0,1]'),
            ])
            pipe.execute("CREATE VECTOR INDEX vi ON v(embedding)")
            compare_topk(pipe, model, 'B1 建立过滤缓存(tag=x)', '[0.9,0.1,0,0]', 2, tag='x')
            # 只改过滤列，不改向量：候选集合必须刷新
            pipe.execute("UPDATE v SET tag='y' WHERE id = 1")
            model.update(1, tag='y')
            compare_topk(pipe, model, 'B2 改过滤列后缓存刷新', '[0.9,0.1,0,0]', 2, tag='x')
            compare_topk(pipe, model, 'B3 新 tag 查询正确', '[0.9,0.1,0,0]', 3, tag='y')
            # 零行修改
            pipe.execute("UPDATE v SET tag='q' WHERE id = 99")
            compare_topk(pipe, model, 'B4 零行修改后结果不变', '[0.9,0.1,0,0]', 2, tag='x')
            # 相同值修改
            pipe.execute("UPDATE v SET tag='y' WHERE id = 1")
            compare_topk(pipe, model, 'B5 相同值修改后结果不变', '[0.9,0.1,0,0]', 3, tag='y')
            # 失败语句后缓存仍正确
            head, _ = pipe.execute("UPDATE v SET tag='toolongvalue' WHERE id = 2")
            check('B6 失败语句报 TYPE_ERROR', head.startswith('ERROR\tTYPE_ERROR\t'), head)
            compare_topk(pipe, model, 'B7 失败语句后结果不变', '[0.9,0.1,0,0]', 2, tag='x')
            # 交错：向量 UPDATE、普通过滤列 UPDATE、INSERT、DELETE
            pipe.execute("UPDATE v SET embedding='[0.9,0.1,0,0]' WHERE id = 4")
            model.update(4, vec_literal='[0.9,0.1,0,0]')
            compare_topk(pipe, model, 'B8 向量更新后', '[0.9,0.1,0,0]', 2, tag='y')
            pipe.execute("INSERT INTO v VALUES (6, 'x', '[0.8,0.2,0,0]')")
            model.insert(6, 'x', '[0.8,0.2,0,0]')
            compare_topk(pipe, model, 'B9 插入后', '[0.9,0.1,0,0]', 2, tag='x')
            pipe.execute("DELETE FROM v WHERE id = 2")
            model.delete(2)
            compare_topk(pipe, model, 'B10 删除后', '[0.9,0.1,0,0]', 2, tag='x')
        finally:
            pipe.close()
    finally:
        temp.cleanup()

    # ---------- 场景 C：事务内交错修改，COMMIT/ROLLBACK 两分支 ----------
    base_spec = [(i, 'x' if i % 2 == 0 else 'y', f'[{i},{i+1},{i+2},0]') for i in range(1, 9)]
    for outcome in ('COMMIT', 'ROLLBACK'):
        temp, directory = fresh()
        try:
            pipe = Pipe(directory)
            model = Model()
            try:
                seed_table(pipe, model, base_spec)
                pipe.execute("CREATE VECTOR INDEX vi ON v(embedding)")
                pipe.execute('BEGIN')
                pipe.execute("INSERT INTO v VALUES (20, 'x', '[1,1,1,1]')")
                model.insert(20, 'x', '[1,1,1,1]')
                pipe.execute("UPDATE v SET embedding='[0,0,0,0]' WHERE id = 3")
                model.update(3, vec_literal='[0,0,0,0]')
                pipe.execute("DELETE FROM v WHERE id = 5")
                model.delete(5)
                pipe.execute("UPDATE v SET tag='x' WHERE id = 7")
                model.update(7, tag='x')
                compare_topk(pipe, model, f'C-{outcome} 事务内 TopK', '[0,0,0,0]', 4)
                compare_topk(pipe, model, f'C-{outcome} 事务内过滤 TopK', '[0,0,0,0]', 3, tag='x')
                head, _ = pipe.execute(outcome)
                check(f'C-{outcome} 执行成功', head == 'OK\t0', head)
                if outcome == 'ROLLBACK':
                    # ROLLBACK 后真值模型回退到 base_spec
                    model = Model()
                    for pk, tag, vec in base_spec:
                        model.insert(pk, tag, vec)
                compare_topk(pipe, model, f'C-{outcome} 后 TopK', '[0,0,0,0]', 4)
            finally:
                pipe.close()
            again = Pipe(directory)
            try:
                if outcome == 'COMMIT':
                    expected = model
                else:
                    expected = Model()
                    for pk, tag, vec in base_spec:
                        expected.insert(pk, tag, vec)
                rows = rows_of(again, "SELECT id FROM v ORDER BY id")
                check(f'C-{outcome} 重启后普通查询一致', rows == [[str(i)] for i in expected.ids()],
                      rows)
                # 重启后用重启进程对照真值
                pipe2_model = expected
                head, rows2 = again.execute(
                    "SELECT id, L2_DISTANCE(embedding, '[0,0,0,0]') AS d FROM v "
                    "ORDER BY d LIMIT 4")
                exp = pipe2_model.topk('[0,0,0,0]', 4)
                ok = ([r[0] for r in rows2] == [str(pk) for _, pk in exp]
                      and all(close_enough(parse_float(r[1]), d) for r, (d, _) in zip(rows2, exp)))
                check(f'C-{outcome} 重启后 TopK 一致', ok,
                      f'got={rows2} exp={exp}')
            finally:
                again.close()
        finally:
            temp.cleanup()

    # ---------- 场景 D：独立真值随机对照 + native vs --disable-native ----------
    import random
    rng = random.Random(20261109)
    spec = []
    for i in range(1, 301):
        vec = '[' + ','.join(f'{rng.uniform(-100, 100):.6f}' for _ in range(4)) + ']'
        spec.append((i, rng.choice(('x', 'y', 'z')), vec))

    results = {}
    for label, extra in (('native', ()), ('python', ('--disable-native',))):
        temp, directory = fresh()
        try:
            pipe = Pipe(directory, extra=extra)
            model = Model()
            try:
                seed_table(pipe, model, spec)
                pipe.execute("CREATE VECTOR INDEX vi ON v(embedding)")
                queries = []
                for _ in range(25):
                    q = '[' + ','.join(f'{rng.uniform(-100, 100):.6f}' for _ in range(4)) + ']'
                    queries.append((q, rng.choice((1, 5, 17)), rng.choice((None, 'x', 'y', 'z'))))
                all_ok = True
                for n, (q, k, tag) in enumerate(queries):
                    ok = compare_topk(pipe, model, f'D-{label} 随机#{n}', q, k, tag)
                    all_ok = all_ok and ok
                results[label] = all_ok
            finally:
                pipe.close()
        finally:
            temp.cleanup()
    check('D native 与 python 路径均与独立真值一致', all(results.values()), results)

    # ---------- 场景 E：边界（N=0、N<K、K=0、并列、LIMIT 边界、ORDER BY 并列）----------
    temp, directory = fresh()
    try:
        pipe = Pipe(directory)
        model = Model()
        try:
            seed_table(pipe, model, [
                (10, 'a', '[1,0,0,0]'), (20, 'a', '[1,0,0,0]'), (30, 'a', '[2,0,0,0]'),
                (5, 'b', '[0,1,0,0]'), (40, 'b', '[0,1,0,0]'),
            ])
            pipe.execute("CREATE VECTOR INDEX vi ON v(embedding)")
            compare_topk(pipe, model, 'E1 等距并列按主键升序', '[1,0,0,0]', 3)
            compare_topk(pipe, model, 'E2 K 超过候选数(N<K)', '[1,0,0,0]', 10, tag='a')
            compare_topk(pipe, model, 'E3 过滤后空集合(N=0)', '[1,0,0,0]', 3, tag='nope')
            head, rows = pipe.execute(
                "SELECT id FROM v ORDER BY L2_DISTANCE(embedding, '[1,0,0,0]') LIMIT 0")
            check('E4 LIMIT 0 返回空', head.startswith('RESULT\t') and rows == [], (head, rows))
            compare_topk(pipe, model, 'E5 K=1 边界', '[1,0,0,0]', 1)
            compare_topk(pipe, model, 'E6 K 恰等于 N', '[1,0,0,0]', 2, tag='b')
            # 普通 ORDER BY 并列按主键升序
            head, rows = pipe.execute("SELECT id FROM v ORDER BY tag")
            expected = sorted(((t, pk) for pk, (t, _) in model.rows.items()))
            check('E7 普通 ORDER BY 并列按主键升序',
                  [r[0] for r in rows] == [str(pk) for _, pk in expected], rows)
            head, rows = pipe.execute("SELECT id FROM v ORDER BY tag DESC")
            expected_desc = sorted(((t, -pk) for pk, (t, _) in model.rows.items()), reverse=True)
            check('E8 ORDER BY DESC 并列仍按主键升序',
                  [r[0] for r in rows] == [str(-neg) for _, neg in expected_desc], rows)
        finally:
            pipe.close()
    finally:
        temp.cleanup()

    # ---------- 场景 F：float32 语义、极值、近似并列、非法向量 ----------
    temp, directory = fresh()
    try:
        pipe = Pipe(directory)
        try:
            pipe.execute("CREATE TABLE f (id BIGINT PRIMARY KEY, embedding VECTOR(2))")
            # float32 舍入敏感值：0.1 在 float32 中并非精确 0.1
            pipe.execute("INSERT INTO f VALUES (1, '[0.1, 0.2]')")
            head, rows = pipe.execute(
                "SELECT L2_DISTANCE(embedding, '[0.1, 0.2]') FROM f WHERE id = 1")
            stored = [f32(0.1), f32(0.2)]
            expect = math.dist(stored, stored)
            check('F1 距离按存储 float32 值计算', close_enough(parse_float(rows[0][0]), 0.0)
                  and close_enough(parse_float(rows[0][0]), expect), rows)
            # 极值：float32 最大量级（先插入小距离行，再插入 3e38 行，验证排序）
            pipe.execute("INSERT INTO f VALUES (2, '[3e38, -3e38]')")
            head, rows = pipe.execute(
                "SELECT id, L2_DISTANCE(embedding, '[0,0]') AS d FROM f ORDER BY d LIMIT 1")
            small = math.dist([f32(0.1), f32(0.2)], [0.0, 0.0])
            check('F2a 小距离行排第一', rows[0][0] == '1'
                  and close_enough(parse_float(rows[0][1]), small), rows)
            head, rows = pipe.execute(
                "SELECT id, L2_DISTANCE(embedding, '[3e38,-3e38]') AS d FROM f WHERE id = 2")
            big = math.dist([f32(3e38), f32(-3e38)], [f32(3e38), f32(-3e38)])
            check('F2b 极值自身距离为 0（float32 无溢出）',
                  len(rows) == 1 and rows[0][0] == '2'
                  and close_enough(parse_float(rows[0][1]), big), rows)
            # 非法向量
            for label, vec in (('NaN', '[NaN, 0]'), ('Infinity', '[Infinity, 0]'),
                               ('维度多', '[0,0,0]'), ('维度少', '[0]'),
                               ('float32 溢出', '[1e40, 0]')):
                head, _ = pipe.execute(f"INSERT INTO f VALUES (50, '{vec}')")
                check(f'F3 拒绝非法向量({label})', head.startswith('ERROR\tINVALID_VECTOR\t'), head)
            head, rows = pipe.execute("SELECT id FROM f ORDER BY id")
            check('F4 非法插入无残留', [r[0] for r in rows] == ['1', '2'], rows)
            # 事务内非法向量后仍可提交
            pipe.execute('BEGIN')
            pipe.execute("INSERT INTO f VALUES (60, '[1,1]')")
            head, _ = pipe.execute("INSERT INTO f VALUES (61, '[NaN,1]')")
            check('F5 事务内拒绝 NaN', head.startswith('ERROR\tINVALID_VECTOR\t'), head)
            head, _ = pipe.execute('COMMIT')
            check('F6 非法后仍可提交', head == 'OK\t0', head)
            head, rows = pipe.execute("SELECT id FROM f ORDER BY id")
            check('F7 提交后无非法残留', [r[0] for r in rows] == ['1', '2', '60'], rows)
        finally:
            pipe.close()
    finally:
        temp.cleanup()

    # ---------- 场景 G：近似并列与精确并列（native 边界守卫）----------
    temp, directory = fresh()
    try:
        pipe = Pipe(directory)
        try:
            pipe.execute("CREATE TABLE g (id BIGINT PRIMARY KEY, embedding VECTOR(2))")
            pipe.execute("CREATE VECTOR INDEX vg ON g(embedding)")
            # 相邻 float32：1.0 与其下一个 float32（插入顺序与主键顺序相反）
            near = struct.unpack('f', struct.pack('f', 1.0000001))[0]
            assert near > f32(1.0), near
            pipe.execute(f"INSERT INTO g VALUES (2, '[{near!r}, 0]')")
            pipe.execute("INSERT INTO g VALUES (1, '[1.0, 0]')")
            pipe.execute("INSERT INTO g VALUES (3, '[1.0, 0]')")
            head, rows = pipe.execute(
                "SELECT id, L2_DISTANCE(embedding, '[0,0]') AS d FROM g ORDER BY d LIMIT 3")
            d1 = math.dist([f32(1.0), 0.0], [0.0, 0.0])
            d2 = math.dist([near, 0.0], [0.0, 0.0])
            # 独立真值：id=1 与 id=3 精确并列（主键升序），id=2 稍远
            exp = sorted([(d1, 1), (d1, 3), (d2, 2)])
            ok = ([r[0] for r in rows] == [str(pk) for _, pk in exp]
                  and all(close_enough(parse_float(r[1]), d) for r, (d, _) in zip(rows, exp)))
            check('G1 近似/精确并列按 (距离,主键) 排序', ok,
                  f'got={rows} exp={exp}')
            head, rows = pipe.execute(
                "SELECT id FROM g ORDER BY L2_DISTANCE(embedding, '[0,0]') LIMIT 1")
            check('G2 LIMIT 1 取最近且并列取小主键', rows == [['1']], rows)
        finally:
            pipe.close()
    finally:
        temp.cleanup()

    # ---------- 场景 H：DDL 事务回滚（本地约定行为记录）----------
    temp, directory = fresh()
    try:
        pipe = Pipe(directory)
        try:
            pipe.execute('BEGIN')
            pipe.execute("CREATE TABLE h1 (id BIGINT PRIMARY KEY, embedding VECTOR(2))")
            pipe.execute("CREATE VECTOR INDEX vh ON h1(embedding)")
            pipe.execute('ROLLBACK')
            head, _ = pipe.execute("SELECT id FROM h1")
            check('H1 回滚后表不可见(NOT_FOUND)', head.startswith('ERROR\tNOT_FOUND\t'), head)
            head, _ = pipe.execute("CREATE TABLE h1 (id BIGINT PRIMARY KEY, embedding VECTOR(2))")
            check('H2 回滚后可重建同名表', head.startswith('OK\t'), head)
            head, _ = pipe.execute("CREATE VECTOR INDEX vh ON h1(embedding)")
            check('H3 回滚后索引名可复用', head.startswith('OK\t'), head)
            head, _ = pipe.execute('COMMIT')
            check('H4 无活动事务 COMMIT 报错(本地约定)', head.startswith('ERROR\tTRANSACTION_ERROR\t'), head)
            head, _ = pipe.execute('BEGIN')
            check('H5 BEGIN 成功', head.startswith('OK\t'), head)
            head, _ = pipe.execute('BEGIN')
            check('H6 嵌套 BEGIN 报错(本地约定)', head.startswith('ERROR\tTRANSACTION_ERROR\t'), head)
            pipe.execute('ROLLBACK')
        finally:
            pipe.close()
    finally:
        temp.cleanup()

    print(f'\nSUMMARY passed={len(PASSED)} failed={len(FAILED)}')
    if FAILED:
        print('FAILED:', *FAILED, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
