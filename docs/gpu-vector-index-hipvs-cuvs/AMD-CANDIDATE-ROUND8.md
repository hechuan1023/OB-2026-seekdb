# Round 8: SINGLE_CTA diagnostic candidate

Branch: `amd-cagra-opt-round8-single-cta`.
Based on round 7 (`b9ad25daf93a3de584738f3453de9aecbdc21213`), which remains unchanged.
This is a compatibility investigation, not a faster replacement for round 7 or
an officially verified fix for the execution failure.

## Policy and motivation

Uniform defaults: graph/intermediate degrees 192/192, NN-Descent iterations 25,
search frontier 512, search width 16, explicitly SINGLE_CTA. Explicit frontiers
above 512 still use the fixed library MULTI_CTA implementation. Historical
baseline/fast/aggressive profiles retain their prior settings, including width 4.
The native worker, snapshot format and CPU path are unchanged. No dataset ID,
query identity, evaluation phase or profiler state selects a policy.

A public entrant reported GPU spot-check failures and changed MULTI_CTA back to
SINGLE_CTA. This is an unconfirmed diagnostic lead, not proof of our root cause
or a contest requirement to use SINGLE_CTA:
https://github.com/Terrified-of-Logic/seekdb-ans/commit/70bbc4290789640b7ee98f7241f058e84c77501b

## Rejected experiments

The initial commit on this branch, f32dc78cd, used 128/128/15 and width 32.
Its 256-dimensional SQL Recall@100 was .846889648, failing quality gates; do not
submit that commit. Degree 192 with width 32 also produced a GPU search error.
The revised width 16 configuration below passed development query validation.

## Native development results, 2026-11-01

One gfx1100 Radeon Cloud instance, fixed hipVS/ROCm 7.2.4, 16 CPU quota,
8 GiB configured seekdb memory. Full source engine build and bridge build passed.
The control reused the archived engine with V5 native source behavior and explicit
V5 bridge settings; it was not a newly rebuilt pristine V5 artifact. This is a
development comparison, not the official paired evaluator.

Inputs: the same public SIFT subset and deterministic 256-dimensional Gaussian
inputs documented in round 7, 250000 rows and 4096 distinct queries. The SIFT
subset's ordering/IDs are not asserted identical to official SIFT250K. Each k
used 4096 single-row SQL queries after 16 warmups. Each build had a fresh runtime
and no reusable graph. Exact references were computed against all loaded rows.

| Input | Control DDL + first query | Candidate | Speedup |
|---|---:|---:|---:|
| Public SIFT 128d | 18.459797 s | 10.104953 s | 1.826807x |
| Gaussian 256d | 21.171685 s | 12.640301 s | 1.674935x |

Weighted geometric build speedup: 1.734107x. This is substantially slower than
the round-7 development candidate and does not reach the 3x full-build-score line.

| Input | Candidate Recall@10 / @100 | Control Recall@10 / @100 | Candidate QPS Top-10 / Top-100 | Geometric throughput ratio |
|---|---:|---:|---:|---:|
| SIFT | .999951172 / .999968262 | .999951172 / .999963379 | 228.617 / 222.695 | 2.038633x |
| Gaussian | .949316406 / .907795410 | .950976563 / .899240723 | 288.086 / 267.420 | 2.190971x |

Both tested inputs passed absolute recall, baseline-delta and .90 throughput
checks. Every query returned exactly k distinct valid IDs. Full process restart
readback passed. Candidate SQL logs contained no bridge search errors or lazy
VSAG fallback builds. The earlier native 1024-row CPU/GPU smoke passed on the
same unchanged engine. Direct GPU smoke on the revised bridge passed at
1024/10000 rows and 128/256 dimensions, both Top-10/100.

## Profiling and limitations

Standalone bridge profiling generated real kernel records, including the fixed
library single_cta_search::search_kernel. This proves that bridge probe's path,
not formal SQL profiler qualification. Native SQL startup under the local
ROCProfiler SDK aborted before queries for both candidate and archived control;
these failures and empty kernel traces are retained, not counted as passes.

Hidden input quality, native GPU spot-check acceptance, formal peak resource
limits and the official score remain unverified. No official failure code for
round 7 is available, so changing the kernel is not a confirmed root-cause fix.
Keep round 7 available and report this as a separate diagnostic candidate.

The development controllers, source proof, binaries, SQL evidence, initial
failures and profiler logs are preserved in the stage backup. Use the existing
fixed build.sh/CMake flow and matching engine/bridge from this branch, with no
OB_CUVS environment overrides for candidate runs. The detailed native test
controllers are in the backup; input origins and FP32 generation are unchanged
from the previous stage.
