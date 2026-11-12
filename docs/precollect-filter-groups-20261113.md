# Snapshot equality-group precollection

Date: 2026-11-13

Baseline source: `e63ff2ef3a3a17e1f007d632824693918dcf4602`

This experiment removes the second table scan performed by the first equality-filtered TopK query. `Snapshot` already reads every visible row while building its vector and primary-key arrays. The candidate builds bounded equality-position groups during that same scan. The groups are ordinary filter-cache entries, so existing invalidation rules still apply.

## Implementation

- Only non-primary-key, non-vector scalar columns are considered.
- `VARCHAR` columns are considered before `BIGINT` columns.
- The collector stores `array('Q')` positions, not a second copy of every scalar value.
- The collector shares the existing `filter_budget` and caps a column at 4,096 groups.
- A column that exceeds either bound is abandoned in full. The first query then follows the original scan-and-cache path.
- Vector-only updates retain the snapshot and the precollected groups.
- Scalar updates, deletes, rollback, snapshot rebuilds, and restart use the existing filter invalidation or rebuild paths.
- If an embedding caller lowers `filter_budget`, the next selection drops entries that no longer fit.

An earlier version retained a raw scalar-value list. It reduced first-query time but was rejected after the 64 MiB pressure test was OOM-killed. The grouped representation is the retained implementation.

## Measurements

All measurements used Docker Desktop Linux containers with 2 CPUs, 256 MiB, no network, a fresh data volume per timed image, and exact TopK. These are local diagnostics, not official T/B/M or E.

Low-cardinality workload: 10,000 rows, 128 dimensions, 5 filter values, 1,000 queries, 5 alternating rounds.

| Metric | Baseline median | Candidate median | Change |
| --- | ---: | ---: | ---: |
| First five distinct filters | 0.035319 s | 0.008424 s | -76.15% |
| Filtered queries | 0.697503 s | 0.693120 s | -0.63% |
| Unfiltered queries | 0.783684 s | 0.772211 s | -1.46% |
| All queries | 1.496955 s | 1.474230 s | -1.52% |
| Query-phase `memory.peak` | 36,184,064 B | 36,081,664 B | -0.28% |

High-cardinality workload: 10,000 rows, 128 dimensions, 600 filter values, 200 queries, 3 alternating rounds.

| Metric | Baseline median | Candidate median | Change |
| --- | ---: | ---: | ---: |
| First five distinct filters | 0.032910 s | 0.007147 s | -78.28% |
| Filtered queries | 0.140262 s | 0.128657 s | -8.27% |
| Unfiltered queries | 0.171440 s | 0.159995 s | -6.68% |
| All queries | 0.314310 s | 0.291820 s | -7.15% |
| Query-phase `memory.peak` | 36,388,864 B | 36,261,888 B | -0.35% |

The query-phase I/O delta remained zero `rbytes` and zero `wbytes` in the low-cardinality runs, with 2–3 write operations from cgroup/stat bookkeeping. High-cardinality writes were unchanged at 6,098,944 bytes. Snapshot preparation is still counted in the platform's B and M windows; this change moves work from query T into that preparation scan.

## Correctness and resource validation

- Windows: 175 tests passed.
- Docker 2 CPU / 256 MiB: 175 tests passed.
- Official public JSONL package: 20/20 cases passed.
- 64 MiB lifecycle: read-your-writes, statement atomicity, rollback, acknowledged commit followed by SIGKILL, uncommitted rollback, EOF, and SIGTERM restart passed.
- 64 MiB pressure: 18,000 rows, 1,024 dimensions, 73,728,000-byte vector file, `oom=0`, `oom_kill=0`; elapsed 11.13 s.
- Added counterexamples covering precollection, vector-update retention, scalar-update invalidation, rebuild, delete invalidation, and a shared budget across multiple tables.

The local host showed substantial wall-clock variance. The first-query improvement was stable across every measured round; the smaller total-query percentage should be treated as a local estimate until the official benchmark runs.
