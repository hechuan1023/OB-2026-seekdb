import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from minivectordb.engine import Engine

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, directory, failpoint=None, ready=True, extra_args=(), engine_root=None):
        environment = dict(os.environ, PYTHONIOENCODING='utf-8')
        if failpoint:
            environment['MINIVECTORDB_FAILPOINT'] = failpoint
        else:
            environment.pop('MINIVECTORDB_FAILPOINT', None)
        self.process = subprocess.Popen(
            [sys.executable, '-m', 'minivectordb', '--data-dir', str(directory), *extra_args],
            cwd=engine_root or ROOT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', bufsize=1)
        self.lines = queue.Queue()
        self.stderr = []
        self.stdout_thread = threading.Thread(target=self._read, daemon=True)
        self.stderr_thread = threading.Thread(target=self._errors, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()
        if ready:
            first = self.line()
            if first != 'READY':
                self.close()
                raise AssertionError(f'Expected READY, got {first!r}; stderr={self.stderr}')

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(line.rstrip('\r\n'))
        self.lines.put(None)

    def _errors(self):
        self.stderr.extend(self.process.stderr.readlines())

    def line(self):
        return self.lines.get(timeout=15)

    def send(self, sql):
        self.process.stdin.write(sql + ';\n')
        self.process.stdin.flush()

    def execute(self, sql):
        self.send(sql)
        first = self.line()
        if first is None:
            raise AssertionError(f'Unexpected process exit: {self.stderr}')
        if not first.startswith('RESULT\t'):
            return first
        rows = []
        while True:
            line = self.line()
            if line is None:
                raise AssertionError('Process exited during a result')
            if line.startswith('END\t'):
                return rows
            rows.append(line.split('\t'))

    def close(self, kill=False):
        if self.process.poll() is None:
            if kill:
                self.process.kill()
            else:
                self.process.stdin.close()
            self.process.wait(timeout=15)
        self.stdout_thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                stream.close()


class RecoveryTests(unittest.TestCase):
    def test_deep_invalid_vector_does_not_abort_transaction(self):
        with self.client() as client:
            self.assertEqual(client.execute('CREATE TABLE v(id BIGINT PRIMARY KEY, x VECTOR(2))'), 'OK\t0')
            self.assertEqual(client.execute('BEGIN'), 'OK\t0')
            self.assertEqual(client.execute('UPDATE t SET value=99 WHERE id=1'), 'OK\t1')
            nested = '[' * 1200 + '0' + ']' * 1200
            reply = client.execute("INSERT INTO v VALUES (1,'[0," + nested + "]')")
            self.assertTrue(reply.startswith('ERROR\tINVALID_VECTOR\t'), reply)
            self.assertEqual(client.execute('SELECT value FROM t WHERE id=1'), [['99']])
            self.assertEqual(client.execute('COMMIT'), 'OK\t0')
        with self.client() as restarted:
            self.assertEqual(restarted.execute('SELECT value FROM t WHERE id=1'), [['99']])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        with self.client() as client:
            self.assertEqual(client.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,value BIGINT)'), 'OK\t0')
            self.assertEqual(client.execute('INSERT INTO t VALUES (1,10),(2,20)'), 'OK\t2')

    def tearDown(self):
        self.temp.cleanup()

    def client(self, **kwargs):
        class Managed(Client):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()
        return Managed(self.directory, **kwargs)

    def test_acknowledged_autocommit_survives_kill(self):
        client = Client(self.directory)
        try:
            self.assertEqual(client.execute('UPDATE t SET value=99 WHERE id=1'), 'OK\t1')
        finally:
            client.close(kill=True)
        with self.client() as restarted:
            self.assertEqual(restarted.execute('SELECT * FROM t ORDER BY id'), [['1','99'], ['2','20']])

    def test_clean_reopen_does_not_sync_the_wal(self):
        import minivectordb.storage as storage_module

        with mock.patch.object(storage_module.os, 'fsync', wraps=os.fsync) as sync:
            db = Engine(self.directory)
            db.close()
        self.assertEqual(sync.call_count, 0)

    def test_uncommitted_transaction_is_discarded(self):
        client = Client(self.directory)
        try:
            client.execute('BEGIN')
            client.execute('UPDATE t SET value=99')
            client.execute('DELETE FROM t WHERE id=2')
            self.assertEqual(client.execute('SELECT * FROM t ORDER BY id'), [['1','99']])
        finally:
            client.close(kill=True)
        with self.client() as restarted:
            self.assertEqual(restarted.execute('SELECT * FROM t ORDER BY id'), [['1','10'], ['2','20']])

    def test_acknowledged_explicit_commit_is_atomic(self):
        client = Client(self.directory)
        try:
            client.execute('BEGIN')
            client.execute('UPDATE t SET value=99')
            client.execute('INSERT INTO t VALUES (3,30)')
            self.assertEqual(client.execute('COMMIT'), 'OK\t0')
        finally:
            client.close(kill=True)
        with self.client() as restarted:
            self.assertEqual(restarted.execute('SELECT * FROM t ORDER BY id'), [['1','99'], ['2','99'], ['3','30']])

    def test_eof_rolls_back(self):
        with self.client() as client:
            client.execute('BEGIN')
            client.execute('DELETE FROM t')
        with self.client() as restarted:
            self.assertEqual(restarted.execute('SELECT * FROM t ORDER BY id'), [['1','10'], ['2','20']])

    def test_commit_interruption_before_footer(self):
        with self.client(failpoint='before_commit_footer') as client:
            client.send('UPDATE t SET value=88')
            self.assertEqual(client.process.wait(timeout=15), 86)
        with self.client() as restarted:
            self.assertEqual(restarted.execute('SELECT * FROM t ORDER BY id'), [['1','10'], ['2','20']])
            self.assertEqual(restarted.execute('INSERT INTO t VALUES (3,30)'), 'OK\t1')

    def test_durable_commit_before_acknowledgement(self):
        with self.client(failpoint='after_commit_sync') as client:
            client.send('UPDATE t SET value=88')
            self.assertEqual(client.process.wait(timeout=15), 86)
        with self.client() as restarted:
            self.assertEqual(restarted.execute('SELECT * FROM t ORDER BY id'), [['1','88'], ['2','88']])

    def test_interrupted_recovery_repeats_safely(self):
        for _ in range(3):
            with self.client(failpoint='during_recovery', ready=False) as client:
                self.assertEqual(client.process.wait(timeout=15), 86)
        with self.client() as restarted:
            self.assertEqual(restarted.execute('SELECT * FROM t ORDER BY id'), [['1','10'], ['2','20']])

    def test_every_partial_write_boundary_of_unacknowledged_frame(self):
        # Capture an UNACKNOWLEDGED commit, then model termination after each
        # prefix of its append. Previously acknowledged bytes are never removed.
        wal = self.directory / 'database.wal'
        before = wal.read_bytes()
        with self.client(failpoint='after_commit_sync') as client:
            client.send('UPDATE t SET value=77')
            self.assertEqual(client.process.wait(timeout=15), 86)
        appended = wal.read_bytes()[len(before):]
        for prefix in range(len(appended) + 1):
            with self.subTest(prefix=prefix):
                wal.write_bytes(before + appended[:prefix])
                db = Engine(self.directory)
                try:
                    rows = list(db.execute('SELECT * FROM t ORDER BY id').rows)
                    expected = [(1,77),(2,77)] if prefix == len(appended) else [(1,10),(2,20)]
                    self.assertEqual(rows, expected)
                finally:
                    db.close()

    def test_error_does_not_break_protocol(self):
        with self.client() as client:
            self.assertTrue(client.execute('SELECT nope FROM t').startswith('ERROR\tNOT_FOUND\t'))
            self.assertEqual(client.execute('SELECT id FROM t ORDER BY id'), [['1'], ['2']])
            self.assertEqual(client.stderr, [])

    def test_checkpoint_crashes_before_and_after_publication(self):
        for point in ('before_checkpoint_replace', 'after_checkpoint_replace'):
            with self.subTest(point=point):
                with self.client() as client:
                    client.execute('DELETE FROM t')
                    client.execute('INSERT INTO t VALUES ' + ','.join(f'({i},{i})' for i in range(500)))
                with self.client(failpoint=point, extra_args=('--checkpoint-mib', '0.004')) as client:
                    client.send('DELETE FROM t WHERE id>=2')
                    self.assertEqual(client.process.wait(timeout=15), 86)
                with self.client() as restarted:
                    self.assertEqual(restarted.execute('SELECT * FROM t ORDER BY id'), [['0','0'], ['1','1']])
                    restarted.execute('INSERT INTO t VALUES (2,200)')



if __name__ == '__main__':
    unittest.main()


class RecordFormatTests(unittest.TestCase):
    def test_recovery_index_scan_does_not_decode_row_payloads(self):
        from minivectordb import storage as storage_module

        with tempfile.TemporaryDirectory() as directory:
            db = Engine(directory)
            db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, tag VARCHAR(8), v VECTOR(2))')
            db.execute("INSERT INTO t VALUES (1,'keep','[1,2]')")
            table = db.table('t')['id']
            db.close()

            original = storage_module._decode_row
            storage_module._decode_row = lambda _: self.fail(
                'recovery must not materialize compact row payloads')
            try:
                reopened = storage_module.Storage(directory)
            finally:
                storage_module._decode_row = original
            try:
                self.assertIn('t', reopened.schemas)
                self.assertIsNotNone(reopened.index.get(table, 1))
            finally:
                reopened.close()

    def test_row_round_trip_and_old_pickle_fallback(self):
        import pickle
        from array import array
        from minivectordb.storage import LENGTH, read_record, write_all, write_record

        row = ('row', 3, -7, (5, '你好', array('f', [1.5, -2.0])))
        delete = ('row', 3, 8, None)
        schema = ('schema', 't', {'id': 1, 'columns': [('id', 'BIGINT', 0)]})
        with tempfile.TemporaryFile() as file:
            positions = [write_record(file, value) for value in (row, delete, schema)]
            for position, expected in zip(positions, (row, delete, schema)):
                self.assertEqual(read_record(file, position), expected)
        # A record written by the previous pickled format still reads.
        with tempfile.TemporaryFile() as file:
            data = pickle.dumps(row, protocol=5)
            write_all(file, LENGTH.pack(len(data)) + data)
            file.seek(0)
            self.assertEqual(read_record(file, 0), row)

    def test_large_string_row_uses_pickle_fallback(self):
        from array import array
        from minivectordb.storage import LENGTH, read_record, write_record

        row = ('row', 1, 5, (5, 'x' * 300))
        with tempfile.TemporaryFile() as file:
            position = write_record(file, row)
            file.seek(position)
            length, = LENGTH.unpack(file.read(LENGTH.size))
            self.assertEqual(file.read(length)[:1], b'\x00')
            self.assertEqual(read_record(file, position), row)
