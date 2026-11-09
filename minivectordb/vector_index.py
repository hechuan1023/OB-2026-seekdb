"""Rebuildable disk flat index plus an optional self-written native exact kernel.

Vectors and primary keys are mapped files under the data directory. Filters cache
candidate positions (never query answers), with a bounded total allocation budget.
"""
import ctypes
import math
import mmap
import os
import struct
import sys
from array import array
from collections import OrderedDict
from pathlib import Path

from .storage import failpoint

KEY = struct.Struct('<q')


class NativeKernel:
    def __init__(self):
        filename = '_native.dll' if os.name == 'nt' else '_native.so'
        self.library = ctypes.CDLL(str(Path(__file__).with_name(filename)))
        if self.library.mv_abi_version() != 1:
            raise RuntimeError('Unsupported native kernel ABI')
        self.search = self.library.mv_topk
        self.search.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64,
                               ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p]
        self.search.restype = ctypes.c_int64

    @staticmethod
    def address(buffer):
        # No exported view survives the synchronous C call; the mmap itself is
        # owned by Snapshot throughout the call and cannot be concurrently closed.
        return ctypes.addressof(ctypes.c_char.from_buffer(buffer))

    def topk(self, snapshot, query, selected, keep):
        count = snapshot.count if selected is None else len(selected)
        keep = min(keep, count)
        if not keep:
            return []
        query = array('f', query)
        positions = array('Q', [0]) * keep
        distances = array('d', [0]) * keep
        returned = self.search(self.address(snapshot.vectors), self.address(snapshot.keys),
                               snapshot.count, snapshot.dimensions, query.buffer_info()[0],
                               selected.buffer_info()[0] if selected is not None else None,
                               count, keep, positions.buffer_info()[0], distances.buffer_info()[0])
        if returned != keep:
            raise RuntimeError('Native exact search failed')
        return list(zip(positions, distances))


class Snapshot:
    def __init__(self, storage, schema, column):
        self.dimensions = schema['columns'][column][2]
        self.paths = [storage.directory / f'flat-{schema["id"]}.{extension}' for extension in ('vec', 'keys')]
        self.vectors = self.keys = None
        self.count = 0
        with open(self.paths[0], 'wb') as vectors, open(self.paths[1], 'wb') as keys:
            for row in storage.rows(schema['id']):
                vectors.write(row[column].tobytes())
                keys.write(KEY.pack(row[schema['pk']]))
                self.count += 1
                failpoint('during_vector_rebuild')
        if self.count:
            with open(self.paths[0], 'rb') as vectors, open(self.paths[1], 'rb') as keys:
                # Private writable mappings allow ctypes buffers, but the kernel
                # never writes. All backing files remain under the data directory.
                self.vectors = mmap.mmap(vectors.fileno(), 0, access=mmap.ACCESS_COPY)
                self.keys = mmap.mmap(keys.fileno(), 0, access=mmap.ACCESS_COPY)

    def key(self, position):
        return KEY.unpack_from(self.keys, position * KEY.size)[0]

    def close(self):
        for mapping in (self.vectors, self.keys):
            if mapping is not None:
                mapping.close()
        for path in self.paths:
            path.unlink(missing_ok=True)


class VectorIndexes:
    def __init__(self, storage, sort_memory, enabled=True):
        self.storage = storage
        self.kernel = None
        if enabled:
            try:
                self.kernel = NativeKernel()
            except OSError:
                pass  # Portable Python path remains fully functional.
        self.entries = {}
        self.filters = OrderedDict()
        self.filter_bytes = 0
        self.filter_budget = min(sort_memory // 2, 1024 * 1024)
        self.max_keep = sort_memory // 512
        self.fallbacks = 0
        self.searches = 0
        for pattern in ('flat-*.vec', 'flat-*.keys'):
            for path in storage.directory.glob(pattern):
                path.unlink()
        for schema in storage.schemas.values():
            self.ensure(schema)

    def ensure(self, schema):
        if self.kernel is None or not schema['indexes']:
            return None
        column = next(i for i, col in enumerate(schema['columns']) if col[1] == 'VECTOR')
        if schema['columns'][column][2] > 65536:
            return None
        table = schema['id']
        if table not in self.entries:
            self.entries[table] = Snapshot(self.storage, schema, column)
        return self.entries[table]

    def invalidate(self, table=None):
        for identifier in list(self.entries):
            if table is None or table == identifier:
                self.entries.pop(identifier).close()
        for key in list(self.filters):
            if table is None or key[0] == table:
                _, size = self.filters.pop(key)
                self.filter_bytes -= size

    def selection(self, schema, snapshot, conditions, matches):
        key = (schema['id'], tuple(conditions))
        if key in self.filters:
            self.filters.move_to_end(key)
            return self.filters[key][0]
        selected = array('Q')
        overhead = 256 + sum(sys.getsizeof(c) + sum(sys.getsizeof(v) for v in c) for c in conditions)
        for position in range(snapshot.count):
            row = self.storage.get(schema['id'], snapshot.key(position))
            if matches(row, conditions):
                selected.append(position)
                if sys.getsizeof(selected) + overhead > self.filter_budget:
                    return False  # Large filters use the bounded streaming path.
        size = sys.getsizeof(selected) + overhead
        if size > self.filter_budget:
            return False
        while self.filters and self.filter_bytes + size > self.filter_budget:
            _, (_, previous) = self.filters.popitem(last=False)
            self.filter_bytes -= previous
        self.filters[key] = selected, size
        self.filter_bytes += size
        return selected

    def topk(self, schema, expression, conditions, limit, matches):
        if limit + 1 > self.max_keep:
            return None
        snapshot = self.ensure(schema)
        if snapshot is None:
            return None
        if not snapshot.count:
            return []
        selected = self.selection(schema, snapshot, conditions, matches) if conditions else None
        if selected is False:
            return None
        nearest = self.kernel.topk(snapshot, expression[2], selected, limit + 1)
        self.searches += 1
        if len(nearest) > limit:
            a, b = nearest[limit - 1][1], nearest[limit][1]
            # Finite float32 differences and dimensions <= 65536 cannot overflow
            # double squares. This guard is deliberately much wider than the
            # compensated-sum rounding bound; ties use the reference executor.
            if b - a <= max(abs(a), abs(b)) * 1e-12 + 1e-300:
                self.fallbacks += 1
                return None
        verified = []
        for position, _ in nearest:
            row = self.storage.get(schema['id'], snapshot.key(position))
            distance = math.dist(row[expression[1]], expression[2])
            verified.append((distance, row[schema['pk']], row))
        verified.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in verified[:limit]]

    def close(self):
        self.invalidate()
