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


if __name__ == '__main__':
    unittest.main()
