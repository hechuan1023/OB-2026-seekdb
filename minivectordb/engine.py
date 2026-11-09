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
from .sorting import Descending, Sorter, file_rows
from .sql import parse
from .storage import Storage, write_record
from .vector_index import VectorIndexes


def convert(literal, column):
    name, typ, size = column
    if typ == 'BIGINT':
        require(literal.kind == 'number' and re.fullmatch(r'[+-]?\d+', literal.value), 'TYPE_ERROR', f'{name} requires BIGINT')
        digits = literal.value.lstrip('+-').lstrip('0') or '0'
        require(len(digits) <= 19, 'TYPE_ERROR', 'BIGINT overflow')
        value = int(('-' if literal.value.startswith('-') else '') + digits)
        require(-(1 << 63) <= value < (1 << 63), 'TYPE_ERROR', 'BIGINT overflow')
        return value
    require(literal.kind == 'string', 'TYPE_ERROR', f'{name} requires a string literal')
    if typ == 'VARCHAR':
        require(len(literal.value) <= size, 'TYPE_ERROR', f'{name} exceeds VARCHAR({size})')
        return literal.value
    try:
        values = json.loads(literal.value)
        if not isinstance(values, list) or len(values) != size:
            raise ValueError('Vector dimension mismatch')
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            raise ValueError('Vector values must be finite numbers')
        result = array('f', values)
        if not all(math.isfinite(value) for value in result):
            raise ValueError('float32 overflow')
        return result
    except (ValueError, OverflowError, TypeError, struct.error) as exc:
        raise DBError('INVALID_VECTOR', str(exc)) from exc


@dataclass
class QueryResult:
    columns: list
    rows: object


class Engine:
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
        self.sort_memory = sort_memory
        self.mode = mode  # Both modes currently use the exact flat search path.
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
            return 0
        if op in ('commit', 'rollback'):
            require(self.explicit, 'TRANSACTION_ERROR', 'No active transaction')
            getattr(self.storage, op)()
            # A successful commit does not change the state seen by an existing
            # snapshot. Rollback changes only tables/columns touched by this tx.
            if op == 'rollback':
                self.invalidate_changes(self.transaction_changes)
            self.transaction_changes = {}
            self.explicit = False
            return 0
        if op == 'select':
            return self.select(*statement[1:])
        table = statement[2] if op == 'index' else statement[1]
        self.statement_changes = {}
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
            raise
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
        for values in rows:
            require(len(values) == len(names), 'TYPE_ERROR', 'Wrong number of values')
            row = tuple(convert(values[positions[i]], col) for i, col in enumerate(schema['columns']))
            key = row[schema['pk']]
            require(self.storage.get(schema['id'], key) is None, 'CONSTRAINT_ERROR', 'Duplicate primary key')
            self.storage.append(('row', schema['id'], key, row))
            self.note_change(schema)
        return len(rows)

    def conditions(self, schema, clauses):
        bound = []
        for name, op, literal in clauses:
            i = self.column(schema, name)
            require(schema['columns'][i][1] != 'VECTOR', 'UNSUPPORTED', 'Vector comparisons require L2_DISTANCE')
            bound.append((i, op, convert(literal, schema['columns'][i])))
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
            updates.append((i, convert(literal, schema['columns'][i])))
        return self.modify(schema, self.conditions(schema, where), updates)

    def delete(self, table, where):
        schema = self.table(table)
        return self.modify(schema, self.conditions(schema, where), None)

    def modify(self, schema, conditions, updates):
        # Freeze this statement's candidate rows before altering the overlay index.
        fd, path = tempfile.mkstemp(prefix='modify-', suffix='.tmp', dir=self.storage.directory)
        count = 0
        try:
            with os.fdopen(fd, 'w+b') as pending:
                for row in self.candidates(schema, conditions):
                    write_record(pending, row)
                pending.seek(0)
                for row in file_rows(pending):
                    key = row[schema['pk']]
                    changed = None
                    if updates is not None:
                        changed = list(row)
                        for i, value in updates:
                            changed[i] = value
                        changed = tuple(changed)
                    self.storage.append(('row', schema['id'], key, changed))
                    self.note_change(schema, None if changed is None else
                                     {i for i, _ in updates if row[i] != changed[i]})
                    count += 1
        finally:
            os.unlink(path)
        return count

    def bind_expression(self, schema, expression):
        if expression[0] == 'column':
            return ('column', self.column(schema, expression[1]))
        require(expression[0] == 'distance', 'UNSUPPORTED', 'Unsupported expression')
        i = self.column(schema, expression[1])
        require(schema['columns'][i][1] == 'VECTOR', 'TYPE_ERROR', 'L2_DISTANCE requires VECTOR')
        vector = convert(expression[2], schema['columns'][i])
        return ('distance', i, tuple(vector))

    @staticmethod
    def evaluate(row, expression):
        if expression[0] == 'column':
            value = row[expression[1]]
            # Do not expose the mutable array held by an immutable cached version.
            return array('f', value) if isinstance(value, array) else value
        return math.dist(row[expression[1]], expression[2])

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

        # Bind shared expressions once. A distance expression contains the query
        # vector: hashing it for every row repeatedly hashes all its dimensions.
        unique = list(dict.fromkeys(expressions + [expr for expr, _ in sort_keys]))
        slots = {expr: i for i, expr in enumerate(unique)}
        projection_slots = [slots[expr] for expr in expressions]
        order_slots = [(slots[expr], descending) for expr, descending in sort_keys]
        point_lookup = any(i == schema['pk'] and op == '=' for i, op, _ in conditions)

        def output():
            if limit == 0:
                return
            if (not point_lookup and limit is not None and len(sort_keys) == 1 and not sort_keys[0][1]
                    and sort_keys[0][0][0] == 'distance'):
                nearest = self.vector_indexes.topk(schema, sort_keys[0][0], conditions, limit, self.matches)
                if nearest is not None:
                    for row in nearest:
                        yield tuple(self.evaluate(row, expr) for expr in expressions)
                    return
            candidates = self.candidates(schema, conditions)
            if not sort_keys:
                for n, row in enumerate(candidates):
                    if limit is not None and n >= limit:
                        break
                    yield tuple(self.evaluate(row, expr) for expr in expressions)
                return
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
