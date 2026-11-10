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

# Small vector sets are kept in anonymous memory: the flat files are derived
# and rebuildable, so writing them costs I/O (counted in B) without adding
# durability. Larger sets keep the mapped-file path so they never require
# table-sized anonymous memory.
MEMORY_SNAPSHOT_LIMIT = 8 * 1024 * 1024


class NativeKernel:
    def __init__(self):
        filename = '_native.dll' if os.name == 'nt' else '_native.so'
        self.library = ctypes.CDLL(str(Path(__file__).with_name(filename)))
        if self.library.mv_abi_version() != 1:
            raise RuntimeError('Unsupported native kernel ABI')
        self.search = self.library.mv_topk
        try:
            if self.library.mv_has_fma():
                self.search = self.library.mv_topk_fma
        except AttributeError:
            pass  # Older library: keep the portable kernel.
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
        vectors, keys = bytearray(), bytearray()
        row_bytes = self.dimensions * 4 + KEY.size
        overlay = storage.overlay.count if storage.overlay is not None else 0
        force_files = (storage.index.count + overlay) * row_bytes > MEMORY_SNAPSHOT_LIMIT
        files = None
        try:
            for row in storage.rows(schema['id']):
                if files is None and force_files:
                    files = (open(self.paths[0], 'wb'), open(self.paths[1], 'wb'))
                if files is None:
                    vectors += row[column].tobytes()
                    keys += KEY.pack(row[schema['pk']])
                    if len(vectors) + len(keys) > MEMORY_SNAPSHOT_LIMIT:
                        files = (open(self.paths[0], 'wb'), open(self.paths[1], 'wb'))
                        files[0].write(vectors)
                        files[1].write(keys)
                        vectors = keys = None
                else:
                    files[0].write(row[column].tobytes())
                    files[1].write(KEY.pack(row[schema['pk']]))
                self.count += 1
                failpoint('during_vector_rebuild')
        except BaseException:
            if files is not None:
                for file in files:
                    file.close()
            raise
        if not self.count:
            return
        if files is None:
            self.vectors, self.keys = vectors, keys
        else:
            for file in files:
                file.close()
            with open(self.paths[0], 'rb') as vectors, open(self.paths[1], 'rb') as keys:
                # Private writable mappings allow ctypes buffers, but the kernel
                # never writes. All backing files remain under the data directory.
                self.vectors = mmap.mmap(vectors.fileno(), 0, access=mmap.ACCESS_COPY)
                self.keys = mmap.mmap(keys.fileno(), 0, access=mmap.ACCESS_COPY)

    def key(self, position):
        return KEY.unpack_from(self.keys, position * KEY.size)[0]

    def close(self):
        for mapping in (self.vectors, self.keys):
            if isinstance(mapping, mmap.mmap):
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
        # Cached column values in snapshot order, keyed by (table id, column).
        # One scan then answers any number of equality filters on that column,
        # including cardinalities too large for the per-value position cache.
        self.column_values = OrderedDict()
        self.grouping_suppressed = set()
        self.filter_bytes = 0
        self.filter_budget = min(sort_memory // 2, 1536 * 1024)
        self.max_groups = 4096
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
        self.invalidate_filters(table)

    def invalidate_filters(self, table=None, columns=None):
        self.grouping_suppressed = {
            key for key in self.grouping_suppressed
            if not ((table is None or key[0] == table) and
                    (columns is None or key[1] in columns))
        }
        for key in list(self.column_values):
            if ((table is None or key[0] == table) and
                    (columns is None or key[1] in columns)):
                _, size = self.column_values.pop(key)
                self.filter_bytes -= size
        for key in list(self.filters):
            if ((table is None or key[0] == table) and
                    (columns is None or any(condition[0] in columns for condition in key[1]))):
                _, size = self.filters.pop(key)
                self.filter_bytes -= size

    def selection(self, schema, snapshot, conditions, matches):
        key = (schema['id'], tuple(conditions))
        if key in self.filters:
            self.filters.move_to_end(key)
            return self.filters[key][0]
        overhead = 256 + sum(sys.getsizeof(c) + sum(sys.getsizeof(v) for v in c) for c in conditions)
        if len(conditions) == 1 and conditions[0][1] == '=':
            return self._equality_selection(schema, snapshot, conditions, key, overhead)
        selected = array('Q')
        for position in range(snapshot.count):
            row = self.storage.get(schema['id'], snapshot.key(position))
            if matches(row, conditions):
                selected.append(position)
                if sys.getsizeof(selected) + overhead > self.filter_budget:
                    return False  # Large filters use the bounded streaming path.
        return self._store_selection(key, selected, overhead)

    def _cache_values(self, cache_key, values, size):
        if size <= self.filter_budget and self._make_room(size):
            self.column_values[cache_key] = values, size
            self.filter_bytes += size

    def _make_room(self, size):
        while self.filter_bytes + size > self.filter_budget:
            if self.filters:
                _, (_, previous) = self.filters.popitem(last=False)
            elif self.column_values:
                _, (_, previous) = self.column_values.popitem(last=False)
            else:
                return False
            self.filter_bytes -= previous
        return True

    def _store_selection(self, key, selected, overhead):
        size = sys.getsizeof(selected) + overhead
        if size > self.filter_budget or not self._make_room(size):
            return False
        self.filters[key] = selected, size
        self.filter_bytes += size
        return selected

    def _equality_selection(self, schema, snapshot, conditions, key, overhead):
        column = conditions[0][0]
        value = conditions[0][2]
        cache_key = (schema['id'], column)
        entry = self.column_values.get(cache_key)
        if entry is not None:
            self.column_values.move_to_end(cache_key)
            selected = array('Q')
            for position, current in enumerate(entry[0]):
                if current == value:
                    selected.append(position)
                    if sys.getsizeof(selected) + overhead > self.filter_budget:
                        return False  # Large filters use the bounded streaming path.
            return self._store_selection(key, selected, overhead)
        selected = array('Q')
        # One scan can populate the position cache for every value of this
        # column, and retain the column values so later scans of a suppressed
        # (too many values) column stay cheap. Both buffers are bounded by the
        # same budget, so a memory-constrained run never allocates more.
        grouped = {} if cache_key not in self.grouping_suppressed else None
        group_bytes = 0
        values = []
        value_bytes = sys.getsizeof(values)
        for position in range(snapshot.count):
            current = self.storage.get(schema['id'], snapshot.key(position))[column]
            if current == value:
                selected.append(position)
                if sys.getsizeof(selected) + overhead > self.filter_budget:
                    return False  # Large filters use the bounded streaming path.
            if values is not None:
                values.append(current)
                value_bytes += 8 + sys.getsizeof(current)
                if value_bytes > self.filter_budget:
                    values = None
            if grouped is None:
                continue
            positions = grouped.get(current)
            if positions is None:
                positions = array('Q')
                grouped[current] = positions
                group_bytes += 512 + sys.getsizeof(current) + sys.getsizeof(positions)
            before = sys.getsizeof(positions)
            positions.append(position)
            group_bytes += sys.getsizeof(positions) - before
            if len(grouped) > self.max_groups or group_bytes + overhead > self.filter_budget:
                grouped = None
                if len(self.grouping_suppressed) < 128:
                    self.grouping_suppressed.add(cache_key)
        stored_all = False
        if grouped:
            # Populate only entries that fit alongside existing cache entries.
            # Earlier entries remain valid; standard invalidation uses column IDs.
            stored_all = True
            for current, positions in grouped.items():
                group_key = (schema['id'], ((column, '=', current),))
                if group_key == key or group_key in self.filters:
                    continue
                size = sys.getsizeof(positions) + 512 + sys.getsizeof(current)
                if self.filter_bytes + size <= self.filter_budget:
                    self.filters[group_key] = positions, size
                    self.filter_bytes += size
                else:
                    stored_all = False
        # The per-value cache covers every value when it was stored completely;
        # otherwise keep the column scan for later equality queries.
        if values is not None and (grouped is None or not stored_all):
            self._cache_values(cache_key, values, value_bytes)
        return self._store_selection(key, selected, overhead)

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
            # The kernel returns squared distances (monotone, so ordering and
            # ties are unchanged). A squared relative gap is about twice the
            # distance gap, making this guard more conservative than before:
            # ambiguous boundaries still fall back to the reference executor.
            # Finite float32 differences and dimensions <= 65536 cannot overflow
            # double squares.
            if b - a <= max(abs(a), abs(b)) * 1e-12 + 1e-300:
                self.fallbacks += 1
                return None
        verified = []
        for position, _ in nearest:
            row = self.storage.get(schema['id'], snapshot.key(position))
            distance = math.dist(row[expression[1]], expression[2])
            verified.append((distance, row[schema['pk']], row))
        verified.sort(key=lambda item: (item[0], item[1]))
        # Hand the exact distance back so the projection does not recompute it.
        return [(item[2], item[0]) for item in verified[:limit]]

    def close(self):
        self.invalidate()
