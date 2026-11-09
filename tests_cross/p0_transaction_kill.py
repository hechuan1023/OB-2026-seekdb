"""独立交叉验证 P0：失败语句原子性、事务边界与 COMMIT 后立即 SIGKILL。

不依赖被测仓库的测试工具（tests/ 内 Client），自带最小管道客户端。
真值预期按赛题 docs/minivectordb-requirements.md TX-03/04/05/06 与 4.3 独立建立。
"""
import os
import queue
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
    """最小独立客户端：等待明确响应后才 kill，并发读取 stderr。"""

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

    def line(self, timeout=15):
        value = self.lines.get(timeout=timeout)
        assert value is not None, f'unexpected exit; stderr={self.stderr_lines}'
        return value

    def execute(self, sql, timeout=15):
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

    def kill_immediately(self):
        assert self.process.poll() is None
        self.process.kill()  # SIGKILL，Windows 上为 TerminateProcess
        return self.process.wait(timeout=20)

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


def fresh():
    temp = tempfile.TemporaryDirectory(prefix='cross-p0-')
    return temp, Path(temp.name)


SETUP = [
    "CREATE TABLE t (id BIGINT PRIMARY KEY, content VARCHAR(16))",
    "INSERT INTO t VALUES (1, '原值'), (2, '保留')",
    "BEGIN",
    "UPDATE t SET content = '已修改' WHERE id = 1",
    "INSERT INTO t VALUES (3, '新增'), (2, '重复')",
    "SELECT id, content FROM t ORDER BY id",
]


def run_setup(pipe):
    """返回事务内 SELECT 的结果；失败 INSERT 已断言报 CONSTRAINT_ERROR。"""
    for sql in SETUP[:-2]:
        head, _ = pipe.execute(sql)
        assert head.startswith('OK\t'), (sql, head)
    head, _ = pipe.execute(SETUP[-2])
    assert head.startswith('ERROR\tCONSTRAINT_ERROR\t'), head
    _, rows = pipe.execute(SETUP[-1])
    return rows


def rows_of(pipe, sql):
    head, rows = pipe.execute(sql)
    assert head.startswith('RESULT\t'), (sql, head)
    return rows


def scenario(name, finish, expected):
    temp, directory = fresh()
    try:
        pipe = Pipe(directory)
        try:
            in_tx = run_setup(pipe)
            check(f'{name}: 事务内失败语句原子性', in_tx == [['1', '已修改'], ['2', '保留']], in_tx)
            finish(pipe)
        finally:
            pipe.close()
        restarted = Pipe(directory)
        try:
            after = rows_of(restarted, "SELECT id, content FROM t ORDER BY id")
            check(f'{name}: 重启后与预期一致', after == expected, after)
        finally:
            restarted.close()
    finally:
        temp.cleanup()


def main():
    # 分支 1：COMMIT 成功，正常退出重启
    def commit_eof(pipe):
        head, _ = pipe.execute('COMMIT')
        assert head == 'OK\t0', head
        pipe.process.stdin.close()

    scenario('COMMIT+EOF', commit_eof, [['1', '已修改'], ['2', '保留']])

    # 分支 2：ROLLBACK 成功，正常退出重启
    def rollback_eof(pipe):
        head, _ = pipe.execute('ROLLBACK')
        assert head == 'OK\t0', head
        pipe.process.stdin.close()

    scenario('ROLLBACK+EOF', rollback_eof, [['1', '原值'], ['2', '保留']])

    # 分支 3：读到 COMMIT 成功响应后立即 SIGKILL（交接文档要求独立补的专门时序）
    def commit_then_kill(pipe):
        t0 = time.perf_counter()
        head, _ = pipe.execute('COMMIT')
        elapsed = time.perf_counter() - t0
        assert head == 'OK\t0', head
        code = pipe.kill_immediately()
        print(f'  [时序] COMMIT 响应到 SIGKILL 间隔 {elapsed*1000:.1f}ms，kill exit code={code}')

    scenario('COMMIT+立即SIGKILL', commit_then_kill, [['1', '已修改'], ['2', '保留']])

    # 分支 4：不发 COMMIT，直接 SIGKILL
    scenario('未COMMIT+SIGKILL', lambda pipe: pipe.kill_immediately(), [['1', '原值'], ['2', '保留']])

    # 迁移反例：去掉 BEGIN 的自动提交失败（语句原子性）
    temp, directory = fresh()
    try:
        pipe = Pipe(directory)
        try:
            pipe.execute("CREATE TABLE u (id BIGINT PRIMARY KEY, content VARCHAR(16))")
            pipe.execute("INSERT INTO u VALUES (1, 'a')")
            head, _ = pipe.execute("INSERT INTO u VALUES (2, 'b'), (1, 'dup')")
            check('自动提交多行失败整条语句无残留', head.startswith('ERROR\tCONSTRAINT_ERROR\t'), head)
            rows = rows_of(pipe, "SELECT id, content FROM u ORDER BY id")
            check('自动提交失败后表内容', rows == [['1', 'a']], rows)
            head, _ = pipe.execute("INSERT INTO u VALUES (2, 'b')")
            check('失败后可继续插入', head == 'OK\t1', head)
        finally:
            pipe.close()
        again = Pipe(directory)
        try:
            rows = rows_of(again, "SELECT id, content FROM u ORDER BY id")
            check('自动提交失败重启后无 id=2 双残留', rows == [['1', 'a'], ['2', 'b']], rows)
        finally:
            again.close()
    finally:
        temp.cleanup()

    # 迁移反例：事务内向量维度错误失败，随后仍可提交
    temp, directory = fresh()
    try:
        pipe = Pipe(directory)
        try:
            pipe.execute("CREATE TABLE w (id BIGINT PRIMARY KEY, embedding VECTOR(2))")
            pipe.execute("INSERT INTO w VALUES (1, '[1,0]')")
            pipe.execute('BEGIN')
            pipe.execute("INSERT INTO w VALUES (2, '[0,1]')")
            head, _ = pipe.execute("INSERT INTO w VALUES (3, '[0,1,2]')")
            check('事务内向量维度错误', head.startswith('ERROR\tINVALID_VECTOR\t'), head)
            head, _ = pipe.execute('COMMIT')
            check('维度失败后仍可提交', head == 'OK\t0', head)
            rows = rows_of(pipe, "SELECT id FROM w ORDER BY id")
            check('维度失败语句无残留（无 id=3）', rows == [['1'], ['2']], rows)
        finally:
            pipe.close()
    finally:
        temp.cleanup()

    # 迁移反例：UPDATE 命中多行、后续行才失败（VARCHAR 超长）
    temp, directory = fresh()
    try:
        pipe = Pipe(directory)
        try:
            pipe.execute("CREATE TABLE m (id BIGINT PRIMARY KEY, content VARCHAR(4))")
            pipe.execute("INSERT INTO m VALUES (1,'aa'),(2,'bb'),(3,'cc')")
            pipe.execute('BEGIN')
            head, _ = pipe.execute("UPDATE m SET content='dddddd' WHERE id>=1")
            check('多行 UPDATE 后续行失败整条回滚', head.startswith('ERROR\tTYPE_ERROR\t'), head)
            rows = rows_of(pipe, "SELECT id, content FROM m ORDER BY id")
            check('UPDATE 失败无部分生效', rows == [['1', 'aa'], ['2', 'bb'], ['3', 'cc']], rows)
            head, _ = pipe.execute('ROLLBACK')
            check('失败后可回滚', head == 'OK\t0', head)
        finally:
            pipe.close()
        again = Pipe(directory)
        try:
            rows = rows_of(again, "SELECT id, content FROM m ORDER BY id")
            check('UPDATE 失败重启后原值', rows == [['1', 'aa'], ['2', 'bb'], ['3', 'cc']], rows)
        finally:
            again.close()
    finally:
        temp.cleanup()

    print(f'\nSUMMARY passed={len(PASSED)} failed={len(FAILED)}')
    if FAILED:
        print('FAILED:', *FAILED, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
