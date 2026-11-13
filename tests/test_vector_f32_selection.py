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
from array import array
from types import SimpleNamespace

from minivectordb.engine import Engine
from minivectordb.native import distances as native_distances


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
        self.assert_rows(self.query(sql), truth)

    def assert_rows(self, rows, truth):
        """IDs and order must match exactly; distances use a tight tolerance.

        The native path evaluates distances with an ordered double loop, which
        differs from math.dist only in the last bits (about 1e-15 relative,
        far inside the official 1e-5 tolerance).
        """
        self.assertEqual([row[0] for row in rows], [row[0] for row in truth])
        for row, expected in zip(rows, truth):
            self.assertTrue(math.isclose(row[1], expected[1], rel_tol=1e-12, abs_tol=1e-12),
                            f'{row} != {expected}')

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
                    self.assert_rows(rows, truth)

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
            self.assert_rows(rows, truth)

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
                    self.assert_rows(fast, truth)

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
        self.assert_rows(rows, truth)

    def test_combined_selection_returns_separate_exact_distances(self):
        values = self.build(8, 400)
        snapshot = self.db.vector_indexes.ensure(self.db.table('t'))
        kernel = self.db.vector_indexes.kernel
        if kernel.search_f32_exact is None:
            self.skipTest('combined native kernel not available')
        query = array('f', [0.25, -0.5, 0.75, 0.125, -0.25, 0.5, -0.75, 0.0])
        selected = self.db.vector_indexes.selection(
            self.db.table('t'), snapshot, [(1, '=', 'g0')], self.db.matches)
        positions, selection_distances, combined_exact = kernel.topk_f32_exact(
            snapshot, query, selected, 12)
        old = kernel.topk_f32(snapshot, query, selected, 12)
        self.assertEqual(list(positions), [position for position, _ in old])
        self.assertEqual(list(selection_distances), [distance for _, distance in old])
        separate_exact = native_distances(
            kernel.address(snapshot.vectors), query, positions)
        self.assertEqual(list(combined_exact), list(separate_exact))
        expected = sorted(
            ((math.dist(values[key], query), key) for key in range(0, 400, 3)),
            key=lambda item: (item[0], item[1]))[:12]
        actual_keys = [
            key for _, key in sorted(
                ((distance, snapshot.key(position))
                 for position, distance in zip(positions, combined_exact)),
                key=lambda item: (item[0], item[1]))
        ]
        self.assertEqual(actual_keys, [key for _, key in expected])

    def test_openmp_exact_kernel_matches_single_thread(self):
        kernel = self.db.vector_indexes.kernel
        if kernel.search_f32_exact_omp is None:
            self.skipTest('OpenMP exact kernel not available')

        def run(snapshot, query, keep, threads):
            positions = array('Q', [0]) * keep
            distances = array('d', [0.0]) * keep
            exact = array('d', [0.0]) * keep
            if threads is None:
                returned = kernel.search_f32_exact(
                    kernel.address(snapshot.vectors),
                    kernel.address(snapshot.keys),
                    snapshot.count,
                    snapshot.dimensions,
                    query.buffer_info()[0],
                    None,
                    snapshot.count,
                    keep,
                    positions.buffer_info()[0],
                    distances.buffer_info()[0],
                    exact.buffer_info()[0],
                )
            else:
                returned = kernel.search_f32_exact_omp(
                    kernel.address(snapshot.vectors),
                    kernel.address(snapshot.keys),
                    snapshot.count,
                    snapshot.dimensions,
                    query.buffer_info()[0],
                    None,
                    snapshot.count,
                    keep,
                    threads,
                    positions.buffer_info()[0],
                    distances.buffer_info()[0],
                    exact.buffer_info()[0],
                )
            self.assertEqual(returned, keep)
            return positions.tolist(), distances.tolist(), exact.tolist()

        rng = random.Random(20261113)
        for dimensions in (7, 128):
            with self.subTest(dimensions=dimensions):
                rows = 4097
                vectors = array(
                    'f',
                    (f32(rng.uniform(-2.0, 2.0))
                     for _ in range(rows * dimensions)),
                )
                snapshot = SimpleNamespace(
                    vectors=vectors,
                    keys=array('q', range(rows)),
                    count=rows,
                    dimensions=dimensions,
                )
                for keep in (1, 10, 11):
                    with self.subTest(keep=keep):
                        query = array(
                            'f',
                            (f32(rng.uniform(-2.0, 2.0))
                             for _ in range(dimensions)),
                        )
                        self.assertEqual(
                            run(snapshot, query, keep, 2),
                            run(snapshot, query, keep, None),
                        )

    def test_bounded_partial_scan_matches_exact_at_128_dimensions(self):
        """Exercise the partial-sum cutoff against the unbounded exact kernel."""
        kernel = self.db.vector_indexes.kernel
        if kernel.search_f32_exact is None:
            self.skipTest('combined native kernel not available')
        rows, dimensions, keep = 10_000, 128, 11
        rng = random.Random(20261116)
        snapshot = SimpleNamespace(
            vectors=array(
                'f',
                (f32(rng.uniform(-1.0, 1.0))
                 for _ in range(rows * dimensions)),
            ),
            keys=array('q', range(rows)),
            count=rows,
            dimensions=dimensions,
        )
        selected = array('Q', range(0, rows, 5))
        for index in range(16):
            query = array(
                'f',
                (f32(rng.uniform(-1.0, 1.0)) for _ in range(dimensions)),
            )
            for selection in (None, selected):
                bounded = kernel.topk_f32_exact(
                    snapshot, query, selection, keep)
                exact = kernel.topk(snapshot, query, selection, keep)
                self.assertEqual(list(bounded[0]), [position for position, _ in exact])
                for actual, (_, squared) in zip(bounded[2], exact):
                    self.assertTrue(math.isclose(
                        actual, math.sqrt(squared),
                        rel_tol=1e-12, abs_tol=1e-12,
                    ))

    def test_openmp_exact_kernel_preserves_ties_and_fallbacks(self):
        kernel = self.db.vector_indexes.kernel
        if kernel.search_f32_exact_omp is None:
            self.skipTest('OpenMP exact kernel not available')

        def run(snapshot, query, keep, threads):
            positions = array('Q', [0]) * keep
            distances = array('d', [0.0]) * keep
            exact = array('d', [0.0]) * keep
            returned = kernel.search_f32_exact_omp(
                kernel.address(snapshot.vectors),
                kernel.address(snapshot.keys),
                snapshot.count,
                snapshot.dimensions,
                query.buffer_info()[0],
                None,
                snapshot.count,
                keep,
                threads,
                positions.buffer_info()[0],
                distances.buffer_info()[0],
                exact.buffer_info()[0],
            )
            self.assertEqual(returned, keep)
            return positions.tolist(), distances.tolist(), exact.tolist()

        rows = 4097
        dimensions = 3
        snapshot = SimpleNamespace(
            vectors=array('f', [1.0, 2.0, 3.0]) * rows,
            keys=array('q', range(rows - 1, -1, -1)),
            count=rows,
            dimensions=dimensions,
        )
        query = array('f', [1.0, 2.0, 3.0])
        positions, distances, exact = run(snapshot, query, 10, 2)
        self.assertEqual(
            [snapshot.keys[position] for position in positions],
            list(range(10)),
        )
        self.assertEqual(distances, [0.0] * 10)
        self.assertEqual(exact, [0.0] * 10)

        small = SimpleNamespace(
            vectors=snapshot.vectors[:dimensions * 32],
            keys=array('q', range(32)),
            count=32,
            dimensions=dimensions,
        )
        self.assertEqual(
            run(small, query, 10, 2),
            run(small, query, 10, 1),
        )

    def test_openmp_route_uses_threads_for_large_filtered_selection(self):
        kernel = self.db.vector_indexes.kernel
        if kernel.search_f32_exact_omp is None:
            self.skipTest('OpenMP exact kernel not available')
        rows, dimensions = 4097, 7
        rng = random.Random(20261114)
        snapshot = SimpleNamespace(
            vectors=array(
                'f',
                (f32(rng.uniform(-1.0, 1.0))
                 for _ in range(rows * dimensions)),
            ),
            keys=array('q', range(rows)),
            count=rows,
            dimensions=dimensions,
        )
        query = array(
            'f',
            (f32(rng.uniform(-1.0, 1.0)) for _ in range(dimensions)),
        )
        selected = array('Q', range(0, rows, 3))
        old_threads = kernel.search_threads
        try:
            kernel.search_threads = 2
            threaded = kernel.topk_f32_exact(snapshot, query, None, 11)
            filtered = kernel.topk_f32_exact(snapshot, query, selected, 11)
            small = array('Q', range(0, 512))
            small_filtered = kernel.topk_f32_exact(snapshot, query, small, 11)
            kernel.search_threads = 1
            sequential = kernel.topk_f32_exact(snapshot, query, None, 11)
            filtered_sequential = kernel.topk_f32_exact(
                snapshot, query, selected, 11)
            small_sequential = kernel.topk_f32_exact(
                snapshot, query, small, 11)
        finally:
            kernel.search_threads = old_threads
        self.assertEqual(threaded, sequential)
        self.assertEqual(filtered, filtered_sequential)
        self.assertEqual(small_filtered, small_sequential)


if __name__ == '__main__':
    unittest.main()
