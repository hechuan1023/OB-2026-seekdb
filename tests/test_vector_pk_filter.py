"""Independent truth checks for primary-key filtered vector searches."""
import math
import tempfile
import unittest

from minivectordb.engine import Engine


class VectorPrimaryKeyFilterTests(unittest.TestCase):
    def test_sparse_ranges_delete_rollback_and_vector_update(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = Engine(directory.name)
        self.addCleanup(db.close)
        if db.vector_indexes.kernel is None:
            self.skipTest('native vector kernel not available')
        db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(2))')
        values = {-9: (0, 1), -2: (1, 0), 0: (0, -1), 7: (-1, 0), 31: (3, 4)}
        for key, (x, y) in values.items():
            db.execute(f"INSERT INTO t VALUES ({key},'a','[{x},{y}]')")
        db.execute('CREATE VECTOR INDEX vi ON t(v)')

        def check(where, expected):
            rows = list(db.execute("SELECT id,L2_DISTANCE(v,'[0,0]') AS d "
                                   f'FROM t WHERE {where} ORDER BY d LIMIT 10').rows)
            truth = sorted(((key, math.hypot(*values[key])) for key in expected),
                           key=lambda row: (row[1], row[0]))
            self.assertEqual(rows, truth)

        check('id >= -9 AND id < 31', [-9, -2, 0, 7])
        check('id > 0 AND id <= 31', [7, 31])
        check('id < -100', [])
        check("id >= -9 AND tag = 'a'", list(values))  # mixed predicate fallback
        db.execute('BEGIN')
        db.execute('DELETE FROM t WHERE id = -2')
        check('id >= -9 AND id < 31', [-9, 0, 7])
        db.execute('ROLLBACK')
        check('id >= -9 AND id < 31', [-9, -2, 0, 7])
        db.execute("UPDATE t SET v='[0,0]' WHERE id=7")
        values[7] = (0, 0)
        check('id >= -9 AND id < 31', [-9, -2, 0, 7])

    def test_large_selection_falls_back_with_small_filter_budget(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = Engine(directory.name, sort_memory=4096)
        self.addCleanup(db.close)
        if db.vector_indexes.kernel is None:
            self.skipTest('native vector kernel not available')
        db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(1))')
        db.execute('INSERT INTO t VALUES ' + ','.join(
            f"({i},'[{i}]')" for i in range(400)))
        db.execute('CREATE VECTOR INDEX vi ON t(v)')
        rows = list(db.execute("SELECT id,L2_DISTANCE(v,'[200]') AS d "
                               'FROM t WHERE id >= 0 ORDER BY d LIMIT 3').rows)
        self.assertEqual(rows, [(200, 0.0), (199, 1.0), (201, 1.0)])


if __name__ == '__main__':
    unittest.main()
