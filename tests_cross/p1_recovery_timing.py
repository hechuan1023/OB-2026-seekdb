"""独立交叉验证 P1：恢复时序矩阵与驱动误判注入。

时序分支（赛题 4.3）：
  T1 COMMIT 请求前强杀           -> 必须回滚未提交事务
  T2 提交请求发出、无成功响应强杀 -> 允许完整生效或完整不生效，禁止部分生效
  T3 已读到成功响应后强杀         -> 必须保留提交（P0 已覆盖，此处用自动提交重复确认）
  T4 恢复途中再次强杀，第三次启动 -> 仍正确恢复
复用被测实现的 MINIVECTORDB_FAILPOINT 注入点，但断言与真值独立建立。
"""
import os
import queue
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


class Pipe:
    """带 failpoint 支持的最小客户端。"""

    def __init__(self, directory, failpoint=None, ready=True):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        if failpoint:
            env['MINIVECTORDB_FAILPOINT'] = failpoint
        else:
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

    def send_only(self, sql):
        """发送但不等待响应（用于 T2 无响应时序）。"""
        self.process.stdin.write(sql + ';\n')
        self.process.stdin.flush()

    def wait_exit(self, timeout=15):
        return self.process.wait(timeout=timeout)

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

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def fresh():
    temp = tempfile.TemporaryDirectory(prefix='cross-p1-')
    return temp, Path(temp.name)


def setup_table(pipe):
    for sql in ("CREATE TABLE t (id BIGINT PRIMARY KEY, tag VARCHAR(8), embedding VECTOR(2))",
                "INSERT INTO t VALUES (1,'a','[1,0]'), (2,'b','[0,1]')"):
        head, _ = pipe.execute(sql)
        assert head.startswith('OK\t'), head


def final_state(pipe):
    head, rows = pipe.execute("SELECT id, tag, embedding FROM t ORDER BY id")
    assert head.startswith('RESULT\t'), head
    return rows


BASE = [['1', 'a', '[1.0,0.0]'], ['2', 'b', '[0.0,1.0]']]
COMMITTED = [['1', 'a', '[5.0,5.0]'], ['2', 'b', '[0.0,1.0]'], ['3', 'c', '[0.0,0.0]']]


def main():
    # ---------- T1：COMMIT 请求前强杀（恢复中 failpoint 在 apply 前）----------
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup_table(pipe)
            pipe.execute('BEGIN')
            pipe.execute("INSERT INTO t VALUES (3,'c','[0,0]')")
            pipe.execute("UPDATE t SET embedding='[5,5]' WHERE id = 1")
            # 不发 COMMIT，直接 kill（进程内无 failpoint，正常路径）
            pipe.process.kill()
            pipe.wait_exit()
        with Pipe(directory) as restarted:
            state = final_state(restarted)
            check('T1 COMMIT 前强杀：未提交修改全部回滚', state == BASE, state)
    finally:
        temp.cleanup()

    # ---------- T2：提交请求已发出、无成功响应（after_commit_sync）----------
    # 注意：该 failpoint 对任何自动提交语句也会触发，因此 DDL/初始数据必须
    # 由前置进程（无 failpoint）写入，failpoint 进程只执行事务内语句与 COMMIT。
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup_table(pipe)
        with Pipe(directory, failpoint='after_commit_sync') as pipe:
            pipe.execute('BEGIN')
            pipe.execute("INSERT INTO t VALUES (3,'c','[0,0]')")
            pipe.execute("UPDATE t SET embedding='[5,5]' WHERE id = 1")
            pipe.send_only('COMMIT')
            code = pipe.wait_exit()
            check('T2 提交处理中终止（exit=86）', code == 86, code)
        with Pipe(directory) as restarted:
            state = final_state(restarted)
            check('T2 无响应提交：完整生效（允许分支）', state == COMMITTED, state)
            # 数据库仍可用
            head, _ = restarted.execute("INSERT INTO t VALUES (9,'d','[1,1]')")
            check('T2 后续写入正常', head == 'OK\t1', head)
    finally:
        temp.cleanup()

    # ---------- T2b：footer 写入前强杀（before_commit_footer）----------
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup_table(pipe)
        with Pipe(directory, failpoint='before_commit_footer') as pipe:
            pipe.execute('BEGIN')
            pipe.execute("INSERT INTO t VALUES (3,'c','[0,0]')")
            pipe.execute("UPDATE t SET embedding='[5,5]' WHERE id = 1")
            pipe.send_only('COMMIT')
            code = pipe.wait_exit()
            check('T2b footer 前终止（exit=86）', code == 86, code)
        with Pipe(directory) as restarted:
            state = final_state(restarted)
            check('T2b 无响应提交：完整未生效（允许分支）', state == BASE, state)
    finally:
        temp.cleanup()

    # ---------- T3：自动提交已确认后强杀（重复确认 P0 结论）----------
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup_table(pipe)
            head, _ = pipe.execute("UPDATE t SET embedding='[5,5]' WHERE id = 1")
            assert head == 'OK\t1', head
            pipe.process.kill()
            pipe.wait_exit()
        with Pipe(directory) as restarted:
            state = final_state(restarted)
            check('T3 自动提交确认后强杀：提交保留', state[0] == ['1', 'a', '[5.0,5.0]'], state)
    finally:
        temp.cleanup()

    # ---------- T4：恢复途中再次强杀，第三次启动 ----------
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup_table(pipe)
            pipe.execute("INSERT INTO t VALUES (3,'c','[0,0]')")  # 自动提交
        for round_no in (1, 2):
            with Pipe(directory, failpoint='during_recovery', ready=False) as pipe:
                code = pipe.wait_exit()
                check(f'T4 第 {round_no} 次恢复途中强杀（exit=86）', code == 86, code)
        with Pipe(directory) as third:
            state = final_state(third)
            check('T4 第三次启动恢复完整（含已提交 id=3）',
                  state == [['1', 'a', '[1.0,0.0]'], ['2', 'b', '[0.0,1.0]'], ['3', 'c', '[0.0,0.0]']], state)
            head, _ = third.execute("INSERT INTO t VALUES (4,'d','[1,1]')")
            check('T4 第三次启动可继续写入', head == 'OK\t1', head)
    finally:
        temp.cleanup()

    # ---------- T5：脏向量快照重建途中强杀（during_vector_rebuild）----------
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            setup_table(pipe)
            pipe.execute('CREATE VECTOR INDEX vi ON t(embedding)')
            pipe.execute("UPDATE t SET embedding='[3,3]' WHERE id = 1")  # 使快照失效
        with Pipe(directory, failpoint='during_vector_rebuild', ready=False) as pipe:
            code = pipe.wait_exit()
            check('T5 向量重建途中强杀（exit=86）', code == 86, code)
        with Pipe(directory) as restarted:
            state = final_state(restarted)
            check('T5 重建中断后恢复正确', state == [['1', 'a', '[3.0,3.0]'], ['2', 'b', '[0.0,1.0]']], state)
            head, rows = restarted.execute(
                "SELECT id FROM t ORDER BY L2_DISTANCE(embedding, '[3,3]') LIMIT 1")
            check('T5 恢复后向量查询正常', rows == [['1']], rows)
    finally:
        temp.cleanup()

    # ---------- T6：checkpoint 发布点强杀 ----------
    # 触发条件：wal_bytes >= 阈值 且 wal_bytes > live_bytes*3。
    # 构造：先写 500 行（WAL 膨胀），failpoint 进程再 DELETE 大部分行（live 骤降）→ 满足条件。
    # 注意：DELETE 语句在 checkpoint 开始前已完整提交（footer+fsync+索引应用），
    # 因此无论发布前后强杀，删除都应保留。
    for point in ('before_checkpoint_replace', 'after_checkpoint_replace'):
        temp, directory = fresh()
        try:
            with Pipe(directory) as pipe:
                pipe.execute('CREATE TABLE c (id BIGINT PRIMARY KEY, value BIGINT)')
                pipe.execute('INSERT INTO c VALUES ' + ','.join(f'({i},{i})' for i in range(500)))
            proc = subprocess.Popen(
                [sys.executable, '-X', 'utf8', '-m', 'minivectordb', '--data-dir', str(directory),
                 '--checkpoint-mib', '0.004'],
                cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING='utf-8',
                                   MINIVECTORDB_FAILPOINT=point),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', bufsize=1)
            proc.stdin.write("DELETE FROM c WHERE id >= 2;\n")
            proc.stdin.flush()
            code = proc.wait(timeout=30)
            check(f'T6-{point} checkpoint 压缩中强杀（exit=86）', code == 86, code)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if not stream.closed:
                    stream.close()
            with Pipe(directory) as restarted:
                head, rows = restarted.execute('SELECT id, value FROM c ORDER BY id')
                check(f'T6-{point} 已提交删除保留（压缩失败不回滚已确认事务）',
                      rows == [['0', '0'], ['1', '1']], rows)
                head, _ = restarted.execute('INSERT INTO c VALUES (2, 200)')
                check(f'T6-{point} 后续写入正常', head == 'OK\t1', head)
                checkpoint_left = (directory / 'checkpoint.wal').exists()
                check(f'T6-{point} 残留 checkpoint.wal 已被启动清理', not checkpoint_left)
        finally:
            temp.cleanup()

    print(f'\nSUMMARY passed={len(PASSED)} failed={len(FAILED)}')
    if FAILED:
        print('FAILED:', *FAILED, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
