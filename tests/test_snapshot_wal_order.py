"""Cross-check sequential snapshot input against visible database rows."""
import tempfile
import unittest
import random
from unittest.mock import patch

from minivectordb.engine import Engine


class SnapshotWalOrderTests(unittest.TestCase):
    def test_many_obsolete_versions_and_multitable_fallback(self):
        rng = random.Random(20261112)
        with tempfile.TemporaryDirectory() as directory:
            db = Engine(directory)
            try:
                db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(2))')
                live = {}
                for step in range(100):
                    key = rng.randrange(-8, 9)
                    if key in live:
                        if step % 3 == 0:
                            db.execute(f'DELETE FROM t WHERE id={key}')
                            del live[key]
                        else:
                            db.execute(f"UPDATE t SET tag='u',v='[{step},0]' WHERE id={key}")
                            live[key] = ('u', [float(step), 0.0])
                    else:
                        db.execute(f"INSERT INTO t VALUES ({key},'n','[{step},0]')")
                        live[key] = ('n', [float(step), 0.0])
                table = db.table('t')['id']
                expected = sorted((key, tag, vector) for key, (tag, vector) in live.items())

                def snapshot():
                    return sorted((r[0], r[1], list(r[2])) for r in db.storage.snapshot_rows(table))

                self.assertEqual(snapshot(), expected)
                db.execute('CREATE TABLE other(id BIGINT PRIMARY KEY,v VECTOR(1))')
                db.execute("INSERT INTO other VALUES (1,'[8]')")
                self.assertEqual(snapshot(), expected)
            finally:
                db.close()

    def test_overwrites_deletes_transaction_rollback_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Engine(directory)
            try:
                db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(2))')
                db.execute("INSERT INTO t VALUES (1,'a','[1,0]'),(2,'b','[0,1]'),(3,'c','[3,4]')")
                db.execute("UPDATE t SET tag='new',v='[2,0]' WHERE id=1")
                db.execute('DELETE FROM t WHERE id=2')
                table = db.table('t')['id']

                def check(expected):
                    rows = list(db.storage.snapshot_rows(table))
                    actual = sorted((row[0], row[1], list(row[2])) for row in rows)
                    self.assertEqual(actual, expected)

                expected = [(1, 'new', [2.0, 0.0]), (3, 'c', [3.0, 4.0])]
                check(expected)
                db.execute('BEGIN')
                db.execute("UPDATE t SET tag='tx' WHERE id=1")
                db.execute("INSERT INTO t VALUES (4,'tx','[4,0]')")
                check([(1, 'tx', [2.0, 0.0]), (3, 'c', [3.0, 4.0]),
                       (4, 'tx', [4.0, 0.0])])
                db.execute('ROLLBACK')
                check(expected)
                db.storage.checkpoint()
                check(expected)
            finally:
                db.close()
            reopened = Engine(directory)
            try:
                rows = list(reopened.storage.snapshot_rows(reopened.table('t')['id']))
                self.assertEqual(sorted((r[0], r[1], list(r[2])) for r in rows), expected)
            finally:
                reopened.close()

    def test_file_snapshot_is_correct_during_mutations_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('minivectordb.vector_index.MEMORY_SNAPSHOT_LIMIT', 16):
            db = Engine(directory)
            try:
                if db.vector_indexes.kernel is None:
                    self.skipTest('native vector kernel not available')
                db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
                db.execute("INSERT INTO t VALUES (9,'[1,0]'),(1,'[0,0]'),(5,'[2,0]')")
                db.execute('CREATE VECTOR INDEX vi ON t(v)')

                def ids():
                    return [r[0] for r in db.execute("SELECT id,L2_DISTANCE(v,'[0,0]') AS d "
                                                     'FROM t ORDER BY d LIMIT 3').rows]

                self.assertEqual(ids(), [1, 9, 5])
                db.execute('BEGIN')
                db.execute('DELETE FROM t WHERE id=1')
                db.execute("INSERT INTO t VALUES (2,'[0,0]')")
                self.assertEqual(ids(), [2, 9, 5])
                db.execute('ROLLBACK')
                self.assertEqual(ids(), [1, 9, 5])
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
