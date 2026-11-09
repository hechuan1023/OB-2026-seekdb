"""Run upstream assertions through typed execution and the local stdio adapter.

This is our adapter, not the organizer's unpublished process protocol driver.
Upstream cases and expected results are read unchanged from mysqltest/.
"""
import json
import math
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from minivectordb.engine import Engine
from minivectordb.protocol import cell
from tests.test_recovery import Client

ROOT = Path(__file__).resolve().parents[1] / 'mysqltest'
MANIFEST = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))


class PublicCases(unittest.TestCase):
    def check_rows(self, actual, expected, unordered, wire):
        self.assertEqual(len(actual), len(expected))
        if unordered:
            # Current unordered public cases contain integers/strings, no floats.
            expected = [[cell(v) for v in row] for row in expected] if wire else expected
            self.assertEqual(Counter(map(tuple, actual)), Counter(map(tuple, expected)))
            return
        for row, truth in zip(actual, expected):
            self.assertEqual(len(row), len(truth))
            for value, reference in zip(row, truth):
                if isinstance(reference, float):
                    self.assertTrue(math.isclose(float(value), reference, rel_tol=1e-5, abs_tol=1e-5))
                elif wire:
                    self.assertEqual(value, cell(reference))
                else:
                    self.assertIs(type(value), type(reference))
                    self.assertEqual(value, reference)

    def run_case(self, case, wire):
        sqls = [s.strip() for s in (ROOT / 'cases' / (case['id'] + '.test')).read_text(encoding='utf-8').splitlines() if s.strip()]
        expected = json.loads((ROOT / 'cases' / (case['id'] + '.result')).read_text(encoding='utf-8'))
        self.assertEqual(len(sqls), case['statements'])
        self.assertEqual(len(expected), len(sqls))
        with tempfile.TemporaryDirectory() as directory:
            db = Client(directory) if wire else Engine(directory)
            try:
                for index, (sql, assertion) in enumerate(zip(sqls, expected)):
                    with self.subTest(statement=index + 1, sql=sql):
                        columns, rows, count = None, None, None
                        if wire:
                            db.send(sql)
                            first = db.line()
                            self.assertIsNotNone(first)
                            self.assertFalse(first.startswith('ERROR'), first)
                            if first.startswith('OK\t'):
                                count = int(first.split('\t')[1])
                            else:
                                self.assertTrue(first.startswith('RESULT\t'), first)
                                columns, rows = first.split('\t')[1:], []
                                while True:
                                    line = db.line()
                                    self.assertIsNotNone(line)
                                    if line.startswith('END\t'):
                                        self.assertEqual(int(line.split('\t')[1]), len(rows))
                                        break
                                    rows.append(line.split('\t'))
                        else:
                            result = db.execute(sql)
                            if isinstance(result, int):
                                count = result
                            else:
                                columns, rows = result.columns, list(result.rows)
                        if 'affected_rows' in assertion:
                            self.assertEqual(count, assertion['affected_rows'])
                        if 'columns' in assertion:
                            self.assertEqual(columns, assertion['columns'])
                        if 'rows' in assertion:
                            self.check_rows(rows, assertion['rows'], assertion.get('unordered', False), wire)
            finally:
                db.close()


def make_test(case, wire):
    def test(self):
        self.run_case(case, wire)
    return test


for case in MANIFEST['cases']:
    for wire in (False, True):
        setattr(PublicCases, f"test_{case['id']}_{'stdio' if wire else 'typed'}", make_test(case, wire))


if __name__ == '__main__':
    unittest.main()
