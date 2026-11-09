"""SQL subset parser. Wire-protocol decisions are intentionally kept elsewhere."""
import re
from dataclasses import dataclass

from .errors import DBError, require


@dataclass(frozen=True)
class Literal:
    kind: str
    value: str


TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>--[^\n]*(?:\n|$)|\#[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)"
    r"|(?P<string>'(?:''|[^'])*')|(?P<quoted>`(?:``|[^`])*`)"
    r"|(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z_0-9]*)|(?P<symbol><=|>=|<>|!=|[(),;*=<>])"
)


class Parser:
    def __init__(self, sql):
        self.tokens = []
        pos = 0
        while pos < len(sql):
            match = TOKEN.match(sql, pos)
            require(match is not None, 'SYNTAX_ERROR', f'Unexpected input at character {pos}')
            kind, value = match.lastgroup, match.group()
            pos = match.end()
            if kind not in ('space', 'comment'):
                self.tokens.append((kind, value))
        self.pos = 0

    def peek(self, value=None):
        if self.pos == len(self.tokens):
            return value is None
        return value is not None and self.tokens[self.pos][1].upper() == value

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
    return Parser(sql).parse()
