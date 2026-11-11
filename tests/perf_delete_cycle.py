"""Paired delete/query cycle benchmark: candidate vs baseline source trees.

Measures the workload the incremental-delete change targets: repeated
DELETE + TopK query cycles on an indexed table. Runs both versions in one
process, alternating order per round, against independent data directories,
and checks every query against an independent truth model.

Local paired measurement only; not the official cgroup T/B/M or score.
"""
import argparse
import json
import math
import random
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_recovery import Client


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def run_version(root, rows, dimensions, cycles, seed):
    rng = random.Random(seed)
    data = {i: [f32(rng.uniform(-1, 1)) for _ in range(dimensions)] for i in range(rows)}
    with tempfile.TemporaryDirectory() as directory:
        client = Client(directory, engine_root=str(root))
        try:
            client.execute(f'CREATE TABLE docs(id BIGINT PRIMARY KEY,v VECTOR({dimensions}))')
            for offset in range(0, rows, 100):
                batch = ','.join(f"({i},'{json.dumps(data[i])}')"
                                 for i in range(offset, min(offset + 100, rows)))
                assert client.execute(f'INSERT INTO docs VALUES {batch}') == 'OK\t100'
            assert client.execute('CREATE VECTOR INDEX vi ON docs(v)') == 'OK\t0'
            # Warm up: build the snapshot once so both versions start hot.
            client.execute("SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM docs ORDER BY d LIMIT 10")
            live = dict(data)
            delete_seconds = query_seconds = 0.0
            for cycle in range(cycles):
                # Range deletes keep the statement short while removing a
                # predictable slice; ids are dense so the count is exact.
                floor = rng.randrange(0, rows - 200)
                span = 100
                sql = f'DELETE FROM docs WHERE id >= {floor} AND id < {floor + span}'
                victims = [key for key in live if floor <= key < floor + span]
                start = time.perf_counter()
                assert client.execute(sql) == f'OK\t{len(victims)}'
                delete_seconds += time.perf_counter() - start
                for key in victims:
                    del live[key]
                query = [f32(rng.uniform(-1, 1)) for _ in range(dimensions)]
                sql = f"SELECT id,L2_DISTANCE(v,'{json.dumps(query)}') AS d FROM docs ORDER BY d LIMIT 10"
                start = time.perf_counter()
                rows_out = client.execute(sql)
                query_seconds += time.perf_counter() - start
                truth = sorted(((key, math.dist(vec, query)) for key, vec in live.items()),
                               key=lambda pair: (pair[1], pair[0]))[:10]
                assert [int(row[0]) for row in rows_out] == [pair[0] for pair in truth], (
                    str(root), cycle, 'id mismatch')
                for row, reference in zip(rows_out, truth):
                    assert math.isclose(float(row[1]), reference[1], rel_tol=1e-12, abs_tol=1e-12), (
                        str(root), cycle, 'distance mismatch')
            remaining = client.execute('SELECT id FROM docs ORDER BY id LIMIT 100000')
            assert len(remaining) == len(live), (str(root), len(remaining), len(live))
        finally:
            client.close()
    return {'delete_seconds': round(delete_seconds, 6),
            'query_seconds': round(query_seconds, 6),
            'rows_remaining': len(live)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--dimensions', type=int, default=128)
    parser.add_argument('--cycles', type=int, default=50)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--seed', type=int, default=20261111)
    parser.add_argument('--output')
    args = parser.parse_args()
    report = {'baseline': args.baseline, 'candidate': args.candidate,
              'rows': args.rows, 'dimensions': args.dimensions,
              'cycles': args.cycles, 'rounds': args.rounds, 'seed': args.seed,
              'pairs': [], 'workload_version': 2,
              'caveat': 'Local paired host measurement; not official T/B/M or score'}
    for round_number in range(args.rounds):
        order = [('baseline', args.baseline), ('candidate', args.candidate)]
        if round_number % 2:
            order.reverse()  # Swap execution order every other round.
        pair = {'round': round_number, 'order': [name for name, _ in order], 'results': {}}
        for name, root in order:
            pair['results'][name] = run_version(Path(root), args.rows,
                                                 args.dimensions, args.cycles, args.seed)
            print(json.dumps({'round': round_number, name: pair['results'][name]}), flush=True)
        report['pairs'].append(pair)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered, flush=True)
    if args.output:
        Path(args.output).write_text(rendered + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
