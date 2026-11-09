"""Local development TSV adapter, NOT the unpublished official round2 protocol."""
import json
import re
from array import array

from .errors import DBError

SPECIAL = re.compile(r"['`;#]|--|/\*")


def statements(stream):
    buffer = []
    quote = None
    block_comment = False
    for line in stream:
        i = 0
        while i < len(line):
            if block_comment:
                end = line.find('*/', i)
                if end < 0:
                    buffer.append(line[i:])
                    break
                buffer.append(line[i:end + 2])
                i = end + 2
                block_comment = False
            elif quote:
                end = line.find(quote, i)
                if end < 0:
                    buffer.append(line[i:])
                    break
                if line[end + 1:end + 2] == quote:
                    buffer.append(line[i:end + 2])
                    i = end + 2
                else:
                    buffer.append(line[i:end + 1])
                    i = end + 1
                    quote = None
            else:
                match = SPECIAL.search(line, i)
                if match is None:
                    buffer.append(line[i:])
                    break
                buffer.append(line[i:match.start()])
                token = match.group()
                i = match.end()
                if token in ('--', '#'):
                    buffer.append(line[match.start():])
                    break
                if token == ';':
                    sql = ''.join(buffer)
                    buffer = []
                    if sql.strip():
                        yield sql
                else:
                    buffer.append(token)
                    if token == '/*':
                        block_comment = True
                    else:
                        quote = token
    if quote or block_comment:
        raise DBError('SYNTAX_ERROR', 'Unterminated string, identifier, or comment')
    sql = ''.join(buffer)
    if sql.strip():
        yield sql


def cell(value):
    if isinstance(value, array):
        return json.dumps(list(value), ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return str(value).replace('\\', '\\\\').replace('\t', '\\t').replace('\n', '\\n').replace('\r', '\\r')


def error(output, exc):
    print('ERROR', exc.code, cell(str(exc)), sep='\t', file=output, flush=True)


def result(output, value):
    if isinstance(value, int):
        print('OK', value, sep='\t', file=output, flush=True)
        return
    print('RESULT', *(cell(name) for name in value.columns), sep='\t', file=output)
    count = 0
    try:
        for row in value.rows:
            print(*(cell(item) for item in row), sep='\t', file=output)
            count += 1
    finally:
        value.rows.close()
    print('END', count, sep='\t', file=output, flush=True)
