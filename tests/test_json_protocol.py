import json
import subprocess
import sys
import tempfile
import unittest
from array import array

from minivectordb.json_protocol import _json, _json_row


class JsonProtocolTests(unittest.TestCase):
    def test_numeric_row_encoder_matches_generic_json(self):
        rows = [(), (0, -1, 9223372036854775807, -9223372036854775808),
                (0.0, -0.0, 1e-300, 1e300, 5e-324),
                (True, None, '中文', array('f', [1, 2])),
                (1, [2, 3], 'x')]
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(_json_row(row), _json(row))
        for value in (float('inf'), float('-inf'), float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _json_row((value,))

    def test_requests_transactions_modes_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            p = subprocess.Popen([sys.executable, '-m', 'minivectordb', '--wire', 'jsonl',
                                  '--data-dir', directory], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8', bufsize=1)
            try:
                self.assertEqual(json.loads(p.stdout.readline()), {'type': 'ready'})
                def request(ident, sql, mode='exact'):
                    p.stdin.write(json.dumps({'id': ident, 'op': 'execute', 'sql': sql,
                                              'search_mode': mode}) + '\n')
                    p.stdin.flush()
                    response = json.loads(p.stdout.readline())
                    self.assertEqual(response['id'], ident)
                    return response
                self.assertTrue(request('create', 'CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(2))')['ok'])
                self.assertEqual(request(2, "INSERT INTO t VALUES (9007199254740993,'[1,0]')")['affected_rows'], 1)
                self.assertTrue(request(3, 'BEGIN')['ok'])
                self.assertTrue(request(4, "UPDATE t SET v='[0,0]' WHERE id=9007199254740993")['ok'])
                result = request(5, "SELECT id,v FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 1", 'ann')
                self.assertEqual(result['rows'], [[9007199254740993, [0.0, 0.0]]])
                self.assertFalse(request(6, "INSERT INTO t VALUES (9007199254740993,'[0,0]')")['ok'])
                self.assertTrue(request(7, 'ROLLBACK')['ok'])
                self.assertEqual(request(8, 'SELECT v FROM t')['rows'], [[[1.0, 0.0]]])
                p.stdin.write(json.dumps({'id': 'end', 'op': 'shutdown'}) + '\n')
                p.stdin.flush()
                self.assertEqual(json.loads(p.stdout.readline()), {'id': 'end', 'ok': True})
                self.assertEqual(p.wait(timeout=15), 0)
                self.assertEqual(p.stdout.read(), '')
                self.assertEqual(p.stderr.read(), '')
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait()
                for stream in (p.stdin, p.stdout, p.stderr):
                    stream.close()


if __name__ == '__main__':
    unittest.main()
