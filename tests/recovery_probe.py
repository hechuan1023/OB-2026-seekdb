"""Direct recovery timing probe: build 10k rows once, reopen the Engine 3 times."""
import json
import random
import struct
import sys
import tempfile
import time


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def main():
    from minivectordb.engine import Engine
    directory = tempfile.mkdtemp()
    db = Engine(directory)
    rng = random.Random(1)
    dims, rows = 128, 10000
    db.execute(f'CREATE TABLE docs(id BIGINT PRIMARY KEY,v VECTOR({dims}))')
    for offset in range(0, rows, 100):
        batch = ','.join(f"({i},'{json.dumps([f32(rng.uniform(-1, 1)) for _ in range(dims)])}')"
                         for i in range(offset, offset + 100))
        db.execute(f'INSERT INTO docs VALUES {batch}')
    db.close()
    times = []
    for _ in range(3):
        start = time.perf_counter()
        db = Engine(directory)
        elapsed = time.perf_counter() - start
        db.close()
        times.append(round(elapsed, 4))
    print(json.dumps({'recovery_seconds': times}))


if __name__ == '__main__':
    main()
