import io
import math
import random
import struct
import tempfile
import unittest
from pathlib import Path

from minivectordb.engine import Engine, QueryResult, convert
from minivectordb.errors import DBError
from minivectordb.protocol import statements
from minivectordb.sql import Literal


class EngineTests(unittest.TestCase):
    def test_bigint_literal_sign_and_unicode_decimal_boundaries(self):
        column = ('id', 'BIGINT', None)
        for text, expected in [('+0001', 1), ('-0000', 0), ('-１２', -12),
                               ('+٠١', 1), ('9223372036854775807', 9223372036854775807)]:
            with self.subTest(text=text):
                self.assertEqual(convert(Literal('number', text), column), expected)
        for text in ('', '+', '-', '++1', '--1', '1.0', '1e1', '²', ' 1', '9223372036854775808'):
            with self.subTest(text=text), self.assertRaises(DBError):
                convert(Literal('number', text), column)

    def test_long_varchar_predicates_do_not_enforce_column_width(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(2))')
        self.db.execute("INSERT INTO t VALUES (1,'ab'),(2,'中')")
        self.assertEqual(self.query("SELECT id FROM t WHERE s='abc'"), [])
        self.assertEqual(self.query("SELECT id FROM t WHERE s<'abc' ORDER BY id"), [(1,)])
        self.assertEqual(self.db.execute("DELETE FROM t WHERE s='abc'"), 0)
        self.assertEqual(self.query('SELECT id FROM t ORDER BY id'), [(1,), (2,)])
        with self.assertRaises(DBError):
            self.db.execute("INSERT INTO t VALUES (3,'abc')")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Engine(self.temp.name, sort_memory=4096)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def query(self, sql):
        result = self.db.execute(sql)
        self.assertIsInstance(result, QueryResult)
        return list(result.rows)

    def create(self):
        self.db.execute('CREATE TABLE d(id BIGINT PRIMARY KEY, tag VARCHAR(8), v VECTOR(2))')

    def restart(self):
        self.db.close()
        self.db = Engine(self.temp.name, sort_memory=4096)

    def test_example_filter_and_l2(self):
        self.create()
        self.db.execute("INSERT INTO d VALUES (1,'other','[0,0]'),(2,'keep','[3,4]'),(3,'keep','[6,8]')")
        rows = self.query("SELECT id,L2_DISTANCE(v,'[0,0]') AS distance FROM d WHERE tag='keep' ORDER BY distance LIMIT 10")
        self.assertEqual(rows, [(2, 5.0), (3, 10.0)])
        self.assertEqual(self.query("SELECT id FROM d WHERE tag='missing' ORDER BY id LIMIT 10"), [])

    def test_distance_ties_use_primary_key(self):
        self.create()
        self.db.execute("INSERT INTO d VALUES (9,'x','[1,0]'),(-3,'x','[0,1]'),(2,'x','[-1,0]')")
        self.assertEqual(self.query("SELECT id FROM d ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 2"), [(-3,), (2,)])

    def test_varchar_unicode_and_codepoint_order(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, s VARCHAR(2))')
        self.db.execute("INSERT INTO t VALUES (1,'中😀'),(2,'é'),(3,'z'),(4,'é')")
        self.assertEqual(self.query('SELECT id FROM t ORDER BY s'), [(4,), (3,), (2,), (1,)])
        with self.assertRaises(DBError):
            self.db.execute("INSERT INTO t VALUES (5,'中😀a')")

    def test_invalid_vectors_are_atomic(self):
        self.create()
        for value in ('[NaN,0]', '[Infinity,0]', '[1e39,0]', '[1]', '[1,2,3]', '[true,0]', '[null,0]', '["1",0]', '{}'):
            with self.subTest(vector=value), self.assertRaises(DBError):
                self.db.execute(f"INSERT INTO d VALUES (1,'ok','[0,0]'),(2,'bad','{value}')")
            self.assertEqual(self.query('SELECT * FROM d'), [])

    def test_float32_storage_rounding(self):
        self.create()
        self.db.execute("INSERT INTO d VALUES (1,'x','[16777217,0]')")
        self.assertEqual(self.query("SELECT L2_DISTANCE(v,'[16777216,0]') FROM d"), [(0.0,)])
        self.restart()
        self.assertEqual(list(self.query('SELECT v FROM d')[0][0]), [16777216.0, 0.0])

    def test_bigint_limits(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY)')
        self.db.execute('INSERT INTO t VALUES (-9223372036854775808),(9223372036854775807)')
        for value in ('9223372036854775808', '-9223372036854775809', '1.0', '1e1'):
            with self.assertRaises(DBError):
                self.db.execute(f'INSERT INTO t VALUES ({value})')
        self.assertEqual(len(self.query('SELECT * FROM t')), 2)

    def test_large_integer_input_returns_error_without_aborting(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY)')
        with self.assertRaises(DBError):
            self.db.execute('INSERT INTO t VALUES (' + '9' * 5000 + ')')
        self.db.execute('INSERT INTO t VALUES (' + '0' * 5000 + '1)')
        self.assertEqual(self.query('SELECT * FROM t'), [(1,)])

    def test_cache_keeps_versions_and_returned_arrays_isolated(self):
        self.create()
        self.db.execute("INSERT INTO d VALUES (1,'x','[1,0]')")
        returned = self.query('SELECT v FROM d')[0][0]
        returned[0] = 99
        self.assertEqual(self.query("SELECT L2_DISTANCE(v,'[0,0]') FROM d"), [(1.0,)])
        self.db.execute('BEGIN')
        self.db.execute("UPDATE d SET v='[8,0]' WHERE id=1")
        self.assertEqual(self.query("SELECT L2_DISTANCE(v,'[0,0]') FROM d"), [(8.0,)])
        self.db.execute('ROLLBACK')
        self.assertEqual(self.query("SELECT L2_DISTANCE(v,'[0,0]') FROM d"), [(1.0,)])
        self.db.execute("UPDATE d SET v='[3,0]' WHERE id=1")
        self.assertEqual(self.query("SELECT L2_DISTANCE(v,'[0,0]') FROM d"), [(3.0,)])

    def test_statement_savepoint_preserves_prior_success(self):
        self.create()
        self.db.execute('BEGIN')
        self.db.execute("INSERT INTO d VALUES (1,'first','[1,1]')")
        with self.assertRaises(DBError):
            self.db.execute("INSERT INTO d VALUES (2,'second','[2,2]'),(1,'dup','[0,0]')")
        self.assertEqual(self.query('SELECT id FROM d'), [(1,)])
        self.db.execute("UPDATE d SET tag='saved' WHERE id=1")
        self.db.execute('COMMIT')
        self.restart()
        self.assertEqual(self.query('SELECT id,tag FROM d'), [(1, 'saved')])

    def test_rollback_vector_and_records(self):
        self.create()
        self.db.execute("INSERT INTO d VALUES (1,'x','[1,0]'),(2,'x','[2,0]')")
        self.db.execute('CREATE VECTOR INDEX iv ON d(v)')
        self.db.execute('BEGIN')
        self.db.execute("UPDATE d SET v='[8,0]' WHERE id=1")
        self.db.execute('DELETE FROM d WHERE id=2')
        self.db.execute("INSERT INTO d VALUES (3,'x','[3,0]')")
        sql = "SELECT id,L2_DISTANCE(v,'[0,0]') AS dist FROM d ORDER BY dist LIMIT 10"
        self.assertEqual(self.query(sql), [(3, 3.0), (1, 8.0)])
        self.db.execute('ROLLBACK')
        self.assertEqual(self.query(sql), [(1, 1.0), (2, 2.0)])

    def test_delete_reinsert_and_overlay_scan(self):
        self.create()
        self.db.execute("INSERT INTO d VALUES (1,'x','[1,0]')")
        self.db.execute('DELETE FROM d')
        self.db.execute('BEGIN')
        self.db.execute("INSERT INTO d VALUES (1,'again','[2,0]'),(2,'new','[3,0]')")
        self.db.execute("UPDATE d SET tag='all'")
        self.assertEqual(self.query('SELECT id,tag FROM d ORDER BY id'), [(1, 'all'), (2, 'all')])
        self.db.execute('COMMIT')
        self.restart()
        self.assertEqual(len(self.query('SELECT * FROM d')), 2)

    def test_order_by_multi_key_desc_and_external_merge(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, a BIGINT, b VARCHAR(8))')
        rng = random.Random(12)
        data = [(i, rng.randrange(5), chr(97 + rng.randrange(5))) for i in range(600)]
        self.db.execute('INSERT INTO t VALUES ' + ','.join(f"({i},{a},'{b}')" for i, a, b in data))
        expected = sorted(data, key=lambda row: (-row[1], row[2], row[0]))
        self.assertEqual(self.query('SELECT * FROM t ORDER BY a DESC,b ASC'), expected)
        self.assertEqual(self.query('SELECT * FROM t ORDER BY a DESC,b ASC LIMIT 91'), expected[:91])
        self.assertEqual(list(Path(self.temp.name).glob('sort-*.tmp')), [])

    def test_randomized_vector_truth(self):
        self.create()
        rng = random.Random(851)
        data = []
        for i in range(200):
            vector = [rng.uniform(-10, 10), rng.uniform(-10, 10)]
            stored = [struct.unpack('<f', struct.pack('<f', x))[0] for x in vector]
            data.append((i, str(i % 3), stored))
            self.db.execute(f"INSERT INTO d VALUES ({i},'{i % 3}','{vector}')")
        for _ in range(15):
            q = [rng.uniform(-10, 10), rng.uniform(-10, 10)]
            fq = [struct.unpack('<f', struct.pack('<f', x))[0] for x in q]
            truth = sorted(((i, math.sqrt(sum((a-b)**2 for a,b in zip(vec,fq))))
                            for i, tag, vec in data if tag == '1'), key=lambda pair: (pair[1], pair[0]))[:10]
            actual = self.query(f"SELECT id,L2_DISTANCE(v,'{q}') AS dist FROM d WHERE tag='1' ORDER BY dist LIMIT 10")
            self.assertEqual([row[0] for row in actual], [row[0] for row in truth])
            for left, right in zip(actual, truth):
                self.assertAlmostEqual(left[1], right[1], places=12)

    def test_randomized_scalar_transactions(self):
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, value BIGINT)')
        rng = random.Random(92)
        truth = {}
        for round_id in range(35):
            self.db.execute('BEGIN')
            pending = dict(truth)
            for _ in range(12):
                key, value = rng.randrange(30), rng.randrange(-100, 100)
                operation = rng.randrange(3)
                if operation == 0:
                    if key in pending:
                        with self.assertRaises(DBError):
                            self.db.execute(f'INSERT INTO t VALUES ({key},{value})')
                    else:
                        self.db.execute(f'INSERT INTO t VALUES ({key},{value})')
                        pending[key] = value
                elif operation == 1:
                    self.db.execute(f'UPDATE t SET value={value} WHERE id={key}')
                    if key in pending:
                        pending[key] = value
                else:
                    self.db.execute(f'DELETE FROM t WHERE id={key}')
                    pending.pop(key, None)
                self.assertEqual(self.query('SELECT * FROM t ORDER BY id'), sorted(pending.items()))
            if round_id % 3:
                self.db.execute('COMMIT')
                truth = pending
            else:
                self.db.execute('ROLLBACK')
            if round_id % 7 == 0:
                self.restart()
            self.assertEqual(self.query('SELECT * FROM t ORDER BY id'), sorted(truth.items()))

    def test_unsupported_and_schema_validation(self):
        for sql in ('CREATE TABLE t(id BIGINT)', 'CREATE TABLE t(id VARCHAR(4) PRIMARY KEY)',
                    'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2),w VECTOR(2))',
                    'CREATE TABLE t(id BIGINT PRIMARY KEY,id BIGINT)',
                    'CREATE TABLE t(id BIGINT PRIMARY KEY,x INT)'):
            with self.assertRaises(DBError):
                self.db.execute(sql)
        self.create()
        for sql in ('SELECT COUNT(*) FROM d', 'SELECT * FROM d JOIN x ON d.id=x.id',
                    'UPDATE d SET id=2', 'SELECT id FROM d WHERE id!=2',
                    'SELECT id FROM d WHERE id=1 OR id=2', 'SELECT missing FROM d',
                    "SELECT id FROM d WHERE tag=NULL", 'ALTER TABLE d ADD x BIGINT'):
            with self.subTest(sql=sql), self.assertRaises(DBError):
                self.db.execute(sql)

    def test_ddl_transaction_and_close_rollback(self):
        self.db.execute('BEGIN')
        self.create()
        self.db.execute("INSERT INTO d VALUES (1,'x','[0,0]')")
        self.restart()
        with self.assertRaises(DBError):
            self.query('SELECT * FROM d')
        self.create()

    def test_parse_failure_keeps_transaction_usable(self):
        self.create()
        self.db.execute('BEGIN')
        self.db.execute("INSERT INTO d VALUES (1,'x','[1,1]')")
        with self.assertRaises(DBError):
            self.db.execute("UPDATE d SET tag='no' WHER id=1")
        with self.assertRaises(DBError):
            self.db.execute('BEGIN')
        self.db.execute('COMMIT')
        self.assertEqual(self.query('SELECT tag FROM d'), [('x',)])

    def test_explicit_insert_columns_and_primary_position(self):
        self.db.execute('CREATE TABLE t(value VARCHAR(10),id BIGINT,PRIMARY KEY(id))')
        self.db.execute("INSERT INTO t(id,value) VALUES (3,'a''b;z')")
        self.assertEqual(self.query('SELECT * FROM t'), [("a'b;z", 3)])

    def test_empty_limit_and_invalid_expressions(self):
        self.create()
        self.assertEqual(self.query('SELECT * FROM d ORDER BY id LIMIT 0'), [])
        with self.assertRaises(DBError):
            self.query("SELECT L2_DISTANCE(v,'[1]') FROM d LIMIT 0")
        with self.assertRaises(DBError):
            self.query('SELECT * FROM d ORDER BY v')

    def test_hash_growth_restart_and_multiple_tables(self):
        self.db.execute('CREATE TABLE a(id BIGINT PRIMARY KEY)')
        self.db.execute('CREATE TABLE b(id BIGINT PRIMARY KEY)')
        self.db.execute('INSERT INTO a VALUES ' + ','.join(f'({i})' for i in range(2500)))
        self.db.execute('INSERT INTO b VALUES (0),(2499)')
        self.restart()
        self.assertEqual(self.query('SELECT id FROM a ORDER BY id'), [(i,) for i in range(2500)])
        self.assertEqual(self.query('SELECT id FROM b ORDER BY id'), [(0,), (2499,)])

    def test_ann_flat_fallback_and_index_restart(self):
        self.create()
        self.db.execute('CREATE VECTOR INDEX iv ON d(v)')
        self.db.execute("INSERT INTO d VALUES (1,'x','[0,0]')")
        self.restart()
        self.db.mode = 'ann'
        self.assertEqual(self.query("SELECT id FROM d ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 1"), [(1,)])
        self.assertIn('iv', self.db.table('d')['indexes'])

    def test_checkpoint_reclaims_versions_and_preserves_metadata(self):
        self.create()
        self.db.execute('CREATE VECTOR INDEX iv ON d(v)')
        self.db.execute('INSERT INTO d VALUES ' + ','.join(f"({i},'x','[{i},0]')" for i in range(200)))
        self.db.execute('DELETE FROM d WHERE id>=2')
        expected = self.query('SELECT * FROM d ORDER BY id')
        path = Path(self.temp.name) / 'database.wal'
        before = path.stat().st_size
        self.db.storage.checkpoint()
        self.assertLess(path.stat().st_size, before // 4)
        self.assertEqual(self.query('SELECT * FROM d ORDER BY id'), expected)
        self.assertEqual(self.db.storage.index.count, 2)
        self.assertIn('iv', self.db.table('d')['indexes'])
        self.db.execute("INSERT INTO d VALUES (2,'again','[9,0]')")
        self.restart()
        self.assertEqual(self.query('SELECT id FROM d ORDER BY id'), [(0,), (1,), (2,)])

    def test_automatic_checkpoint_threshold_and_rollback(self):
        self.db.storage.checkpoint_bytes = 4096
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VARCHAR(64))')
        self.db.execute('INSERT INTO t VALUES ' + ','.join(f"({i},'{'x'*64}')" for i in range(300)))
        self.db.execute('BEGIN')
        self.db.execute('DELETE FROM t')
        self.db.execute('ROLLBACK')
        self.assertEqual(len(self.query('SELECT * FROM t')), 300)
        self.db.execute('DELETE FROM t WHERE id>=2')
        self.assertLess((Path(self.temp.name) / 'database.wal').stat().st_size, 4096)
        self.assertEqual(self.db.storage.index.count, 2)
        self.restart()
        self.assertEqual(self.query('SELECT id FROM t ORDER BY id'), [(0,), (1,)])

    def test_short_writes_cannot_silently_lose_committed_data(self):
        class ShortWriter:
            def __init__(self, file):
                self.file = file

            def write(self, data):
                return self.file.write(data[:3])

            def __getattr__(self, name):
                return getattr(self.file, name)

        self.create()
        self.db.storage.wal = ShortWriter(self.db.storage.wal)
        self.db.execute("INSERT INTO d VALUES (1,'saved','[1,2]'),(2,'saved','[3,4]')")
        self.restart()
        self.assertEqual(self.query('SELECT id,tag FROM d ORDER BY id'), [(1, 'saved'), (2, 'saved')])


class StatementTests(unittest.TestCase):
    def test_multiline_comments_quotes_and_semicolons(self):
        text = "-- ignored ;\nSELECT 'a;''b'; /* x; */ SELECT 2;\n# c;\nSELECT `a;b`;"
        values = list(statements(io.StringIO(text)))
        self.assertEqual(len(values), 3)
        self.assertIn("'a;''b'", values[0])

    def test_unterminated_input(self):
        for text in ("SELECT 'unfinished", '/* unfinished'):
            with self.assertRaises(DBError):
                list(statements(io.StringIO(text)))


if __name__ == '__main__':
    unittest.main()
