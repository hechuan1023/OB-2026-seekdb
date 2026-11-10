"""Official newline-delimited JSON transport, separate from SQL execution."""
import json
from array import array

from .errors import DBError
from .engine import QueryResult
from .sql import parse


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'),
                      default=lambda x: list(x) if isinstance(x, array) else x)


def _emit(output, value):
    output.write(_json(value) + '\n')
    output.flush()


def serve(engine, input_stream, output):
    _emit(output, {'type': 'ready'})
    for line in input_stream:
        ident = None
        try:
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
            if mode not in ('exact', 'ann'):
                raise DBError('INVALID_REQUEST', 'Invalid search mode')
            sql = request.get('sql')
            if not isinstance(sql, str):
                raise DBError('INVALID_REQUEST', 'SQL must be a string')
            engine.mode = mode
            value = engine.execute_statement(parse(sql))
            if isinstance(value, QueryResult):
                # Stream rows in bounded chunks instead of retaining the whole
                # result; with unbuffered stdout this also avoids one syscall
                # per row on large results.
                output.write('{"id":' + _json(ident) + ',"ok":true,"columns":' +
                             _json(value.columns) + ',"rows":[')
                first = True
                chunk = []
                try:
                    for row in value.rows:
                        chunk.append(('' if first else ',') + _json(row))
                        first = False
                        if len(chunk) >= 64:
                            output.write(''.join(chunk))
                            chunk.clear()
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
