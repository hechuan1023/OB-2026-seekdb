# 75-iteration CAGRA candidate

Historical round-2 candidate. On `amd-cagra-opt-round3`, see
[AMD-CANDIDATE-ROUND3.md](AMD-CANDIDATE-ROUND3.md) for current defaults and checks.

This candidate is based on V5 commit `634888d9b037153fa143d2c913ba4b25503e1d62`.
Development branch: `amd-cagra-opt-round2`. The older `amd-cagra-submit` branch is
not updated by this change.

## Implementation

The default NN-Descent construction iteration count is 75 instead of 100.
Graph degree remains 128, intermediate graph degree remains 256, and search
parameters remain unchanged. No dataset-specific or evaluation-phase-specific
selection is introduced. The fixed hipVS library, CAGRA search, FP32 SQL interface,
snapshot format, and CPU VSAG implementation are unchanged.

The candidate does not require evaluator-supplied environment variables.
For development, `OB_CUVS_PROFILE=baseline` restores 100 iterations; an explicit
`OB_CUVS_NN_DESCENT_NITER` overrides that profile as before.

The development benchmark also disables MySQL column headings so returned IDs
can be parsed. The current benchmark launches a mysql process per query, so its
timings include client startup. It is not a reproduction of official throughput.

## Reported development evidence, 2026-10-30

The user ran the tests on a Radeon Cloud instance and supplied the results.
These are synthetic Gaussian FP32 datasets, not formal SIFT250K or Hidden250K.
Both comparisons used 250000 vectors, fixed graph/search parameters and the
same deterministic inputs. Quality was checked with 256 generated queries,
exact CPU reference neighbors computed in bounded batches, and native SQL
approximate queries. Returned result cardinality, uniqueness and ID ranges were
checked. No official hidden data or annotations were read.

| Dimension | 100 iterations, build + first query | 75 iterations | Speedup |
|---|---:|---:|---:|
| 128 | 44.215 s | 37.859 s | about 1.168x |
| 256 | 48.453 s (another run: 49.855 s) | 40.583 s | about 1.194x |

The 128-dimensional reference log contained a success record and a later missing
table error, possibly from overlapping tests. The existing table was subsequently
read successfully during expanded quality comparison; its reported build timing
should be repeated in an isolated run before making a stable performance claim.
Build samples are insufficient to estimate variance or official speedup.

| Dimension | Metric | 100 iterations | 75 iterations | Difference |
|---|---|---:|---:|---:|
| 128 | Recall@10 | 0.987891 | 0.987500 | -0.000391 |
| 128 | Recall@100 | 0.967109 | 0.966797 | -0.000313 |
| 256 | Recall@10 | 0.943359 | 0.942187 | -0.001172 |
| 256 | Recall@100 | 0.895391 | 0.893164 | -0.002227 |

For 256 dimensions, paired query-only comparisons against persisted graphs in
the same server alternated the order, with 512 queries per group. The reported
75/100 throughput ratios were 1.0117 for Top-10 and 0.9775 for Top-100. These
results suggest no large regression in that workload; they do not prove the
official nq=4096 throughput gate. Comparable expanded throughput measurement
for 128 dimensions is still outstanding.

A 50-iteration prototype was rejected: its 32-query Recall@100 fell from 0.8984
to 0.8844 in the 256-dimensional development workload.

## Reproduction and remaining checks

Compile with the contest's unchanged build command and frozen dependencies.
Do not set `OB_CUVS_PROFILE=baseline` when measuring the candidate's default.
Capture server build logs to confirm `niter=75`, and verify snapshot persistence,
the first SQL query, native GPU build/search execution and CPU VSAG SQL behavior.
Run baseline and candidate from fresh indexes with identical public inputs and
record build time including the first query. Do not use bridge-only timing as
the end-to-end score. Avoid concurrent benchmark processes or shared test DBs.

The resolver can be tested without a GPU:

```bash
python3 tools/hipvs_bridge/test_params.py --cc gcc
```

This test compiles the actual production resolver extracted from the C source
and checks default selection, baseline profile, explicit override precedence and
existing profiles/bounds. It does not test GPU ABI or quality.

Official SIFT and hidden-data recall, repeated graph builds, nq=4096 query gates,
VRAM limits, and official profiler verification remain unconfirmed. The candidate
is ready for formal evaluation of these risks, not certified to pass or score full
marks. A relative quality drop under 0.005 on synthetic data is not an official
quality guarantee. The frozen V5 library and official measurements remain the
authority.
