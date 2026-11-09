"""Append-only committed transactions and rebuildable, disk-backed hash indexes.

Only the WAL is authoritative. Indexes and transaction staging files are disposable.
No table-sized Python dictionary is used for primary keys or transaction changes.
"""
import copy
import mmap
import os
import pickle
import struct
import sys
import zlib
from collections import OrderedDict
from pathlib import Path

MAGIC = b'MVDB0001'
HEADER = struct.Struct('<4sQI')
TRAILER = struct.Struct('<4sQ')
LENGTH = struct.Struct('<I')
SLOT = struct.Struct('<qqq')  # table id (zero means unused), primary key, signed offset
MASK64 = (1 << 64) - 1


def sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def failpoint(name):
    if os.environ.get('MINIVECTORDB_FAILPOINT') == name:
        os._exit(86)


def write_all(file, data):
    remaining = memoryview(data)
    while remaining:
        written = file.write(remaining)
        if written is None or written <= 0:
            raise OSError('Database write made no progress')
        remaining = remaining[written:]


def hash_key(table, key):
    value = (key ^ (table * 0x9E3779B97F4A7C15)) & MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK64
    return value ^ (value >> 31)


class DiskHash:
    def __init__(self, path, capacity=1024):
        self.path = Path(path)
        self.capacity = capacity
        self.count = 0
        self.file = open(self.path, 'w+b')
        self.file.truncate(capacity * SLOT.size)
        self.map = mmap.mmap(self.file.fileno(), 0)

    def _slot(self, table, key):
        slot = hash_key(table, key) & (self.capacity - 1)
        for _ in range(self.capacity):
            t, k, value = SLOT.unpack_from(self.map, slot * SLOT.size)
            if t == 0 or (t == table and k == key):
                return slot, t, value
            slot = (slot + 1) & (self.capacity - 1)
        raise RuntimeError('Hash table is full')

    def get(self, table, key):
        _, t, value = self._slot(table, key)
        return value if t else None

    def put(self, table, key, value):
        slot, t, _ = self._slot(table, key)
        if not t and (self.count + 1) * 10 > self.capacity * 6:
            self._grow()
            slot, t, _ = self._slot(table, key)
        SLOT.pack_into(self.map, slot * SLOT.size, table, key, value)
        self.count += int(not t)

    def entries(self, table=None):
        for i in range(self.capacity):
            t, key, value = SLOT.unpack_from(self.map, i * SLOT.size)
            if t and (table is None or table == t):
                yield t, key, value

    def _grow(self):
        replacement = DiskHash(str(self.path) + '.resize', self.capacity * 2)
        for table, key, value in self.entries():
            replacement.put(table, key, value)
        self.close()
        replacement.close()
        os.replace(replacement.path, self.path)
        self.capacity = replacement.capacity
        self.count = replacement.count
        self.file = open(self.path, 'r+b')
        self.map = mmap.mmap(self.file.fileno(), 0)

    def close(self):
        self.map.close()
        self.file.close()


def write_record(file, value):
    data = pickle.dumps(value, protocol=5)
    position = file.tell()
    write_all(file, LENGTH.pack(len(data)))
    write_all(file, data)
    return position


def read_record(file, position):
    file.seek(position)
    raw = file.read(LENGTH.size)
    if len(raw) != LENGTH.size:
        raise RuntimeError('Truncated record length')
    length, = LENGTH.unpack(raw)
    data = file.read(length)
    if len(data) != length:
        raise RuntimeError('Truncated record')
    return pickle.loads(data)


def records(file, start, end):
    position = start
    while position < end:
        record = read_record(file, position)
        next_position = file.tell()
        if next_position > end:
            raise RuntimeError('Record crosses transaction boundary')
        yield position, record
        position = next_position


class Storage:
    def __init__(self, directory, cache_memory=16 * 1024 * 1024, checkpoint_bytes=64 * 1024 * 1024):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.schemas = {}
        self.base_schemas = None
        self.stage = None
        self.overlay = None
        self.cache_limit = cache_memory
        self.cache_bytes = 0
        self.cache = OrderedDict()
        self.checkpoint_bytes = checkpoint_bytes
        self.live_bytes = 0
        self.index = DiskHash(self.directory / 'primary.idx')
        wal_path = self.directory / 'database.wal'
        self.wal = open(wal_path, 'r+b' if wal_path.exists() else 'w+b', buffering=0)
        if os.fstat(self.wal.fileno()).st_size < len(MAGIC):
            self.wal.seek(0)
            write_all(self.wal, MAGIC)
            self.wal.truncate(len(MAGIC))
            os.fsync(self.wal.fileno())
            sync_directory(self.directory)
        self.recover()
        # Names below are owned by this implementation, never user SQL paths.
        for pattern in ('sort-*.tmp', 'modify-*.tmp'):
            for path in self.directory.glob(pattern):
                path.unlink()
        for name in ('transaction.stage', 'transaction.idx', 'transaction.idx.resize', 'primary.idx.resize', 'checkpoint.wal'):
            (self.directory / name).unlink(missing_ok=True)

    def recover(self):
        self.wal.seek(0)
        if self.wal.read(len(MAGIC)) != MAGIC:
            raise RuntimeError('Unsupported WAL format')
        size = os.fstat(self.wal.fileno()).st_size
        position = len(MAGIC)
        while position + HEADER.size <= size:
            self.wal.seek(position)
            magic, length, checksum = HEADER.unpack(self.wal.read(HEADER.size))
            if magic != b'TXN1':
                raise RuntimeError('Invalid WAL transaction header')
            start = position + HEADER.size
            end = start + length
            if end + TRAILER.size > size:
                break
            self.wal.seek(end)
            trailer, repeated = TRAILER.unpack(self.wal.read(TRAILER.size))
            if trailer != b'DONE' or repeated != length:
                raise RuntimeError('Invalid WAL transaction footer')
            self.wal.seek(start)
            crc, remaining = 0, length
            while remaining:
                chunk = self.wal.read(min(65536, remaining))
                crc = zlib.crc32(chunk, crc)
                remaining -= len(chunk)
            if crc != checksum:
                raise RuntimeError('WAL checksum mismatch')
            for offset, record in records(self.wal, start, end):
                self._apply(record, offset, self.index)
                failpoint('during_recovery')
            position = end + TRAILER.size
        self.wal.truncate(position)
        self.wal.seek(position)
        os.fsync(self.wal.fileno())

    def _apply(self, record, offset, index):
        if record[0] == 'schema':
            self.schemas[record[1]] = record[2]
        else:
            _, table, key, row = record
            if index is self.index:
                old = index.get(table, key)
                if old is not None and old > 0:
                    self.wal.seek(old - 1)
                    self.live_bytes -= LENGTH.unpack(self.wal.read(LENGTH.size))[0] + LENGTH.size
                if row is not None:
                    self.wal.seek(offset)
                    self.live_bytes += LENGTH.unpack(self.wal.read(LENGTH.size))[0] + LENGTH.size
            index.put(table, key, offset + 1 if row is not None else -(offset + 1))

    def begin(self):
        if self.stage is not None:
            raise RuntimeError('Internal nested transaction')
        self.base_schemas = copy.deepcopy(self.schemas)
        self.stage = open(self.directory / 'transaction.stage', 'w+b', buffering=0)
        self.overlay = DiskHash(self.directory / 'transaction.idx')

    def savepoint(self):
        return self.stage.seek(0, os.SEEK_END), copy.deepcopy(self.schemas)

    def restore(self, savepoint):
        end, schemas = savepoint
        self.stage.truncate(end)
        self.schemas = schemas
        self.overlay.close()
        self.overlay = DiskHash(self.directory / 'transaction.idx')
        for offset, record in records(self.stage, 0, end):
            if record[0] == 'row':
                self._apply(record, offset, self.overlay)
        self.stage.seek(end)

    def append(self, record):
        self.stage.seek(0, os.SEEK_END)
        offset = write_record(self.stage, record)
        self._apply(record, offset, self.overlay)

    def commit(self):
        end = self.stage.seek(0, os.SEEK_END)
        self.stage.seek(0)
        crc = 0
        while chunk := self.stage.read(65536):
            crc = zlib.crc32(chunk, crc)
        self.wal.seek(0, os.SEEK_END)
        write_all(self.wal, HEADER.pack(b'TXN1', end, crc))
        start = self.wal.tell()
        self.stage.seek(0)
        while chunk := self.stage.read(65536):
            write_all(self.wal, chunk)
        failpoint('before_commit_footer')
        write_all(self.wal, TRAILER.pack(b'DONE', end))
        os.fsync(self.wal.fileno())
        failpoint('after_commit_sync')
        for offset, record in records(self.wal, start, start + end):
            self._apply(record, offset, self.index)
        self._finish()
        wal_bytes = os.fstat(self.wal.fileno()).st_size
        if wal_bytes >= self.checkpoint_bytes and wal_bytes > max(self.live_bytes * 3, 4096):
            self.checkpoint()

    def checkpoint(self):
        """Publish a compact committed snapshot with a single atomic rename.

        The old WAL remains authoritative until the completely synced replacement
        is renamed. This operation is forbidden while a transaction is staged.
        """
        if self.stage is not None:
            raise RuntimeError('Cannot checkpoint an active transaction')
        temporary = self.directory / 'checkpoint.wal'
        with open(temporary, 'w+b') as output:
            output.write(MAGIC)
            output.write(b'\0' * HEADER.size)
            start = output.tell()
            for name, schema in sorted(self.schemas.items()):
                write_record(output, ('schema', name, schema))
            for table, key, offset in self.index.entries():
                if offset > 0:
                    write_record(output, ('row', table, key, self.read_row(self.wal, offset - 1)))
            end = output.tell()
            output.seek(start)
            crc = 0
            while chunk := output.read(65536):
                crc = zlib.crc32(chunk, crc)
            output.seek(len(MAGIC))
            output.write(HEADER.pack(b'TXN1', end - start, crc))
            output.seek(end)
            output.write(TRAILER.pack(b'DONE', end - start))
            output.flush()
            os.fsync(output.fileno())
        failpoint('before_checkpoint_replace')
        self.wal.close()
        os.replace(temporary, self.directory / 'database.wal')
        sync_directory(self.directory)
        failpoint('after_checkpoint_replace')
        self.wal = open(self.directory / 'database.wal', 'r+b', buffering=0)
        self.index.close()
        self.index = DiskHash(self.directory / 'primary.idx')
        self.cache.clear()
        self.cache_bytes = self.live_bytes = 0
        self.schemas = {}
        self.recover()

    def rollback(self):
        self.schemas = self.base_schemas
        self._finish()

    def _finish(self):
        self.stage.close()
        self.overlay.close()
        self.stage = self.overlay = self.base_schemas = None
        for name in ('transaction.stage', 'transaction.idx'):
            (self.directory / name).unlink(missing_ok=True)

    def get(self, table, key):
        offset = self.overlay.get(table, key) if self.overlay is not None else None
        file = self.stage
        if offset is None:
            offset = self.index.get(table, key)
            file = self.wal
        if offset is None or offset < 0:
            return None
        return self.read_row(file, offset - 1)

    def read_row(self, file, offset):
        # WAL offsets identify immutable versions. Staging offsets can be reused
        # after a statement rollback, and must never enter this cache.
        if file is not self.wal or self.cache_limit == 0:
            return read_record(file, offset)[3]
        if offset in self.cache:
            self.cache.move_to_end(offset)
            return self.cache[offset][0]
        row = read_record(file, offset)[3]
        size = sys.getsizeof(row) + sum(sys.getsizeof(value) for value in row) + 256
        if size <= self.cache_limit:
            while self.cache and self.cache_bytes + size > self.cache_limit:
                _, (_, previous_size) = self.cache.popitem(last=False)
                self.cache_bytes -= previous_size
            self.cache[offset] = row, size
            self.cache_bytes += size
        return row

    def rows(self, table):
        for _, key, offset in self.index.entries(table):
            staged = self.overlay.get(table, key) if self.overlay is not None else None
            if staged is not None:
                if staged > 0:
                    yield self.read_row(self.stage, staged - 1)
            elif offset > 0:
                yield self.read_row(self.wal, offset - 1)
        if self.overlay is not None:
            for _, key, offset in self.overlay.entries(table):
                if offset > 0 and self.index.get(table, key) is None:
                    yield self.read_row(self.stage, offset - 1)

    def close(self):
        if self.stage is not None:
            self.rollback()
        self.index.close()
        self.wal.close()
