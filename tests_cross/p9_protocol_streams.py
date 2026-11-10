"""独立交叉验证 P9：协议与输入分片鲁棒性（第九组，纯本地黑盒）。

官方驱动未公布，本组按赛题 5.2 的协议要求（stdin 收 SQL、stdout 结果、
stderr 日志、及时刷新、同一进程连续处理、正常关闭回滚未提交事务）验证
传输层边界：
  T1 语句分片写入（多次 write 拼成一条语句）
  T2 一次写入多条语句，响应按序返回
  T3 超长单条语句（数千行 INSERT、超长注释行）
  T4 流水线：连续发送大量语句后再读响应，无死锁、顺序正确
  T5 混合错误流水线：错误与成功交错，进程存活、响应数正确
  T6 EOF 于语句中间：正常退出，不崩溃
  T7 事务中终止进程（Linux SIGTERM / Windows terminate）：重启回滚
  T8 快速重启循环 10 次：每次数据一致
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

    def raw(self, text):
        self.process.stdin.write(text)
        self.process.stdin.flush()

    def execute(self, sql, timeout=120):
        self.raw(sql + ';\n')
        return self.read_response(timeout)

    def read_response(self, timeout=120):
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
    temp = tempfile.TemporaryDirectory(prefix='cross-p9-')
    return temp, Path(temp.name)


def rows_of(pipe, sql):
    head, rows = pipe.execute(sql)
    assert head.startswith('RESULT\t'), (sql, head)
    return rows


def main():
    # ================= T1：语句分片写入 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            head, _ = pipe.execute('CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(8))')
            check('T1a1 建表', head == 'OK\t0', head)
            # 分三次写入同一条语句
            pipe.raw('SEL')
            pipe.raw('ECT id')
            pipe.raw(' FROM t;\n')
            head, rows = pipe.read_response()
            check('T1a2 三次写入拼成一条语句', head.startswith('RESULT\t') and rows == [], (head, rows))
            # 分片插入，分号单独一次写入
            pipe.raw("INSERT INTO t VALUES (1,'a')")
            pipe.raw(';\n')
            head, _ = pipe.read_response()
            check('T1a3 分号单独写入', head == 'OK\t1', head)
            # 分片跨行：第一行不完整，第二行补全
            pipe.raw('INSERT INTO t\n')
            pipe.raw("VALUES (2,'b');\n")
            head, _ = pipe.read_response()
            check('T1a4 跨行分片', head == 'OK\t1', head)
            rows = rows_of(pipe, 'SELECT id FROM t ORDER BY id')
            check('T1a5 分片写入结果正确', [r[0] for r in rows] == ['1', '2'], rows)
    finally:
        temp.cleanup()

    # ================= T2：一次写入多条语句 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            head, _ = pipe.execute('CREATE TABLE t (id BIGINT PRIMARY KEY, n BIGINT)')
            check('T2a1 建表', head == 'OK\t0', head)
            pipe.raw('INSERT INTO t VALUES (1,10);\nINSERT INTO t VALUES (2,20);\n'
                     'SELECT id FROM t ORDER BY id;\n')
            responses = [pipe.read_response() for _ in range(3)]
            check('T2a2 三条语句三个响应',
                  responses[0][0] == 'OK\t1' and responses[1][0] == 'OK\t1'
                  and responses[2][0].startswith('RESULT\t'), [r[0] for r in responses])
            check('T2a3 第三条结果正确', [r[0] for r in responses[2][1]] == ['1', '2'],
                  responses[2][1])
    finally:
        temp.cleanup()

    # ================= T3：超长单条语句 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            head, _ = pipe.execute('CREATE TABLE big (id BIGINT PRIMARY KEY, n BIGINT)')
            check('T3a1 建表', head == 'OK\t0', head)
            values = ','.join(f'({i},{i})' for i in range(5000))
            sql = 'INSERT INTO big VALUES ' + values
            started = time.perf_counter()
            head, _ = pipe.execute(sql)
            elapsed = time.perf_counter() - started
            check(f'T3a2 5000 行单语句（{len(sql)} 字节）成功', head == 'OK\t5000', head)
            print(f'  [T3] 5000 行单语句 {len(sql)} 字节，{elapsed:.2f}s')
            rows = rows_of(pipe, 'SELECT id FROM big ORDER BY id DESC LIMIT 1')
            check('T3a3 超长语句数据完整', rows == [['4999']], rows)
            # 超长注释行后接正常语句（纯注释行本身不产生响应）
            pipe.raw('-- ' + 'x' * 1_000_000 + '\n')
            pipe.raw('SELECT id FROM big ORDER BY id DESC LIMIT 1;\n')
            head, rows = pipe.read_response()
            check('T3a4 1MB 注释行后语句正常', head.startswith('RESULT\t') and rows == [['4999']], (head, rows))
    finally:
        temp.cleanup()

    # ================= T4：流水线（先写后读） =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            head, _ = pipe.execute('CREATE TABLE p (id BIGINT PRIMARY KEY, n BIGINT)')
            check('T4a1 建表', head == 'OK\t0', head)
            statements = ''.join(f'INSERT INTO p VALUES ({i},{i});\n' for i in range(1, 201))
            started = time.perf_counter()
            pipe.raw(statements)
            responses = [pipe.read_response() for _ in range(200)]
            elapsed = time.perf_counter() - started
            check('T4a2 200 条流水线全部成功',
                  all(r[0] == 'OK\t1' for r in responses), responses[:3])
            rows = rows_of(pipe, 'SELECT id FROM p ORDER BY id DESC LIMIT 1')
            check('T4a3 流水线数据完整', rows == [['200']], rows)
            print(f'  [T4] 200 条流水线往返 {elapsed:.2f}s')
    finally:
        temp.cleanup()

    # ================= T5：混合错误流水线 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            head, _ = pipe.execute('CREATE TABLE m (id BIGINT PRIMARY KEY, n BIGINT)')
            check('T5a1 建表', head == 'OK\t0', head)
            statements = []
            for i in range(1, 101):
                if i % 3 == 0:
                    statements.append(f'INSERT INTO m VALUES ({i},{i})')
                elif i % 3 == 1:
                    statements.append('SELECT nope FROM m')
                else:
                    statements.append(f'INSERT INTO m VALUES ({i},{i})')
            pipe.raw(''.join(s + ';\n' for s in statements))
            responses = [pipe.read_response() for _ in range(100)]
            ok_count = sum(1 for r in responses if r[0].startswith('OK\t'))
            err_count = sum(1 for r in responses if r[0].startswith('ERROR\t'))
            check('T5a2 100 条混合流水线响应数正确', len(responses) == 100, len(responses))
            check('T5a3 成功/错误计数正确', ok_count == 66 and err_count == 34, (ok_count, err_count))
            rows = rows_of(pipe, 'SELECT id FROM m')
            check('T5a4 成功语句全部生效', len(rows) == 66, len(rows))
            check('T5a5 进程存活', pipe.process.poll() is None)
    finally:
        temp.cleanup()

    # ================= T6：EOF 于语句中间 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            head, _ = pipe.execute('CREATE TABLE e (id BIGINT PRIMARY KEY, n BIGINT)')
            check('T6a1 建表', head == 'OK\t0', head)
            pipe.raw('INSERT INTO e VALUES (1,1)')  # 无分号
            pipe.process.stdin.close()
            code = pipe.process.wait(timeout=60)
            check('T6a2 语句中间 EOF 正常退出(exit=0)', code == 0, code)
            check('T6a3 无 stderr 输出', pipe.stderr_lines == [], pipe.stderr_lines[:2])
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT id FROM e')
            check('T6a4 EOF 后数据一致（0 或 1 行均可，且无残留损坏）',
                  rows in ([], [['1']]), rows)
    finally:
        temp.cleanup()

    # ================= T7：事务中终止进程 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            head, _ = pipe.execute('CREATE TABLE t (id BIGINT PRIMARY KEY, n BIGINT)')
            check('T7a1 建表', head == 'OK\t0', head)
            head, _ = pipe.execute('INSERT INTO t VALUES (1,1)')
            check('T7a2 初始数据', head == 'OK\t1', head)
            head, _ = pipe.execute('BEGIN')
            check('T7a3 BEGIN', head == 'OK\t0', head)
            head, _ = pipe.execute('UPDATE t SET n = 99 WHERE id = 1')
            check('T7a4 事务内更新', head == 'OK\t1', head)
            pipe.process.terminate()  # POSIX SIGTERM；Windows TerminateProcess
            code = pipe.process.wait(timeout=60)
            check('T7a5 终止进程后退出', code is not None, code)
        with Pipe(directory) as pipe:
            rows = rows_of(pipe, 'SELECT n FROM t WHERE id = 1')
            check('T7a6 终止后未提交事务回滚', rows == [['1']], rows)
    finally:
        temp.cleanup()

    # ================= T8：快速重启循环 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            head, _ = pipe.execute('CREATE TABLE r (id BIGINT PRIMARY KEY, n BIGINT)')
            check('T8a1 建表', head == 'OK\t0', head)
            head, _ = pipe.execute('INSERT INTO r VALUES (1,0)')
            check('T8a2 初始数据', head == 'OK\t1', head)
        failures = []
        for cycle in range(1, 11):
            with Pipe(directory) as pipe:
                head, _ = pipe.execute(f'UPDATE r SET n = {cycle} WHERE id = 1')
                if head != 'OK\t1':
                    failures.append((cycle, 'update', head))
                    break
            with Pipe(directory) as pipe:
                rows = rows_of(pipe, 'SELECT n FROM r WHERE id = 1')
                if rows != [[str(cycle)]]:
                    failures.append((cycle, 'read', rows))
                    break
        check('T8a3 10 次快速重启数据一致', not failures, failures)
    finally:
        temp.cleanup()

    print(f'\nSUMMARY passed={len(PASSED)} failed={len(FAILED)}')
    if FAILED:
        print('FAILED:', *FAILED, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
