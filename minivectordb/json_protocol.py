"""Official newline-delimited JSON transport, separate from SQL execution."""
import json
import math
from array import array

from .errors import DBError
from .engine import QueryResult
from .sql import parse


_EXECUTE_OP = ',"op":"execute","sql":"'


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'),
                      default=lambda x: list(x) if isinstance(x, array) else x)


def _json_row(row):
    """Format an all-integer/float row without the generic encoder.

    repr() and json.dumps() use the same shortest round-trip form for finite
    floats, so the bytes are identical. Anything else falls back to _json.
    """
    parts = []
    for value in row:
        kind = type(value)
        if kind is int:
            parts.append(str(value))
        elif kind is float:
            if not math.isfinite(value):
                return _json(row)
            parts.append(repr(value))
        else:
            return _json(row)
    return '[' + ','.join(parts) + ']'


def _emit(output, value):
    output.write(_json(value) + '\n')
    output.flush()


def _parse_fast_execute(line):
    """Parse the canonical benchmark request without constructing JSON objects."""
    if not line.startswith('{"id":'):
        return None
    op_at = line.find(_EXECUTE_OP, 6)
    if op_at < 0:
        return None
    ident_text = line[6:op_at]
    digits = ident_text[1:] if ident_text.startswith('-') else ident_text
    if not digits.isdigit() or (len(digits) > 1 and digits.startswith('0')):
        return None
    sql_start = op_at + len(_EXECUTE_OP)
    mode_at = line.rfind(',"search_mode":"')
    if mode_at <= sql_start or line[mode_at - 1] != '"':
        return None
    sql = line[sql_start:mode_at - 1]
    if not sql.isprintable() or '\\' in sql or '"' in sql:
        return None
    tail = line[mode_at + len(',"search_mode":"'):]
    if tail == 'exact"}':
        mode = 'exact'
    elif tail == 'ann"}':
        mode = 'ann'
    else:
        return None
    return int(ident_text), sql, mode


def serve(engine, input_stream, output):
    _emit(output, {'type': 'ready'})
    for line in input_stream:
        ident = None
        try:
            payload = line[:-1] if line.endswith('\n') else line
            fast = _parse_fast_execute(payload)
            if fast is not None:
                ident, sql, mode = fast
            else:
                request = json.loads(line)
                if not isinstance(request, dict) or 'id' not in request:
                    raise DBError('INVALID_REQUEST', 'Expected object with id')
                ident = request['id']
                op = request.get('op')
                if op == 'shutdown':
                    engine.close()
                    _emit(output, {'id': ident, 'ok': True})
                    return True
                if op != 'execute':
                    raise DBError('UNSUPPORTED_OP', 'Unsupported operation')
                mode = request.get('search_mode', 'exact')
                sql = request.get('sql')
            if mode not in ('exact', 'ann'):
                raise DBError('INVALID_REQUEST', 'Invalid search mode')
            if not isinstance(sql, str):
                raise DBError('INVALID_REQUEST', 'SQL must be a string')
            engine.mode = mode
            value = engine.execute_statement(parse(sql))
            if isinstance(value, QueryResult):
                # Stream rows in bounded chunks instead of retaining the whole
                # result; with unbuffered stdout this also avoids one syscall
                # per row on large results. Keep the prefix in the first chunk
                # so the common ten-row TopK response is one write.
                chunk = ['{"id":' + _json(ident) + ',"ok":true,"columns":' +
                         _json(value.columns) + ',"rows":[']
                rows_in_chunk = 0
                first = True
                try:
                    for row in value.rows:
                        chunk.append(('' if first else ',') + _json_row(row))
                        first = False
                        rows_in_chunk += 1
                        if rows_in_chunk >= 64:
                            output.write(''.join(chunk))
                            chunk.clear()
                            rows_in_chunk = 0
                finally:
                    value.rows.close()
                chunk.append(']}\n')
                output.write(''.join(chunk))
                output.flush()
            else:
                _emit(output, {'id': ident, 'ok': True, 'affected_rows': value})
        except DBError as exc:
            _emit(output, {'id': ident, 'ok': False,
                           'error': {'code': exc.code, 'message': str(exc)}})
        except (ValueError, TypeError) as exc:
            _emit(output, {'id': ident, 'ok': False,
                           'error': {'code': 'INVALID_REQUEST', 'message': str(exc)}})
