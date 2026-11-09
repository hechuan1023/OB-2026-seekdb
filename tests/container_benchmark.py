"""Host-side, three-round Docker benchmark. Never computes a competition score.

The database runs alone in a limited container. The workload, reference answers
and timing driver run on the host. A small sync/cat metrics helper is included in
the container's resource totals; no global cache dropping is performed.
"""
import argparse
import hashlib
import json
import math
import queue
import random
import statistics
import subprocess
import threading
import time
import uuid
from pathlib import Path

from tests.benchmark import f32
from tests.test_recovery import Client

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, encoding='utf-8').strip()


class ContainerClient(Client):
    def __init__(self, image, volume, name, memory):
        self.name = name
        self.process = subprocess.Popen([
            'docker', 'run', '-i', '--name', name, '--network=none', '--cpus=2',
            f'--memory={memory}m', f'--memory-swap={memory}m',
            '-v', f'{volume}:/data', '--mount',
            f'type=bind,source={ROOT / "tests" / "measure_entry.py"},target=/app/measure_entry.py,readonly',
            '--entrypoint', 'python', image, '/app/measure_entry.py', '--data-dir', '/data'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', bufsize=1)
        self.lines, self.stderr = queue.Queue(), []
        self.stdout_thread = threading.Thread(target=self._read, daemon=True)
        self.stderr_thread = threading.Thread(target=self._errors, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()
        assert self.line() == 'READY', self.stderr

    def line(self):
        return self.lines.get(timeout=60)

    def metrics(self):
        # The helper runs outside timed SQL windows. Report its inclusion honestly.
        text = docker('exec', self.name, 'sh', '-c',
                      'sync; for f in memory.peak memory.max memory.swap.max memory.events io.stat cpu.stat; '
                      'do echo ===$f; cat /sys/fs/cgroup/$f; done; '
                      'echo ===process_io; cat /proc/1/io')
        parts = {}
        for block in text.split('===')[1:]:
            name, _, value = block.partition('\n')
            parts[name] = value.strip()
        events = dict(line.split() for line in parts['memory.events'].splitlines())
        assert int(events['oom']) == int(events['oom_kill']) == 0, events
        assert parts['memory.swap.max'] == '0'
        assert parts['io.stat'], 'Missing I/O accounting; do not substitute zero'
        return parts

    def builds(self):
        events = [json.loads(line[len('MVDB_BUILD '):]) for line in self.stderr if line.startswith('MVDB_BUILD ')]
        errors = [line for line in self.stderr if not line.startswith('MVDB_BUILD ')]
        assert not errors, errors
        return {'count': len(events), 'logical_bytes': sum(e['logical_bytes'] for e in events),
                'seconds': sum(e['seconds'] for e in events)}


def latency(values):
    ordered = sorted(values)
    return {'count': len(values), 'total_seconds': sum(values),
            'median_seconds': statistics.median(values),
            'p95_seconds': ordered[math.ceil(len(ordered) * .95) - 1]}


def truth(data, vector, tag=None):
    return sorted(((row[0], math.dist(row[3], vector)) for row in data
                   if tag is None or row[1] == tag), key=lambda x: (x[1], x[0]))[:10]


def check(actual, expected):
    assert isinstance(actual, list), actual
    assert [int(row[0]) for row in actual] == [row[0] for row in expected], (actual, expected)
    for row, reference in zip(actual, expected):
        assert math.isclose(float(row[1]), reference[1], rel_tol=1e-12, abs_tol=1e-12)


def query_sql(vector, tag=None):
    where = '' if tag is None else f" WHERE tag='{tag}'"
    return f"SELECT id,L2_DISTANCE(v,'{json.dumps(vector)}') AS d FROM docs{where} ORDER BY d LIMIT 10"


def run_round(args, round_number):
    rng = random.Random(args.seed)
    data = [[i, f'g{i % 5}', 'original', [f32(rng.uniform(-1, 1)) for _ in range(args.dimensions)]] for i in range(args.rows)]
    batches = ['INSERT INTO docs VALUES ' + ','.join(
        f"({row[0]},'{row[1]}','{row[2]}','{json.dumps(row[3])}')" for row in data[first:first+100])
        for first in range(0, args.rows, 100)]
    workload = []
    for i in range(args.queries):
        vec = [f32(rng.uniform(-1, 1)) for _ in range(args.dimensions)]
        tag = f'g{i % 5}' if i % 2 else None
        workload.append((query_sql(vec, tag), truth(data, vec, tag), bool(i % 2)))
    prefix = 'mvdb-bench-' + uuid.uuid4().hex[:12]
    volume = prefix + '-data'
    docker('volume', 'create', volume)
    clients = []
    names = []
    try:
        def connect():
            name = prefix + f'-{len(names)}'
            names.append(name)
            start = time.perf_counter()
            client = ContainerClient(args.image, volume, name, args.memory)
            clients.append(client)
            return client, time.perf_counter() - start

        c, startup = connect()
        assert c.execute(f'CREATE TABLE docs(id BIGINT PRIMARY KEY,tag VARCHAR(16),content VARCHAR(32),v VECTOR({args.dimensions}))') == 'OK\t0'
        start = time.perf_counter()
        for i, sql in enumerate(batches):
            assert c.execute(sql) == f'OK\t{min(100, args.rows-i*100)}'
        write = time.perf_counter() - start
        write_resources = c.metrics()
        start = time.perf_counter()
        assert c.execute('CREATE VECTOR INDEX vi ON docs(v)') == 'OK\t0'
        for sql, expected, _ in workload[:4]:
            check(c.execute(sql), expected)
        preparation = time.perf_counter() - start
        elapsed, results, groups = [], [], {False: [], True: []}
        for sql, expected, filtered in workload:
            start = time.perf_counter()
            actual = c.execute(sql)
            seconds = time.perf_counter() - start
            elapsed.append(seconds)
            groups[filtered].append(seconds)
            results.append((actual, expected))
        static_resources = c.metrics()
        for actual, expected in results:
            check(actual, expected)
        mixed = {}
        vec = [f32(rng.uniform(-1, 1)) for _ in range(args.dimensions)]
        for scenario in ('read_only_commit', 'zero_match', 'text_update', 'filter_update', 'vector_update', 'failed_insert'):
            samples = []
            for cycle in range(args.cycles):
                row = data[cycle % args.rows]
                if scenario == 'read_only_commit':
                    commands = [('BEGIN', 'OK\t0'), ('COMMIT', 'OK\t0')]
                elif scenario == 'zero_match':
                    commands = [(f"UPDATE docs SET content='no' WHERE id={args.rows+cycle}", 'OK\t0')]
                elif scenario == 'text_update':
                    row[2] = f'changed{cycle}'
                    commands = [(f"UPDATE docs SET content='{row[2]}' WHERE id={row[0]}", 'OK\t1')]
                elif scenario == 'filter_update':
                    row[1] = f'g{(cycle+1) % 5}'
                    commands = [(f"UPDATE docs SET tag='{row[1]}' WHERE id={row[0]}", 'OK\t1')]
                elif scenario == 'vector_update':
                    row[3] = vec[:]
                    commands = [(f"UPDATE docs SET v='{json.dumps(vec)}' WHERE id={row[0]}", 'OK\t1')]
                else:
                    commands = [(f"INSERT INTO docs VALUES ({args.rows+cycle},'g1','bad','{json.dumps(vec)}'),(0,'g1','dup','{json.dumps(vec)}')", 'ERROR\tCONSTRAINT_ERROR\t')]
                tag = 'g1' if cycle % 2 else None
                expected = truth(data, vec, tag)
                sql = query_sql(vec, tag)
                start = time.perf_counter()
                for command, expected_status in commands:
                    status = c.execute(command)
                    assert status.startswith(expected_status) if expected_status.startswith('ERROR') else status == expected_status, status
                actual = c.execute(sql)
                samples.append(time.perf_counter() - start)
                check(actual, expected)
            mixed[scenario] = latency(samples)
        mixed_resources = c.metrics()
        c.close()
        assert c.process.returncode == 0
        build_stats = c.builds()
        c, recovery = connect()
        recovery_resources = c.metrics()  # Before verification reads.
        restored = c.execute('SELECT id,tag,content,v FROM docs ORDER BY id')
        assert len(restored) == len(data)
        for actual, expected in zip(restored, data):
            assert [int(actual[0]), actual[1], actual[2], json.loads(actual[3])] == expected
        c.close()
        assert c.process.returncode == 0
        report = {'round': round_number, 'startup_seconds': startup, 'write_seconds': write,
                  'preparation_seconds': preparation, 'static_query': latency(elapsed),
                  'filtered_query': latency(groups[True]), 'unfiltered_query': latency(groups[False]),
                  'mixed': mixed, 'snapshot_builds': build_stats, 'recovery_seconds': recovery,
                  'recovery_builds': c.builds(), 'write_resources': write_resources,
                  'static_resources': static_resources, 'mixed_resources': mixed_resources,
                  'recovery_resources': recovery_resources, 'all_results_verified': True,
                  'filtered_recall_at_10': 1.0, 'unfiltered_recall_at_10': 1.0,
                  'restart_content_verified': True}
        print(json.dumps({'round': round_number, 'write_seconds': write,
                          'query_seconds': report['static_query']['total_seconds'],
                          'snapshot_builds': build_stats}, ensure_ascii=False), flush=True)
        return report
    finally:
        for name in names:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, check=False)
        for client in clients:
            client.close()
        subprocess.run(['docker', 'volume', 'rm', volume], stdout=subprocess.DEVNULL, check=False)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--image', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--rows', type=int, default=10000)
    p.add_argument('--dimensions', type=int, default=128)
    p.add_argument('--queries', type=int, default=1000)
    p.add_argument('--cycles', type=int, default=8)
    p.add_argument('--rounds', type=int, default=3)
    p.add_argument('--memory', type=int, default=128)
    p.add_argument('--seed', type=int, default=20261112)
    args = p.parse_args()
    assert args.rows >= 10 and args.queries >= 4 and args.rounds > 0 and args.cycles > 0
    # Hash actual image source, not a potentially different host working tree.
    code = "import hashlib,json,pathlib; p=pathlib.Path('/app'); print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((p/'minivectordb').glob('*.py'))}))"
    source = json.loads(docker('run', '--rm', '--network=none', '--entrypoint', 'python', args.image, '-c', code))
    hashes = {f'tests/{name}': hashlib.sha256((ROOT/'tests'/name).read_bytes()).hexdigest()
              for name in ('container_benchmark.py', 'measure_entry.py', 'benchmark.py', 'test_recovery.py')}
    report = {'version': 'host-docker-v1', 'config': vars(args),
              'image_id': docker('image', 'inspect', args.image, '--format', '{{.Id}}'),
              'source_sha256': source, 'driver_sha256': hashes, 'rounds': [],
              'caveat': 'Local comparison, not official T/B/M. Driver outside database cgroup. sync/cat probe included in memory/I/O. Cumulative per-device io.stat; no global cold-cache control. Recovery includes Docker startup. Snapshot bytes are logical output, not physical I/O.'}
    for i in range(args.rounds):
        report['rounds'].append(run_round(args, i + 1))
        Path(args.output).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    report['medians'] = {
        'write_seconds': statistics.median(r['write_seconds'] for r in report['rounds']),
        'query_seconds': statistics.median(r['static_query']['total_seconds'] for r in report['rounds']),
        'recovery_seconds': statistics.median(r['recovery_seconds'] for r in report['rounds']),
        'mixed_seconds': {name: statistics.median(r['mixed'][name]['total_seconds'] for r in report['rounds'])
                          for name in report['rounds'][0]['mixed']},
    }
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report['medians']), flush=True)


if __name__ == '__main__':
    main()
