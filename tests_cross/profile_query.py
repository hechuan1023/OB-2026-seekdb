"""剖析查询路径热点：进程内 Engine + cProfile，负载形状与 tests/benchmark.py 相同。"""
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
    rows, dimensions, queries, seed = 10_000, 128, 400, 20261109
    rng = random.Random(seed)
    data = [(i, i % 5, [f32(rng.uniform(-1, 1)) for _ in range(dimensions)]) for i in range(rows)]
    workload = []
    for i in range(queries):
        query = [f32(rng.uniform(-1, 1)) for _ in range(dimensions)]
        tag = i % 5 if i % 2 else None
        where = f" WHERE tag='g{tag}'" if tag is not None else ''
        workload.append(f"SELECT id,L2_DISTANCE(v,'{json.dumps(query)}') AS distance FROM docs{where} ORDER BY distance LIMIT 10")

    with tempfile.TemporaryDirectory() as directory:
        engine = Engine(directory)
        engine.execute('CREATE TABLE docs(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(128))')
        for offset in range(0, rows, 100):
            batch = data[offset:offset + 100]
            sql = 'INSERT INTO docs VALUES ' + ','.join(
                f"({i},'g{t}','{json.dumps(v)}')" for i, t, v in batch)
            engine.execute(sql)
        engine.execute('CREATE VECTOR INDEX vectors ON docs(v)')
        for sql in workload[:4]:
            engine.execute(sql)

        profiler = cProfile.Profile()
        profiler.enable()
        for sql in workload:
            result = engine.execute(sql)
            for _ in result.rows:  # 消费生成器，计入 TopK 实际执行
                pass
        profiler.disable()
        engine.close()

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.strip_dirs().sort_stats('cumulative').print_stats(28)
    text = stream.getvalue()
    print(text)
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.strip_dirs().sort_stats('tottime').print_stats(20)
    print(stream.getvalue())


if __name__ == '__main__':
    main()
