"""Validate local handoff files, not the official evaluation protocol."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError(f'Nonstandard JSON constant: {value}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--git', action='store_true', help='Also verify source bytes in HEAD')
    args = parser.parse_args()
    path = ROOT / 'agent' / 'conversation.json'
    data = path.read_bytes()
    assert len(data) <= 128 * 1024, 'conversation.json exceeds 128 KiB'
    material = json.loads(data.decode('utf-8'), object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    assert isinstance(material, dict), 'conversation.json must be an object'
    assert not (ROOT / 'conversation.json').exists(), 'Keep one canonical conversation.json'
    assert (ROOT / 'Dockerfile').stat().st_size > 0
    manifest = json.loads((ROOT / 'docs' / 'source-manifest.json').read_text(encoding='utf-8'))
    assert material['version'] == manifest, 'Material and source manifest disagree'
    for name, digest in manifest['sha256'].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, f'Source changed: {name}'
        if args.git:
            blob = subprocess.check_output(['git', 'show', f'HEAD:{name}'], cwd=ROOT)
            assert hashlib.sha256(blob).hexdigest() == digest, f'Committed bytes differ: {name}'
    if args.git:
        for name in ('agent/conversation.json', 'docs/source-manifest.json'):
            blob = subprocess.check_output(['git', 'show', f'HEAD:{name}'], cwd=ROOT)
            assert blob == (ROOT / name).read_bytes(), f'Uncommitted material: {name}'
    for name in manifest.get('verified_reports', []):
        report = json.loads((ROOT / name).read_text(encoding='utf-8'))
        for source, digest in {**report['source_sha256'], **report.get('driver_sha256', {})}.items():
            assert manifest['sha256'][source] == digest, f'Benchmark source mismatch: {name}: {source}'
    for historical in manifest.get('historical_reports', []):
        report = json.loads((ROOT / historical['path']).read_text(encoding='utf-8'))
        for source, digest in report['source_sha256'].items():
            blob = subprocess.check_output(['git', 'show', f"{historical['source_commit']}:{source}"], cwd=ROOT)
            assert hashlib.sha256(blob).hexdigest() == digest, f'Historical source mismatch: {historical["path"]}: {source}'
    print(json.dumps({'conversation_bytes': len(data), 'json_valid': True,
                      'source_files_verified': len(manifest['sha256']),
                      'benchmark_reports_verified': len(manifest.get('verified_reports', [])),
                      'historical_reports_verified': len(manifest.get('historical_reports', [])),
                      'git_blob_verified': args.git,
                      'official_protocol_checked': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
