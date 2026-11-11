"""Independent counterexamples: resource behaviour after big transactions.

The reused transaction stage and overlay must not tax later small
transactions: a grown overlay shrinks back at finish, reset never allocates
a map-sized temporary, and crash recovery still verifies every record.
"""
import json
import os
import random
import struct
import tempfile
import time
import unittest
from pathlib import Path

from minivectordb.engine import Engine

BIG = 20_000  # enough to force several overlay growths, fast enough for CI


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


class BigTransactionResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Engine(self.temp.name)
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(4))')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def small_txn_cost(self, count=100, base=1_000_000):
        start = time.perf_counter()
        for i in range(count):
            self.db.execute(f"INSERT INTO t VALUES ({base + i},'g{i % 5}','[{i % 97},0,{i % 13},0]')")
        return (time.perf_counter() - start) / count

    def fresh_small_txn_cost(self):
        """Same small-transaction shape on a fresh engine: the environment's
        baseline cost (fsync, CPU) without any big-transaction history."""
        with tempfile.TemporaryDirectory() as fresh:
            engine = Engine(fresh)
            try:
                engine.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(4))')
                return self._cost_on(engine)
            finally:
                engine.close()

    @staticmethod
    def _cost_on(engine, count=100):
        start = time.perf_counter()
        for i in range(count):
            engine.execute(f"INSERT INTO t VALUES ({900_000 + i},'g{i % 5}','[{i % 97},0,{i % 13},0]')")
        return (time.perf_counter() - start) / count

    def test_big_commit_does_not_tax_small_transactions(self):
        self.db.execute('BEGIN')
        for i in range(BIG):
            self.db.execute(f"INSERT INTO t VALUES ({i},'g{i % 5}','[{i % 97},0,{i % 13},0]')")
        self.db.execute('COMMIT')
        self.assertEqual(self.db.storage.overlay.capacity,
                         self.db.storage.overlay_initial_capacity)
        after = self.small_txn_cost()
        fresh = self.fresh_small_txn_cost()
        # Without the shrink, a grown overlay zeroed MBs per small begin and
        # small transactions measured ~2.5x the fresh baseline.
        self.assertLess(after, fresh * 1.5,
                        f'small transaction costs {after * 1000:.2f} ms after a big '
                        f'commit vs {fresh * 1000:.2f} ms fresh')

    def test_big_rollback_does_not_tax_small_transactions(self):
        self.db.execute('BEGIN')
        for i in range(BIG):
            self.db.execute(f"INSERT INTO t VALUES ({i},'g{i % 5}','[{i % 97},0,{i % 13},0]')")
        self.db.execute('ROLLBACK')
        self.assertEqual(self.db.storage.overlay.capacity,
                         self.db.storage.overlay_initial_capacity)
        after = self.small_txn_cost()
        fresh = self.fresh_small_txn_cost()
        self.assertLess(after, fresh * 1.5,
                        f'small transaction costs {after * 1000:.2f} ms after a big '
                        f'rollback vs {fresh * 1000:.2f} ms fresh')

    def test_reset_never_allocates_map_sized_temporary(self):
        import tracemalloc
        self.db.execute('BEGIN')
        for i in range(BIG):
            self.db.execute(f"INSERT INTO t VALUES ({i},'g0','[0,0,0,0]')")
        capacity = self.db.storage.overlay.capacity
        map_bytes = capacity * 24
        tracemalloc.start()
        self.db.storage.overlay.reset()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, min(256 * 1024, map_bytes),
                        f'reset peaked at {peak} bytes for a {map_bytes}-byte map')

    def test_failed_statement_and_savepoint_recovery_inside_big_txn(self):
        self.db.execute('BEGIN')
        for i in range(5000):
            self.db.execute(f"INSERT INTO t VALUES ({i},'g{i % 5}','[{i % 97},0,{i % 13},0]')")
        with self.assertRaises(Exception):
            self.db.execute("INSERT INTO t VALUES (999999,'g0','[bad')")
        # The savepoint restore keeps the staged rows; commit then verifies.
        self.db.execute('COMMIT')
        rows = list(self.db.execute('SELECT id FROM t ORDER BY id').rows)
        self.assertEqual([r[0] for r in rows], list(range(5000)))
        # Same shape, but roll the whole transaction back instead.
        self.db.execute('BEGIN')
        self.db.execute("UPDATE t SET tag='x' WHERE id < 2500")
        with self.assertRaises(Exception):
            self.db.execute("INSERT INTO t VALUES (999998,'g0','[bad')")
        self.db.execute('ROLLBACK')
        tags = [r[0] for r in self.db.execute("SELECT id FROM t WHERE tag='x' ORDER BY id").rows]
        self.assertEqual(tags, [])

    def test_repeated_update_delete_reinsert_same_key_after_big_txn(self):
        self.db.execute('BEGIN')
        for i in range(BIG):
            self.db.execute(f"INSERT INTO t VALUES ({i},'g{i % 5}','[{i % 97},0,{i % 13},0]')")
        self.db.execute('COMMIT')
        rng = random.Random(20261111)
        for cycle in range(200):
            key = rng.randrange(1000)
            vec = json.dumps([f32(rng.uniform(-1, 1)) for _ in range(4)])
            self.db.execute(f"UPDATE t SET v='{vec}' WHERE id={key}")
            self.db.execute(f'DELETE FROM t WHERE id={key}')
            self.db.execute(f"INSERT INTO t VALUES ({key},'g{cycle % 5}','{vec}')")
        rows = list(self.db.execute('SELECT id,tag,v FROM t ORDER BY id').rows)
        self.assertEqual(len(rows), BIG)
        # Restart and verify the same state survives recovery.
        self.db.close()
        self.db = Engine(self.temp.name)
        again = list(self.db.execute('SELECT id,tag,v FROM t ORDER BY id').rows)
        self.assertEqual(again, rows)

    def test_vector_query_correct_after_big_txn_and_restart(self):
        self.db.execute('BEGIN')
        for i in range(BIG):
            self.db.execute(f"INSERT INTO t VALUES ({i},'g{i % 5}','[{i % 97},0,{i % 13},0]')")
        self.db.execute('COMMIT')
        self.db.execute('CREATE VECTOR INDEX vi ON t(v)')
        query = '[1,0,0,0]'
        before = list(self.db.execute(
            f"SELECT id,L2_DISTANCE(v,'{query}') AS d FROM t WHERE tag='g1' ORDER BY d LIMIT 10").rows)
        self.db.close()
        self.db = Engine(self.temp.name)
        after = list(self.db.execute(
            f"SELECT id,L2_DISTANCE(v,'{query}') AS d FROM t WHERE tag='g1' ORDER BY d LIMIT 10").rows)
        self.assertEqual(after, before)
        self.assertEqual(len(after), 10)


if __name__ == '__main__':
    unittest.main()


class BigTransactionKillRecoveryTests(unittest.TestCase):
    """Kill during recovery after a big transaction, repeatedly, then verify."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def test_repeated_kills_during_recovery_of_big_transaction(self):
        from tests.test_recovery import Client
        root = str(Path(__file__).resolve().parents[1])
        client = Client(self.directory, engine_root=root)
        try:
            client.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(4))')
            client.execute('BEGIN')
            for i in range(5000):
                client.execute(f"INSERT INTO t VALUES ({i},'g{i % 5}','[{i % 97},0,{i % 13},0]')")
            client.execute('COMMIT')
        finally:
            client.close()
        # Kill repeatedly during recovery; each attempt must be repeatable.
        for _ in range(3):
            killed = Client(self.directory, failpoint='during_recovery', ready=False,
                            engine_root=root)
            try:
                self.assertEqual(killed.process.wait(timeout=30), 86)
            finally:
                killed.close()
        restarted = Client(self.directory, engine_root=root)
        try:
            rows = restarted.execute('SELECT id FROM t ORDER BY id LIMIT 3')
            self.assertEqual([int(r[0]) for r in rows], [0, 1, 2])
            count = restarted.execute('SELECT id FROM t ORDER BY id')
            self.assertEqual(len(count), 5000)
            nearest = restarted.execute(
                "SELECT id FROM t WHERE tag='g1' ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 10")
            self.assertEqual(len(nearest), 10)
        finally:
            restarted.close()


if __name__ == '__main__':
    unittest.main()
