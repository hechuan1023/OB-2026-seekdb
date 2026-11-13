# JSONL small-result output batching

Date: 2026-11-13

Baseline commit: `46937676b`

The official JSONL transport writes the response prefix and result rows directly
to unbuffered stdout. A ten-row TopK response previously used two writes: one
for the response prefix and one for all rows plus the suffix. The candidate
keeps the prefix in the first bounded output chunk, so a response with at most
64 rows is emitted in one write. Larger responses still flush every 64 rows and
retain bounded memory.

The SQL, storage, transaction, vector selection and exact-distance paths are
unchanged.

## Measurements

All measurements are local Docker Desktop Linux diagnostics, not official
T/B/M or E.

Seven alternating fresh-container runs used 10,000 rows, 128 dimensions, 1,000
TopK queries and the same `.local-incontainer-bench.py` workload. The candidate
and baseline images were built from the same source tree.

| Metric | Baseline median | Candidate median | Change |
| --- | ---: | ---: | ---: |
| All queries | 0.34 s | 0.31 s | -8.82% |
| Filtered queries | 0.14 s | 0.13 s | -7.14% |
| Unfiltered queries | 0.20 s | 0.18 s | -10.00% |
| Thin protocol query | 106.9 us | 91.7 us | -14.22% |

The write phase and `/data` layout did not change. Write-time differences in
the paired runs were host noise.

## Correctness and resource validation

- Windows: 177 tests passed.
- Docker 2 CPU / 256 MiB: 177 tests passed.
- Official public JSONL package: 20/20 cases passed.
- 64 MiB lifecycle: read-your-writes, multi-row statement atomicity, rollback,
  acknowledged commit followed by SIGKILL, uncommitted rollback, EOF rollback,
  and SIGTERM restart passed.
- 64 MiB pressure: 18,000 rows, 1,024 dimensions, 73,728,000-byte vector file,
  `oom=0`, `oom_kill=0`; elapsed 12.18 s.
- Added a 65-row response test. It initially caught a missing comma between the
  two output chunks; the final implementation preserves the first-row state
  across chunk boundaries and passes the test.

## Rejected experiment

Extending the compile-time 128-dimension distance loop to filtered selection
lists showed no stable gain in a same-process two-library A/B. That experiment
was discarded without changing `native/exact.cpp`.
