"""Independent counterexamples for the buffered sequential record reader."""
import io
import os
import tempfile
import unittest

import minivectordb.storage as storage
from minivectordb.storage import (LENGTH, Storage, _encode_row, records,
                                  records_buffered)


def make_wal(rows=500):
    directory = tempfile.mkdtemp()
    store = Storage(directory)
    store.begin()
    store.append(('schema', 't', {'columns': [('id', 'BIGINT', None)], 'pk': 0}))
    for i in range(rows):
        store.append(('row', 1, i, (i,) if i % 7 else None))
    store.commit()
    store.wal.seek(0)
    store.wal.read(len(storage.MAGIC))
    _, length, _ = storage.HEADER.unpack(store.wal.read(storage.HEADER.size))
    start = len(storage.MAGIC) + storage.HEADER.size
    return store, start, start + length


class BufferedRecordsTests(unittest.TestCase):
    def setUp(self):
        self.store, self.start, self.end = make_wal()
        self.expected = list(records(self.store.wal, self.start, self.end))

    def tearDown(self):
        self.store.close()

    def test_identical_output_at_every_block_size(self):
        for block in (1 << 20, 65536, 4096, 256, 64, 32, 16, 12, 8, 4):
            with self.subTest(block=block):
                storage.RECORD_BLOCK = block
                self.assertEqual(list(records_buffered(self.store.wal, self.start, self.end)),
                                 self.expected)
        storage.RECORD_BLOCK = 1 << 20

    def test_truncated_payload_raises_like_the_unbuffered_reader(self):
        data = LENGTH.pack(10) + b'\x01' + b'x' * 10 + LENGTH.pack(100) + b'\x01' + b'y' * 20
        outcomes = []
        for scan in (records, records_buffered):
            try:
                list(scan(io.BytesIO(data), 0, len(data)))
                outcomes.append('no-raise')
            except RuntimeError as error:
                outcomes.append(f'RuntimeError:{error}')
            except Exception as error:  # struct.error from the malformed payload
                outcomes.append(type(error).__name__)
        self.assertEqual(outcomes[0], outcomes[1])

    def test_boundary_crossing_raises_after_decode(self):
        payload = b'\x01' + _encode_row(('row', 1, 5, (5,)))
        data = LENGTH.pack(len(payload)) + payload
        outcomes = []
        for scan in (records, records_buffered):
            try:
                list(scan(io.BytesIO(data), 0, len(data) - 3))
                outcomes.append('no-raise')
            except RuntimeError as error:
                outcomes.append(str(error))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertIn('boundary', outcomes[0])

    def test_interleaved_seeks_do_not_corrupt_the_scan(self):
        # _apply seeks the WAL between yields for live_bytes bookkeeping; the
        # reader must tolerate an arbitrary file position on re-entry.
        seen = []
        for offset, record, length in records_buffered(self.store.wal, self.start, self.end):
            seen.append((offset, record, length))
            self.store.wal.seek(0)  # hostile reposition between yields
        self.assertEqual(seen, self.expected)

    def test_recovery_replays_identically(self):
        directory = tempfile.mkdtemp()
        store = Storage(directory)
        store.begin()
        store.append(('schema', 't', {'columns': [('id', 'BIGINT', None)], 'pk': 0}))
        for i in range(300):
            store.append(('row', 1, i, (i,)))
        store.commit()
        store.close()
        store = Storage(directory)  # recover() now uses the buffered reader
        self.assertEqual(len(list(store.rows(1))), 300)
        store.close()


class CommitMetadataScanTests(unittest.TestCase):
    def test_commit_applies_index_without_decoding_row_payloads(self):
        from minivectordb import storage as storage_module
        from minivectordb.engine import Engine

        with tempfile.TemporaryDirectory() as directory:
            db = Engine(directory)
            db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, tag VARCHAR(8), v VECTOR(2))')
            db.execute("INSERT INTO t VALUES (1,'a','[1,2]'),(2,'b','[3,4]'),(3,'c','[5,6]')")
            table = db.table('t')['id']
            db.execute('BEGIN')
            db.execute("INSERT INTO t VALUES (4,'d','[7,8]')")
            db.execute("UPDATE t SET tag='z', v='[9,9]' WHERE id=2")
            db.execute('DELETE FROM t WHERE id=3')
            original = storage_module._decode_row
            storage_module._decode_row = lambda _: self.fail(
                'commit must not materialize compact row payloads')
            try:
                db.execute('COMMIT')
            finally:
                storage_module._decode_row = original
            try:
                rows = {row[0]: row for row in
                        db.execute('SELECT id,tag,v FROM t ORDER BY id').rows}
                self.assertEqual(sorted(rows), [1, 2, 4])
                self.assertEqual(rows[2][1], 'z')
                self.assertEqual(list(rows[2][2]), [9.0, 9.0])
                self.assertEqual(list(rows[4][2]), [7.0, 8.0])
                self.assertIsNone(db.storage.get(table, 3))
                self.assertLess(db.storage.index.get(table, 3), 0)
                # live_bytes must equal the serialized size of current live
                # versions, computed independently from the index and WAL headers.
                expected = 0
                for _, key, offset in db.storage.index.entries(table):
                    if offset > 0:
                        db.storage.wal.seek(offset - 1)
                        expected += LENGTH.unpack(
                            db.storage.wal.read(LENGTH.size))[0] + LENGTH.size
                self.assertEqual(db.storage.live_bytes, expected)
            finally:
                db.close()

    def test_restore_rebuilds_overlay_without_decoding_row_payloads(self):
        from array import array
        from minivectordb import storage as storage_module

        with tempfile.TemporaryDirectory() as directory:
            s = storage_module.Storage(directory)
            s.begin()
            s.append(('schema', 't', {'id': 1, 'pk': 0, 'indexes': {},
                                      'columns': [('id', 'BIGINT', 0), ('v', 'VECTOR', 2)]}))
            s.append(('row', 1, 1, (1, array('f', [1.0, 2.0]))))
            savepoint = s.savepoint()
            s.append(('row', 1, 2, (2, array('f', [3.0, 4.0]))))
            s.append(('row', 1, 1, None))
            original = storage_module._decode_row
            storage_module._decode_row = lambda _: self.fail(
                'restore must not materialize compact row payloads')
            try:
                s.restore(savepoint)
            finally:
                storage_module._decode_row = original
            try:
                self.assertGreater(s.overlay.get(1, 1), 0)
                self.assertIsNone(s.overlay.get(1, 2))
            finally:
                s.close()


if __name__ == '__main__':
    unittest.main()
