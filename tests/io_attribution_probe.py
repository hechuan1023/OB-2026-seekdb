"""Probe local cgroup-v2 data-device write attribution in an isolated volume."""
import argparse
import json
import subprocess
import uuid


PROGRAM = r'''
import json, os
from pathlib import Path

device = os.stat('/data').st_dev
key = str(os.major(device)) + ':' + str(os.minor(device))
def io():
    result = {}
    for line in Path('/sys/fs/cgroup/io.stat').read_text().splitlines():
        parts = line.split()
        result[parts[0]] = dict(item.split('=', 1) for item in parts[1:])
    return result
before = io()
path = '/data/io-attribution-probe'
with open(path, 'wb', buffering=0) as f:
    block = os.urandom(1024 * 1024)
    for _ in range(8):
        f.write(block)
    os.fsync(f.fileno())
os.sync()
after = io()
with open(path, 'rb', buffering=0) as f:
    os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    total = 0
    while chunk := f.read(1024 * 1024):
        total += len(chunk)
after_read = io()
os.unlink(path)
old = int(before.get(key, {}).get('wbytes', 0))
new = int(after.get(key, {}).get('wbytes', 0))
print(json.dumps({'device': key, 'mounts': [line for line in Path('/proc/self/mountinfo').read_text().splitlines() if ' /data ' in line],
                  'bytes_written_by_test': 8 * 1024 * 1024,
                  'device_write_bytes_before': old, 'device_write_bytes_after': new,
                  'device_write_bytes_delta': new - old, 'bytes_read_by_test': total,
                  'device_read_bytes_delta': int(after_read.get(key, {}).get('rbytes', 0)) - int(after.get(key, {}).get('rbytes', 0)),
                  'io_before': before, 'io_after_write': after, 'io_after_read': after_read}))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    volume = 'mvdb-io-probe-' + uuid.uuid4().hex[:12]
    subprocess.run(['docker', 'volume', 'create', volume], check=True, capture_output=True)
    try:
        raw = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none',
                                       '--cpus=2', '--memory=256m', '--memory-swap=256m',
                                       '-v', volume + ':/data', '--entrypoint', 'python',
                                       args.image, '-c', PROGRAM], text=True, encoding='utf-8')
        result = json.loads(raw)
        assert result['mounts'], 'No /data mount found'
        assert result['device_write_bytes_delta'] >= result['bytes_written_by_test'], result
        assert result['device_read_bytes_delta'] >= result['bytes_read_by_test'], result
        print(json.dumps(result, indent=2))
    finally:
        subprocess.run(['docker', 'volume', 'rm', volume], check=True, capture_output=True)


if __name__ == '__main__':
    main()
