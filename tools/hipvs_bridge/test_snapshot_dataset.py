"""Real-GPU snapshot dataset round trip; uses only the Python standard library."""
import argparse
import array
import ctypes as C
import random
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("library")
    args = parser.parse_args()
    lib = C.CDLL(str(Path(args.library).resolve()))
    pointer = C.POINTER(C.c_float)
    for name, arguments, result in (
        ("seekdb_cuvs_build", [pointer, C.c_long, C.c_long], C.c_void_p),
        ("seekdb_cuvs_copy_dataset", [C.c_void_p, pointer, C.c_long, C.c_long], C.c_int),
        ("seekdb_cuvs_serialize", [C.c_void_p, C.c_char_p], C.c_int),
        ("seekdb_cuvs_deserialize", [C.c_char_p, C.c_long, C.c_long], C.c_void_p),
        ("seekdb_cuvs_free", [C.c_void_p], None),
    ):
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    assert lib.seekdb_cuvs_copy_dataset(None, None, 0, 0) != 0
    rng = random.Random(20261030)
    # Non-aligned dimensions exercise the library dataset's padded row strides.
    for dim in (17, 128, 256):
        rows = 1536
        original = array.array("f", (rng.uniform(-8, 8) for _ in range(rows * dim)))
        original[0:4] = array.array("f", [0.0, -0.0, 1e-30, -1e10])
        storage = (C.c_float * len(original)).from_buffer(original)
        copied = (C.c_float * len(original))()
        handle = lib.seekdb_cuvs_build(storage, rows, dim)
        assert handle, f"build failed for dimension {dim}"
        restored = None
        try:
            assert lib.seekdb_cuvs_copy_dataset(handle, copied, rows, dim + 1) != 0
            assert lib.seekdb_cuvs_copy_dataset(handle, None, rows, dim) != 0
            assert lib.seekdb_cuvs_copy_dataset(handle, copied, rows, dim) == 0
            assert bytes(copied) == original.tobytes(), "live copy changed FP32 bits"
            with tempfile.TemporaryDirectory() as directory:
                filename = str(Path(directory) / "index.bin").encode()
                assert lib.seekdb_cuvs_serialize(handle, filename) == 0
                restored = lib.seekdb_cuvs_deserialize(filename, rows, dim)
                assert restored, "snapshot deserialize failed"
                assert lib.seekdb_cuvs_copy_dataset(restored, copied, rows, dim) == 0
                assert bytes(copied) == original.tobytes(), "snapshot changed FP32 bits"
            print(f"FP32 snapshot round trip passed: {rows} x {dim}", flush=True)
        finally:
            lib.seekdb_cuvs_free(restored)
            lib.seekdb_cuvs_free(handle)


if __name__ == "__main__":
    main()
