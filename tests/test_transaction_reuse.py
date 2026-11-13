"""Independent counterexamples for the reused transaction stage and overlay."""
import os
import tempfile
from pathlib import Path
import unittest

from minivectordb.engine import Engine


class TransactionReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Engine(self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_rollback_then_read_sees_committed_state(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
        self.db.execute("INSERT INTO t VALUES (1,'[1,0]')")
        self.db.execute('BEGIN')
        self.db.execute("INSERT INTO t VALUES (2,'[2,0]')")
        self.db.execute("UPDATE t SET v='[9,0]' WHERE id=1")
        self.db.execute('DELETE FROM t WHERE id=1')
        self.db.execute('ROLLBACK')
        rows = list(self.db.execute('SELECT id FROM t ORDER BY id').rows)
        self.assertEqual(rows, [(1,)])
        # And the overlay must not resurrect row 2 even via a point lookup.
        self.assertEqual(list(self.db.execute('SELECT v FROM t WHERE id=2').rows), [])
        # A subsequent transaction still works on the reused files.
        self.db.execute('BEGIN')
        self.db.execute("INSERT INTO t VALUES (2,'[2,0]')")
        self.db.execute('COMMIT')
        self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM t ORDER BY id').rows],
                         [1, 2])

    def test_failed_statement_inside_transaction_keeps_prior_stage(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
        self.db.execute("INSERT INTO t VALUES (1,'[1,0]')")
        self.db.execute('BEGIN')
        self.db.execute("INSERT INTO t VALUES (2,'[2,0]')")
        with self.assertRaises(Exception):
            self.db.execute("INSERT INTO t VALUES (3,'[bad')")
        # The savepoint restore must keep row 2 visible inside the transaction.
        self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM t ORDER BY id').rows],
                         [1, 2])
        self.db.execute('COMMIT')
        self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM t ORDER BY id').rows],
                         [1, 2])

    def test_sequential_autocommit_transactions_are_isolated(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
        for i in range(5):
            self.db.execute(f"INSERT INTO t VALUES ({i},'[{i},0]')")
            # After each commit the overlay must be empty: a probe for a key
            # from an EARLIER staged transaction must not see stale offsets.
            self.assertIsNone(self.db.storage.overlay.get(1, 99))
        self.assertEqual(len(list(self.db.execute('SELECT id FROM t').rows)), 5)

    def test_stage_handle_survives_between_transactions_for_reuse(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
        self.db.execute("INSERT INTO t VALUES (1,'[1,0]')")
        # The stage handle stays open; small transactions remain in memory.
        self.assertIsNotNone(self.db.storage.stage)
        self.assertTrue(self.db.storage.active is False)
        self.db.execute("INSERT INTO t VALUES (2,'[2,0]')")
        self.assertEqual(len(list(self.db.execute('SELECT id FROM t').rows)), 2)

    def test_large_stage_spills_under_data_directory_and_returns_to_memory(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VARCHAR(256))')
        payload = 'x' * 200
        self.db.execute('BEGIN')
        for i in range(9000):
            self.db.execute(f"INSERT INTO t VALUES ({i},'{payload}{i:08d}')")
        stage = self.db.storage.stage
        self.assertTrue(getattr(stage, '_rolled', False))
        self.assertGreater(stage.tell(), 512 * 1024)
        self.db.execute('COMMIT')
        self.assertFalse(getattr(self.db.storage.stage, '_rolled', True))
        self.assertEqual(self.db.storage.stage.tell(), 0)

    def test_close_releases_and_removes_scratch_files(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
        self.db.execute("INSERT INTO t VALUES (1,'[1,0]')")
        self.db.execute('BEGIN')
        self.db.execute("INSERT INTO t VALUES (2,'[2,0]')")  # leave open on purpose
        self.db.close()
        for name in ('transaction.stage', 'transaction.idx'):
            self.assertFalse((Path(self.temp.name) / name).exists(), name)
        # A fresh engine can reopen the directory (handles were really released).
        engine = Engine(self.temp.name)
        try:
            self.assertEqual([r[0] for r in engine.execute('SELECT id FROM t').rows], [1,])
        finally:
            engine.close()

    def test_crash_leftovers_are_removed_on_startup(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
        self.db.execute("INSERT INTO t VALUES (1,'[1,0]')")
        self.db.close()
        # Simulate a crash: write junk scratch files after the clean close.
        with open(os.path.join(self.temp.name, 'transaction.stage'), 'wb') as handle:
            handle.write(b'junk from a crashed run')
        with open(os.path.join(self.temp.name, 'transaction.idx'), 'wb') as handle:
            handle.write(b'junk')
        engine = Engine(self.temp.name)
        try:
            self.assertEqual([r[0] for r in engine.execute('SELECT id FROM t').rows], [1,])
        finally:
            engine.close()


if __name__ == '__main__':
    unittest.main()
