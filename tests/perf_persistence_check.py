"""Verify the complete deterministic benchmark dataset after a container restart."""
import argparse
import array
import json
import random
import subprocess
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    rng = random.Random(20261110)
    data = [(i, 'g' + str(i % 5), list(array.array('f',
             (rng.uniform(-1, 1) for _ in range(128))))) for i in range(10000)]
    volume = 'mvdb-persist-' + uuid.uuid4().hex[:12]
    subprocess.run(['docker', 'volume', 'create', volume], check=True, capture_output=True)
    process = None
    try:
        def start():
            p = subprocess.Popen(['docker', 'run', '--rm', '-i', '--network', 'none',
                                  '--cpus=2', '--memory=256m', '--memory-swap=256m',
                                  '-v', volume + ':/data', args.image], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8', bufsize=1)
            assert json.loads(p.stdout.readline()) == {'type': 'ready'}
            return p

        def request(p, ident, sql):
            p.stdin.write(json.dumps({'id': ident, 'op': 'execute', 'sql': sql,
                                      'search_mode': 'exact'}, separators=(',', ':')) + '\n')
            p.stdin.flush()
            result = json.loads(p.stdout.readline())
            assert result['id'] == ident and result['ok'], result
            return result

        def shutdown(p):
            p.stdin.write('{"id":"done","op":"shutdown"}\n')
            p.stdin.flush()
            assert json.loads(p.stdout.readline()) == {'id': 'done', 'ok': True}
            assert p.wait(timeout=60) == 0
            p.stdin.close()
            p.stdout.close()
            p.stderr.close()

        process = start()
        request(process, 'create', 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(128))')
        for start_row in range(0, len(data), 100):
            sql = 'INSERT INTO t VALUES ' + ','.join(
                "(%d,'%s','%s')" % (pk, tag, json.dumps(vector, separators=(',', ':')))
                for pk, tag, vector in data[start_row:start_row + 100])
            assert request(process, start_row, sql)['affected_rows'] == 100
        shutdown(process)
        process = start()
        rows = request(process, 'check', 'SELECT id,tag,v FROM t ORDER BY id')['rows']
        assert len(rows) == len(data)
        for actual, expected in zip(rows, data):
            assert actual == list(expected), (actual[0], expected[0])
        shutdown(process)
        process = None
        print(json.dumps({'image': args.image, 'rows_verified': len(rows),
                          'dimensions': 128, 'restart': 'normal', 'status': 'PASS'}))
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        subprocess.run(['docker', 'volume', 'rm', volume], check=True, capture_output=True)


if __name__ == '__main__':
    main()
