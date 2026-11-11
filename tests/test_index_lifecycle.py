import copy
import json
import math
import random
import tempfile
import unittest

from minivectordb.engine import Engine
from minivectordb.errors import DBError


class IndexLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Engine(self.temp.name)
        if self.db.vector_indexes.kernel is None:
            self.db.close()
            self.temp.cleanup()
            self.skipTest('Native library required for snapshot lifecycle assertions')
        for table in ('a', 'b'):
            self.db.execute(f'CREATE TABLE {table}(id BIGINT PRIMARY KEY,tag VARCHAR(16),content VARCHAR(16),v VECTOR(2))')
            self.db.execute(f"INSERT INTO {table} VALUES (1,'yes','old','[1,0]'),(2,'no','old','[2,0]'),(3,'yes','old','[3,0]')")
            self.db.execute(f'CREATE VECTOR INDEX idx_{table} ON {table}(v)')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def query(self, table='a', where=''):
        return list(self.db.execute(f"SELECT id,tag,content,L2_DISTANCE(v,'[0,0]') AS d FROM {table} {where} ORDER BY d LIMIT 10").rows)

    def snapshot(self, table='a'):
        return self.db.vector_indexes.entries[self.db.table(table)['id']]

    def test_read_only_and_zero_match_preserve_both_tables(self):
        a, b = self.snapshot(), self.snapshot('b')
        for end in ('COMMIT', 'ROLLBACK'):
            self.db.execute('BEGIN')
            self.query()
            self.db.execute(end)
            self.assertIs(self.snapshot(), a)
            self.assertIs(self.snapshot('b'), b)
        for statement in ("UPDATE a SET v='[9,9]' WHERE id=99", 'DELETE FROM a WHERE id=99',
                          "UPDATE a SET v='[1,0]',content='old' WHERE id=1"):
            self.db.execute(statement)
            self.assertIs(self.snapshot(), a)
            self.assertEqual(self.query()[0], (1, 'yes', 'old', 1.0))

    def test_scalar_projection_and_only_dependent_filters_change(self):
        self.query(where="WHERE tag='yes'")
        self.query(where="WHERE content='old'")
        original = self.snapshot()
        filters = self.db.vector_indexes.filters
        tag_key = next(key for key in filters if key[1][0][0] == 1)
        content_key = next(key for key in filters if key[1][0][0] == 2)
        tag_positions = filters[tag_key][0]
        self.db.execute("UPDATE a SET content='new' WHERE id=1")
        self.assertIs(self.snapshot(), original)
        self.assertIs(filters[tag_key][0], tag_positions)
        self.assertNotIn(content_key, filters)
        self.assertEqual(self.query(where="WHERE content='new'"), [(1, 'yes', 'new', 1.0)])
        self.assertEqual([row[0] for row in self.query(where="WHERE content='old'")], [2, 3])

    def test_scalar_rollback_invalidates_transaction_filters_only(self):
        a, b = self.snapshot(), self.snapshot('b')
        self.db.execute('BEGIN')
        self.db.execute("UPDATE a SET tag='yes' WHERE id=2")
        self.assertEqual(len(self.query(where="WHERE tag='yes'")), 3)
        self.db.execute('ROLLBACK')
        self.assertIs(self.snapshot(), a)
        self.assertIs(self.snapshot('b'), b)
        self.assertEqual([r[0] for r in self.query(where="WHERE tag='yes'")], [1, 3])

    def test_partial_failed_statement_preserves_current_snapshot(self):
        for explicit in (False, True):
            if explicit:
                self.db.execute('BEGIN')
                self.db.execute("UPDATE a SET tag='yes' WHERE id=2")
            before = self.query(where="WHERE tag='yes'")
            snapshot = self.snapshot()
            filters = dict(self.db.vector_indexes.filters)
            with self.assertRaises(DBError):
                self.db.execute("INSERT INTO a VALUES (4,'yes','bad','[0,0]'),(1,'dup','bad','[0,0]')")
            self.assertIs(self.snapshot(), snapshot)
            self.assertEqual(dict(self.db.vector_indexes.filters), filters)
            self.assertEqual(self.query(where="WHERE tag='yes'"), before)
            if explicit:
                self.db.execute('COMMIT')
                self.assertIs(self.snapshot(), snapshot)
                self.assertEqual(len(self.query(where="WHERE tag='yes'")), 3)

    def test_commit_retains_current_vector_snapshot_but_rollback_discards_it(self):
        b = self.snapshot('b')
        self.db.execute('BEGIN')
        self.db.execute("UPDATE a SET v='[0,0]' WHERE id=3")
        self.assertEqual(self.query()[0][0], 3)
        transaction_snapshot = self.snapshot()
        self.db.execute('COMMIT')
        self.assertIs(self.snapshot(), transaction_snapshot)
        self.assertIs(self.snapshot('b'), b)
        self.db.execute('BEGIN')
        self.db.execute('DELETE FROM a WHERE id=3')
        self.assertEqual(self.query()[0][0], 1)
        self.db.execute('ROLLBACK')
        # The incremental delete keeps the snapshot and removes its tombstone.
        self.assertIn(self.db.table('a')['id'], self.db.vector_indexes.entries)
        self.assertNotIn(3, self.snapshot().deleted)
        self.assertEqual(self.query()[0][0], 3)
        self.assertIs(self.snapshot('b'), b)

    def test_incremental_delete_tombstone_behaviour(self):
        # Autocommit delete: tombstone only, no rebuild, query stays correct.
        a = self.snapshot()
        self.db.execute('DELETE FROM a WHERE id=1')
        self.assertIs(self.snapshot(), a)
        self.assertIn(1, a.deleted)
        self.assertEqual([r[0] for r in self.query()], [2, 3])
        self.assertEqual([r[0] for r in self.query(where="WHERE tag='yes'")], [3])

    def test_rollback_delete_restores_rows_and_filters(self):
        # Filters cached before the delete hold positions that must not
        # survive it; after rollback the rows must be queryable again.
        self.query(where="WHERE tag='yes'")
        self.query(where="WHERE tag='no'")
        a = self.snapshot()
        self.db.execute('BEGIN')
        self.db.execute('DELETE FROM a WHERE id=1')
        self.assertEqual([r[0] for r in self.query(where="WHERE tag='yes'")], [3])
        self.db.execute('ROLLBACK')
        self.assertIs(self.snapshot(), a)
        self.assertNotIn(1, a.deleted)
        self.assertEqual([r[0] for r in self.query(where="WHERE tag='yes'")], [1, 3])
        self.assertEqual([r[0] for r in self.query(where="WHERE tag='no'")], [2])

    def test_transaction_filters_created_after_delete_go_stale_on_rollback(self):
        # A filter cached between the delete and the rollback excludes the
        # deleted row; it must be dropped when the row returns.
        self.db.execute('BEGIN')
        self.db.execute('DELETE FROM a WHERE id=1')
        self.assertEqual([r[0] for r in self.query(where="WHERE tag='yes'")], [3])
        self.db.execute('ROLLBACK')
        self.assertEqual([r[0] for r in self.query(where="WHERE tag='yes'")], [1, 3])

    def test_tombstone_threshold_rebuilds_snapshot(self):
        # Tombstones outnumbering live rows force a rebuild (the entry is
        # dropped and rebuilt lazily), and a rollback after that rebuild must
        # invalidate rather than un-mark tombstones on a stale snapshot.
        for i in range(4, 9):
            self.db.execute(f"INSERT INTO a VALUES ({i},'no','old','[{i},0]')")
        self.query()  # INSERTs invalidated the snapshot; rebuild it first.
        a = self.snapshot()
        self.db.execute('BEGIN')
        self.db.execute('DELETE FROM a WHERE id > 2')  # 6 of 8 rows: rebuild.
        self.assertNotIn(self.db.table('a')['id'], self.db.vector_indexes.entries)
        self.db.execute('ROLLBACK')
        self.assertEqual([r[0] for r in self.query()], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertIsNot(self.snapshot(), a)

    def test_delete_then_failed_statement_keeps_tombstone(self):
        # A failed statement after the delete must not resurrect the row.
        self.db.execute('BEGIN')
        self.db.execute('DELETE FROM a WHERE id=1')
        with self.assertRaises(DBError):
            self.db.execute("UPDATE a SET tag='x', v='[bad' WHERE id=2")
        self.assertEqual([r[0] for r in self.query()], [2, 3])
        self.db.execute('ROLLBACK')
        self.assertEqual([r[0] for r in self.query()], [1, 2, 3])

    def test_rollback_created_index_and_reused_table_identifier(self):
        self.db.execute('BEGIN')
        self.db.execute('CREATE TABLE c(id BIGINT PRIMARY KEY,v VECTOR(2))')
        self.db.execute("INSERT INTO c VALUES (1,'[0,0]')")
        self.db.execute('CREATE VECTOR INDEX ic ON c(v)')
        identifier = self.db.table('c')['id']
        self.assertIn(identifier, self.db.vector_indexes.entries)
        self.db.execute('ROLLBACK')
        self.assertNotIn(identifier, self.db.vector_indexes.entries)
        self.db.execute('CREATE TABLE c(id BIGINT PRIMARY KEY,v VECTOR(2))')
        self.db.execute("INSERT INTO c VALUES (2,'[2,0]')")
        self.db.execute('CREATE VECTOR INDEX ic ON c(v)')
        self.assertEqual(list(self.db.execute("SELECT id FROM c ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 1").rows), [(2,)])

    def test_checkpoint_and_restart_preserve_current_values(self):
        self.db.storage.checkpoint_bytes = 4096
        self.db.execute('BEGIN')
        for i in range(200):
            self.db.execute(f"UPDATE a SET content='v{i}',tag='yes' WHERE id=2")
        before = self.query(where="WHERE tag='yes'")
        snapshot = self.snapshot()
        self.db.execute('COMMIT')
        self.assertIs(self.snapshot(), snapshot)
        self.assertEqual(self.query(where="WHERE tag='yes'"), before)
        self.db.close()
        self.db = Engine(self.temp.name)
        self.assertEqual(self.query(where="WHERE tag='yes'"), before)


class RandomStateMachineTests(unittest.TestCase):
    def test_seeded_vectors_filters_transactions_and_restarts(self):
        for seed in (1701, 1702, 1703, 1704):
            rng = random.Random(seed)
            with tempfile.TemporaryDirectory() as directory:
                db = Engine(directory, checkpoint_bytes=4096)
                model, saved, history = {}, None, []
                db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(2))')
                db.execute('CREATE VECTOR INDEX vi ON t(v)')
                try:
                    for step in range(180):
                        operation = rng.choice(('insert', 'update', 'delete', 'failed', 'begin', 'end', 'restart'))
                        key = rng.randrange(25)
                        tag = f'g{rng.randrange(3)}'
                        vector = [rng.randrange(-5, 6), rng.randrange(-5, 6)]
                        if operation == 'begin' and saved is None:
                            sql = 'BEGIN'
                            saved = copy.deepcopy(model)
                        elif operation == 'end' and saved is not None:
                            sql = rng.choice(('COMMIT', 'ROLLBACK'))
                            if sql == 'ROLLBACK':
                                model = saved
                            saved = None
                        elif operation == 'restart':
                            history.append('EOF and restart')
                            db.close()
                            db = Engine(directory, checkpoint_bytes=4096)
                            if saved is not None:
                                model, saved = saved, None
                            sql = None
                        elif operation == 'insert' and key not in model:
                            sql = f"INSERT INTO t VALUES ({key},'{tag}','{vector}')"
                            model[key] = (tag, vector)
                        elif operation == 'update':
                            scalar_only = bool(rng.randrange(2))
                            assign = f"tag='{tag}'" + ('' if scalar_only else f",v='{vector}'")
                            sql = f'UPDATE t SET {assign} WHERE id={key}'
                            if key in model:
                                model[key] = (tag, model[key][1] if scalar_only else vector)
                        elif operation == 'delete':
                            sql = f'DELETE FROM t WHERE id={key}'
                            model.pop(key, None)
                        elif operation == 'failed':
                            sql = "INSERT INTO t VALUES (100,'g1','[0,0]'),(101,'g2','[1]')"
                            history.append(sql)
                            with self.assertRaises(DBError):
                                db.execute(sql)
                            sql = None
                        else:
                            sql = 'SELECT id FROM t WHERE id=-1'
                        if sql is not None:
                            history.append(sql)
                            value = db.execute(sql)
                            if not isinstance(value, int):
                                list(value.rows)
                        query = [rng.randrange(-3, 4), rng.randrange(-3, 4)]
                        filter_tag = f'g{rng.randrange(3)}' if step % 2 else None
                        where = '' if filter_tag is None else f" WHERE tag='{filter_tag}'"
                        expected = sorted(((k, math.dist(v, query)) for k, (t, v) in model.items()
                                           if filter_tag is None or filter_tag == t), key=lambda pair: (pair[1], pair[0]))[:7]
                        actual = list(db.execute(f"SELECT id,L2_DISTANCE(v,'{query}') AS d FROM t{where} ORDER BY d LIMIT 7").rows)
                        self.assertEqual(actual, expected)
                        if step % 9 == 0:
                            rows = list(db.execute('SELECT id,tag,v FROM t ORDER BY id').rows)
                            self.assertEqual([(r[0], r[1], list(r[2])) for r in rows],
                                             [(k, *model[k]) for k in sorted(model)])
                except Exception as exc:
                    self.fail(f'seed={seed}, step={step}, history={json.dumps(history)}; {exc}')
                finally:
                    db.close()


if __name__ == '__main__':
    unittest.main()
