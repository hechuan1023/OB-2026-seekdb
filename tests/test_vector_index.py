import io
import math
import random
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from minivectordb.engine import Engine
from minivectordb.errors import DBError
from minivectordb.protocol import statements
from tests.test_recovery import Client


class NativeIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Engine(self.temp.name)
        if self.db.vector_indexes.kernel is None:
            self.db.close()
            self.temp.cleanup()
            self.skipTest('Build native kernel with python -m minivectordb.build_native')
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(4))')
        rng = random.Random(93)
        self.db.execute('INSERT INTO t VALUES ' + ','.join(
            f"({i},'g{i%3}','{[rng.uniform(-10,10) for _ in range(4)]}')" for i in range(120)))
        self.db.execute('CREATE VECTOR INDEX vi ON t(v)')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def query(self, sql):
        return list(self.db.execute(sql).rows)

    def compare_reference(self, sql):
        native = self.query(sql)
        kernel = self.db.vector_indexes.kernel
        self.db.vector_indexes.kernel = None
        try:
            reference = self.query(sql)
        finally:
            self.db.vector_indexes.kernel = kernel
        self.assertEqual(native, reference)
        return native

    def test_native_path_randomized_and_arbitrary_filters(self):
        rng = random.Random(207)
        for where in ('', "WHERE tag='g1'", "WHERE tag>='g1' AND id<55", 'WHERE id=-1', "WHERE tag='missing'"):
            for k in (1, 10, 300):
                vector = [rng.random() for _ in range(4)]
                self.compare_reference(f"SELECT id,tag,v,L2_DISTANCE(v,'{vector}') AS d FROM t {where} ORDER BY d LIMIT {k}")
        self.assertGreater(self.db.vector_indexes.searches, 0)

    def test_mutation_commit_rollback_and_failed_statement(self):
        sql = "SELECT id,L2_DISTANCE(v,'[1,2,3,4]') AS d FROM t WHERE tag='g1' ORDER BY d LIMIT 10"
        before = self.compare_reference(sql)
        self.db.execute('BEGIN')
        self.db.execute("UPDATE t SET v='[1,2,3,4]',tag='g1' WHERE id=0")
        self.assertEqual(self.compare_reference(sql)[0], (0, 0.0))
        with self.assertRaises(DBError):
            self.db.execute("INSERT INTO t VALUES (200,'g1','[1,2,3,4]'),(0,'dup','[0,0,0,0]')")
        self.assertEqual(self.compare_reference(sql)[0], (0, 0.0))
        self.db.execute('ROLLBACK')
        self.assertEqual(self.compare_reference(sql), before)
        self.db.execute('BEGIN')
        self.db.execute('DELETE FROM t WHERE id=1')
        self.db.execute("INSERT INTO t VALUES (1,'g1','[1,2,3,4]')")
        self.db.execute('COMMIT')
        self.assertEqual(self.compare_reference(sql)[0], (1, 0.0))

    def test_filter_cache_invalidated_by_scalar_only_update(self):
        sql = "SELECT id FROM t WHERE tag='g1' ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 10"
        self.compare_reference(sql)
        self.assertGreater(len(self.db.vector_indexes.filters), 0)
        self.db.execute("UPDATE t SET tag='gone' WHERE tag='g1'")
        self.assertEqual(self.compare_reference(sql), [])

    def test_ties_use_reference_and_primary_key(self):
        self.db.execute("UPDATE t SET v='[1,0,0,0]'")
        rows = self.compare_reference("SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 7")
        self.assertEqual(rows, [(i,) for i in range(7)])
        self.assertGreater(self.db.vector_indexes.fallbacks, 0)

    def test_extreme_finite_float32_and_rounded_distance_ties(self):
        self.db.execute('DELETE FROM t')
        self.db.execute("INSERT INTO t VALUES (-2,'x','[3e38,1,0,0]'),(-3,'x','[3e38,0,0,0]'),(5,'x','[1e-45,0,0,0]'),(6,'x','[0,0,0,0]')")
        self.compare_reference("SELECT id,L2_DISTANCE(v,'[-3e38,0,0,0]') AS d FROM t ORDER BY d LIMIT 3")
        self.compare_reference("SELECT id,L2_DISTANCE(v,'[0,0,0,0]') AS d FROM t ORDER BY d LIMIT 1")
        self.compare_reference("SELECT id FROM t WHERE id<0 ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 1")

    def test_large_k_and_filter_budget_use_streaming_fallback(self):
        self.db.vector_indexes.max_keep = 4
        before = self.db.vector_indexes.searches
        self.compare_reference("SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 10")
        self.assertEqual(self.db.vector_indexes.searches, before)
        self.db.vector_indexes.max_keep = 100
        self.db.vector_indexes.filter_budget = 512
        self.compare_reference("SELECT id FROM t WHERE id>=0 ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 3")
        self.assertLessEqual(self.db.vector_indexes.filter_bytes, 512)

    def test_descending_and_secondary_order_remain_correct(self):
        for order in ('d DESC', 'd ASC,tag DESC'):
            self.compare_reference("SELECT id,L2_DISTANCE(v,'[0,0,0,0]') AS d FROM t ORDER BY " + order + ' LIMIT 10')
        self.assertEqual(self.db.vector_indexes.searches, 0)

    def test_primary_key_lookup_does_not_scan_vector_index(self):
        def no_scan(*_):
            self.fail('Primary key equality must use direct lookup, not scan the vector snapshot')
        self.db.vector_indexes.topk = no_scan
        rows = self.query("SELECT id FROM t WHERE id=42 ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 10")
        self.assertEqual(rows, [(42,)])

    def test_rebuild_before_ready_and_checkpoint(self):
        sql = "SELECT id FROM t ORDER BY L2_DISTANCE(v,'[2,3,4,5]') LIMIT 10"
        expected = self.query(sql)
        self.db.storage.checkpoint()
        self.assertEqual(self.query(sql), expected)
        self.db.close()
        self.db = Engine(self.temp.name)
        self.assertEqual(self.db.vector_indexes.entries[self.db.table('t')['id']].count, 120)
        self.assertEqual(self.query(sql), expected)

    def test_empty_table_and_index_created_inside_transaction(self):
        self.db.execute('BEGIN')
        self.db.execute('CREATE TABLE new_table(id BIGINT PRIMARY KEY,v VECTOR(4))')
        self.db.execute('CREATE VECTOR INDEX new_index ON new_table(v)')
        self.assertEqual(self.query("SELECT id FROM new_table ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 1"), [])
        self.db.execute('ROLLBACK')
        self.assertNotIn('new_table', self.db.storage.schemas)
        self.compare_reference("SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 3")

    def test_python_only_mode_and_native_failure_is_not_required(self):
        expected = self.query("SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 10")
        self.db.close()
        self.db = Engine(self.temp.name, native=False)
        self.assertIsNone(self.db.vector_indexes.kernel)
        self.assertEqual(self.query("SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 10"), expected)

    def test_interrupted_vector_rebuild_and_stale_files(self):
        expected = self.query("SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 3")
        self.db.close()
        client = Client(self.temp.name, failpoint='during_vector_rebuild', ready=False)
        try:
            self.assertEqual(client.process.wait(timeout=15), 86)
            self.assertIsNone(client.line())  # READY must not precede rebuilding.
        finally:
            client.close()
        self.db = Engine(self.temp.name)
        self.assertEqual(self.query("SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0,0,0]') LIMIT 3"), expected)

    def test_process_kill_discards_uncommitted_flat_index(self):
        sql = "SELECT id,L2_DISTANCE(v,'[0,0,0,0]') AS d FROM t WHERE tag='g1' ORDER BY d LIMIT 3"
        expected = self.query(sql)
        self.db.close()
        client = Client(self.temp.name)
        try:
            client.execute('BEGIN')
            client.execute("UPDATE t SET v='[0,0,0,0]',tag='g1' WHERE id=0")
            self.assertEqual(client.execute(sql)[0], ['0', '0.0'])
        finally:
            client.close(kill=True)
        self.db = Engine(self.temp.name)
        self.assertEqual(self.query(sql), expected)


class StatementScannerTests(unittest.TestCase):
    def test_multiline_quotes_and_comments_preserve_original_sql(self):
        text = "SELECT 'a\nb;''c';/* comment\n; */SELECT `a``;b`;-- tail;\nSELECT 3;"
        self.assertEqual(list(statements(io.StringIO(text))),
                         ["SELECT 'a\nb;''c'", '/* comment\n; */SELECT `a``;b`', '-- tail;\nSELECT 3'])

    def test_large_string_and_structural_characters(self):
        payload = 'x' * 100000 + '; -- # /* */'
        sql = "INSERT INTO t VALUES ('" + payload + "');SELECT 1;"
        self.assertEqual(list(statements(io.StringIO(sql))), [sql[:sql.index(';SELECT')], 'SELECT 1'])

    def test_float32_vector_bulk_conversion_overflow_and_bool(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Engine(directory)
            try:
                db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
                for vec in ('[3.5e38,0]', '[true,0]', '[1e999,0]'):
                    with self.assertRaises(DBError):
                        db.execute(f"INSERT INTO t VALUES (1,'{vec}')")
                db.execute("INSERT INTO t VALUES (1,'[16777217,1e-45]')")
                result = list(db.execute('SELECT v FROM t').rows)[0][0]
                self.assertEqual(result[0], 16777216.0)
                self.assertTrue(math.isfinite(result[1]) and result[1] > 0)
            finally:
                db.close()


class NativeMemoryTests(unittest.TestCase):
    def test_mapped_vectors_exceed_python_allocation_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Engine(directory, sort_memory=64 * 1024, cache_memory=0)
            try:
                if db.vector_indexes.kernel is None:
                    self.skipTest('Native kernel not built')
                db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(128))')
                tail = ',0' * 127
                for start in range(0, 20000, 100):
                    db.execute('INSERT INTO t VALUES ' + ','.join(
                        f"({i},'[{i}{tail}]')" for i in range(start, start + 100)))
                tracemalloc.start()
                try:
                    db.execute('CREATE VECTOR INDEX vi ON t(v)')
                    self.assertGreater((Path(directory) / 'flat-1.vec').stat().st_size, 8 * 1024 * 1024)
                    sql = f"SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0{tail}]') LIMIT 10"
                    self.assertEqual(list(db.execute(sql).rows), [(i,) for i in range(10)])
                    self.assertGreater(db.vector_indexes.searches, 0)
                    _, peak = tracemalloc.get_traced_memory()
                    self.assertLess(peak, 2 * 1024 * 1024)
                finally:
                    tracemalloc.stop()
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
