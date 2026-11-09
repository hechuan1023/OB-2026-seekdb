"""Run inside a disposable Linux container with /data and a 64 MiB limit.

The driver and database share this cgroup; this is a resource correctness probe,
not the competition benchmark. No large in-memory reference dataset is built.
"""
import json
import time
from pathlib import Path

from minivectordb.engine import Engine


def main():
    directory = Path('/data/resource-probe')
    assert not directory.exists(), 'Use a fresh data volume'
    limit = int(Path('/sys/fs/cgroup/memory.max').read_text())
    assert limit == 64 * 1024 * 1024
    assert Path('/sys/fs/cgroup/memory.swap.max').read_text().strip() == '0'
    count, payload = 20000, 'x' * 8192
    start = time.perf_counter()
    db = Engine(directory, sort_memory=256 * 1024, cache_memory=256 * 1024)
    db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,payload VARCHAR(8192))')
    for first in range(0, count, 16):
        db.execute('INSERT INTO t VALUES ' + ','.join(
            f"({i},'{payload}')" for i in range(first, min(first + 16, count))))
    db.close()
    wal_bytes = (directory / 'database.wal').stat().st_size
    assert wal_bytes > 2 * limit
    print(json.dumps({'phase': 'loaded', 'wal_bytes': wal_bytes}), flush=True)
    db = Engine(directory, sort_memory=256 * 1024, cache_memory=256 * 1024)
    seen = 0
    for row in db.execute('SELECT * FROM t ORDER BY id DESC').rows:
        assert row[0] == count - seen - 1 and row[1] == payload, (seen, row[0])
        seen += 1
    assert seen == count
    assert not list(directory.glob('sort-*.tmp'))
    db.close()
    events = dict(line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())
    assert int(events['oom']) == int(events['oom_kill']) == 0
    print(json.dumps({
        'rows_verified_after_reopen_and_external_sort': seen,
        'wal_bytes': wal_bytes, 'memory_max_bytes': limit,
        'memory_peak_bytes': int(Path('/sys/fs/cgroup/memory.peak').read_text()),
        'memory_events': events, 'swap_max_bytes': 0,
        'elapsed_seconds': round(time.perf_counter() - start, 6),
        'io_stat': Path('/sys/fs/cgroup/io.stat').read_text(),
        'caveat': 'Local Docker Desktop resource correctness probe, not official T/B/M.'
    }, indent=2), flush=True)


if __name__ == '__main__':
    main()
