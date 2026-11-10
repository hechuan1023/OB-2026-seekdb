"""独立交叉验证 P5：官方 benchmark 形状的检索正确性（exact + ann）。

依据 docs/minivectordb-requirements.md：
  - 7.1 负载：默认 10,000 条、128 维、1,000 次 TopK K=10、一半带过滤；
    写入从空表开始、每批 100 条提交。本组按同一形状缩小查询数（300）以控制
    真值计算时间，写入仍为每批 100 条。
  - 3 VEC-09：返回 ID 有效、无重复、满足过滤、距离对应当前记录。
  - 3 召回：真值在过滤后集合内计算；分母 min(K,N)；空集合不参与平均；
    两组平均 Recall@10 分别不低于 0.95。
  - VEC-07：建索引后新增、向量更新、删除都须正确反映到检索结果。
  - TX-07：表记录与向量检索可见状态一致。
  - 4.1：向量索引允许从持久化记录重建（删除派生文件后重启仍正确）。
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
ROWS = 5000
DIMENSIONS = 128
K = 10
QUERIES = 300
SEED = 20261110


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
    def __init__(self, directory, extra=(), ready=True, failpoint=None):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        if failpoint:
            env['MINIVECTORDB_FAILPOINT'] = failpoint
        else:
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

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        self.process.wait(timeout=60)
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
    temp = tempfile.TemporaryDirectory(prefix='cross-p5-')
    return temp, Path(temp.name)


def truth_topk(rows, query, tag, k=K):
    pairs = sorted((math.dist(row[1], query), pk)
                   for pk, row in rows.items() if tag is None or row[0] == tag)
    return pairs[:k]


def validate_queries(pipe, rows, queries, label, failures):
    """执行并逐条校验 VEC-09；返回 (filtered_recalls, unfiltered_recalls)。"""
    filtered, unfiltered = [], []
    for index, (query, tag) in enumerate(queries):
        where = '' if tag is None else f" WHERE tag='{tag}'"
        sql = (f"SELECT id, L2_DISTANCE(v, '{json.dumps(query)}') AS d FROM docs{where} "
               f"ORDER BY d LIMIT {K}")
        head, result = pipe.execute(sql)
        if not head.startswith('RESULT\t'):
            failures.append((label, index, 'query failed', head))
            continue
        expected = truth_topk(rows, query, tag)
        expected_ids = [pk for _, pk in expected]
        ids = []
        for row in result:
            if len(row) != 2:
                failures.append((label, index, 'column count', row))
                continue
            pk = int(row[0])
            distance = parse_float(row[1])
            ids.append(pk)
            if pk not in rows:
                failures.append((label, index, 'unknown id', pk))
            elif tag is not None and rows[pk][0] != tag:
                failures.append((label, index, 'filter violated', (pk, rows[pk][0], tag)))
            elif not close_enough(distance, math.dist(rows[pk][1], query)):
                failures.append((label, index, 'distance mismatch',
                                 (pk, distance, math.dist(rows[pk][1], query))))
        if len(ids) != len(set(ids)):
            failures.append((label, index, 'duplicate ids', ids))
        if len(ids) != len(expected_ids):
            failures.append((label, index, 'count', (len(ids), len(expected_ids))))
        denominator = min(K, len(expected_ids))
        if denominator:
            recall = len(set(ids) & set(expected_ids)) / denominator
            (filtered if tag is not None else unfiltered).append(recall)
        if ids != expected_ids:
            failures.append((label, index, 'order/ids', (ids[:5], expected_ids[:5])))
    return filtered, unfiltered


def main():
    rng = random.Random(SEED)
    rows = {i: (f'g{i % 5}', [f32(rng.uniform(-1, 1)) for _ in range(DIMENSIONS)])
            for i in range(1, ROWS + 1)}
    queries = []
    for i in range(QUERIES):
        query = [f32(rng.uniform(-1, 1)) for _ in range(DIMENSIONS)]
        queries.append((query, None if i % 2 == 0 else f'g{i % 5}'))

    temp, directory = fresh()
    try:
        # ---------- 写入：每批 100 条提交（官方 7.1 形状）----------
        with Pipe(directory) as pipe:
            head, _ = pipe.execute(
                f'CREATE TABLE docs (id BIGINT PRIMARY KEY, tag VARCHAR(8), v VECTOR({DIMENSIONS}))')
            check('P5a1 建表', head == 'OK\t0', head)
            started = time.perf_counter()
            for offset in range(1, ROWS + 1, 100):
                chunk = [(pk, rows[pk][0], rows[pk][1]) for pk in range(offset, min(offset + 100, ROWS + 1))]
                sql = ('INSERT INTO docs VALUES '
                       + ','.join(f"({pk},'{tag}','{json.dumps(vec)}')" for pk, tag, vec in chunk))
                head, _ = pipe.execute(sql)
                if head != f'OK\t{len(chunk)}':
                    check('P5a2 批插入失败', False, (offset, head))
                    break
            else:
                check('P5a2 每批 100 条插入全部成功', True)
            check('P5a3 写入耗时记录', True, f'{time.perf_counter() - started:.2f}s')
            head, _ = pipe.execute('CREATE VECTOR INDEX docs_vec ON docs(v)')
            check('P5a4 建向量索引', head == 'OK\t0', head)

            # ---------- exact 模式 300 查询 ----------
            failures = []
            filtered, unfiltered = validate_queries(pipe, rows, queries, 'exact', failures)
            check('P5b1 exact 300 查询 VEC-09 全部合法（ID/去重/过滤/距离/顺序）',
                  not failures, failures[:5])
            f_avg = sum(filtered) / len(filtered) if filtered else 0.0
            u_avg = sum(unfiltered) / len(unfiltered) if unfiltered else 0.0
            check(f'P5b2 exact 过滤组平均 Recall@10={f_avg:.4f} ≥ 0.95', f_avg >= 0.95, f_avg)
            check(f'P5b3 exact 无过滤组平均 Recall@10={u_avg:.4f} ≥ 0.95', u_avg >= 0.95, u_avg)
            check('P5b4 exact 两组均为 1.0（精确路径）', f_avg == 1.0 and u_avg == 1.0, (f_avg, u_avg))

        # ---------- ann 模式：同一数据、独立进程 ----------
        with Pipe(directory, extra=('--mode', 'ann')) as pipe:
            failures = []
            filtered, unfiltered = validate_queries(pipe, rows, queries, 'ann', failures)
            check('P5c1 ann 300 查询 VEC-09 全部合法', not failures, failures[:5])
            f_avg = sum(filtered) / len(filtered) if filtered else 0.0
            u_avg = sum(unfiltered) / len(unfiltered) if unfiltered else 0.0
            check(f'P5c2 ann 过滤组平均 Recall@10={f_avg:.4f} ≥ 0.95', f_avg >= 0.95, f_avg)
            check(f'P5c3 ann 无过滤组平均 Recall@10={u_avg:.4f} ≥ 0.95', u_avg >= 0.95, u_avg)

            # ---------- VEC-07：索引维护交错 ----------
            for pk in range(5001, 5011):
                vec = [f32(rng.uniform(-1, 1)) for _ in range(DIMENSIONS)]
                head, _ = pipe.execute(f"INSERT INTO docs VALUES ({pk},'g0','{json.dumps(vec)}')")
                if head != 'OK\t1':
                    check('P5d1 维护期插入', False, head)
                rows[pk] = ('g0', vec)
            for pk in (1, 2, 3, 4, 5):
                vec = [f32(rng.uniform(-1, 1)) for _ in range(DIMENSIONS)]
                head, _ = pipe.execute(f"UPDATE docs SET v='{json.dumps(vec)}' WHERE id = {pk}")
                if head != 'OK\t1':
                    check('P5d2 维护期向量更新', False, head)
                rows[pk] = (rows[pk][0], vec)
            for pk in (6, 7, 8, 9, 10):
                head, _ = pipe.execute(f'DELETE FROM docs WHERE id = {pk}')
                if head != 'OK\t1':
                    check('P5d3 维护期删除', False, head)
                rows.pop(pk)
            check('P5d4 维护操作完成（10 增 5 改 5 删）', True)
            failures = []
            post = queries[:50]
            filtered, unfiltered = validate_queries(pipe, rows, post, 'post-mutation', failures)
            check('P5d5 维护后 50 查询 VEC-09 合法且与更新后真值一致',
                  not failures, failures[:5])
            check('P5d6 维护后 Recall@10 两组均 1.0',
                  all(r == 1.0 for r in filtered + unfiltered), (filtered[:3], unfiltered[:3]))

            # ---------- TX-07：普通查询与向量检索可见集合一致 ----------
            tx7_failures = []
            for tag in ('g0', 'g1', 'g2', 'g3', 'g4'):
                head, plain = pipe.execute(f"SELECT id FROM docs WHERE tag='{tag}'")
                plain_ids = {int(r[0]) for r in plain}
                head, top = pipe.execute(
                    f"SELECT id FROM docs WHERE tag='{tag}' "
                    f"ORDER BY L2_DISTANCE(v, '{json.dumps([0.0] * DIMENSIONS)}') LIMIT 100000")
                top_ids = {int(r[0]) for r in top}
                if plain_ids != top_ids:
                    tx7_failures.append((tag, len(plain_ids), len(top_ids)))
            check('P5e1 TX-07 五组过滤集合：普通查询 == TopK(K≥N)', not tx7_failures, tx7_failures)

        # ---------- 4.1：派生索引可重建（强杀留下 flat-* → 删除 → 重启）----------
        # 正常关闭会删除派生文件（设计为可丢弃）；强杀才能保留它们。
        with Pipe(directory) as pipe:
            validate_queries(pipe, rows, queries[:1], 'prepare', [])
            pipe.process.kill()
            pipe.process.wait(timeout=60)
            pipe.close()
        stale = list(directory.glob('flat-*.vec')) + list(directory.glob('flat-*.keys'))
        check('P5f1 强杀后派生索引文件存在', len(stale) >= 2, [p.name for p in stale])
        removed = 0
        for path in stale:
            path.unlink()
            removed += 1
        check('P5f2 删除派生索引文件', removed >= 2, removed)
        with Pipe(directory) as pipe:
            failures = []
            filtered, unfiltered = validate_queries(pipe, rows, queries[:20], 'rebuild', failures)
            check('P5f3 重启后从已提交数据重建索引，查询正确',
                  not failures and all(r == 1.0 for r in filtered + unfiltered), failures[:5])
            check('P5f4 重建后 flat 文件重新生成',
                  len(list(directory.glob('flat-*.vec'))) >= 1)
    finally:
        temp.cleanup()

    print(f'\nSUMMARY passed={len(PASSED)} failed={len(FAILED)} '
          f'rows={ROWS} dims={DIMENSIONS} queries={QUERIES} K={K}')
    if FAILED:
        print('FAILED:', *FAILED, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
