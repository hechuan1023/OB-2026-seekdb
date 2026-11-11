"""Cold-start query A/B: write, restart the container, then measure queries.

The platform controls cold start and cache initialization; a single-container
benchmark keeps the row cache and page cache warm across phases. This driver
shuts the database down after the write phase and restarts it (same volume)
before the query phase, so the query phase starts with cold caches.

Local paired measurement only; not the official T/B/M or score.
"""
import argparse
import array
import json
import math
import random
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def send(process, ident, sql):
    process.stdin.write(json.dumps({'id': ident, 'op': 'execute', 'sql': sql,
                                    'search_mode': 'exact'}, separators=(',', ':')) + '\n')
    process.stdin.flush()
    response = json.loads(process.stdout.readline())
    assert response['id'] == ident and response['ok'], response
    return response


def start(image, volume, name, rounds_done):
    process = subprocess.Popen(
        ['docker', 'run', '--rm', '-i', '--name', name, '--network', 'none',
         '--cpus=2', '--memory=256m', '--memory-swap=256m', '-v', volume + ':/data', image],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', bufsize=1)
    assert json.loads(process.stdout.readline()) == {'type': 'ready'}
    return process


def shutdown(process):
    process.stdin.write('{"id":"done","op":"shutdown"}\n')
    process.stdin.flush()
    assert json.loads(process.stdout.readline()) == {'id': 'done', 'ok': True}
    assert process.wait(timeout=60) == 0


def run_round(image, args, rng_seed_offset):
    rng = random.Random(20261110 + rng_seed_offset)
    rows = [(i, 'g' + str(i % args.groups),
             list(array.array('f', (rng.uniform(-1, 1) for _ in range(128)))))
            for i in range(10000)]
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
        sql = ('SELECT id,' + "L2_DISTANCE(v,'" +
               json.dumps(query, separators=(',', ':')) + "') AS d FROM t" + where +
               ' ORDER BY d LIMIT 10')
        queries.append((sql, tag, truth))
    volume = 'mvdb-cold-' + uuid.uuid4().hex[:12]
    subprocess.run(['docker', 'volume', 'create', volume], check=True, capture_output=True)
    processes = []
    try:
        process = start(image, volume, volume + '-w', len(processes))
        processes.append(process)
        send(process, 'create', 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(128))')
        begin = time.perf_counter()
        for i, batch in enumerate(batches):
            assert send(process, i, batch)['affected_rows'] == 100
        writing = time.perf_counter() - begin
        send(process, 'index', 'CREATE VECTOR INDEX vi ON t(v)')
        shutdown(process)
        # Cold restart: the query phase starts with empty row and filter caches.
        process = start(image, volume, volume + '-q', len(processes))
        processes.append(process)
        times = {'filtered': [], 'unfiltered': []}
        for i, (sql, tag, truth) in enumerate(queries):
            begin = time.perf_counter()
            found = send(process, i, sql)['rows']
            times['filtered' if tag is not None else 'unfiltered'].append(time.perf_counter() - begin)
            assert [row[0] for row in found] == [pk for _, pk in truth]
            for actual, (distance, pk) in zip(found, truth):
                assert math.isclose(actual[1], distance, rel_tol=1e-12, abs_tol=1e-12)
        shutdown(process)
        return {'image': image, 'write_seconds': writing,
                'query_seconds': sum(sum(v) for v in times.values()),
                'filtered_seconds': sum(times['filtered']),
                'unfiltered_seconds': sum(times['unfiltered'])}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        subprocess.run(['docker', 'volume', 'rm', volume], check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--compare', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--groups', type=int, default=5)
    parser.add_argument('--queries', type=int, default=1000)
    args = parser.parse_args()
    report = {'images': [args.image, args.compare], 'groups': args.groups,
              'queries': args.queries, 'rounds': [],
              'caveat': 'Cold-start query phase (container restarted after write). '
                        'Local paired measurement; not official T/B/M or score.'}
    for index in range(args.rounds * 2):
        image = args.image if index % 2 == 0 else args.compare
        record = run_round(image, args, 0)
        record['round'] = index // 2 + 1
        report['rounds'].append(record)
        print(json.dumps(record), flush=True)
        Path(args.output).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    for image in (args.image, args.compare):
        values = [r['query_seconds'] for r in report['rounds'] if r['image'] == image]
        writes = [r['write_seconds'] for r in report['rounds'] if r['image'] == image]
        print(image, 'query median', round(statistics.median(values), 3),
              'write median', round(statistics.median(writes), 3), flush=True)
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
