"""剖析写入路径热点：进程内 Engine + cProfile。"""
import cProfile
import io
import json
import pstats
import random
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from minivectordb.engine import Engine


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def main():
    rows, dimensions, seed = 10_000, 128, 20261109
    rng = random.Random(seed)
    data = [(i, i % 5, [f32(rng.uniform(-1, 1)) for _ in range(dimensions)]) for i in range(rows)]
    batches = ['INSERT INTO docs VALUES ' + ','.join(
        f"({i},'g{t}','{json.dumps(v)}')" for i, t, v in data[offset:offset + 100])
        for offset in range(0, rows, 100)]

    with tempfile.TemporaryDirectory() as directory:
        engine = Engine(directory)
        engine.execute('CREATE TABLE docs(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(128))')
        profiler = cProfile.Profile()
        profiler.enable()
        for sql in batches:
            engine.execute(sql)
        profiler.disable()
        engine.close()

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.strip_dirs().sort_stats('tottime').print_stats(20)
    print(stream.getvalue())


if __name__ == '__main__':
    main()
