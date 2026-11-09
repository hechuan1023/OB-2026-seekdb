"""Host-side validation of the actual image ENTRYPOINT, stdin and restart.

Creates uniquely named containers/volume and removes only those it owns.
Run: python -m tests.docker_validation --output docs/docker-lifecycle.json
"""
import argparse
import json
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .test_recovery import Client


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, encoding='utf-8').strip()


class DockerClient(Client):
    def __init__(self, image, volume, name):
        self.name = name
        self.process = subprocess.Popen([
            'docker', 'run', '-i', '--name', name, '--network', 'none',
            '--cpus=2', '--memory=64m', '--memory-swap=64m',
            '-v', f'{volume}:/data', image,
            '--data-dir', '/data', '--cache-memory-mib', '1'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', bufsize=1)
        self.lines, self.stderr = queue.Queue(), []
        self.stdout_thread = threading.Thread(target=self._read, daemon=True)
        self.stderr_thread = threading.Thread(target=self._errors, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()
        assert self.line() == 'READY', self.stderr


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='minivectordb:round2-validated')
    parser.add_argument('--output', default='docs/docker-lifecycle.json')
    args = parser.parse_args()
    prefix = 'mvdb-verify-' + uuid.uuid4().hex[:12]
    volume = prefix + '-data'
    docker('volume', 'create', volume)
    clients, checks, exits = [], [], []
    start = time.perf_counter()
    try:
        def connect():
            client = DockerClient(args.image, volume, prefix + f'-{len(clients)}')
            clients.append(client)
            return client

        def success(client, sql, expected):
            actual = client.execute(sql)
            assert actual == expected, (sql, actual, expected)

        def finish(client, signal=None):
            if signal:
                docker('kill', '--signal', signal, client.name)
                client.process.wait(timeout=20)
            client.close()
            state = json.loads(docker('inspect', '--format', '{{json .State}}', client.name))
            assert not state['OOMKilled']
            assert state['ExitCode'] == (137 if signal == 'KILL' else 0), state
            exits.append({'signal': signal or 'EOF', 'exit_code': state['ExitCode'], 'oom_killed': False})

        c = connect()
        success(c, 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(16),v VECTOR(2))', 'OK\t0')
        success(c, "INSERT INTO t VALUES (1,'中文','[1,0]'),(2,'b','[0,1]'),(3,'b','[2,0]')", 'OK\t3')
        success(c, 'CREATE VECTOR INDEX vi ON t(v)', 'OK\t0')
        success(c, 'BEGIN', 'OK\t0')
        success(c, "UPDATE t SET v='[0,0]' WHERE id=1", 'OK\t1')
        success(c, 'DELETE FROM t WHERE id=2', 'OK\t1')
        success(c, "INSERT INTO t VALUES (4,'c','[0,0]')", 'OK\t1')
        success(c, 'SELECT id FROM t ORDER BY id', [['1'], ['3'], ['4']])
        failed = c.execute("INSERT INTO t VALUES (9,'x','[0,0]'),(1,'duplicate','[0,0]')")
        assert failed.startswith('ERROR\tCONSTRAINT_ERROR\t'), failed
        success(c, 'SELECT id FROM t WHERE id=9', [])
        success(c, 'ROLLBACK', 'OK\t0')
        success(c, 'SELECT id FROM t ORDER BY id', [['1'], ['2'], ['3']])
        checks.append('read-own-writes, multirow statement atomicity, rollback')
        success(c, 'BEGIN', 'OK\t0')
        success(c, "UPDATE t SET v='[9,9]' WHERE id=1", 'OK\t1')
        success(c, 'COMMIT', 'OK\t0')
        success(c, 'BEGIN', 'OK\t0')
        success(c, "UPDATE t SET v='[0,0]' WHERE id=1", 'OK\t1')
        success(c, "SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 1", [['1']])
        finish(c, 'KILL')
        c = connect()
        success(c, 'SELECT id,tag,v FROM t WHERE id=1', [['1', '中文', '[9.0,9.0]']])
        success(c, "SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 2", [['2'], ['3']])
        success(c, "SELECT id FROM t WHERE tag='中文' ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 2", [['1']])
        checks.append('SIGKILL preserves acknowledged commit and discards uncommitted vector snapshot')
        success(c, 'BEGIN', 'OK\t0')
        success(c, 'DELETE FROM t WHERE id=2', 'OK\t1')
        finish(c)
        c = connect()
        success(c, 'SELECT id FROM t ORDER BY id', [['1'], ['2'], ['3']])
        checks.append('EOF rollback and same-volume restart')
        success(c, 'BEGIN', 'OK\t0')
        success(c, 'DELETE FROM t WHERE id=3', 'OK\t1')
        finish(c, 'TERM')
        c = connect()
        success(c, 'SELECT id FROM t ORDER BY id', [['1'], ['2'], ['3']])
        checks.append('PID 1 SIGTERM rollback and restart')
        finish(c)
        report = {
            'image_id': docker('image', 'inspect', args.image, '--format', '{{.Id}}'),
            'platform': docker('image', 'inspect', args.image, '--format', '{{.Os}}/{{.Architecture}}'),
            'network': 'none', 'cpu_limit': 2, 'memory_limit_bytes': 64 * 1024 * 1024,
            'swap_disabled': True, 'checks_passed': checks, 'container_exits': exits,
            'elapsed_seconds': round(time.perf_counter() - start, 6),
            'official_protocol_checked': False,
        }
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        for client in clients:
            subprocess.run(['docker', 'rm', '-f', client.name], stdout=subprocess.DEVNULL, check=False)
            client.close()
        subprocess.run(['docker', 'volume', 'rm', volume], stdout=subprocess.DEVNULL, check=False)


if __name__ == '__main__':
    main()
