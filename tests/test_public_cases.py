"""Unmodified upstream assertions via typed execution and our LOCAL stdio adapter.

Not the organizer's unpublished process driver. Error semantics are explicitly
mapped to our development codes; unknown meanings fail rather than pass loosely.
"""
import json
import math
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from minivectordb.engine import Engine
from minivectordb.errors import DBError
from minivectordb.protocol import cell
from tests.test_recovery import Client

ROOT = Path(__file__).resolve().parents[1] / 'mysqltest'
MANIFEST = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
ERROR_CODES = {
    'duplicate_key': 'CONSTRAINT_ERROR',
    'dimension_mismatch': 'INVALID_VECTOR',
    'unsupported_sql': 'UNSUPPORTED',
}


def read_case(case, root=ROOT):
    steps = []
    for number, line in enumerate((root / case['test']).read_text(encoding='utf-8').splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith('--') and line not in ('--restart', '--mode exact', '--mode ann'):
            raise ValueError(f"{case['id']}:{number}: unknown driver instruction: {line}")
        steps.append((number, line))
    expected = json.loads((root / case['result']).read_text(encoding='utf-8'))
    sql_count = sum(not line.startswith('--') for _, line in steps)
    if sql_count != case['statements'] or sql_count != len(expected):
        raise ValueError(f"{case['id']}: SQL={sql_count}, manifest={case['statements']}, assertions={len(expected)}")
    return steps, expected


class LocalSession:
    def __init__(self, directory, wire):
        self.directory, self.wire, self.mode = directory, wire, 'exact'
        self.db = self.open()

    def open(self):
        if self.wire:
            return Client(self.directory, extra_args=('--mode', self.mode, '--local-mode-controls'))
        return Engine(self.directory, mode=self.mode)

    def close(self):
        self.db.close()
        if self.wire:
            if self.db.process.returncode != 0 or self.db.stderr:
                raise AssertionError(f'Unclean shutdown: {self.db.process.returncode}, {self.db.stderr}')

    def restart(self):
        self.close()  # EOF, wait for exit, same directory; never delete its data.
        self.db = self.open()

    def set_mode(self, mode):
        if mode not in ('exact', 'ann'):
            raise ValueError(f'Unknown mode: {mode}')
        if self.wire:
            # Keep the process and any active transaction alive. This command
            # is enabled only for our local adapter, not claimed as official.
            self.db.process.stdin.write(f'--mode {mode}\n')
            self.db.process.stdin.flush()
            reply = self.db.line()
            if reply != 'OK\t0':
                raise AssertionError(f'Mode control failed: {reply!r}')
        else:
            self.db.mode = mode
        self.mode = mode

    def execute(self, sql):
        if not self.wire:
            try:
                result = self.db.execute(sql)
                if isinstance(result, int):
                    return {'affected_rows': result}
                return {'columns': result.columns, 'rows': list(result.rows)}
            except DBError as exc:
                return {'error': exc.code, 'message': str(exc)}
        self.db.send(sql)
        first = self.db.line()
        if first is None:
            raise AssertionError(f'Unexpected exit: {self.db.stderr}')
        if first.startswith('ERROR\t'):
            fields = first.split('\t', 2)
            if len(fields) != 3 or not fields[1]:
                raise AssertionError(f'Malformed error: {first!r}')
            return {'error': fields[1], 'message': fields[2]}
        if first.startswith('OK\t'):
            return {'affected_rows': int(first.split('\t')[1])}
        if not first.startswith('RESULT\t'):
            raise AssertionError(f'Malformed response: {first!r}')
        columns, rows = first.split('\t')[1:], []
        while True:
            line = self.db.line()
            if line is None:
                raise AssertionError('Process exited during result')
            if line.startswith('END\t'):
                if int(line.split('\t')[1]) != len(rows):
                    raise AssertionError('Wrong result row count')
                return {'columns': columns, 'rows': rows}
            rows.append(line.split('\t'))


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

    def check_result(self, actual, expected, wire):
        if 'error' in expected:
            self.assertIn(expected['error'], ERROR_CODES, 'Unmapped public error semantic')
            self.assertEqual(actual.get('error'), ERROR_CODES[expected['error']], actual)
            return
        self.assertNotIn('error', actual, actual)
        for key in ('affected_rows', 'columns'):
            if key in expected:
                self.assertEqual(actual.get(key), expected[key])
        if 'rows' in expected:
            self.assertIn('rows', actual)
            self.check_rows(actual['rows'], expected['rows'], expected.get('unordered', False), wire)

    def run_case(self, case, wire):
        steps, expected = read_case(case)
        with tempfile.TemporaryDirectory() as directory:
            session = LocalSession(directory, wire)
            try:
                index = 0
                for line_number, sql in steps:
                    if sql == '--restart':
                        session.restart()
                    elif sql.startswith('--mode '):
                        session.set_mode(sql.split()[1])
                    else:
                        actual = session.execute(sql)
                        try:
                            self.check_result(actual, expected[index], wire)
                        except AssertionError as exc:
                            self.fail(f"{case['id']}:{line_number}, SQL #{index + 1}: {sql}\n{exc}")
                        index += 1
                self.assertEqual(index, len(expected))
            finally:
                session.close()


def make_test(case, wire):
    def test(self):
        self.run_case(case, wire)
    return test


for case in MANIFEST['cases']:
    for wire in (False, True):
        setattr(PublicCases, f"test_{case['id']}_{'stdio' if wire else 'typed'}", make_test(case, wire))


def load_tests(loader, tests, pattern):
    # Preserve the upstream manifest order even under unittest discovery.
    return unittest.TestSuite(PublicCases(f"test_{case['id']}_{path}")
                              for case in MANIFEST['cases'] for path in ('typed', 'stdio'))


if __name__ == '__main__':
    unittest.main()
