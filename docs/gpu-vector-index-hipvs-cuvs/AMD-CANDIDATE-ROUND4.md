# Round 4: remove duplicate FP32 snapshot storage

Branch: `amd-cagra-opt-round4`, based on round 3 commit
`0bf72476b839ff7a7cde4e311cf380b019dcabfa`. Previous submission branches
are retained. The frozen V5 ancestor is unchanged.

## Implementation

The previous OBCUVS02 outer snapshot wrote IDs, raw FP32 vectors and a native
CAGRA snapshot. `cuvsCagraSerialize(..., true)` already includes those same FP32
vectors. OBCUVS03 writes IDs and the native snapshot once. After deserialization,
the bridge uses `cuvsCagraIndexGetDataset` and a synchronous device-to-host 2D copy
to restore the adaptor's original row-major vectors, including padded device row
strides. These vectors remain available to the existing CPU fallback path.

The bridge validates shape, FP32 type, device, strides and size bounds; the
non-owning tensor view's deleter releases its shape metadata. A failed copy
destroys the newly loaded GPU handle and leaves the registry unchanged. The
adaptor still reads OBCUVS02, including its original external vector payload.
Existing CPU snapshot handling is unchanged. Graph parameters remain 192/192/35;
queries, source vectors and distances remain FP32. The installed hipVS library,
GPU search algorithm and benchmark inputs are unmodified.

Build and deploy the engine together with this branch's bridge: the new
`seekdb_cuvs_copy_dataset` symbol is required. A round-3 engine does not understand
new OBCUVS03 files; retaining the old Git branch does not make a new-format runtime
downgrade-compatible. Keep independent runtime directories when comparing builds.

## Executed checks, 2026-10-30

The agent ran these checks over SSH on the same Radeon Cloud instance used for
round 3. Development data are synthetic 250000-row Gaussian FP32 datasets, not
the official SIFT250K or hidden input. Each quality check uses 256 exact-reference
queries. Native SQL was executed through a persistent libmysqlclient connection.

| Dimension | Round 3 build + first SQL query | Round 4 | Observed reduction |
|---|---:|---:|---:|
| 128 | 11.118212 s | 10.501202 s | 5.55% |
| 256 | 13.745394 s | 13.007846 s | 5.37% |

Each timed build used its own initially empty runtime. These are single build
observations at different times, not repeated interleaved trials or an official
score. GPU graph construction is nondeterministic; do not attribute every timing
or recall difference to persistence. The deterministic improvement is removal
of exactly `rows * dimensions * sizeof(float)` bytes from each outer snapshot.

For the observed native graph-file sizes, total adaptor payload sizes are:

| Dimension | OBCUVS02 bytes | OBCUVS03 bytes | Bytes removed |
|---|---:|---:|---:|
| 128 | 450001745 | 322001745 | 128000000 |
| 256 | 706001745 | 450001745 | 256000000 |

| Dimension | Recall@10 | Recall@100 | After full restart |
|---|---:|---:|---|
| 128 | 0.993359375 | 0.979296875 | Both identical |
| 256 | 0.957812500 | 0.916796875 | Both identical |

New-engine readback of the two round-3 OBCUVS02 SQL databases also passed,
preserving their previous recall values (128: .9921875/.9775390625;
256: .955078125/.9160546875).

After restart, each new-format database ran 4096 individual SQL queries per k,
cycling 256 distinct probes 16 times, with 16 warmup calls. All results had exactly
k distinct valid IDs. This differs from the formal query set.

| Dimension | Top-k | Round 3 QPS | Round 4 QPS |
|---|---:|---:|---:|
| 128 | 10 | 229.665 | 231.084 |
| 128 | 100 | 216.541 | 217.537 |
| 256 | 10 | 199.256 | 205.024 |
| 256 | 100 | 197.644 | 198.891 |

Additional executed checks:

- Full engine and bridge compilation passed.
- Real-GPU live/serialized FP32 copies at dimensions 17, 128 and 256 were bitwise
  identical, including signed zero and small/large finite values. Dimension 17
  exercises non-aligned row storage. Invalid shape and null output are rejected.
- Production snapshot framing compiled with a bridge double: v2/v3 readback,
  exact payload size, ID/vector restoration, every truncated prefix, invalid
  headers, dataset-copy failure cleanup and successful retry passed.
- Nine production parameter tests and 8000 concurrent worker jobs passed again.

The first 256-dimensional runtime bootstrap failed because the private 98 GiB
workspace reached its database log-space reserve. After confirming no active
database work, old generated lab databases were removed with their logs archived;
the incomplete bootstrap directory was recreated. The successful 256-dimensional
case starts from an empty runtime. The failed attempt and logs are retained.

Round 4 does not establish full marks. Formal input quality, official profiler
limits and scoring remain to be verified. Round-3 resource observations remain
historical measurements, not a fresh round-4 peak-memory certification.

## Reproduction

Build the matching bridge before linking the engine, then run:

```bash
LD_LIBRARY_PATH=/opt/hipvs/lib:/opt/rocm/lib \
  python3 tools/hipvs_bridge/test_snapshot_dataset.py /path/to/libseekdb_cuvs_bridge.so
python3 unittest/oblib/vector/test_gpu_snapshot.py --cxx g++
python3 unittest/oblib/vector/test_gpu_search_worker.py --cxx g++
python3 tools/hipvs_bridge/test_params.py --cc gcc
```

The first test requires the assigned GPU and fixed hipVS installation. Framing
tests require Linux and pthreads but no GPU. Evidence and controller scripts are
also saved in the local stage backup outside the product repository.
