# Python TopK result materialization

Date: 2026-11-13

This candidate reduces work after the exact native TopK kernel has selected and
verified its candidates. It keeps the native scan, exact-distance verification,
filtering, tie handling, and transaction-visible snapshot unchanged.

## Change

- A primary-key-only TopK result now carries `(distance, key)` instead of the
  redundant `(distance, key, key)` tuple.
- Verified candidates use native tuple ordering, avoiding a separate sorting
  key for every candidate.
- The common `SELECT id, L2_DISTANCE(...) AS distance` projection yields the
  materialized result directly instead of rebuilding each two-item tuple.
- The SELECT parser skips the unrelated INSERT-prefix fast-path regex.
- A regression test covers the reversed projection order
  `L2_DISTANCE(...), id` and equal-distance primary-key ordering.

## Measurement

Each result below is from alternating fresh-container A/B pairs using the JSONL
protocol, 10,000 rows, 128 dimensions, and 1,000 exact TopK queries with 500
filtered and 500 unfiltered queries. Every returned ID and distance was checked
against the exact reference before the pair was accepted.

| Seed | Pairs | Total candidate/baseline | Filtered | Unfiltered | Peak memory |
|---|---:|---:|---:|---:|---:|
| 20261114 | 7 | 0.974021 | 0.966876 | 0.981830 | 0.9997 |
| 20261115 | 7 | 0.991058 | 0.986559 | 1.014295 | 0.9976 |
| 20261116 | 21 | 0.971324 | 0.973389 | 0.978834 | 0.995666 |

The third run was added because the second run's unfiltered subgroup reversed
direction. Across the three runs, total and filtered query medians improved;
the unfiltered subgroup improved in two runs and regressed by 1.4% in one.

A same-process engine A/B over seven pairs measured total-time ratio 0.987660,
per-query median ratio 0.956989, and P95 ratio 0.931707. The candidate was
faster in 5/7 total-time pairs, 5/7 per-query median pairs, and 6/7 P95 pairs.

## Validation

- Windows unit suite: 182/182.
- Linux container unit suite at 256 MiB: 182/182.
- Official public JSONL protocol: 20/20 cases.
- Public SQL adapter: 40/40 across typed and STDIO paths.
- 64 MiB lifecycle and SIGKILL recovery: 4/4.
- 18,000 x 1,024 pressure run: passed with `oom=0` and `oom_kill=0`;
  the 73,728,000-byte vector file exceeded the 67,108,864-byte memory limit.
- `git diff --check`: passed.

These are local Docker Desktop diagnostics, not official T/B/M or E values.
The small gain is retained because it is exact, has no durable-format change,
and did not increase the measured peak memory.
