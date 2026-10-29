#!/usr/bin/env python3
"""Compile and exercise the actual parameter resolver without GPU dependencies.

This checks parameter selection only, not hipVS ABI, GPU execution, or quality.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=shutil.which("cc") or shutil.which("gcc"))
    args = parser.parse_args()
    if not args.cc:
        parser.error("a C compiler is required (--cc)")
    source = Path(__file__).with_name("seekdb_cuvs_bridge.c").read_text()
    start = source.index("typedef struct {\n  int   build_algo;")
    end = source.index("static double sk_now_sec()", start)
    unit = (
        '#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n'
        '#define NN_DESCENT 1\n#define ITERATIVE_CAGRA_SEARCH 2\n'
        + source[start:end]
        + '\nint main(void) { sk_cuvs_params p; sk_resolve_params(&p); '
          'printf("%d %ld %ld %ld %ld %ld\\n", p.build_algo, p.graph_degree, '
          'p.intermediate_graph_degree, p.nn_descent_niter, p.itopk_size, '
          'p.search_width); return 0; }\n'
    )
    cases = [
        ({}, (1, 128, 128, 15, 1024, 4)),
        ({"OB_CUVS_PROFILE": "baseline"}, (1, 128, 256, 100, 0, 4)),
        ({"OB_CUVS_PROFILE": "fast"}, (1, 64, 128, 30, 0, 4)),
        ({"OB_CUVS_PROFILE": "aggressive"}, (1, 64, 128, 20, 0, 4)),
        ({"OB_CUVS_PROFILE": "baseline", "OB_CUVS_NN_DESCENT_NITER": "75"},
         (1, 128, 256, 75, 0, 4)),
        ({"OB_CUVS_NN_DESCENT_NITER": "100"}, (1, 128, 128, 100, 1024, 4)),
        ({"OB_CUVS_NN_DESCENT_NITER": "invalid"}, (1, 128, 128, 15, 1024, 4)),
        ({"OB_CUVS_NN_DESCENT_NITER": "0"}, (1, 128, 128, 1, 1024, 4)),
        ({"OB_CUVS_BUILD_ALGO": "iterative_search"}, (2, 128, 128, 15, 1024, 4)),
        ({"OB_CUVS_ITOPK_SIZE": "512"}, (1, 128, 128, 15, 512, 4)),
        ({"OB_CUVS_PROFILE": "baseline", "OB_CUVS_ITOPK_SIZE": "1024"},
         (1, 128, 256, 100, 1024, 4)),
    ]
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("OB_CUVS_")}
    with tempfile.TemporaryDirectory(prefix="seekdb-cuvs-params-") as directory:
        path = Path(directory)
        cfile = path / "resolver.c"
        binary = path / ("resolver.exe" if os.name == "nt" else "resolver")
        cfile.write_text(unit)
        subprocess.run([args.cc, "-std=c99", "-Wall", "-Wextra", "-Werror",
                        str(cfile), "-o", str(binary)], check=True)
        for overrides, expected in cases:
            result = subprocess.check_output([str(binary)], env={**clean_env, **overrides}, text=True)
            actual = tuple(map(int, result.split()))
            if actual != expected:
                raise AssertionError((overrides, expected, actual))
    print(f"PASS: {len(cases)} production parameter-resolution cases")


if __name__ == "__main__":
    main()
