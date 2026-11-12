"""Differential checks for the float32 candidate-selection kernel.

The float32 kernel only proposes candidates. These tests compare its answers
with the exact double kernel and with an independent Python reference across
ties, extreme magnitudes, filters, mutations and several dimensions.
"""
import math
import random
import struct
import tempfile
import unittest

from minivectordb.engine import Engine


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


class Float32SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Engine(self.temp.name)
        self.addCleanup(self.db.close)
        if self.db.vector_indexes.kernel is None:
            self.skipTest('native vector kernel not available')
        if self.db.vector_indexes.kernel.search_f32 is None:
            self.skipTest('float32 candidate kernel not available')

    def query(self, sql):
        return list(self.db.execute(sql).rows)

    def check_against_reference(self, sql, truth):
        self.assertEqual(self.query(sql), truth)

    def check_both_kernels(self, sql):
        kernel = self.db.vector_indexes.kernel
        saved = kernel.search_f32
        fast = self.query(sql)
        kernel.search_f32 = None
        try:
            exact = self.query(sql)
        finally:
            kernel.search_f32 = saved
        self.assertEqual(fast, exact)
        return fast

    def build(self, dimensions, rows, tags=3, rng=None):
        rng = rng or random.Random(11)
        self.db.execute(f'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR({dimensions}))')
        values = {}
        for key in range(rows):
            vector = [f32(rng.uniform(-1, 1)) for _ in range(dimensions)]
            values[key] = vector
        # Duplicate a few vectors so ties exercise the primary-key tie-break.
        for key in range(0, rows, 17):
            values[key] = list(values[(key + 1) % rows])
        self.db.execute('INSERT INTO t VALUES ' + ','.join(
            f"({key},'g{key % tags}','{list(vector)}')" for key, vector in values.items()))
        self.db.execute('CREATE VECTOR INDEX vi ON t(v)')
        return values

    def test_random_queries_match_exact_kernel_and_reference(self):
        rng = random.Random(12)
        values = self.build(8, 400, rng=rng)
        for _ in range(60):
            query = [f32(rng.uniform(-1, 1)) for _ in range(8)]
            for where in ('', "WHERE tag='g0'", "WHERE id>=50 AND id<300", "WHERE tag='absent'"):
                for limit in (1, 10, 25):
                    sql = (f"SELECT id,L2_DISTANCE(v,'{query}') AS d FROM t {where} "
                           f'ORDER BY d LIMIT {limit}')
                    rows = self.check_both_kernels(sql)
                    def selected(key):
                        if where == '':
                            return True
                        if where == "WHERE tag='g0'":
                            return key % 3 == 0
                        if where.startswith('WHERE id'):
                            return 50 <= key < 300
                        return False  # WHERE tag='absent'
                    candidates = [(key, math.dist(vector, query))
                                  for key, vector in values.items() if selected(key)]
                    truth = sorted(candidates, key=lambda item: (item[1], item[0]))[:limit]
                    self.assertEqual(rows, truth)

    def test_all_equal_vectors_and_ties(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(3))')
        self.db.execute("INSERT INTO t VALUES " + ','.join(
            f"({key},'g{key % 2}','[1.0,2.0,3.0]')" for key in range(50)))
        self.db.execute('CREATE VECTOR INDEX vi ON t(v)')
        for where in ('', "WHERE tag='g1'"):
            rows = self.check_both_kernels(
                f"SELECT id,L2_DISTANCE(v,'[1.0,2.0,3.0]') AS d FROM t {where} ORDER BY d LIMIT 10")
            expected = [key for key in range(50) if where == '' or key % 2 == 1][:10]
            self.assertEqual([row[0] for row in rows], expected)
            self.assertTrue(all(row[1] == 0.0 for row in rows))

    def test_extreme_magnitudes_and_subnormals(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(4))')
        vectors = {
            0: [1e30, -1e30, 1e30, -1e30],
            1: [3.4e38, 3.4e38, 3.4e38, 3.4e38],
            2: [1e-30, 1e-30, 1e-30, 1e-30],
            3: [1e-45, 1e-45, 1e-45, 1e-45],
            4: [0.0, 0.0, 0.0, 0.0],
            5: [-1e-38, 1e-38, 0.0, 0.0],
        }
        self.db.execute('INSERT INTO t VALUES ' + ','.join(
            f"({key},'{vector}')" for key, vector in vectors.items()))
        self.db.execute('CREATE VECTOR INDEX vi ON t(v)')
        stored = {key: [f32(value) for value in vector] for key, vector in vectors.items()}
        for query in ([0.0, 0.0, 0.0, 0.0], [1e30, 1e30, 1e30, 1e30],
                      [1e-45, 0.0, 0.0, 0.0], [3.4e38, -3.4e38, 0.0, 0.0]):
            narrowed = [f32(value) for value in query]
            rows = self.check_both_kernels(
                f"SELECT id,L2_DISTANCE(v,'{query}') AS d FROM t ORDER BY d LIMIT 6")
            truth = sorted(((key, math.dist(vector, narrowed)) for key, vector in stored.items()),
                           key=lambda item: (item[1], item[0]))
            self.assertEqual(rows, truth)

    def test_dimensions_around_simd_boundaries(self):
        for dimensions in (1, 2, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65, 127, 128, 129):
            with self.subTest(dimensions=dimensions):
                directory = tempfile.TemporaryDirectory()
                self.addCleanup(directory.cleanup)
                db = Engine(directory.name)
                self.addCleanup(db.close)
                if db.vector_indexes.kernel is None or db.vector_indexes.kernel.search_f32 is None:
                    self.skipTest('native float32 kernel not available')
                rng = random.Random(dimensions)
                db.execute(f'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR({dimensions}))')
                values = {key: [f32(rng.uniform(-1, 1)) for _ in range(dimensions)]
                          for key in range(300)}
                db.execute('INSERT INTO t VALUES ' + ','.join(
                    f"({key},'{vector}')" for key, vector in values.items()))
                db.execute('CREATE VECTOR INDEX vi ON t(v)')
                for _ in range(8):
                    query = [f32(rng.uniform(-1, 1)) for _ in range(dimensions)]
                    sql = (f"SELECT id,L2_DISTANCE(v,'{query}') AS d FROM t "
                           'ORDER BY d LIMIT 10')
                    fast = list(db.execute(sql).rows)
                    kernel = db.vector_indexes.kernel
                    saved = kernel.search_f32
                    kernel.search_f32 = None
                    try:
                        exact = list(db.execute(sql).rows)
                    finally:
                        kernel.search_f32 = saved
                    self.assertEqual(fast, exact)
                    truth = sorted(((key, math.dist(vector, query))
                                    for key, vector in values.items()),
                                   key=lambda item: (item[1], item[0]))[:10]
                    self.assertEqual(fast, truth)

    def test_mutations_keep_both_kernels_consistent(self):
        values = self.build(4, 200)
        sql = "SELECT id,L2_DISTANCE(v,'[0.1,0.2,0.3,0.4]') AS d FROM t ORDER BY d LIMIT 12"
        self.check_both_kernels(sql)
        self.db.execute('BEGIN')
        self.db.execute("UPDATE t SET v='[0.1,0.2,0.3,0.4]' WHERE id=77")
        values[77] = [0.1, 0.2, 0.3, 0.4]
        self.assertEqual(self.check_both_kernels(sql)[0][0], 77)
        self.db.execute('DELETE FROM t WHERE id=5')
        del values[5]
        self.check_both_kernels(sql)
        self.db.execute('ROLLBACK')
        self.db.execute("UPDATE t SET tag='moved' WHERE id=9")
        self.check_both_kernels(sql)
        rows = self.check_both_kernels(sql)
        self.assertNotIn(5, [row[0] for row in rows])

    def test_boundary_fallback_still_exact(self):
        # Force the margin test to fail by making the top distances nearly equal.
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
        self.db.execute('INSERT INTO t VALUES ' + ','.join(
            f"({key},'[{1.0 + key * 1e-7},0.0]')" for key in range(100)))
        self.db.execute('CREATE VECTOR INDEX vi ON t(v)')
        rows = self.check_both_kernels(
            "SELECT id,L2_DISTANCE(v,'[1.0,0.0]') AS d FROM t ORDER BY d LIMIT 10")
        truth = sorted(((key, math.dist([f32(1.0 + key * 1e-7), f32(0.0)], [1.0, 0.0]))
                        for key in range(100)), key=lambda item: (item[1], item[0]))[:10]
        self.assertEqual(rows, truth)


if __name__ == '__main__':
    unittest.main()
