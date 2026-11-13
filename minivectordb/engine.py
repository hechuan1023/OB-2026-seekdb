"""SQL execution over the disk store. This module has no stdout protocol logic."""
import copy
import json
import math
import os
import re
import struct
import tempfile
from array import array
from dataclasses import dataclass

from .errors import DBError, require
from .native import distance as native_distance
from .native import parse_vector, parse_vectors
from .sorting import Descending, Sorter, file_rows
from .sql import parse
from .storage import Storage, write_record
from .vector_index import VectorIndexes


def convert(literal, column, native=True):
    name, typ, size = column
    if typ == 'BIGINT':
        text = literal.value
        digits = text[1:] if text[:1] in '+-' else text
        # isdecimal() matches the same Unicode decimal digits as \d but avoids
        # a regex call per inserted value.
        require(literal.kind == 'number' and digits.isdecimal(), 'TYPE_ERROR', f'{name} requires BIGINT')
        digits = digits.lstrip('0') or '0'
        require(len(digits) <= 19, 'TYPE_ERROR', 'BIGINT overflow')
        value = int(('-' if text.startswith('-') else '') + digits)
        require(-(1 << 63) <= value < (1 << 63), 'TYPE_ERROR', 'BIGINT overflow')
        return value
    require(literal.kind == 'string', 'TYPE_ERROR', f'{name} requires a string literal')
    if typ == 'VARCHAR':
        require(len(literal.value) <= size, 'TYPE_ERROR', f'{name} exceeds VARCHAR({size})')
        return literal.value
    try:
        # The native path accepts only the JSON number grammar and returns None
        # for everything else, so the Python path below keeps the exact
        # accepted language and error messages.
        result = parse_vector(literal.value, size) if native else None
        if result is not None:
            return result
        values = json.loads(literal.value)
        if not isinstance(values, list) or len(values) != size:
            raise ValueError('Vector dimension mismatch')
        # set membership via map avoids a generator frame per element; bool is
        # rejected because type(True) is bool, not int.
        if not all(map(_VECTOR_NUMERIC_TYPES.__contains__, map(type, values))):
            raise ValueError('Vector values must be finite numbers')
        result = array('f', values)  # float64-to-float32 溢出在此抛 OverflowError
        if not all(map(math.isfinite, result)):  # NaN/Infinity 转换后仍为非有限值
            raise ValueError('float32 overflow')
        return result
    except (ValueError, OverflowError, TypeError, RecursionError, struct.error) as exc:
        raise DBError('INVALID_VECTOR', str(exc)) from exc


@dataclass
class QueryResult:
    columns: list
    rows: object


_VECTOR_NUMERIC_TYPES = frozenset({int, float})


class Engine:
    # UPDATE/DELETE statements matching at most this many rows buffer their
    # candidate rows in memory instead of a temp file; larger matches spill.
    _MODIFY_BUFFER = 4096

    def __init__(self, directory, sort_memory=4 * 1024 * 1024, mode='exact', cache_memory=16 * 1024 * 1024,
                 checkpoint_bytes=64 * 1024 * 1024, native=True):
        require(mode in ('exact', 'ann'), 'UNSUPPORTED', 'Unknown search mode')
        require(sort_memory >= 4096, 'TYPE_ERROR', 'Sort memory must be at least 4096 bytes')
        require(cache_memory >= 0, 'TYPE_ERROR', 'Cache memory must be nonnegative')
        require(checkpoint_bytes >= 4096, 'TYPE_ERROR', 'Checkpoint threshold must be at least 4096 bytes')
        self.storage = Storage(directory, cache_memory, checkpoint_bytes)
        self.explicit = False
        self.transaction_changes = {}
        self.statement_changes = {}
        self.statement_vector_patches = {}
        self.transaction_deletes = {}
        self.statement_deletes = {}
        self.sort_memory = sort_memory
        self.mode = mode  # Both modes currently use the exact flat search path.
        self.native = native
        self.vector_indexes = VectorIndexes(self.storage, sort_memory, native)

    def close(self):
        self.vector_indexes.close()
        self.storage.close()

    def table(self, name):
        require(name in self.storage.schemas, 'NOT_FOUND', f'Unknown table {name}')
        return self.storage.schemas[name]

    def column(self, schema, name):
        for i, col in enumerate(schema['columns']):
            if col[0] == name:
                return i
        raise DBError('NOT_FOUND', f'Unknown column {name}')

    def execute(self, sql):
        return self.execute_statement(parse(sql))

    def execute_statement(self, statement):
        op = statement[0]
        if op == 'begin':
            require(not self.explicit, 'TRANSACTION_ERROR', 'Transaction already active')
            self.storage.begin()
            self.explicit = True
            self.transaction_changes = {}
            self.transaction_deletes = {}
            return 0
        if op in ('commit', 'rollback'):
            require(self.explicit, 'TRANSACTION_ERROR', 'No active transaction')
            getattr(self.storage, op)()
            # A successful commit does not change the state seen by an existing
            # snapshot. Rollback changes only tables/columns touched by this tx.
            if op == 'rollback':
                self.invalidate_changes(self.transaction_changes)
                for identifier, (keys, snapshot) in self.transaction_deletes.items():
                    self.vector_indexes.restore_keys(identifier, keys, snapshot)
            self.transaction_changes = {}
            self.transaction_deletes = {}
            self.explicit = False
            return 0
        if op == 'select':
            return self.select(*statement[1:])
        table = statement[2] if op == 'index' else statement[1]
        self.statement_changes = {}
        self.statement_vector_patches = {}
        self.statement_deletes = {}
        if not self.explicit:
            self.storage.begin()
        saved = self.storage.savepoint()
        try:
            count = getattr(self, op)(*statement[1:])
        except DBError:
            if self.explicit:
                self.storage.restore(saved)
            else:
                self.storage.rollback()
            self.statement_changes = {}
            self.statement_vector_patches = {}
            self.statement_deletes = {}
            raise
        # A vector-only update keeps row membership and hash slots, so the
        # snapshot can be patched in place instead of rebuilt. A rollback still
        # invalidates the table (None) so the old vectors are restored.
        for identifier, (column, rows) in self.statement_vector_patches.items():
            self.vector_indexes.patch(identifier, column, rows)
            if self.explicit:
                self.merge_change(self.transaction_changes, identifier, None)
        # Deletes only add tombstones; the snapshot and filter caches stay.
        for identifier, keys in self.statement_deletes.items():
            snapshot = self.vector_indexes.delete_keys(identifier, keys)
            if self.explicit:
                entry = self.transaction_deletes.setdefault(identifier, [set(), None])
                entry[0].update(keys)
                # Keep the first snapshot the deletes landed on; a later rebuild
                # during the transaction replaces it and forces full invalidation
                # on rollback (the rebuilt snapshot lacks the deleted rows).
                if snapshot is not None and entry[1] is None:
                    entry[1] = snapshot
        # No query runs during a statement. Its old snapshots are still valid if
        # it fails and restores its savepoint, even after partial row writes.
        self.invalidate_changes(self.statement_changes)
        if self.explicit:
            for identifier, columns in self.statement_changes.items():
                self.merge_change(self.transaction_changes, identifier, columns)
        if not self.explicit:
            self.storage.commit()
        if op == 'index':
            self.vector_indexes.ensure(self.table(table))
        return count

    @staticmethod
    def merge_change(changes, table, columns):
        # None means row membership, vector values or index metadata changed.
        if columns is None:
            changes[table] = None
        elif table not in changes or changes[table] is not None:
            changes.setdefault(table, set()).update(columns)

    def note_change(self, schema, columns=None):
        if columns is not None and any(schema['columns'][i][1] == 'VECTOR' for i in columns):
            columns = None
        if columns is None or columns:
            self.merge_change(self.statement_changes, schema['id'], columns)

    def invalidate_changes(self, changes):
        for table, columns in changes.items():
            if columns is None:
                self.vector_indexes.invalidate(table)
            else:
                self.vector_indexes.invalidate_filters(table, columns)

    def create(self, name, columns, primary):
        require(name not in self.storage.schemas, 'CONSTRAINT_ERROR', 'Table already exists')
        names = [col[0] for col in columns]
        require(len(names) == len(set(names)), 'CONSTRAINT_ERROR', 'Duplicate column')
        require(primary in names, 'CONSTRAINT_ERROR', 'Exactly one primary key is required')
        pk = names.index(primary)
        require(columns[pk][1] == 'BIGINT', 'CONSTRAINT_ERROR', 'Primary key must be BIGINT')
        require(sum(col[1] == 'VECTOR' for col in columns) <= 1, 'CONSTRAINT_ERROR', 'At most one vector column')
        table_id = max((table['id'] for table in self.storage.schemas.values()), default=0) + 1
        schema = dict(id=table_id, columns=columns, pk=pk, indexes={})
        self.storage.append(('schema', name, schema))
        self.note_change(schema)
        return 0

    def index(self, name, table, column):
        schema = self.table(table)
        index = self.column(schema, column)
        require(schema['columns'][index][1] == 'VECTOR', 'TYPE_ERROR', 'Index requires a vector column')
        require(all(name not in item['indexes'] for item in self.storage.schemas.values()), 'CONSTRAINT_ERROR', 'Index name already exists')
        schema = copy.deepcopy(schema)
        schema['indexes'][name] = dict(column=column, algorithm='flat-exact')
        self.storage.append(('schema', table, schema))
        self.note_change(schema)
        return 0

    def insert(self, table, columns, rows):
        schema = self.table(table)
        names = [col[0] for col in schema['columns']]
        if columns is None:
            columns = names
        require(len(columns) == len(names) and set(columns) == set(names), 'TYPE_ERROR', 'INSERT must supply each column exactly once')
        positions = [columns.index(name) for name in names]
        batched_vectors = {}
        if (self.native and len(rows) > 1
                and all(len(values) == len(names) for values in rows)):
            for index, column in enumerate(schema['columns']):
                if column[1] != 'VECTOR':
                    continue
                literals = [values[positions[index]] for values in rows]
                if any(literal.kind != 'string' for literal in literals):
                    continue
                parsed = parse_vectors([literal.value for literal in literals],
                                       column[2])
                if parsed is not None:
                    batched_vectors[index] = parsed
        for row_index, values in enumerate(rows):
            require(len(values) == len(names), 'TYPE_ERROR', 'Wrong number of values')
            row = tuple([batched_vectors[i][row_index]
                         if i in batched_vectors else
                         convert(values[positions[i]], col, self.native)
                         for i, col in enumerate(schema['columns'])])
            key = row[schema['pk']]
            require(self.storage.get(schema['id'], key) is None, 'CONSTRAINT_ERROR', 'Duplicate primary key')
            self.storage.append(('row', schema['id'], key, row))
        if rows:
            # One membership-change note covers the whole statement; a failed
            # statement restores its savepoint before this point.
            self.note_change(schema)
        return len(rows)

    def conditions(self, schema, clauses):
        bound = []
        for name, op, literal in clauses:
            i = self.column(schema, name)
            require(schema['columns'][i][1] != 'VECTOR', 'UNSUPPORTED', 'Vector comparisons require L2_DISTANCE')
            column = schema['columns'][i]
            if column[1] == 'VARCHAR':
                require(literal.kind == 'string', 'TYPE_ERROR', f'{name} requires a string literal')
                value = literal.value
            else:
                value = convert(literal, column, self.native)
            bound.append((i, op, value))
        return bound

    @staticmethod
    def matches(row, conditions):
        for i, op, value in conditions:
            current = row[i]
            if ((op == '=' and current != value) or (op == '<' and not current < value)
                    or (op == '<=' and not current <= value) or (op == '>' and not current > value)
                    or (op == '>=' and not current >= value)):
                return False
        return True

    def candidates(self, schema, conditions):
        for i, op, value in conditions:
            if i == schema['pk'] and op == '=':
                row = self.storage.get(schema['id'], value)
                if row is not None and self.matches(row, conditions):
                    yield row
                return
        # Range predicates on an integer primary key bound the probe set: the
        # hash index answers each key directly, so a bounded range never
        # scans the whole table. Mixed-type or open ranges fall back to a scan
        # (the bounds below keep the probe set finite either way).
        bounds = {}
        for i, op, value in conditions:
            if i != schema['pk'] or not isinstance(value, int) or isinstance(value, bool):
                bounds = None
                break
            low, high = bounds.get(op, (None, None))
            if op in ('>', '>='):
                bounds[op] = (max(low, value) if low is not None else value, high)
            elif op in ('<', '<='):
                bounds[op] = (low, min(high, value) if high is not None else value)
        if bounds:
            lower = upper = None
            for op, (low, high) in bounds.items():
                if op in ('>', '>='):
                    lower = max(lower, low) if lower is not None else low
                    closed_low = op == '>='
                else:
                    upper = min(upper, high) if upper is not None else high
                    closed_high = op == '<='
            # The probe window is at most one key wider than the range on each
            # side; matches() re-checks every condition, so the closed/open
            # distinction does not need to be exact here.
            if lower is not None and upper is not None and lower > upper + 1:
                return
            start = lower if lower is not None else None
            stop = upper + 1 if upper is not None else None
            # Both bounds present: probe exactly that window (at most one key
            # wider than the range; matches() re-filters). An open side uses
            # the table's key extent so the probe set stays bounded.
            if start is None or stop is None:
                lowest, highest = self.storage.index.key_extent(schema['id'])
                if lowest is None:
                    return  # Empty table.
                if start is None:
                    start = lowest
                if stop is None:
                    stop = highest + 1
            if stop - start <= self.storage.index.count + 1:
                for key in range(start, stop):
                    row = self.storage.get(schema['id'], key)
                    if row is not None and self.matches(row, conditions):
                        yield row
                return
        for row in self.storage.rows(schema['id']):
            if self.matches(row, conditions):
                yield row

    def update(self, table, assignments, where):
        schema = self.table(table)
        updates, used = [], set()
        for name, literal in assignments:
            i = self.column(schema, name)
            require(i != schema['pk'], 'UNSUPPORTED', 'Primary key updates are unsupported')
            require(i not in used, 'CONSTRAINT_ERROR', 'Repeated assignment')
            used.add(i)
            updates.append((i, convert(literal, schema['columns'][i], self.native)))
        return self.modify(schema, self.conditions(schema, where), updates)

    def delete(self, table, where):
        schema = self.table(table)
        return self.modify(schema, self.conditions(schema, where), None)

    def modify(self, schema, conditions, updates):
        # Freeze this statement's candidate rows before altering the overlay index.
        # Small candidate sets buffer in memory; a statement matching more than
        # _MODIFY_BUFFER rows spills to the temp file so a huge UPDATE stays
        # within memory limits. Both paths freeze candidates before the first
        # modification, preserving statement atomicity.
        buffered = []
        pending = path = None
        count = 0
        try:
            for row in self.candidates(schema, conditions):
                if pending is None:
                    if len(buffered) < self._MODIFY_BUFFER:
                        buffered.append(row)
                        continue
                    fd, path = tempfile.mkstemp(prefix='modify-', suffix='.tmp',
                                                dir=self.storage.directory)
                    pending = os.fdopen(fd, 'w+b')
                    for frozen in buffered:
                        write_record(pending, frozen)
                    buffered = None
                write_record(pending, row)
            if pending is not None:
                pending.seek(0)
            # Iterate the frozen candidates: from the buffer, or the spilled file.
            if pending is not None:
                frozen_rows = file_rows(pending)
            else:
                frozen_rows = iter(buffered)
            for row in frozen_rows:
                key = row[schema['pk']]
                changed = None
                changed_columns = None
                if updates is not None:
                    changed = list(row)
                    for i, value in updates:
                        changed[i] = value
                    changed = tuple(changed)
                    changed_columns = {i for i, _ in updates if row[i] != changed[i]}
                self.storage.append(('row', schema['id'], key, changed))
                if updates is None:
                    # Delete: a tombstone is enough; the snapshot and filter
                    # caches stay valid until too many tombstones accumulate.
                    self.statement_deletes.setdefault(schema['id'], set()).add(key)
                else:
                    vector_columns = ({i for i in changed_columns
                                       if schema['columns'][i][1] == 'VECTOR'}
                                      if changed_columns else set())
                    if vector_columns:
                        # Row membership and hash slots are unchanged; record
                        # the new vectors so the snapshot can be patched.
                        column = next(iter(vector_columns))
                        self.statement_vector_patches.setdefault(
                            schema['id'], (column, {}))[1][key] = changed
                        self.note_change(schema, changed_columns - vector_columns)
                    else:
                        self.note_change(schema, changed_columns)
                count += 1
        finally:
            if pending is not None:
                pending.close()
            if path is not None:
                os.unlink(path)
        return count

    def bind_expression(self, schema, expression):
        if expression[0] == 'column':
            return ('column', self.column(schema, expression[1]))
        require(expression[0] == 'distance', 'UNSUPPORTED', 'Unsupported expression')
        i = self.column(schema, expression[1])
        require(schema['columns'][i][1] == 'VECTOR', 'TYPE_ERROR', 'L2_DISTANCE requires VECTOR')
        vector = convert(expression[2], schema['columns'][i], self.native)
        # Keep the array: the native kernel and distance functions consume its
        # buffer directly. The generic sorter path converts it to a tuple only
        # where a hashable expression is required.
        return ('distance', i, vector)

    @staticmethod
    def evaluate(row, expression):
        if expression[0] == 'column':
            value = row[expression[1]]
            # Do not expose the mutable array held by an immutable cached version.
            return array('f', value) if isinstance(value, array) else value
        return native_distance(row[expression[1]], expression[2])

    def select(self, table, projections, where, order, limit):
        schema = self.table(table)
        conditions = self.conditions(schema, where)
        expressions, labels, aliases = [], [], {}
        for expression, alias in projections:
            if expression[0] == 'star':
                require(alias is None, 'SYNTAX_ERROR', 'Cannot alias *')
                expressions.extend(('column', i) for i in range(len(schema['columns'])))
                labels.extend(col[0] for col in schema['columns'])
            else:
                bound = self.bind_expression(schema, expression)
                expressions.append(bound)
                label = alias or (expression[1] if expression[0] == 'column' else 'l2_distance')
                labels.append(label)
                if alias:
                    require(alias not in aliases, 'CONSTRAINT_ERROR', 'Duplicate alias')
                    aliases[alias] = bound
        sort_keys = []
        for expression, descending in order:
            bound = aliases.get(expression[1]) if expression[0] == 'column' else None
            if bound is None:
                bound = self.bind_expression(schema, expression)
            require(bound[0] == 'distance' or schema['columns'][bound[1]][1] != 'VECTOR', 'UNSUPPORTED', 'Cannot ORDER BY a vector directly')
            sort_keys.append((bound, descending))

        point_lookup = any(i == schema['pk'] and op == '=' for i, op, _ in conditions)

        def output():
            if limit == 0:
                return
            if (not point_lookup and limit is not None and len(sort_keys) == 1 and not sort_keys[0][1]
                    and sort_keys[0][0][0] == 'distance'):
                distance_expression = sort_keys[0][0]
                pk_expression = ('column', schema['pk'])
                # When only the primary key and the distance are projected, the
                # snapshot already holds both; skip the row-index probes.
                pk_only = all(expr == pk_expression or expr == distance_expression
                              for expr in expressions)
                nearest = self.vector_indexes.topk(schema, distance_expression, conditions, limit,
                                                   self.matches, pk_only, self.mode)
                if nearest is not None:
                    if pk_only:
                        if expressions == [pk_expression, distance_expression]:
                            yield from nearest
                        else:
                            for key, distance in nearest:
                                yield tuple(key if expr == pk_expression else distance
                                            for expr in expressions)
                    else:
                        for row, distance in nearest:
                            # The kernel path already evaluated this exact distance.
                            yield tuple(distance if expr == distance_expression else self.evaluate(row, expr)
                                        for expr in expressions)
                    return
            candidates = self.candidates(schema, conditions)
            if not sort_keys:
                for n, row in enumerate(candidates):
                    if limit is not None and n >= limit:
                        break
                    yield tuple(self.evaluate(row, expr) for expr in expressions)
                return
            # Bind shared expressions once. A distance expression contains the
            # query vector: hashing it for every row repeatedly hashes all its
            # dimensions. Only the generic sorter path needs the slot table, so
            # convert the query array to a hashable tuple here.
            def hashable(expr):
                return (expr[0], expr[1], tuple(expr[2])) if expr[0] == 'distance' else expr
            projection_expressions = [hashable(expr) for expr in expressions]
            order_expressions = [hashable(expr) for expr, _ in sort_keys]
            unique_hashable = list(dict.fromkeys(projection_expressions + order_expressions))
            originals = {}
            for expr, key in zip(expressions, projection_expressions):
                originals[key] = expr
            for (expr, _), key in zip(sort_keys, order_expressions):
                originals[key] = expr
            unique = [originals[key] for key in unique_hashable]
            slots = {key: i for i, key in enumerate(unique_hashable)}
            projection_slots = [slots[key] for key in projection_expressions]
            order_slots = [(slots[key], descending) for key, (_, descending)
                           in zip(order_expressions, sort_keys)]
            sorter = Sorter(self.storage.directory, self.sort_memory, limit)
            try:
                for row in candidates:
                    values = [self.evaluate(row, expr) for expr in unique]
                    key = tuple(Descending(values[i]) if desc else values[i]
                                for i, desc in order_slots) + (row[schema['pk']],)
                    sorter.add(key, tuple(values[i] for i in projection_slots))
                yield from sorter.results()
            finally:
                sorter.close()
        return QueryResult(labels, output())
