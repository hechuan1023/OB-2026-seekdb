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

from .native import distances as native_distances
from .storage import failpoint

KEY = struct.Struct('<q')

# Small vector sets are kept in anonymous memory: the flat files are derived
# and rebuildable, so writing them costs I/O (counted in B) without adding
# durability. Larger sets keep the mapped-file path so they never require
# table-sized anonymous memory.
MEMORY_SNAPSHOT_LIMIT = 8 * 1024 * 1024

# Placeholder for a deleted row in the cached column-value list, so list
# indices stay aligned with snapshot positions.
DELETED = object()

# Float32 candidate selection is only trusted when the (keep+margin)-th
# squared distance is clearly larger than the keep-th. The per-dimension
# coefficient is twice the float32 unit roundoff (subtraction and product each
# round once); the absolute term covers subnormal underflow. The observed
# random-data boundary ratio for 128 dimensions is above 1.01.
F32_MARGIN = 16
F32_ROUNDING = 2.4e-7
F32_SUBNORMAL = 6e-45


class NativeKernel:
    def __init__(self):
        filename = '_native.dll' if os.name == 'nt' else '_native.so'
        self.library = ctypes.CDLL(str(Path(__file__).with_name(filename)))
        if self.library.mv_abi_version() != 1:
            raise RuntimeError('Unsupported native kernel ABI')
        self.search = self.library.mv_topk
        self.search_f32 = None
        try:
            if self.library.mv_has_fma():
                self.search = self.library.mv_topk_fma
                # Candidate-only kernel: float32 distances, exact re-check later.
                self.search_f32 = self.library.mv_topk_f32
        except AttributeError:
            pass  # Older library: keep the portable kernel.
        self.search.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64,
                               ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p]
        self.search.restype = ctypes.c_int64
        if self.search_f32 is not None:
            self.search_f32.argtypes = self.search.argtypes
            self.search_f32.restype = ctypes.c_int64

    @staticmethod
    def address(buffer):
        # No exported view survives the synchronous C call; the mmap itself is
        # owned by Snapshot throughout the call and cannot be concurrently closed.
        return ctypes.addressof(ctypes.c_char.from_buffer(buffer))

    def topk(self, snapshot, query, selected, keep):
        return self._run(self.search, snapshot, query, selected, keep)

    def topk_f32(self, snapshot, query, selected, keep):
        """Float32 candidate selection; distances are squared and approximate."""
        return self._run(self.search_f32, snapshot, query, selected, keep)

    def _run(self, search, snapshot, query, selected, keep):
        count = snapshot.count if selected is None else len(selected)
        keep = min(keep, count)
        if not keep:
            return []
        if type(query) is not array:
            query = array('f', query)
        positions = array('Q', [0]) * keep
        distances = array('d', [0]) * keep
        returned = search(self.address(snapshot.vectors), self.address(snapshot.keys),
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
        # Lazily created cast views: re-evaluating a candidate then costs one
        # slice instead of building three memoryview objects per row.
        self._flat = None
        self._key_view = None
        self.count = 0
        self.deleted = set()
        vectors, keys = bytearray(), bytearray()
        row_bytes = self.dimensions * 4 + KEY.size
        overlay = storage.overlay.count if storage.overlay is not None else 0
        force_files = (storage.index.count + overlay) * row_bytes > MEMORY_SNAPSHOT_LIMIT
        files = None
        try:
            source = (storage.snapshot_rows(schema['id']) if force_files
                      else storage.rows(schema['id']))
            for row in source:
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

    def difference_update(self, keys):
        self.deleted.difference_update(keys)

    def _views(self):
        if self._flat is None:
            self._flat = memoryview(self.vectors).cast('f')
            # Big-endian hosts keep the portable struct path for keys.
            self._key_view = (memoryview(self.keys).cast('q')
                              if sys.byteorder == 'little' else None)
        return self._flat

    def key(self, position):
        self._views()
        if self._key_view is not None:
            return self._key_view[position]
        return KEY.unpack_from(self.keys, position * KEY.size)[0]

    def vector(self, position):
        flat = self._views()
        start = position * self.dimensions
        return flat[start:start + self.dimensions]

    def patch(self, rows, column):
        """Overwrite the vectors of updated rows without rebuilding the snapshot.

        A vector-only UPDATE keeps row membership and hash slots, so snapshot
        positions are stable. One key scan patches a whole statement's rows.
        """
        if not self.count or not rows:
            return
        view = memoryview(self.keys).cast('q') if sys.byteorder == 'little' else None
        width = self.dimensions * 4
        for position in range(self.count):
            key = (view[position] if view is not None
                   else KEY.unpack_from(self.keys, position * KEY.size)[0])
            row = rows.get(key)
            if row is not None:
                start = position * width
                self.vectors[start:start + width] = row[column].tobytes()

    def close(self):
        # Release exported views before closing a mapped file.
        for view in (self._flat, self._key_view):
            if view is not None:
                view.release()
        self._flat = self._key_view = None
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

    def patch(self, table, column, rows):
        """Apply vector-only updates to an existing snapshot in place."""
        snapshot = self.entries.get(table)
        if snapshot is not None:
            snapshot.patch(rows, column)

    def delete_keys(self, table, keys):
        """Mark deleted rows so queries skip them without a full rebuild.

        Cached filters hold positions, so they must not outlive a delete.
        Returns the snapshot the tombstones landed on, or None if the table
        has no snapshot (or it was rebuilt past the tombstone threshold).
        """
        snapshot = self.entries.get(table)
        if snapshot is None or not keys:
            self.invalidate_filters(table)
            return None
        snapshot.deleted.update(keys)
        self.invalidate_filters(table)
        if len(snapshot.deleted) * 2 > snapshot.count:
            self.invalidate(table)  # Tombstones outnumber live rows: rebuild.
            return None
        return snapshot

    def restore_keys(self, table, keys, snapshot):
        """Undo tombstones after a rollback, keeping the snapshot when safe.

        The snapshot may have been rebuilt during the transaction (tombstone
        threshold, INSERT membership change) and then no longer contains the
        deleted rows; only the snapshot the deletes landed on can un-mark them.
        Filter caches are always dropped: caches built during the transaction
        exclude the deleted rows and go stale once the rows return.
        """
        self.invalidate_filters(table)
        if not keys:
            return
        if snapshot is not None and self.entries.get(table) is snapshot:
            snapshot.difference_update(keys)
            if len(snapshot.deleted) * 2 > snapshot.count:
                self.invalidate(table)
        else:
            self.invalidate(table)

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
        # Primary keys are already stored alongside the vectors.  Filtering
        # only on that key must not reread every full row (and its vector),
        # especially when the working set is larger than the page cache.
        if (conditions[0][0] == schema['pk'] and
                all(column == schema['pk'] for column, _, _ in conditions)):
            selected = array('Q')
            for position in range(snapshot.count):
                current = snapshot.key(position)
                if current in snapshot.deleted:
                    continue
                for _, op, value in conditions:
                    if ((op == '=' and current != value) or
                            (op == '<' and not current < value) or
                            (op == '<=' and not current <= value) or
                            (op == '>' and not current > value) or
                            (op == '>=' and not current >= value)):
                        break
                else:
                    selected.append(position)
                    if sys.getsizeof(selected) + overhead > self.filter_budget:
                        return False
            return self._store_selection(key, selected, overhead)
        if len(conditions) == 1 and conditions[0][1] == '=':
            return self._equality_selection(schema, snapshot, conditions, key, overhead)
        selected = array('Q')
        for position in range(snapshot.count):
            row = self.storage.get(schema['id'], snapshot.key(position))
            if row is None:
                continue  # deleted row
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
            row = self.storage.get(schema['id'], snapshot.key(position))
            current = DELETED if row is None else row[column]
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

    def topk(self, schema, expression, conditions, limit, matches, pk_only=False):
        if limit + 1 > self.max_keep:
            return None
        snapshot = self.ensure(schema)
        if snapshot is None:
            return None
        deleted = snapshot.deleted
        if snapshot.count - len(deleted) <= 0:
            return []
        selected = self.selection(schema, snapshot, conditions, matches) if conditions else None
        if selected is False:
            return None
        # The filter scan already excludes deleted rows; an unfiltered scan may
        # return them, so fetch enough extra candidates to skip the tombstones.
        keep = limit + 1 + (len(deleted) if selected is None else 0)
        if keep > self.max_keep:
            return None
        available = snapshot.count if selected is None else len(selected)
        nearest = None
        if self.kernel.search_f32 is not None:
            # Float32 selection is a candidate filter, not the answer. The
            # margin below proves that no row outside the returned set can
            # enter the exact top `keep`; otherwise fall back to the exact
            # double kernel. Only candidates inside the uncertainty band of
            # the keep-th squared distance need exact re-evaluation.
            requested = min(keep + F32_MARGIN, available)
            candidates = self.kernel.topk_f32(snapshot, expression[2], selected, requested)
            if requested >= available:
                nearest = candidates  # Every row was already a candidate.
            else:
                dimensions = snapshot.dimensions
                error = min(0.25, (dimensions + 1) * F32_ROUNDING)
                slack = (dimensions + 1) * F32_SUBNORMAL
                threshold = candidates[keep - 1][1]
                if candidates[-1][1] > threshold * (1.0 + 4.0 * error) + 4.0 * slack:
                    bound = threshold * (1.0 + 2.0 * error) + 2.0 * slack
                    count = keep
                    while count < len(candidates) and candidates[count][1] <= bound:
                        count += 1
                    nearest = candidates[:count]
        if nearest is None:
            nearest = self.kernel.topk(snapshot, expression[2], selected, keep)
        self.searches += 1
        verified = []
        # One native call re-evaluates every candidate in double precision; the
        # generic executor uses the same loop, so both paths agree bit for bit.
        candidates = array('Q', [position for position, _ in nearest])
        exact = native_distances(self.kernel.address(snapshot.vectors), expression[2], candidates)
        for index, (position, _) in enumerate(nearest):
            key = snapshot.key(position)
            if key in deleted:
                continue
            if pk_only:
                # The projection only needs the primary key and the distance:
                # read both from the snapshot instead of probing the row index.
                distance = exact[index] if exact is not None else math.dist(
                    snapshot.vector(position), expression[2])
                verified.append((distance, key, key))
            else:
                row = self.storage.get(schema['id'], key)
                distance = exact[index] if exact is not None else math.dist(
                    row[expression[1]], expression[2])
                verified.append((distance, row[schema['pk']], row))
        verified.sort(key=lambda item: (item[0], item[1]))
        if len(verified) > limit:
            a, b = verified[limit - 1][0], verified[limit][0]
            # Exact-distance boundary guard; ambiguous ties fall back to the
            # reference executor.
            if b - a <= max(abs(a), abs(b)) * 1e-12 + 1e-300:
                self.fallbacks += 1
                return None
        # Hand the exact distance back so the projection does not recompute it.
        return [(item[2], item[0]) for item in verified[:limit]]

    def close(self):
        self.invalidate()
