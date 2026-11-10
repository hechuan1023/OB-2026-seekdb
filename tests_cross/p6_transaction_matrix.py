"""独立交叉验证 P6：事务语义矩阵 TX-01～TX-07 与协议/恢复契约。

依据 docs/minivectordb-requirements.md：
  - 4.2 TX-01 自动提交；TX-02 事务内读自身修改（普通与向量）；
    TX-03 失败语句无部分修改；TX-04 失败后仍可提交或回滚（两分支）；
    TX-05 回滚恢复内容、可见集合与向量检索；TX-06 提交确认后重启保留；
    TX-07 表记录与向量检索可见状态一致。
  - 4.1 向量索引重建完成后才报告就绪；索引可从持久化记录重建。
  - 5.2 同一进程连续处理；stdout 只输出协议内容；日志走 stderr；
    stdout 及时刷新。
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
            assert self.line(timeout=60) == 'READY'

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
    temp = tempfile.TemporaryDirectory(prefix='cross-p6-')
    return temp, Path(temp.name)


def rows_of(pipe, sql):
    head, rows = pipe.execute(sql)
    assert head.startswith('RESULT\t'), (sql, head)
    return rows


def topk(pipe, query, k, tag=None):
    where = '' if tag is None else f" WHERE tag='{tag}'"
    head, rows = pipe.execute(
        f"SELECT id, L2_DISTANCE(v, '{query}') AS d FROM t{where} ORDER BY d LIMIT {k}")
    assert head.startswith('RESULT\t'), head
    return [(r[0], parse_float(r[1])) for r in rows]


def setup(pipe):
    for sql in ("CREATE TABLE t (id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(2))",
                "INSERT INTO t VALUES (1,'a','[1,0]'), (2,'a','[0,1]'), (3,'b','[2,2]')"):
        head, _ = pipe.execute(sql)
        assert head.startswith('OK\t'), head


def main():
    # ================= TX-01 自动提交 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup(pipe)
            head, _ = pipe.execute("INSERT INTO t VALUES (4,'b','[3,3]')")
            check('TX-01a 自动提交插入', head == 'OK\t1', head)
            head, _ = pipe.execute("UPDATE t SET v='[9,9]' WHERE id = 1")
            check('TX-01b 自动提交更新', head == 'OK\t1', head)
            head, _ = pipe.execute('DELETE FROM t WHERE id = 2')
            check('TX-01c 自动提交删除', head == 'OK\t1', head)
            pipe.kill()
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT id, v FROM t ORDER BY id')
            check('TX-01d 强杀后自动提交全部保留',
                  rows == [['1', '[9.0,9.0]'], ['3', '[2.0,2.0]'], ['4', '[3.0,3.0]']], rows)
    finally:
        temp.cleanup()

    # ================= TX-02 事务内读自身修改（普通 + 向量）=================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup(pipe)
            pipe.execute('CREATE VECTOR INDEX ti ON t(v)')
            pipe.execute('BEGIN')
            pipe.execute("UPDATE t SET v='[0,0]' WHERE id = 1")
            pipe.execute("INSERT INTO t VALUES (5,'a','[0.1,0.1]')")
            rows = rows_of(pipe, "SELECT id, v FROM t WHERE id = 1")
            check('TX-02a 普通查询读到事务内修改', rows == [['1', '[0.0,0.0]']], rows)
            nearest = topk(pipe, '[0,0]', 1)
            check('TX-02b 向量检索读到事务内修改（id=1 最近）', nearest == [('1', 0.0)], nearest)
            rows = rows_of(pipe, 'SELECT id FROM t ORDER BY id')
            check('TX-02c 事务内可见新插入行', [r[0] for r in rows] == ['1', '2', '3', '5'], rows)
            pipe.execute('ROLLBACK')
            rows = rows_of(pipe, "SELECT v FROM t WHERE id = 1")
            check('TX-02d 回滚恢复原向量', rows == [['[1.0,0.0]']], rows)
            nearest = topk(pipe, '[0,0]', 2)
            check('TX-02e 回滚后向量检索恢复（并列按主键升序 1,2）',
                  nearest == [('1', 1.0), ('2', 1.0)], nearest)
    finally:
        temp.cleanup()

    # ================= TX-03 失败语句无部分修改 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup(pipe)
            head, _ = pipe.execute("INSERT INTO t VALUES (10,'c','[1,1]'), (1,'dup','[1,1]'), (11,'c','[1,1]')")
            check('TX-03a 多行插入中途重复报错', head.startswith('ERROR\tCONSTRAINT_ERROR\t'), head)
            rows = rows_of(pipe, 'SELECT id FROM t ORDER BY id')
            check('TX-03b 失败语句无部分修改（无 10/11）', [r[0] for r in rows] == ['1', '2', '3'], rows)
            head, _ = pipe.execute("INSERT INTO t VALUES (10,'c','[1,1]'), (11,'c','[1,1]')")
            check('TX-03c 失败后同一语句可重试成功', head == 'OK\t2', head)
    finally:
        temp.cleanup()

    # ================= TX-04 失败后仍可提交 / 回滚（两分支）=================
    for outcome, expected in (('COMMIT', [['1', '[5.0,5.0]'], ['2', '[0.0,1.0]'], ['3', '[2.0,2.0]']]),
                              ('ROLLBACK', [['1', '[1.0,0.0]'], ['2', '[0.0,1.0]'], ['3', '[2.0,2.0]']])):
        temp, directory = fresh()
        try:
            with Pipe(directory) as pipe:
                setup(pipe)
                pipe.execute('BEGIN')
                head, _ = pipe.execute("UPDATE t SET v='[5,5]' WHERE id = 1")
                check(f'TX-04 {outcome} 前成功修改', head == 'OK\t1', head)
                head, _ = pipe.execute("INSERT INTO t VALUES (6,'x','[0,0]'), (2,'dup','[0,0]')")
                check(f'TX-04 {outcome} 分支失败语句报错', head.startswith('ERROR\t'), head)
                head, _ = pipe.execute(outcome)
                check(f'TX-04 {outcome} 失败后仍可 {outcome}', head == 'OK\t0', head)
                pipe.kill()
            with Pipe(directory) as pipe:
                rows = rows_of(pipe, 'SELECT id, v FROM t ORDER BY id')
                check(f'TX-04 {outcome} 分支重启后状态正确', rows == expected, rows)
        finally:
            temp.cleanup()

    # ================= TX-05 回滚恢复内容、可见集合与向量检索 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup(pipe)
            pipe.execute('CREATE VECTOR INDEX ti ON t(v)')
            baseline = topk(pipe, '[0,0]', 3)
            check('TX-05a 基线 TopK', baseline == [('1', 1.0), ('2', 1.0), ('3', 2.8284271247461903)],
                  baseline)
            pipe.execute('BEGIN')
            pipe.execute("UPDATE t SET v='[5,5]' WHERE id = 1")
            pipe.execute('DELETE FROM t WHERE id = 2')
            pipe.execute("INSERT INTO t VALUES (4,'a','[0,0]')")
            in_tx = topk(pipe, '[0,0]', 3)
            check('TX-05b 事务内检索反映增删改', in_tx[0] == ('4', 0.0) and '2' not in [p for p, _ in in_tx], in_tx)
            pipe.execute('ROLLBACK')
            after = topk(pipe, '[0,0]', 3)
            check('TX-05c 回滚后 TopK 与基线完全一致', after == baseline, (after, baseline))
            rows = rows_of(pipe, 'SELECT id, v FROM t ORDER BY id')
            check('TX-05d 回滚后内容恢复',
                  rows == [['1', '[1.0,0.0]'], ['2', '[0.0,1.0]'], ['3', '[2.0,2.0]']], rows)
    finally:
        temp.cleanup()

    # ================= TX-06 提交确认后强杀保留 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup(pipe)
            pipe.execute('BEGIN')
            pipe.execute("UPDATE t SET v='[7,7]' WHERE id = 3")
            head, _ = pipe.execute('COMMIT')
            check('TX-06a 提交确认', head == 'OK\t0', head)
            pipe.kill()
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, "SELECT v FROM t WHERE id = 3")
            check('TX-06b 确认后强杀，重启保留', rows == [['[7.0,7.0]']], rows)
    finally:
        temp.cleanup()

    # ================= TX-07 表记录与向量检索可见状态一致 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup(pipe)
            pipe.execute('CREATE VECTOR INDEX ti ON t(v)')
            phases = []
            def consistency(label):
                failures = []
                for tag in ('a', 'b'):
                    plain = {int(r[0]) for r in rows_of(pipe, f"SELECT id FROM t WHERE tag='{tag}'")}
                    top = {int(r[0]) for r in rows_of(
                        pipe,
                        f"SELECT id FROM t WHERE tag='{tag}' "
                        f"ORDER BY L2_DISTANCE(v, '[0,0]') LIMIT 100000")}
                    if plain != top:
                        failures.append((tag, plain, top))
                phases.append((label, not failures, failures))
            consistency('初始')
            pipe.execute("INSERT INTO t VALUES (4,'a','[0.5,0.5]')")
            consistency('插入后')
            pipe.execute("UPDATE t SET tag='b' WHERE id = 4")
            consistency('过滤列更新后')
            pipe.execute("UPDATE t SET v='[8,8]' WHERE id = 1")
            consistency('向量更新后')
            pipe.execute('DELETE FROM t WHERE id = 3')
            consistency('删除后')
            pipe.execute('BEGIN')
            pipe.execute("INSERT INTO t VALUES (5,'a','[0.1,0.1]')")
            pipe.execute('ROLLBACK')
            consistency('回滚后')
            check('TX-07 六个阶段普通查询与 TopK 可见集合一致',
                  all(ok for _, ok, _ in phases), [p for p in phases if not p[1]])
    finally:
        temp.cleanup()

    # ================= 协议契约（5.2）=================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup(pipe)
            pid = pipe.process.pid
            latencies = []
            for i in range(50):
                started = time.perf_counter()
                head, _ = pipe.execute(f"SELECT id FROM t WHERE id = {i % 3 + 1}")
                latencies.append(time.perf_counter() - started)
                if not head.startswith('RESULT\t'):
                    check('协议 50 条语句连续执行', False, head)
                    break
            else:
                check('协议 50 条语句连续执行', True)
            check('协议 同一进程处理全部语句', pipe.process.pid == pid and pipe.process.poll() is None)
            in_result, illegal = False, []
            for line in pipe.transcript:
                if line.startswith('RESULT\t'):
                    in_result = True
                elif line.startswith('END\t'):
                    in_result = False
                elif in_result:
                    continue  # RESULT 与 END 之间是数据行，属协议内容
                elif not (line == 'READY' or line.startswith(('OK\t', 'ERROR\t'))):
                    illegal.append(line)
            check('协议 stdout 仅协议行（含结果数据行）', not illegal, illegal[:3])
            check('协议 正常操作 stderr 为空', pipe.stderr_lines == [], pipe.stderr_lines[:3])
            check(f'协议 响应及时（最大 {max(latencies)*1000:.1f}ms < 2000ms）',
                  max(latencies) < 2.0, max(latencies))
            head, _ = pipe.execute('SELECT nope FROM t')
            check('协议 错误后继续可用', head.startswith('ERROR\tNOT_FOUND\t')
                  and pipe.execute('SELECT id FROM t WHERE id = 1')[1] == [['1']])
    finally:
        temp.cleanup()

    # ================= READY 必须在索引重建完成之后（4.1）=================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup(pipe)
            pipe.execute('CREATE VECTOR INDEX ti ON t(v)')
            pipe.execute("UPDATE t SET v='[3,3]' WHERE id = 1")  # 使快照失效
        pipe = Pipe(directory, failpoint='during_vector_rebuild', ready=False)
        try:
            code = pipe.process.wait(timeout=60)
        finally:
            pipe.close()
        check('READY 重建未完成即终止（exit=86）', code == 86, code)
        check('READY 重建完成前从未输出 READY', 'READY' not in pipe.transcript, pipe.transcript[:5])
        with Pipe(directory) as pipe:
            nearest = topk(pipe, '[3,3]', 1)
            check('READY 重启完成后查询正确', nearest == [('1', 0.0)], nearest)
    finally:
        temp.cleanup()

    # ================= 派生索引可重建（4.1）=================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup(pipe)
            pipe.execute('CREATE VECTOR INDEX ti ON t(v)')
            baseline = topk(pipe, '[0,0]', 3)
        # 正常关闭会删除派生文件（可丢弃设计）；强杀保留后再删除。
        probe = Pipe(directory)
        try:
            topk(probe, '[0,0]', 2)
            probe.process.kill()
            probe.process.wait(timeout=60)
        finally:
            probe.close()
        stale = list(directory.glob('flat-*.vec')) + list(directory.glob('flat-*.keys'))
        check('重建 强杀后派生文件存在', len(stale) >= 2, [p.name for p in stale])
        removed = 0
        for path in stale:
            path.unlink()
            removed += 1
        check('重建 删除派生索引文件', removed >= 2, removed)
        with Pipe(directory) as pipe:
            after = topk(pipe, '[0,0]', 3)
            check('重建 重启后索引从已提交数据重建，结果一致', after == baseline, (after, baseline))
    finally:
        temp.cleanup()

    # ================= 数据目录收敛（4.1）=================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup(pipe)
            pipe.execute('CREATE VECTOR INDEX ti ON t(v)')
            topk(pipe, '[0,0]', 2)
            pipe.execute('BEGIN')
            pipe.execute("UPDATE t SET v='[4,4]' WHERE id = 1")
            pipe.execute('ROLLBACK')
            topk(pipe, '[0,0]', 2)  # 回滚会失效快照；再查询一次以重建派生文件
            leftovers = [p.name for p in directory.iterdir()
                         if p.name.endswith('.tmp') or p.name.startswith('transaction.')
                         or p.name == 'checkpoint.wal']
            check('目录 无临时/事务残留文件', leftovers == [], leftovers)
            check('目录 权威日志存在', (directory / 'database.wal').exists())
            check('目录 派生索引文件位于数据目录（运行期）',
                  len(list(directory.glob('flat-*'))) >= 2)
    finally:
        temp.cleanup()

    print(f'\nSUMMARY passed={len(PASSED)} failed={len(FAILED)}')
    if FAILED:
        print('FAILED:', *FAILED, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
