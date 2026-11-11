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
from array import array
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


def large_memory_limit():
    """True when the cgroup allows an anonymous, rebuildable hash index."""
    try:
        text = Path('/sys/fs/cgroup/memory.max').read_text().strip()
    except OSError:
        return False
    if text == 'max':
        return True
    try:
        return int(text) >= 128 * 1024 * 1024
    except ValueError:
        return False


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
    def __init__(self, path, capacity=1024, anonymous=False):
        self.anonymous = anonymous
        self.path = None if anonymous else Path(path)
        self.capacity = capacity
        self.count = 0
        if anonymous:
            # Rebuildable index in anonymous memory: no file writes, and the WAL
            # remains the only authoritative state.
            self.file = None
            self.map = mmap.mmap(-1, capacity * SLOT.size)
        else:
            self.file = open(self.path, 'w+b')
            self.file.truncate(capacity * SLOT.size)
            self.map = mmap.mmap(self.file.fileno(), 0)
        # Little-endian hosts can probe slots through a cast view, avoiding a
        # tuple per probe. Big-endian hosts keep the portable struct path.
        self.view = memoryview(self.map).cast('q') if sys.byteorder == 'little' else None
        # Keys never leave the hash (deletes keep their slot with a negative
        # offset), so each table's key extent only grows. Maintained on insert,
        # it bounds primary-key range probes without a slot scan.
        self.extents = {}

    def _slot(self, table, key):
        slot = hash_key(table, key) & (self.capacity - 1)
        if self.view is not None:
            view = self.view
            base = slot * 3
            for _ in range(self.capacity):
                t = view[base]
                if t == 0 or (t == table and view[base + 1] == key):
                    return slot, t, view[base + 2]
                slot = (slot + 1) & (self.capacity - 1)
                base = slot * 3
            raise RuntimeError('Hash table is full')
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
        if not t:
            self._widen(table, key)
        self.count += int(not t)

    def replace(self, table, key, value):
        """Write one slot and return the previous value, probing only once."""
        slot, t, old = self._slot(table, key)
        if not t and (self.count + 1) * 10 > self.capacity * 6:
            self._grow()
            slot, t, old = self._slot(table, key)
        SLOT.pack_into(self.map, slot * SLOT.size, table, key, value)
        if not t:
            self._widen(table, key)
        self.count += int(not t)
        return old if t else None

    def _widen(self, table, key):
        low, high = self.extents.get(table, (None, None))
        if low is None or key < low:
            low = key
        if high is None or key > high:
            high = key
        self.extents[table] = low, high

    def entries(self, table=None):
        for i in range(self.capacity):
            t, key, value = SLOT.unpack_from(self.map, i * SLOT.size)
            if t and (table is None or table == t):
                yield t, key, value

    def key_extent(self, table):
        """Smallest and largest key of a table, or (None, None) if empty.

        Maintained incrementally on insert; keys never leave the hash (a
        delete keeps its slot with a negative offset), so the cached extent
        only ever grows and stays valid.
        """
        return self.extents.get(table, (None, None))

    def _grow(self):
        replacement = DiskHash(None if self.anonymous else str(self.path) + '.resize',
                               self.capacity * 2, anonymous=self.anonymous)
        for table, key, value in self.entries():
            replacement.put(table, key, value)
        self.close()
        if self.anonymous:
            # Take ownership of the replacement map instead of reopening a file.
            self.map, self.view = replacement.map, replacement.view
            replacement.map = replacement.view = None
            self.capacity, self.count = replacement.capacity, replacement.count
            self.extents = replacement.extents
            return
        replacement.close()
        os.replace(replacement.path, self.path)
        self.capacity = replacement.capacity
        self.count = replacement.count
        self.extents = replacement.extents
        self.file = open(self.path, 'r+b')
        self.map = mmap.mmap(self.file.fileno(), 0)
        self.view = memoryview(self.map).cast('q') if sys.byteorder == 'little' else None

    def close(self):
        if self.view is not None:
            self.view.release()
            self.view = None
        if self.map is not None:
            self.map.close()
            self.map = None
        if self.file is not None:
            self.file.close()
            self.file = None

    def reset(self):
        """Empty the hash for reuse: zero the slots, keep the mapping.

        The table is empty when every slot's table id is zero; extents and
        count follow. Grown capacity is retained so a later transaction that
        needed it does not regrow.
        """
        self.map[:] = bytes(len(self.map))
        self.count = 0
        self.extents = {}


def _encode_row(record):
    """Compact row encoding: flag, table, key, then one tagged value each.

    The type byte lets read_record tell this apart from the older pickled
    records (which start with the pickle PROTO byte) without a WAL version bump.
    Rows only ever hold int/str/array/None by construction (convert() rejects
    every other type), so exact type() dispatch is safe and faster than the
    isinstance chain; the on-disk bytes are identical.
    """
    _, table, key, row = record
    parts = [b'\x00' if row is None else b'\x01', struct.pack('<qq', table, key)]
    if row is not None:
        parts.append(struct.pack('<I', len(row)))
        for value in row:
            kind = type(value)
            if kind is int:
                parts.append(b'\x01' + struct.pack('<q', value))
            elif kind is str:
                encoded = value.encode('utf-8')
                parts.append(b'\x02' + struct.pack('<I', len(encoded)) + encoded)
            elif kind is array:
                parts.append(b'\x03' + struct.pack('<I', len(value)) + value.tobytes())
            elif value is None:
                parts.append(b'\x00')
            else:
                raise TypeError('Unsupported row value')
    return b''.join(parts)


def _decode_row(data):
    table, key = struct.unpack_from('<qq', data, 2)
    if data[1] == 0:
        return ('row', table, key, None)
    count, = struct.unpack_from('<I', data, 18)
    offset = 22
    values = []
    for _ in range(count):
        kind = data[offset]
        offset += 1
        if kind == 0:
            values.append(None)
        elif kind == 1:
            value, = struct.unpack_from('<q', data, offset)
            offset += 8
            values.append(value)
        elif kind == 2:
            length, = struct.unpack_from('<I', data, offset)
            offset += 4
            values.append(data[offset:offset + length].decode('utf-8'))
            offset += length
        elif kind == 3:
            length, = struct.unpack_from('<I', data, offset)
            offset += 4
            values.append(array('f', data[offset:offset + 4 * length]))
            offset += 4 * length
        else:
            raise RuntimeError('Invalid row value type')
    return ('row', table, key, tuple(values))


def write_record(file, value):
    # Large strings are cheaper through the C pickle path; vector rows (and
    # deletes) use the compact tagged encoding.
    if value[0] == 'row' and (value[3] is None or
                              not any(isinstance(item, str) and len(item) > 256
                                      for item in value[3])):
        data = b'\x01' + _encode_row(value)
    else:
        data = b'\x00' + pickle.dumps(value, protocol=5)
    position = file.tell()
    # One write for length+payload halves the syscall count per record; the
    # on-disk bytes are identical to the previous two-write form.
    write_all(file, LENGTH.pack(len(data)) + data)
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
    kind = data[:1]
    if kind == b'\x01':
        return _decode_row(data)
    if kind == b'\x00':
        return pickle.loads(data[1:])
    return pickle.loads(data)  # Older pickled record (starts with PROTO).


def records(file, start, end):
    position = start
    while position < end:
        record = read_record(file, position)
        next_position = file.tell()
        if next_position > end:
            raise RuntimeError('Record crosses transaction boundary')
        # The serialized payload length lets _apply update live_bytes without
        # seeking back to the record header.
        yield position, record, next_position - position - LENGTH.size
        position = next_position


RECORD_BLOCK = 1 << 20


def records_buffered(file, start, end):
    """Sequential record scan with block reads instead of two reads per record.

    Yields exactly the same (offset, record, payload-length) tuples as records();
    only the read granularity differs. Random-access callers (read_row) keep the
    unbuffered path so the WAL file position stays predictable for them.
    """
    position = start
    block = b''
    base = start  # File offset of block[0].
    while position < end:
        # Refill whenever the block cannot even hold the next length header; a
        # short read at the transaction end is fine because end bounds the scan.
        if position + LENGTH.size > base + len(block):
            file.seek(position)
            block = file.read(min(RECORD_BLOCK, end - position))
            base = position
            if len(block) < LENGTH.size:
                raise RuntimeError('Truncated record length')
        offset_in_block = position - base
        length, = LENGTH.unpack_from(block, offset_in_block)
        payload_start = position + LENGTH.size
        crossing = payload_start + length > end
        if payload_start + length > base + len(block):
            file.seek(payload_start)
            payload = file.read(length)
            if len(payload) != length:
                raise RuntimeError('Truncated record')
        else:
            payload = block[offset_in_block + LENGTH.size:
                            offset_in_block + LENGTH.size + length]
        if crossing:
            # Match the unbuffered order: decode first (which may raise on a
            # malformed payload exactly like read_record), then reject.
            kind = payload[:1]
            if kind == b'\x01':
                _decode_row(payload)
            elif kind == b'\x00':
                pickle.loads(payload[1:])
            else:
                pickle.loads(payload)
            raise RuntimeError('Record crosses transaction boundary')
        kind = payload[:1]
        if kind == b'\x01':
            record = _decode_row(payload)
        elif kind == b'\x00':
            record = pickle.loads(payload[1:])
        else:
            record = pickle.loads(payload)  # Older pickled record (starts with PROTO).
        yield position, record, length
        position = payload_start + length


class Storage:
    def __init__(self, directory, cache_memory=16 * 1024 * 1024, checkpoint_bytes=64 * 1024 * 1024):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.schemas = {}
        self.base_schemas = None
        self.stage = None
        self.overlay = None
        # True while a transaction is staged; the files stay open across
        # transactions for reuse, so the stage handle alone cannot signal this.
        self.active = False
        self.cache_limit = cache_memory
        self.cache_bytes = 0
        self.cache = OrderedDict()
        self.checkpoint_bytes = checkpoint_bytes
        self.live_bytes = 0
        self.index_anonymous = large_memory_limit()
        self.index = DiskHash(self.directory / 'primary.idx', anonymous=self.index_anonymous)
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
        leftovers = ['transaction.stage', 'transaction.idx', 'transaction.idx.resize',
                     'primary.idx.resize', 'checkpoint.wal']
        if self.index_anonymous:
            leftovers.append('primary.idx')  # stale file from a file-backed run
        for name in leftovers:
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
            for offset, record, length in records_buffered(self.wal, start, end):
                self._apply(record, offset, self.index, length)
                failpoint('during_recovery')
            position = end + TRAILER.size
        self.wal.truncate(position)
        self.wal.seek(position)
        os.fsync(self.wal.fileno())

    def _apply(self, record, offset, index, length=None):
        if record[0] == 'schema':
            self.schemas[record[1]] = record[2]
        else:
            _, table, key, row = record
            value = offset + 1 if row is not None else -(offset + 1)
            if index is self.index:
                old = index.replace(table, key, value)
                if old is not None and old > 0:
                    self.wal.seek(old - 1)
                    self.live_bytes -= LENGTH.unpack(self.wal.read(LENGTH.size))[0] + LENGTH.size
                if row is not None:
                    if length is None:
                        self.wal.seek(offset)
                        length = LENGTH.unpack(self.wal.read(LENGTH.size))[0]
                    self.live_bytes += length + LENGTH.size
            else:
                index.put(table, key, value)

    def begin(self):
        if self.active:
            raise RuntimeError('Internal nested transaction')
        self.base_schemas = copy.deepcopy(self.schemas)
        self.active = True
        if self.stage is None:
            # The stage is scratch: never authoritative, never synced, and
            # startup unlinks any crash leftover. Keep one file open across
            # transactions instead of paying open/close/unlink per statement.
            self.stage = open(self.directory / 'transaction.stage', 'w+b', buffering=0)
        else:
            self.stage.truncate(0)
            self.stage.seek(0)
        if self.overlay is None:
            self.overlay = DiskHash(self.directory / 'transaction.idx')
        else:
            self.overlay.reset()

    def savepoint(self):
        return self.stage.seek(0, os.SEEK_END), copy.deepcopy(self.schemas)

    def restore(self, savepoint):
        end, schemas = savepoint
        self.stage.truncate(end)
        self.schemas = schemas
        self.overlay.reset()
        for offset, record, _ in records_buffered(self.stage, 0, end):
            if record[0] == 'row':
                self._apply(record, offset, self.overlay)
        self.stage.seek(end)

    def append(self, record):
        self.stage.seek(0, os.SEEK_END)
        offset = write_record(self.stage, record)
        self._apply(record, offset, self.overlay)

    def commit(self):
        end = self.stage.seek(0, os.SEEK_END)
        self.wal.seek(0, os.SEEK_END)
        header_position = self.wal.tell()
        # Write a placeholder checksum, copy the stage while computing the real
        # one, then publish it before the footer. A crash before the footer
        # leaves an incomplete transaction that recovery truncates; a complete
        # transaction always carries its final checksum.
        write_all(self.wal, HEADER.pack(b'TXN1', end, 0))
        start = self.wal.tell()
        self.stage.seek(0)
        crc = 0
        while chunk := self.stage.read(65536):
            crc = zlib.crc32(chunk, crc)
            write_all(self.wal, chunk)
        self.wal.seek(header_position)
        write_all(self.wal, HEADER.pack(b'TXN1', end, crc))
        self.wal.seek(start + end)
        failpoint('before_commit_footer')
        write_all(self.wal, TRAILER.pack(b'DONE', end))
        os.fsync(self.wal.fileno())
        failpoint('after_commit_sync')
        for offset, record, length in records_buffered(self.wal, start, start + end):
            self._apply(record, offset, self.index, length)
        self._finish()
        wal_bytes = os.fstat(self.wal.fileno()).st_size
        if wal_bytes >= self.checkpoint_bytes and wal_bytes > max(self.live_bytes * 3, 4096):
            self.checkpoint()

    def checkpoint(self):
        """Publish a compact committed snapshot with a single atomic rename.

        The old WAL remains authoritative until the completely synced replacement
        is renamed. This operation is forbidden while a transaction is staged.
        """
        if self.active:
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
        self.index = DiskHash(self.directory / 'primary.idx', anonymous=self.index_anonymous)
        self.cache.clear()
        self.cache_bytes = self.live_bytes = 0
        self.schemas = {}
        self.recover()

    def rollback(self):
        self.schemas = self.base_schemas
        self._finish()

    def _finish(self):
        # Keep the stage file and overlay mapping open for the next begin();
        # both are scratch state, never authoritative. Clear the overlay now
        # so no reader can see this transaction's staged entries afterwards;
        # begin() resets both again. Storage.close() releases the files, and
        # startup unlinks crash leftovers.
        self.active = False
        self.base_schemas = None
        if self.overlay is not None:
            self.overlay.reset()

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
        if self.active:
            self.rollback()
        if self.stage is not None:
            self.stage.close()
            self.stage = None
        if self.overlay is not None:
            self.overlay.close()
            self.overlay = None
        for name in ('transaction.stage', 'transaction.idx'):
            (self.directory / name).unlink(missing_ok=True)
        self.index.close()
        self.wal.close()
