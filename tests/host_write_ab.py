"""Interleaved host write A/B: run tests.benchmark via subprocess, parse JSON robustly.

The benchmark prints progress JSON lines before the final report; find the
report object by scanning for the line that starts a complete JSON document
with a "rows" key.
"""
import json
import statistics
import subprocess
import sys


def run_once(cwd):
    out = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'tests.benchmark',
                         '--rows', '10000', '--queries', '1000', '--seed', '20261111'],
                        cwd=cwd, capture_output=True, text=True, timeout=900)
    report = None
    decoder = json.JSONDecoder()
    marker = '"workload_version"'
    if marker in out.stdout:
        idx = out.stdout.rindex(marker)
        try:
            obj, _ = decoder.raw_decode(out.stdout, out.stdout.rindex('{\n', 0, idx))
            if isinstance(obj, dict) and 'write_seconds' in obj:
                report = obj
        except ValueError:
            pass
    if report is None:
        print(out.stdout[-500:], out.stderr[-500:], file=sys.stderr)
        raise SystemExit(f'no report found in {cwd}')
    return report


def main():
    base, cand = sys.argv[1], sys.argv[2]
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    results = {'base': [], 'cand': []}
    for i in range(rounds):
        for name, cwd in (('base', base), ('cand', cand)):
            report = run_once(cwd)
            results[name].append(report['write_seconds'])
            print(i, name, report['write_seconds'], flush=True)
    for name in ('base', 'cand'):
        values = results[name]
        print(name, 'write median', round(statistics.median(values), 4),
              [round(v, 3) for v in values], flush=True)


if __name__ == '__main__':
    main()
