"""Paired local benchmark with /data device attribution and restart verification.

Run each ordering separately. The sampler runs inside the database cgroup, so
its overhead is included. This is not the official cold-cache benchmark.
"""
import argparse
import array
import json
import math
import random
import subprocess
import time
import uuid
from pathlib import Path


SAMPLER = r'''
import json, os
from pathlib import Path
os.sync()
d=os.stat('/data').st_dev
key=str(os.major(d))+':'+str(os.minor(d))
lines=Path('/sys/fs/cgroup/io.stat').read_text().splitlines()
io={p[0]:dict(x.split('=',1) for x in p[1:]) for p in (x.split() for x in lines)}
print(json.dumps({'device':key,'io':io,'memory_peak_bytes':int(Path('/sys/fs/cgroup/memory.peak').read_text())}))
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', required=True)
    ap.add_argument('--compare', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--groups', type=int, default=5)
    ap.add_argument('--queries', type=int, default=1000)
    ap.add_argument('--projection', choices=('numeric', 'full'), default='numeric')
    args = ap.parse_args()
    if args.groups < 1 or args.rounds < 1 or args.queries < 2:
        ap.error('Invalid groups, rounds or query count')
    rng = random.Random(20261110)
    rows = [(i, 'g' + str(i % args.groups), list(array.array('f',
             (rng.uniform(-1, 1) for _ in range(128))))) for i in range(10000)]
    batches = ['INSERT INTO t VALUES ' + ','.join(
        "(%d,'%s','%s')" % (pk, tag, json.dumps(vec, separators=(',', ':')))
        for pk, tag, vec in rows[start:start + 100]) for start in range(0, len(rows), 100)]
    queries = []
    for i in range(args.queries):
        query = list(array.array('f', (rng.uniform(-1, 1) for _ in range(128))))
        tag = 'g' + str((i // 2) % args.groups) if i % 2 else None
        truth = sorted((math.dist(vec, query), pk) for pk, value, vec in rows
                       if tag is None or value == tag)[:10]
        where = '' if tag is None else " WHERE tag='" + tag + "'"
        projection = 'id,tag,v,' if args.projection == 'full' else 'id,'
        sql = ('SELECT ' + projection + "L2_DISTANCE(v,'" +
               json.dumps(query, separators=(',', ':')) + "') AS d FROM t" + where +
               ' ORDER BY d LIMIT 10')
        queries.append((sql, tag, truth))
    results = []
    for index in range(args.rounds * 2):
        image = args.image if index % 2 == 0 else args.compare
        volume = 'mvdb-attributed-' + uuid.uuid4().hex[:12]
        subprocess.run(['docker', 'volume', 'create', volume], check=True, capture_output=True)
        processes = []
        try:
            def start():
                name = volume + '-' + str(len(processes))
                p = subprocess.Popen(['docker', 'run', '--rm', '-i', '--name', name,
                    '--network', 'none', '--cpus=2', '--memory=256m', '--memory-swap=256m',
                    '-v', volume + ':/data', image], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding='utf-8', bufsize=1)
                processes.append(p)
                assert json.loads(p.stdout.readline()) == {'type': 'ready'}
                return p, name

            def send(p, ident, sql):
                p.stdin.write(json.dumps({'id': ident, 'op': 'execute', 'sql': sql,
                                         'search_mode': 'exact'}, separators=(',', ':')) + '\n')
                p.stdin.flush()
                response = json.loads(p.stdout.readline())
                assert response['id'] == ident and response['ok'], response
                return response

            def sample(name):
                result = subprocess.check_output(['docker', 'exec', name, 'python', '-c', SAMPLER],
                                                  text=True, encoding='utf-8')
                return json.loads(result)

            def shutdown(p):
                p.stdin.write('{"id":"done","op":"shutdown"}\n')
                p.stdin.flush()
                assert json.loads(p.stdout.readline()) == {'id': 'done', 'ok': True}
                assert p.wait(timeout=60) == 0

            p, name = start()
            send(p, 'create', 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(128))')
            t = time.perf_counter()
            for i, batch in enumerate(batches):
                assert send(p, i, batch)['affected_rows'] == 100
            writing = time.perf_counter() - t
            after_write = sample(name)
            send(p, 'index', 'CREATE VECTOR INDEX vi ON t(v)')
            times = {'filtered': [], 'unfiltered': []}
            for i, (sql, tag, truth) in enumerate(queries):
                t = time.perf_counter()
                found = send(p, i, sql)['rows']
                times['filtered' if tag is not None else 'unfiltered'].append(time.perf_counter() - t)
                assert [row[0] for row in found] == [pk for _, pk in truth]
                distance_column = 3 if args.projection == 'full' else 1
                for actual, (distance, pk) in zip(found, truth):
                    assert math.isclose(actual[distance_column], distance, rel_tol=1e-12, abs_tol=1e-12)
                    if args.projection == 'full':
                        assert actual[1] == rows[pk][1] and actual[2] == rows[pk][2]
            after_query = sample(name)
            assert after_write['device'] == after_query['device']
            shutdown(p)
            # Correctness check occurs after measurements in a separate container.
            p, _ = start()
            actual = send(p, 'verify', 'SELECT id,tag,v FROM t ORDER BY id')['rows']
            assert len(actual) == len(rows)
            for got, expected in zip(actual, rows):
                assert got == list(expected), got[0]
            shutdown(p)
            record = {'image': image, 'round': index // 2 + 1,
                      'write_seconds': writing,
                      'filtered_seconds': sum(times['filtered']),
                      'unfiltered_seconds': sum(times['unfiltered']),
                      'query_seconds': sum(sum(x) for x in times.values()),
                      'after_write': after_write, 'after_query': after_query,
                      'verified_rows_after_restart': len(actual)}
            results.append(record)
            print(json.dumps(record), flush=True)
        finally:
            for p in processes:
                if p.poll() is None:
                    p.kill()
                    p.wait()
                for stream in (p.stdin, p.stdout, p.stderr):
                    stream.close()
            subprocess.run(['docker', 'volume', 'rm', volume], check=True, capture_output=True)
    Path(args.output).write_text(json.dumps({
        'images': [args.image, args.compare], 'rows': len(rows), 'dimensions': 128,
        'groups': args.groups, 'queries': args.queries, 'projection': args.projection,
        'seed': 20261110, 'rounds': results,
        'notes': ('Write snapshot is cumulative since startup; query snapshot is cumulative since '
                  'startup including load and index. Sampler runs in cgroup. Restart verification '
                  'is outside measurement. No cold-cache control or post-cgroup writeback capture; '
                  'local device counters are not the official B.')}, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
