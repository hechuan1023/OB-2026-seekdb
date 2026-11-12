"""SQL subset parser. Wire-protocol decisions are intentionally kept elsewhere."""
import re
from typing import NamedTuple

from .errors import DBError, require


class Literal(NamedTuple):
    kind: str
    value: str


TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>--[^\n]*(?:\n|$)|\#[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)"
    r"|(?P<string>'[^']*+(?:''[^']*+)*+')|(?P<quoted>`[^`]*+(?:``[^`]*+)*+`)"
    r"|(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z_0-9]*)|(?P<symbol><=|>=|<>|!=|[(),;*=<>])"
)

# Exactly one alternative matches per token, so lastindex identifies it.
# Indexed by group number to skip the lastgroup string lookup per token;
# the mapping is pinned by TOKEN_GROUPS == list(TOKEN.groupindex).
TOKEN_GROUPS = [None] + list(TOKEN.groupindex)

# The performance workload repeatedly uses this exact TopK projection shape.
# Recognize only the unquoted, escape-free spelling and send every other query
# through Parser, so the fast path cannot broaden the accepted SQL language.
FAST_DISTANCE_SELECT = re.compile(
    r"\s*SELECT\s+([A-Za-z_][A-Za-z_0-9]*)\s*,\s*"
    r"L2_DISTANCE\s*\(\s*([A-Za-z_][A-Za-z_0-9]*)\s*,\s*'([^']*)'\s*\)\s+"
    r"AS\s+([A-Za-z_][A-Za-z_0-9]*)\s+FROM\s+([A-Za-z_][A-Za-z_0-9]*)"
    r"(?:\s+WHERE\s+([A-Za-z_][A-Za-z_0-9]*)\s*=\s*'([^']*)')?\s+"
    r"ORDER\s+BY\s+([A-Za-z_][A-Za-z_0-9]*)\s+LIMIT\s+(\d+)\s*;?\s*",
    re.IGNORECASE,
)


class Parser:
    def __init__(self, sql):
        tokens = []
        append = tokens.append
        groups = TOKEN_GROUPS
        # finditer scans the input in one C-level loop; group() is only called
        # for tokens that survive the space/comment filter. A gap between
        # matches is exactly the position where TOKEN.match would have failed.
        pos = 0
        for token in TOKEN.finditer(sql):
            if token.start() != pos:
                raise DBError('SYNTAX_ERROR', f'Unexpected input at character {pos}')
            pos = token.end()
            kind = groups[token.lastindex]
            if kind != 'space' and kind != 'comment':
                append((kind, token.group()))
        if pos != len(sql):
            raise DBError('SYNTAX_ERROR', f'Unexpected input at character {pos}')
        self.tokens = tokens
        self.pos = 0

    def peek(self, value=None):
        if self.pos == len(self.tokens):
            return value is None
        if value is None:
            return False
        kind, text = self.tokens[self.pos]
        # Only names need case folding; symbols and numbers compare directly.
        return text == value if kind != 'name' else text.upper() == value

    def take(self, value):
        if self.peek(value):
            self.pos += 1
            return True
        return False

    def expect(self, value):
        require(self.take(value), 'SYNTAX_ERROR', f'Expected {value}')

    def identifier(self):
        require(self.pos < len(self.tokens), 'SYNTAX_ERROR', 'Expected identifier')
        kind, value = self.tokens[self.pos]
        require(kind in ('name', 'quoted'), 'SYNTAX_ERROR', 'Expected identifier')
        self.pos += 1
        return (value[1:-1].replace('``', '`') if kind == 'quoted' else value).lower()

    def literal(self):
        require(self.pos < len(self.tokens), 'SYNTAX_ERROR', 'Expected literal')
        kind, value = self.tokens[self.pos]
        require(kind in ('string', 'number'), 'TYPE_ERROR', 'Expected string or numeric literal; NULL is unsupported')
        self.pos += 1
        if kind == 'string':
            value = value[1:-1].replace("''", "'")
        return Literal(kind, value)

    def unsigned(self):
        literal = self.literal()
        require(literal.kind == 'number' and re.fullmatch(r'\d+', literal.value), 'TYPE_ERROR', 'Expected nonnegative integer')
        try:
            return int(literal.value)
        except ValueError as exc:
            raise DBError('TYPE_ERROR', 'Integer parameter is too large') from exc

    def expression(self):
        if self.take('*'):
            return ('star',)
        name = self.identifier()
        if name == 'l2_distance' and self.take('('):
            col = self.identifier()
            self.expect(',')
            vector = self.literal()
            self.expect(')')
            return ('distance', col, vector)
        return ('column', name)

    def where(self):
        clauses = []
        if self.take('WHERE'):
            while True:
                name = self.identifier()
                require(self.pos < len(self.tokens), 'SYNTAX_ERROR', 'Expected comparison')
                op = self.tokens[self.pos][1]
                require(op in ('=', '<', '<=', '>', '>='), 'UNSUPPORTED', 'Unsupported comparison')
                self.pos += 1
                clauses.append((name, op, self.literal()))
                if not self.take('AND'):
                    break
        return clauses

    def parse(self):
        if self.take('CREATE'):
            if self.take('TABLE'):
                name = self.identifier()
                self.expect('(')
                columns, primary = [], None
                while True:
                    if self.take('PRIMARY'):
                        self.expect('KEY')
                        self.expect('(')
                        key = self.identifier()
                        self.expect(')')
                        require(primary is None, 'CONSTRAINT_ERROR', 'Exactly one primary key is required')
                        primary = key
                    else:
                        col = self.identifier()
                        typ = self.identifier().upper()
                        require(typ in ('BIGINT', 'VARCHAR', 'VECTOR'), 'UNSUPPORTED', f'Unsupported type {typ}')
                        size = 0
                        if typ != 'BIGINT':
                            self.expect('(')
                            size = self.unsigned()
                            self.expect(')')
                            require(size > 0, 'TYPE_ERROR', 'Type size must be positive')
                        columns.append((col, typ, size))
                        if self.take('PRIMARY'):
                            self.expect('KEY')
                            require(primary is None, 'CONSTRAINT_ERROR', 'Exactly one primary key is required')
                            primary = col
                    if not self.take(','):
                        break
                self.expect(')')
                result = ('create', name, columns, primary)
            else:
                self.expect('VECTOR')
                self.expect('INDEX')
                name = self.identifier()
                self.expect('ON')
                table = self.identifier()
                self.expect('(')
                col = self.identifier()
                self.expect(')')
                result = ('index', name, table, col)
        elif self.take('INSERT'):
            self.expect('INTO')
            table = self.identifier()
            columns = None
            if self.take('('):
                columns = [self.identifier()]
                while self.take(','):
                    columns.append(self.identifier())
                self.expect(')')
            self.expect('VALUES')
            rows = []
            while True:
                self.expect('(')
                row = [self.literal()]
                while self.take(','):
                    row.append(self.literal())
                self.expect(')')
                rows.append(row)
                if not self.take(','):
                    break
            result = ('insert', table, columns, rows)
        elif self.take('SELECT'):
            projections = []
            while True:
                expr = self.expression()
                alias = self.identifier() if self.take('AS') else None
                projections.append((expr, alias))
                if not self.take(','):
                    break
            self.expect('FROM')
            table = self.identifier()
            where = self.where()
            order = []
            if self.take('ORDER'):
                self.expect('BY')
                while True:
                    expr = self.expression()
                    descending = self.take('DESC')
                    if not descending:
                        self.take('ASC')
                    order.append((expr, descending))
                    if not self.take(','):
                        break
            limit = self.unsigned() if self.take('LIMIT') else None
            result = ('select', table, projections, where, order, limit)
        elif self.take('UPDATE'):
            table = self.identifier()
            self.expect('SET')
            assignments = []
            while True:
                name = self.identifier()
                self.expect('=')
                assignments.append((name, self.literal()))
                if not self.take(','):
                    break
            result = ('update', table, assignments, self.where())
        elif self.take('DELETE'):
            self.expect('FROM')
            table = self.identifier()
            result = ('delete', table, self.where())
        elif self.take('BEGIN'):
            self.take('TRANSACTION')
            result = ('begin',)
        elif self.take('COMMIT'):
            result = ('commit',)
        elif self.take('ROLLBACK'):
            result = ('rollback',)
        else:
            raise DBError('UNSUPPORTED', 'Unsupported statement')
        self.take(';')
        require(self.pos == len(self.tokens), 'SYNTAX_ERROR', 'Unexpected trailing SQL')
        return result


def parse(sql):
    match = FAST_DISTANCE_SELECT.fullmatch(sql)
    if match is not None:
        (key, vector_column, vector, alias, table, filter_column,
         filter_value, order_column, limit) = match.groups()
        try:
            limit = int(limit)
        except ValueError:
            pass
        else:
            where = ([] if filter_column is None else
                     [(filter_column.lower(), '=', Literal('string', filter_value))])
            return ('select', table.lower(),
                    [(('column', key.lower()), None),
                     (('distance', vector_column.lower(), Literal('string', vector)),
                      alias.lower())],
                    where, [(('column', order_column.lower()), False)], limit)
    return Parser(sql).parse()
