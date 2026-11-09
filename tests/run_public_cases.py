"""Record reproducible public-case evidence; never report official admission."""
import argparse
import hashlib
import io
import json
import platform
import sys
import unittest
from pathlib import Path

from tests import test_public_cases as public

UPSTREAM_COMMIT = '9f6df2ba5acf871376e22b64a3d0724e4b575044'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    stream = io.StringIO()
    suite = public.load_tests(None, None, None)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    root = public.ROOT.parent
    paths = sorted(public.ROOT.rglob('*')) + sorted((root / 'minivectordb').glob('*.py'))
    paths += [root / 'native/exact.cpp', Path(public.__file__), Path(__file__), root / 'tests/test_recovery.py']
    counts = {'sql': 0, 'restart': 0, 'mode': 0, 'expected_error': 0}
    for case in public.MANIFEST['cases']:
        steps, expected = public.read_case(case)
        counts['sql'] += case['statements']
        counts['restart'] += sum(line == '--restart' for _, line in steps)
        counts['mode'] += sum(line.startswith('--mode ') for _, line in steps)
        counts['expected_error'] += sum('error' in item for item in expected)
    report = {
        'upstream_commit': UPSTREAM_COMMIT,
        'package_version': public.MANIFEST['version'],
        'environment': {'platform': platform.platform(), 'python': platform.python_version()},
        'case_order': [case['id'] for case in public.MANIFEST['cases']],
        'case_count': len(public.MANIFEST['cases']), 'counts_per_path': counts,
        'execution_paths': ['typed', 'stdio'], 'tests_run': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors),
        'exit_code': 0 if result.wasSuccessful() else 1,
        'error_mapping': public.ERROR_CODES,
        'source_sha256': {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in paths if p.is_file()},
        'output': stream.getvalue(), 'official_protocol_checked': False,
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(report['output'], end='')
    print(json.dumps({k: report[k] for k in ('case_count', 'counts_per_path', 'tests_run', 'exit_code')}))
    return report['exit_code']


if __name__ == '__main__':
    sys.exit(main())
