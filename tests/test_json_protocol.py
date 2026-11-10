import json
import subprocess
import sys
import tempfile
import unittest


class JsonProtocolTests(unittest.TestCase):
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
