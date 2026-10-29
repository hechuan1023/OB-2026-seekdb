# Round 3: FP32 CAGRA construction and reusable query worker

Branch: `amd-cagra-opt-round3`. Parent candidate: `b21935ada6d7592abc33c15666c3e581d14b3478`.
The previous `amd-cagra-opt-round2` and `amd-cagra-submit` branches are retained.
The commit remains a descendant of the frozen V5 baseline
`634888d9b037153fa143d2c913ba4b25503e1d62`.

## Changes

The bridge defaults to a 192-degree intermediate graph, 192 final graph edges,
and 35 NN-Descent iterations, instead of the V5 settings 256/128/100. Retaining
more final edges compensated for reducing intermediate work in development
quality tests. These settings are uniform: there is no selection by document,
query, dataset identity, dimension, or evaluation stage. The explicit development
profile `OB_CUVS_PROFILE=baseline` restores all three V5 settings. The candidate
requires no evaluator-provided environment overrides.

GPU searches previously created and joined a 32 MiB-stack pthread per single-row
query. The adaptor now reuses one synchronous large-stack search worker. It
retains the existing registry lock, index ownership, original construction path,
and per-query CAGRA search call. If the reusable worker cannot start, the original
thread path remains available. Requests finish before returning to the caller;
exceptions propagate back, and normal worker teardown joins its thread.

Data, queries and distances remain FP32. The snapshot format is unchanged. The
fixed hipVS library and its CAGRA search are unmodified. SQL index parameters and
the CPU VSAG path are unchanged. No query batching, CPU replacement of GPU search,
prebuilt-graph reuse across build trials, or changes to official evaluation tools
are introduced.

## Executed development checks (2026-10-30)

The agent executed these checks directly over SSH on the user's Radeon Cloud
instance, rather than inferring success from screenshots. The source was built
with the contest CMake switches, ROCm 7.2.4, the installed dependency cache and
the platform's unmodified `/opt/hipvs`. The control uses the previous source
candidate with V5 bridge settings and the original per-query thread path; it is
not a separately compiled pristine frozen-V5 checkout. These results are not
official scores.

Inputs are deterministic synthetic Gaussian datasets: 250000 FP32 vectors each
at 128 and 256 dimensions, using the same six-decimal SQL representation on both
paths. They are not formal SIFT250K or Hidden250K. Exact CPU neighbors were
computed from the actual rounded development inputs in bounded batches.

- Full seekdb and bridge compilation succeeded.
- Nine production parameter-selection cases passed on Windows and Linux.
- Eight thousand concurrent synchronous worker jobs, exception recovery and
  teardown checks passed on Windows and Linux.
- Native GPU build and snapshot logs confirmed the requested graph settings and
  successful GPU CAGRA persistence. Process mappings confirmed the intended
  bridge was loaded.
- Each of four final build cases had 256-query quality checks and 4096 native
  single-row SQL queries for each of Top-10 and Top-100: 32768 measured SQL
  queries, each checked for cardinality, unique IDs and valid ID range.
- Eight real concurrent SQL clients issued 256 GPU queries per candidate
  dimension, 512 total, with valid result cardinality and identities.
- CPU VSAG SQL create/insert/approximate-search checks passed in both candidate
  cases. Dedicated fresh-instance snapshots were readable after process restart.

The private SQL harness uses the installed libmysqlclient through ctypes and a
persistent connection. No extra runtime package is added to the product. Each
4096-query timing cycles 256 distinct generated probes 16 times, with 16 warmup
queries. This differs from the organizer's actual query set and is disclosed
rather than presented as an official benchmark.

### Build and quality in fresh dedicated runtime directories

Each case below used a separate database directory with no historical indexes.
Time includes CREATE VECTOR INDEX and the first legal approximate SQL query.

| Dimension | Control build + first query | Candidate | Speedup |
|---|---:|---:|---:|
| 128 | 19.666280 s | 11.118212 s | 1.768835x |
| 256 | 23.483215 s | 13.745394 s | 1.708442x |

| Dimension | Metric | Control | Candidate |
|---|---|---:|---:|
| 128 | Recall@10 | 0.990234375 | 0.992187500 |
| 128 | Recall@100 | 0.967500000 | 0.977539063 |
| 256 | Recall@10 | 0.945703125 | 0.955078125 |
| 256 | Recall@100 | 0.895781250 | 0.916054688 |

At the contest's dimension weights, this single development build pair gives
approximately 1.732x weighted build speedup. It is not evidence of 3x/full marks,
and repeated fresh builds and the official data remain necessary.

### Query worker isolation and final query timing

An ABBA comparison used the same persisted F32 graphs with only the query worker
changed. Pooled candidate/control QPS ratios were approximately 1.93/1.81 for
128-dimensional Top-10/100 and 1.89/1.80 for 256-dimensional Top-10/100. Recall
was equal for every group. Some complete ID-order hashes varied even between
control repeats at 256-dimensional Top-100, so hash equality was not treated as
a product requirement; result validity and exact-reference recall were checked
independently. The first hash-asserting experiment and its failure were retained.

With final graph settings, the 4096-query measurements in the shared development
catalog gave Top-10/100 geometric QPS ratios of 1.6518 at 128 dimensions and
1.6418 at 256 dimensions. The fresh-catalog query-only repeat gave:

| Dimension | Top-k | Control QPS | Candidate QPS |
|---|---:|---:|---:|
| 128 | 10 | 130.508 | 229.665 |
| 128 | 100 | 127.387 | 216.541 |
| 256 | 10 | 123.123 | 199.256 |
| 256 | 100 | 124.041 | 197.644 |

Each row contains 4096 single-row SQL queries, cycling 256 distinct probes.
Every result had k valid, distinct IDs. Only the official query workload
determines scoring.

### Memory validation and an investigated failed sample

The initial 128-dimensional shared-catalog GPU memory ratio was 1.4403, which
exceeded the 1.25 gate. Logs also showed historical indexes being loaded in the
background. The result was not counted as a pass. Both paths were rerun using
separate empty runtime directories so only the tested index existed.

| Dimension | Control GPU peak | Candidate GPU peak | Ratio |
|---|---:|---:|---:|
| 128 | 926633984 B | 797220864 B | 0.860341 |
| 256 | 862380032 B | 925892608 B | 1.073648 |

GPU memory was sampled from the assigned device's sysfs counter every 20 ms.
Peak seekdb RSS in the fresh cases was below 4 GiB. Sampling is observational,
not a substitute for the organizer's profiler or peak-memory measurement.

## Reproduction and limitations

Use the unchanged official source build command and fixed dependencies. Do not
set `OB_CUVS_PROFILE=baseline` when measuring the candidate's defaults. Confirm
build logs show `degree=192 inter=192 niter=35`. Compare with a fresh control at
`degree=128 inter=256 niter=100`, identical public inputs, and the same resources.
Include the first query in build time. Do not compare timings from different
cloud instances or confuse bridge-only timing with end-to-end SQL timing.

Host-only focused checks:

```bash
python3 tools/hipvs_bridge/test_params.py --cc gcc
python3 unittest/oblib/vector/test_gpu_search_worker.py --cxx g++
```

The raw private harnesses, logs, binaries, metadata and JSON evidence are backed
up outside the product repository under the local `_build` directory. They do not
replace organizer tests. Other explored GPU construction mode and FP16 prototypes
were not included in this candidate. Official SIFT/Hidden recall, GPU profiler
checks, exact formal resource peaks, compiler differences and final scoring
remain unconfirmed. Submit the source commit for that evaluation; do not use
development measurements to claim a guaranteed score.
