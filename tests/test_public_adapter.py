"""Independent checks that the public-case adapter cannot hide state/errors."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from minivectordb.protocol import ModeChange, statements
from tests import test_public_cases as public


class AdapterTests(unittest.TestCase):
    def test_mode_switch_preserves_process_transaction_and_restart(self):
        for wire in (False, True):
            with self.subTest(wire=wire), tempfile.TemporaryDirectory() as directory:
                session = public.LocalSession(directory, wire)
                check = public.PublicCases('runTest')
                try:
                    session.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(2))')
                    session.execute("INSERT INTO t VALUES (1,'[3,4]')")
                    session.execute('BEGIN')
                    session.execute("UPDATE t SET v='[0,0]' WHERE id=1")
                    original = session.db
                    session.set_mode('ann')
                    self.assertIs(session.db, original, 'Mode switch must not reopen the database')
                    if wire:
                        self.assertIsNone(original.process.poll())
                    else:
                        self.assertEqual(session.db.mode, 'ann')
                    sql = "SELECT id,L2_DISTANCE(v,'[0,0]') AS distance FROM t ORDER BY distance LIMIT 1"
                    check.check_result(session.execute(sql), {'rows': [[1, 0.0]]}, wire)
                    session.set_mode('exact')
                    session.execute('ROLLBACK')
                    check.check_result(session.execute(sql), {'rows': [[1, 5.0]]}, wire)
                    session.set_mode('ann')
                    session.execute('BEGIN')
                    session.execute('DELETE FROM t')
                    session.restart()
                    self.assertEqual(session.mode, 'ann')
                    check.check_result(session.execute(sql), {'rows': [[1, 5.0]]}, wire)
                finally:
                    session.close()
                fresh = public.LocalSession(directory, wire)
                try:
                    self.assertEqual(fresh.mode, 'exact')
                finally:
                    fresh.close()

    def test_expected_error_does_not_accept_success_or_unrelated_error(self):
        check = public.PublicCases('runTest')
        for actual, expected in [
            ({'affected_rows': 1}, {'error': 'duplicate_key'}),
            ({'error': 'NOT_FOUND'}, {'error': 'duplicate_key'}),
            ({'error': 'CONSTRAINT_ERROR'}, {}),
            ({'error': ''}, {'error': 'duplicate_key'}),
            ({'error': 'CONSTRAINT_ERROR'}, {'error': 'future_unknown_semantic'}),
        ]:
            with self.subTest(actual=actual, expected=expected), self.assertRaises(AssertionError):
                check.check_result(actual, expected, wire=False)

    def test_directives_do_not_consume_assertions_and_unknowns_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = {'id': 'fixture', 'test': 'input.test', 'result': 'output.result', 'statements': 2}
            (root / 'input.test').write_text('BEGIN;\n--mode ann\n--restart\nSELECT id FROM t;\n', encoding='utf-8')
            (root / 'output.result').write_text(json.dumps([{}, {'rows': []}]), encoding='utf-8')
            steps, assertions = public.read_case(case, root)
            self.assertEqual((len(steps), len(assertions)), (4, 2))
            (root / 'input.test').write_text('BEGIN;\n--restarrt\nSELECT id FROM t;\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'unknown driver instruction'):
                public.read_case(case, root)
            (root / 'input.test').write_text('BEGIN;\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'assertions=2'):
                public.read_case(case, root)

    def test_mode_controls_are_opt_in_and_do_not_escape_sql_strings(self):
        text = "--mode ann\nSELECT 'hello\n--mode exact\nworld';\n--mode exact\n"
        plain = list(statements(io.StringIO(text)))
        self.assertFalse(any(isinstance(item, ModeChange) for item in plain))
        enabled = list(statements(io.StringIO(text), local_mode_controls=True))
        self.assertEqual(enabled[0], ModeChange('ann'))
        self.assertEqual(enabled[-1], ModeChange('exact'))
        self.assertIn('hello\n--mode exact\nworld', enabled[1])
        self.assertEqual(len(enabled), 3)


if __name__ == '__main__':
    unittest.main()
