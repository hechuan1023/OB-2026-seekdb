"""Reproducible local end-to-end smoke benchmark, not the official cgroup benchmark."""
import argparse
import ctypes
import hashlib
import json
import math
import random
import struct
import tempfile
import time
from pathlib import Path

from tests.test_recovery import Client


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def peak_working_set(process):
    if not hasattr(ctypes, 'WinDLL'):
        return None
    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
            'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]
    counter = Counters()
    counter.cb = ctypes.sizeof(counter)
    function = ctypes.WinDLL('psapi').GetProcessMemoryInfo
    function.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
    function.restype = ctypes.c_int
    if function(int(process._handle), ctypes.byref(counter), counter.cb):
        return counter.PeakWorkingSetSize
    return None


def run(rows, dimensions, queries, seed, engine_root=None, extra_args=(), warmup=4):
    engine_root = Path(engine_root or Path(__file__).resolve().parents[1]).resolve()
    source_hashes = {str(path.relative_to(engine_root)).replace('\\', '/'):
                     hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted((engine_root / 'minivectordb').glob('*.py')) +
                     sorted((engine_root / 'native').glob('*.cpp'))}
    native_libraries = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (engine_root / 'minivectordb').glob('_native.*')}
    rng = random.Random(seed)
    data = [(i, i % 5, [f32(rng.uniform(-1, 1)) for _ in range(dimensions)]) for i in range(rows)]
    workload = []
    expected = []
    for i in range(queries):
        query = [f32(rng.uniform(-1, 1)) for _ in range(dimensions)]
        tag = i % 5 if i % 2 else None
        where = f" WHERE tag='g{tag}'" if tag is not None else ''
        workload.append(f"SELECT id,L2_DISTANCE(v,'{json.dumps(query)}') AS distance FROM docs{where} ORDER BY distance LIMIT 10")
        truth = sorted(((row_id, math.dist(vec, query)) for row_id, category, vec in data
                        if tag is None or tag == category), key=lambda pair: (pair[1], pair[0]))[:10]
        expected.append(truth)
    print(json.dumps({'phase': 'reference_ready', 'rows': rows, 'dimensions': dimensions, 'queries': queries}), flush=True)
    batches = ['INSERT INTO docs VALUES ' + ','.join(
        f"({i},'g{tag}','{json.dumps(vector)}')" for i, tag, vector in data[offset:offset + 100])
        for offset in range(0, rows, 100)]
    with tempfile.TemporaryDirectory() as directory:
        client = Client(directory, engine_root=engine_root, extra_args=extra_args)
        try:
            assert client.execute(f'CREATE TABLE docs(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR({dimensions}))') == 'OK\t0'
            start = time.perf_counter()
            for batch, sql in enumerate(batches):
                assert client.execute(sql) == f'OK\t{min(100, rows - batch * 100)}'
            write_seconds = time.perf_counter() - start
            print(json.dumps({'phase': 'loaded', 'write_seconds': write_seconds}), flush=True)
            prepare_start = time.perf_counter()
            assert client.execute('CREATE VECTOR INDEX vectors ON docs(v)') == 'OK\t0'
            for sql in workload[:warmup]:
                client.execute(sql)
            preparation_seconds = time.perf_counter() - prepare_start
            results = []
            start = time.perf_counter()
            for i, sql in enumerate(workload):
                results.append(client.execute(sql))
                if (i + 1) % 100 == 0:
                    print(json.dumps({'phase': 'queries', 'completed': i + 1}), flush=True)
            query_seconds = time.perf_counter() - start
            peak = peak_working_set(client.process)
            for actual, truth in zip(results, expected):
                assert [int(row[0]) for row in actual] == [row[0] for row in truth]
                for row, reference in zip(actual, truth):
                    assert math.isclose(float(row[1]), reference[1], rel_tol=1e-12, abs_tol=1e-12)
            wal_bytes = (Path(directory) / 'database.wal').stat().st_size
            data_files_bytes = sum(path.stat().st_size for path in Path(directory).iterdir() if path.is_file())
            derived_index_bytes = sum(path.stat().st_size for path in Path(directory).glob('flat-*'))
        finally:
            client.close()
        start = time.perf_counter()
        restarted = Client(directory, engine_root=engine_root, extra_args=extra_args)
        recovery_seconds = time.perf_counter() - start
        try:
            restored = restarted.execute('SELECT id,tag,v FROM docs ORDER BY id')
            assert len(restored) == rows
            for actual, (row_id, tag, vector) in zip(restored, data):
                assert int(actual[0]) == row_id and actual[1] == f'g{tag}'
                assert json.loads(actual[2]) == vector
        finally:
            restarted.close()
    return dict(rows=rows, dimensions=dimensions, queries=queries, seed=seed, mode='exact',
                write_seconds=round(write_seconds, 6), query_seconds=round(query_seconds, 6),
                recovery_seconds=round(recovery_seconds, 6), wal_bytes=wal_bytes,
                preparation_seconds=round(preparation_seconds, 6), warmup_queries=warmup,
                extra_args=list(extra_args), workload_version=2,
                windows_peak_working_set_bytes=peak, filtered_recall_at_10=1.0,
                unfiltered_recall_at_10=1.0, restart_content_verified=True,
                source_sha256=source_hashes,
                native_library_sha256=native_libraries, data_files_bytes=data_files_bytes,
                derived_index_bytes=derived_index_bytes,
                caveat='Local smoke benchmark; driver and database may share a container; not official cgroup T/B/M or score')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--dimensions', type=int, default=128)
    parser.add_argument('--queries', type=int, default=1000)
    parser.add_argument('--seed', type=int, default=20261109)
    parser.add_argument('--output')
    parser.add_argument('--engine-root')
    parser.add_argument('--disable-native', action='store_true')
    args = parser.parse_args()
    report = run(args.rows, args.dimensions, args.queries, args.seed, args.engine_root,
                 ('--disable-native',) if args.disable_native else ())
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered, flush=True)
    if args.output:
        Path(args.output).write_text(rendered + '\n', encoding='utf-8')
