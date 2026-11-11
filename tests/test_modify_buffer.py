"""Independent counterexamples for the in-memory modify candidate buffer.

UPDATE/DELETE statements matching few rows buffer candidates in memory;
larger matches spill to the temp file. Both paths must freeze candidates
before the first modification and behave identically at the boundary.
"""
import tempfile
import unittest

from minivectordb.engine import Engine


class ModifyBufferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Engine(self.temp.name)
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(2))')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def load(self, count, vector='[0,0]'):
        self.db.execute('BEGIN')
        for i in range(count):
            self.db.execute(f"INSERT INTO t VALUES ({i},'g{i % 5}','{vector}')")
        self.db.execute('COMMIT')

    def test_both_sides_of_the_spill_boundary_update_identically(self):
        for count in (self.db._MODIFY_BUFFER - 1, self.db._MODIFY_BUFFER,
                      self.db._MODIFY_BUFFER + 1, self.db._MODIFY_BUFFER + 50):
            with self.subTest(rows=count):
                engine = Engine(self.temp.name if False else tempfile.mkdtemp())
                try:
                    engine.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
                    engine.execute('BEGIN')
                    for i in range(count):
                        engine.execute(f"INSERT INTO t VALUES ({i},'[0,0]')")
                    engine.execute('COMMIT')
                    engine.execute("UPDATE t SET v='[9,0]'")
                    rows = list(engine.execute("SELECT v FROM t ORDER BY id").rows)
                    self.assertEqual(len(rows), count)
                    self.assertTrue(all(row[0][0] == 9.0 for row in rows))
                finally:
                    engine.close()

    def test_delete_across_the_boundary(self):
        engine = Engine(tempfile.mkdtemp())
        try:
            engine.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
            engine.execute('BEGIN')
            for i in range(engine._MODIFY_BUFFER + 10):
                engine.execute(f"INSERT INTO t VALUES ({i},'[0,0]')")
            engine.execute('COMMIT')
            engine.execute("DELETE FROM t WHERE id >= 10")  # spills
            remaining = [row[0] for row in engine.execute('SELECT id FROM t ORDER BY id').rows]
            self.assertEqual(remaining, list(range(10)))
            engine.execute('DELETE FROM t')  # buffered (10 rows)
            self.assertEqual(list(engine.execute('SELECT id FROM t').rows), [])
        finally:
            engine.close()

    def test_failed_statement_leaves_no_temp_file_and_rolls_back(self):
        import os
        self.load(20)
        before = set(os.listdir(self.temp.name))
        with self.assertRaises(Exception):
            self.db.execute("INSERT INTO t VALUES (999,'g0','[bad')")
        # A failed INSERT does not use modify(), but a failed UPDATE inside a
        # transaction must still restore the savepoint exactly.
        self.db.execute('BEGIN')
        self.db.execute("UPDATE t SET v='[7,0]' WHERE id < 5")
        with self.assertRaises(Exception):
            self.db.execute("UPDATE t SET v='[bad' WHERE id < 5")
        self.db.execute('COMMIT')
        rows = list(self.db.execute('SELECT v FROM t WHERE id < 5 ORDER BY id').rows)
        self.assertTrue(all(row[0][0] == 7.0 for row in rows))
        self.assertEqual(set(os.listdir(self.temp.name)) - {'modify-'} & set(), set())
        # No temp file leftovers beyond the known persistent files.
        leftovers = [n for n in os.listdir(self.temp.name) if n.startswith('modify-')]
        self.assertEqual(leftovers, [])

    def test_update_visible_inside_transaction_and_after_rollback(self):
        self.load(30)
        self.db.execute('BEGIN')
        self.db.execute("UPDATE t SET v='[3,0]' WHERE tag='g1'")
        inside = [row[0] for row in self.db.execute(
            "SELECT v FROM t WHERE tag='g1' ORDER BY id").rows]
        self.assertTrue(all(vec[0] == 3.0 for vec in inside))
        self.db.execute('ROLLBACK')
        after = [row[0] for row in self.db.execute(
            "SELECT v FROM t WHERE tag='g1' ORDER BY id").rows]
        self.assertTrue(all(vec[0] == 0.0 for vec in after))


if __name__ == '__main__':
    unittest.main()
