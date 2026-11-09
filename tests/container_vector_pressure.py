"""64 MiB cgroup probe: vector file > limit, long tx, native query and SIGKILL.

Run in a fresh container with TMPDIR=/data and tests mounted at /app/tests.
The probe and its database child share the limit; this is correctness testing.
"""
import json
import math
import time
from pathlib import Path

from minivectordb.engine import Engine
from minivectordb.errors import DBError
from tests.test_recovery import Client


class PressureClient(Client):
    def line(self):
        return self.lines.get(timeout=120)


def main():
    directory = Path('/data/vector-pressure')
    assert not directory.exists()
    limit = int(Path('/sys/fs/cgroup/memory.max').read_text())
    assert limit == 64 * 1024 * 1024
    assert Path('/sys/fs/cgroup/memory.swap.max').read_text().strip() == '0'
    count, dimensions = 18000, 1024
    zero = json.dumps([0] * dimensions)
    tail = ',0' * (dimensions - 2)
    start = time.perf_counter()
    db = Engine(directory, sort_memory=256 * 1024, cache_memory=0)
    assert db.vector_indexes.kernel is not None
    db.execute(f'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(16),v VECTOR({dimensions}))')
    for first in range(0, count, 16):
        db.execute('INSERT INTO t VALUES ' + ','.join(
            f"({i},'g{i%5}','[{i/32},{i%17}{tail}]')" for i in range(first, min(first+16, count))))
    db.execute('CREATE VECTOR INDEX vi ON t(v)')
    vector_bytes = sum(p.stat().st_size for p in directory.glob('flat-*.vec'))
    assert vector_bytes > limit
    print(json.dumps({'phase': 'indexed', 'vector_bytes': vector_bytes, 'limit': limit}), flush=True)

    def sql(where=''):
        return f"SELECT id,L2_DISTANCE(v,'{zero}') AS d FROM t {where} ORDER BY d LIMIT 10"

    def expected(predicate):
        return sorted(((i, math.hypot(i / 32, i % 17)) for i in range(count) if predicate(i)),
                      key=lambda row: (row[1], row[0]))[:10]

    def verify(rows, truth):
        assert len(rows) == len(truth)
        for row, reference in zip(rows, truth):
            assert int(row[0]) == reference[0]
            assert math.isclose(float(row[1]), reference[1], rel_tol=1e-12, abs_tol=1e-12)

    verify(list(db.execute(sql()).rows), expected(lambda i: True))
    verify(list(db.execute(sql("WHERE tag='g2'")).rows), expected(lambda i: i % 5 == 2))
    for upper in (0, count // 100, count * 4 // 5, count):
        verify(list(db.execute(sql(f'WHERE id<{upper}')).rows), expected(lambda i: i < upper))
    original_snapshot = db.vector_indexes.entries[db.table('t')['id']]
    db.execute('BEGIN')
    assert db.execute("UPDATE t SET tag='changed' WHERE id<1000") == 1000
    stage_bytes = (directory / 'transaction.stage').stat().st_size
    try:
        db.execute(f"INSERT INTO t VALUES ({count},'bad','{zero}'),(0,'dup','{zero}')")
        raise AssertionError('Expected duplicate primary-key error')
    except DBError:
        pass
    verify(list(db.execute(sql("WHERE tag='changed'")).rows), expected(lambda i: i < 1000))
    db.execute('ROLLBACK')
    assert db.vector_indexes.entries[db.table('t')['id']] is original_snapshot
    assert list(db.execute("SELECT id FROM t WHERE tag='changed'").rows) == []
    db.execute('BEGIN')
    db.execute("UPDATE t SET tag='changed' WHERE id<1000")
    db.execute('COMMIT')
    verify(list(db.execute(sql("WHERE tag='changed'")).rows), expected(lambda i: i < 1000))
    db.close()
    print(json.dumps({'phase': 'long_transaction_verified', 'stage_bytes': stage_bytes}), flush=True)

    args = ('--cache-memory-mib', '0', '--sort-memory-mib', '0.25')
    client = PressureClient(directory, extra_args=args)
    try:
        verify(client.execute(sql("WHERE tag='changed'")), expected(lambda i: i < 1000))
        assert client.execute('BEGIN') == 'OK\t0'
        assert client.execute(f"UPDATE t SET v='{zero}' WHERE id=2") == 'OK\t1'
        assert client.execute('DELETE FROM t WHERE id=5') == 'OK\t1'
        assert [int(r[0]) for r in client.execute(sql())][:2] == [0, 2]
    finally:
        client.close(kill=True)
    client = PressureClient(directory, extra_args=args)
    try:
        verify(client.execute(sql()), expected(lambda i: True))
        verify(client.execute(sql("WHERE tag='changed'")), expected(lambda i: i < 1000))
        assert client.execute('SELECT id FROM t WHERE id=5') == [['5']]
    finally:
        client.close()
    events = dict(line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())
    assert int(events['oom']) == int(events['oom_kill']) == 0
    assert not list(directory.glob('sort-*.tmp')) and not list(directory.glob('modify-*.tmp'))
    print(json.dumps({'rows': count, 'dimensions': dimensions, 'vector_bytes': vector_bytes,
                      'wal_bytes': (directory/'database.wal').stat().st_size,
                      'transaction_stage_bytes': stage_bytes,
                      'memory_max_bytes': limit,
                      'memory_peak_bytes': int(Path('/sys/fs/cgroup/memory.peak').read_text()),
                      'memory_events': events, 'elapsed_seconds': time.perf_counter()-start,
                      'checks': ['exact and filtered TopK at 0%, 1%, 20%, 80%, 100% selectivity', 'long transaction partial failure and rollback',
                                 'scalar commit', 'restart', 'SIGKILL after dirty vector snapshot',
                                 'committed state restored and temp cleanup'],
                      'io_stat': Path('/sys/fs/cgroup/io.stat').read_text(),
                      'caveat': 'Local resource correctness probe; probe and DB child both counted; not official score.'}, indent=2))


if __name__ == '__main__':
    main()
