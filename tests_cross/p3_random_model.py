"""独立交叉验证 P3：模型驱动随机差分测试（第五组）。

对同一数据库进程施加随机操作序列（增删改查、事务、EOF 重启、COMMIT 后
立即强杀），逐步与独立内存真值模型对照。模型语义独立建立：
  - acked：已确认提交的持久状态（COMMIT 后才并入）
  - session：活动事务覆盖层（COMMIT 并入 acked；ROLLBACK/EOF/强杀丢弃）
  - 失败语句整条无效果（语句原子性）
边界浮点值覆盖：次正规、相邻 float32、-0.0、float32 最大量级。
行数越过磁盘哈希初始容量（1024）以触发扩容路径。
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASSED = []
FAILED = []
VERIFICATIONS = []


def check(name, condition, detail=''):
    (PASSED if condition else FAILED).append(name)
    if not condition:
        print('FAIL ' + name + ' :: ' + str(detail))


def f32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def parse_float(text):
    got = float(text)
    assert math.isfinite(got), text
    return got


def close_enough(actual, expected, tol=1e-5):
    return abs(actual - expected) <= tol + tol * abs(expected)


class Pipe:
    def __init__(self, directory, ready=True):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        env.pop('MINIVECTORDB_FAILPOINT', None)
        self.process = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'minivectordb', '--data-dir', str(directory)],
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

    def execute(self, sql, timeout=60):
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
        return self.process.wait(timeout=30)

    def eof_close(self):
        self.process.stdin.close()
        return self.process.wait(timeout=30)

    def cleanup(self):
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass


DELETED = object()


class Model:
    """acked = 已提交持久状态；session = 活动事务覆盖层。"""

    def __init__(self):
        self.acked = {}
        self.session = None  # None 或 {table: {pk: row 或 DELETED}}

    def visible(self, table):
        base = dict(self.acked.get(table, {}))
        if self.session is not None:
            for pk, row in self.session.get(table, {}).items():
                base[pk] = row
        return {pk: row for pk, row in base.items() if row is not DELETED}

    def begin(self):
        self.session = {}

    def in_tx(self):
        return self.session is not None

    def commit(self):
        for table, overlay in self.session.items():
            target = self.acked.setdefault(table, {})
            for pk, row in overlay.items():
                target[pk] = row
        self.session = None

    def rollback(self):
        self.session = None

    def crash(self):
        self.session = None

    def apply(self, table, pk, row):
        if self.session is not None:
            self.session.setdefault(table, {})[pk] = row
        else:
            self.acked.setdefault(table, {})[pk] = row

    def remove(self, table, pk):
        self.apply(table, pk, DELETED)


def boundary_vector(rng, dims=4):
    style = rng.random()
    if style < 0.2:
        return [f32(rng.uniform(-1, 1)) for _ in range(dims)]
    if style < 0.4:
        return [float(rng.randint(-5, 5)) for _ in range(dims)]
    if style < 0.55:
        base = f32(rng.uniform(0.5, 2.0))
        return [f32(math.nextafter(base, math.inf)) if rng.random() < 0.5 else base for _ in range(dims)]
    if style < 0.7:
        return [-0.0 if rng.random() < 0.5 else 0.0 for _ in range(dims)]
    if style < 0.85:
        return [f32(rng.choice((1e-45, -1e-45, 1e-40, 3e38, -3e38, 1.0))) for _ in range(dims)]
    return [0.0] * dims  # 全零：精确并列常发


def fmt(vector):
    return '[' + ','.join(repr(v) for v in vector) + ']'


def topk_truth(rows_dict, query_vec, k, tag=None):
    pairs = sorted((math.dist(row[1], query_vec), pk)
                   for pk, row in rows_dict.items() if tag is None or row[0] == tag)
    return pairs[:k] if k else []


def run_seed(seed):
    rng = random.Random(seed)
    temp = tempfile.TemporaryDirectory(prefix=f'cross-p3-{seed}-')
    directory = Path(temp.name)
    model = Model()
    pipe = None
    local_failures = 0
    ops_log = []

    def fail(name, detail=''):
        nonlocal local_failures
        local_failures += 1
        check(f'[{seed}] {name} step={step} ops={ops_log[-6:]}', False, detail)

    def open_db():
        nonlocal pipe
        pipe = Pipe(directory)

    def reopen():
        nonlocal pipe
        code = pipe.eof_close()
        if code != 0:
            fail('EOF 退出码 0', code)
        if pipe.stderr_lines:
            fail('EOF 重启无 stderr', pipe.stderr_lines[-3:])
        pipe.cleanup()
        open_db()

    def kill_and_reopen():
        nonlocal pipe
        pipe.kill()
        pipe.cleanup()
        open_db()

    def visible():
        return model.visible('docs')

    def verify_full():
        VERIFICATIONS.append(f'{seed}:full')
        head, rows = pipe.execute('SELECT id, tag, v FROM docs ORDER BY id')
        if not head.startswith('RESULT\t'):
            fail('全量查询成功', head)
            return
        expected = [[str(pk), row[0], fmt(row[1])] for pk, row in sorted(visible().items())]
        if rows != expected:
            if len(rows) == len(expected):
                diff = next(((a, b) for a, b in zip(rows, expected) if a != b), None)
            else:
                diff = (f'count {len(rows)}', f'count {len(expected)}')
            fail('全量状态一致', diff)

    def verify_topk():
        VERIFICATIONS.append(f'{seed}:topk')
        current = visible()
        query = boundary_vector(rng)
        tag = rng.choice((None, 'a', 'b', 'zz'))
        k = rng.choice((0, 1, 3, 10, len(current) + 3))
        where = '' if tag is None else f" WHERE tag='{tag}'"
        sql = f"SELECT id, L2_DISTANCE(v, '{fmt(query)}') AS d FROM docs{where} ORDER BY d LIMIT {k}"
        head, rows = pipe.execute(sql)
        if not head.startswith('RESULT\t'):
            fail('TopK 查询成功', head)
            return
        expected = topk_truth(current, query, k, tag)
        got = [(r[0], parse_float(r[1])) for r in rows]
        exp = [(str(pk), d) for d, pk in expected]
        ok = ([r[0] for r in rows] == [e[0] for e in exp]
              and all(close_enough(g[1], e[1]) for g, e in zip(got, exp)))
        if not ok:
            fail(f'TopK 一致 tag={tag} k={k}', f'got={got[:6]} exp={exp[:6]}')

    try:
        open_db()
        head, _ = pipe.execute('CREATE TABLE docs (id BIGINT PRIMARY KEY, tag VARCHAR(8), v VECTOR(4))')
        if head != 'OK\t0':
            fail('建表', head)
        model.acked['docs'] = {}
        head, _ = pipe.execute('CREATE TABLE aux (id BIGINT PRIMARY KEY, note VARCHAR(8))')
        if head != 'OK\t0':
            fail('辅助表', head)
        model.acked['aux'] = {}

        next_pk = 1
        ops_since_restart = 0
        for step in range(220):
            ops_since_restart += 1
            current = visible()
            choice = rng.random()
            ops_log.append(f'{step}:choice={choice:.3f} in_tx={model.in_tx()} next_pk={next_pk} rows={len(current)}')

            if choice < 0.22:
                # 多行插入：预校验整条语句（语句原子性：任一行重复主键或向量非法则整条失败）
                count = rng.randint(1, 4)
                values, model_rows = [], []
                projected = dict(current)
                valid = True
                for _ in range(count):
                    if rng.random() < 0.06:
                        valid = False  # 故意维度错误
                        values.append((next_pk, rng.choice('abczz'), '[1,2]'))
                        next_pk += 1
                        continue
                    pk = rng.choice((next_pk, rng.randint(1, max(2, next_pk + 5))))
                    if pk == next_pk:
                        next_pk += 1
                    tag = rng.choice('aabbczz')
                    vec = boundary_vector(rng)
                    if pk in projected:
                        valid = False
                    else:
                        projected[pk] = (tag, vec)
                        model_rows.append((pk, tag, vec))
                    values.append((pk, tag, fmt(vec)))
                sql = ('INSERT INTO docs VALUES '
                       + ','.join(f"({pk},'{t}','{v}')" for pk, t, v in values))
                VERIFICATIONS.append(f'{seed}:insert')
                head, _ = pipe.execute(sql)
                if valid:
                    if head != f'OK\t{count}':
                        fail('插入成功', (head, sql[:90]))
                    else:
                        for pk, tag, vec in model_rows:
                            model.apply('docs', pk, (tag, vec))
                else:
                    if not head.startswith('ERROR\t'):
                        fail('非法插入整条失败', head)
            elif choice < 0.38:
                # UPDATE：点查 / tag 等值 / 范围 / 永假；SET 过滤列或向量；偶尔相同值
                form = rng.random()
                if rng.random() < 0.08:
                    where_sql, matched = ' WHERE id = 999999', []
                elif form < 0.4 and current:
                    pk = rng.choice(list(current))
                    where_sql, matched = f' WHERE id = {pk}', [pk]
                elif form < 0.7:
                    tag_t = rng.choice('abczz')
                    where_sql = f" WHERE tag = '{tag_t}'"
                    matched = [pk for pk, (t, _) in current.items() if t == tag_t]
                else:
                    bound = rng.randint(1, max(2, next_pk))
                    where_sql = f' WHERE id <= {bound}'
                    matched = [pk for pk in current if pk <= bound]
                set_vector = rng.random() >= 0.5
                if set_vector:
                    new_vec = current[matched[0]][1] if (matched and rng.random() < 0.25) else boundary_vector(rng)
                    sql = f"UPDATE docs SET v = '{fmt(new_vec)}'{where_sql}"
                else:
                    new_tag = rng.choice('aabbczz')
                    sql = f"UPDATE docs SET tag = '{new_tag}'{where_sql}"
                head, _ = pipe.execute(sql)
                if head != f'OK\t{len(matched)}':
                    fail('UPDATE 影响行数', (head, len(matched), where_sql))
                else:
                    for pk in matched:
                        tag0, vec0 = current[pk]
                        model.apply('docs', pk, (new_tag if not set_vector else tag0,
                                                 new_vec if set_vector else vec0))
            elif choice < 0.46:
                # DELETE：点查或永假
                if current and rng.random() < 0.7:
                    pk = rng.choice(list(current))
                    where_sql, expect = f' WHERE id = {pk}', 1
                else:
                    pk, where_sql, expect = None, ' WHERE id = 999999', 0
                head, _ = pipe.execute(f'DELETE FROM docs{where_sql}')
                if head != f'OK\t{expect}':
                    fail('DELETE 影响行数', (head, expect))
                elif expect:
                    model.remove('docs', pk)
            elif choice < 0.56:
                verify_topk()
            elif choice < 0.63:
                verify_full()
            elif choice < 0.67:
                # 范围倒序 + LIMIT
                VERIFICATIONS.append(f'{seed}:range')
                head, rows = pipe.execute('SELECT id FROM docs WHERE id <= 20 ORDER BY id DESC LIMIT 5')
                exp = [str(pk) for pk in sorted((p for p in current if p <= 20), reverse=True)[:5]]
                if [r[0] for r in rows] != exp:
                    fail('范围倒序查询', (rows, exp))
            elif choice < 0.73:
                if not model.in_tx():
                    head, _ = pipe.execute('BEGIN')
                    if head != 'OK\t0':
                        fail('BEGIN', head)
                    model.begin()
            elif choice < 0.79:
                if model.in_tx():
                    head, _ = pipe.execute('COMMIT')
                    if head != 'OK\t0':
                        fail('COMMIT', head)
                    model.commit()
            elif choice < 0.83:
                if model.in_tx():
                    head, _ = pipe.execute('ROLLBACK')
                    if head != 'OK\t0':
                        fail('ROLLBACK', head)
                    model.rollback()
            elif choice < 0.87:
                # 事务内插入并读自身修改
                if model.in_tx():
                    pk = next_pk
                    while pk in visible():
                        pk += 1
                    next_pk = pk + 1
                    vec = boundary_vector(rng)
                    head, _ = pipe.execute(f"INSERT INTO docs VALUES ({pk}, 'tx', '{fmt(vec)}')")
                    if head != 'OK\t1':
                        fail('事务内插入', head)
                    else:
                        model.apply('docs', pk, ('tx', vec))
                    head, rows = pipe.execute(f'SELECT tag FROM docs WHERE id = {pk}')
                    if rows != [['tx']]:
                        fail('事务内读自身修改', rows)
            elif choice < 0.91:
                # 辅助表隔离
                note = f'n{step}'
                head, _ = pipe.execute(f"INSERT INTO aux VALUES ({step}, '{note}')")
                if head != 'OK\t1':
                    fail('辅助表插入', head)
                else:
                    model.apply('aux', step, (note, None))
                head, rows = pipe.execute(f'SELECT note FROM aux WHERE id = {step}')
                if rows != [[note]]:
                    fail('辅助表读回', rows)
            elif choice < 0.95:
                # EOF 重启：未提交事务回滚
                reopen()
                if model.in_tx():
                    model.crash()
                verify_full()
            else:
                # 已确认提交后立即强杀：提交必须保留；或带未提交事务强杀：必须丢弃
                if not model.in_tx():
                    pk = next_pk
                    while pk in visible():
                        pk += 1
                    next_pk = pk + 1
                    vec = boundary_vector(rng)
                    head, _ = pipe.execute(f"INSERT INTO docs VALUES ({pk}, 'kz', '{fmt(vec)}')")
                    if head != 'OK\t1':
                        fail('强杀前插入', head)
                    else:
                        model.apply('docs', pk, ('kz', vec))
                elif rng.random() < 0.5:
                    head, _ = pipe.execute('COMMIT')
                    if head != 'OK\t0':
                        fail('强杀前 COMMIT', head)
                    model.commit()
                kill_and_reopen()
                model.crash()
                verify_full()

            if ops_since_restart >= 25:
                ops_since_restart = 0
                reopen()
                if model.in_tx():
                    model.crash()
                verify_full()

        # 收尾：提交残留事务，重启后最终核对
        if model.in_tx():
            head, _ = pipe.execute('COMMIT')
            if head != 'OK\t0':
                fail('收尾 COMMIT', head)
            model.commit()
        head, rows = pipe.execute('SELECT id FROM docs ORDER BY id')
        expected = [str(pk) for pk in sorted(visible())]
        if [r[0] for r in rows] != expected:
            fail('最终主键集合', (len(rows), len(expected)))
        verify_topk()
        reopen()
        verify_full()
        verify_topk()
    finally:
        if pipe is not None:
            try:
                if pipe.process.poll() is None:
                    pipe.process.stdin.close()
                    pipe.process.wait(timeout=30)
            except OSError:
                pass
            pipe.cleanup()
        temp.cleanup()
    return local_failures


def main():
    seeds = [int(x) for x in (sys.argv[1:] or (301, 302, 303))]
    total_failures = 0
    for seed in seeds:
        failures = run_seed(seed)
        total_failures += failures
        print(f'seed {seed}: failures={failures} verifications={sum(1 for v in VERIFICATIONS if v.startswith(str(seed)+":"))}')
    print(f'\nSUMMARY seeds={len(seeds)} total_failures={total_failures} '
          f'checks_passed={len(PASSED)} checks_failed={len(FAILED)}')
    if FAILED:
        print('FAILED CHECKS:', *FAILED[:20], sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
