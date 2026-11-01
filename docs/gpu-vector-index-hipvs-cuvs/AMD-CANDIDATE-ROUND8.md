# Round 8: SINGLE_CTA investigation (not submission-ready)

Branch: `amd-cagra-opt-round8-single-cta`.
Parent: `b9ad25daf93a3de584738f3453de9aecbdc21213` (round 7 is unchanged).

This is an unvalidated experiment, not a proven fix for the platform failure.
Another public entrant reported a GPU spot-check failure with MULTI_CTA and
reverted to SINGLE_CTA. That observation does not establish the cause of our
failure or a requirement to use SINGLE_CTA:
https://github.com/Terrified-of-Logic/seekdb-ans/commit/70bbc4290789640b7ee98f7241f058e84c77501b

## Experimental configuration

Use final/intermediate graph degrees 128/128, NN-Descent iterations 15,
itopk 512 and search width 32 uniformly. Explicitly select the fixed-library
SINGLE_CTA kernel for this configuration. Keep the previous MULTI_CTA selection
for explicit frontiers above 512. Historical development profiles retain their
previous graph, frontier and width settings.

A smaller frontier cannot simply replace 4096 in round 7 without checking graph
quality. This experiment therefore increases graph degree and iteration count.
No result, dataset identity, workload phase or profiler presence selects policy.
The native worker and snapshot code are unchanged.

## Evidence and next steps

Local parameter resolver: 11 cases passed. This is not GPU validation.
The old AMD SSH endpoint refused connections on 2026-11-01. Native compilation,
small-input GPU smoke, public SIFT and independent 256-dimensional development
quality, paired cold-build timings, 4096 single-row queries for both Top-10/100,
restart readback, CPU path and GPU profiling remain pending on a new instance.
Do not report round-7 recall, throughput or 3.15x speedup as round-8 results.

Use matching engine and bridge from this branch with the fixed hipVS library.
Run with no OB_CUVS environment overrides for the candidate. Compare against a
separate frozen V5 checkout on the same instance. Require exact k distinct legal
IDs, both recall absolute and baseline-delta gates, per-dataset throughput >= .9,
and resource limits before publishing a submission candidate. Switching kernels
requires quality and performance remeasurement, not just a kernel-name check.
