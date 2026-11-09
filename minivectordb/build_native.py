"""Build the optional, repository-owned C++ kernel from source."""
import os
import subprocess
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / 'minivectordb' / ('_native.dll' if os.name == 'nt' else '_native.so')
    command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-O3', '-fno-fast-math',
               '-ffp-contract=off', '-shared', '-static-libstdc++', '-static-libgcc']
    command += ['-static'] if os.name == 'nt' else ['-fPIC']
    command += [str(root / 'native/exact.cpp'), '-o', str(output)]
    subprocess.run(command, check=True)
    print(output)


if __name__ == '__main__':
    main()
