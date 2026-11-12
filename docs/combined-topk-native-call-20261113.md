# Combined native TopK selection and verification (2026-11-13)

## Change

The exact TopK path previously made two native calls per query:

1. `mv_topk_f32` selected `K+1` float32 candidates.
2. `mv_exact_distances` re-evaluated the selected positions in double
   precision.

The new `mv_topk_f32_exact` entry point performs both operations in one C call.
It keeps the float32 distances for the existing conservative boundary check and
writes ordered-double exact distances for the same selected rows. Python still
falls back to the full double kernel when the candidate boundary is ambiguous,
and the old two-call path remains available when the new symbol is absent.

The exact-distance output uses the same ordered double loop and `sqrt` as
`mv_exact_distances`; a differential test verifies the arrays are bit-identical.

## Measurement

Baseline is commit `7f94a1acb` (canonical distance-select parser fast path).
Local Docker Desktop Linux used 2 CPU, 256 MiB, swap disabled, and network
disabled. Each end-to-end run loaded 10,000 rows of 128 dimensions and executed
1,000 exact TopK queries, K=10, with 500 filtered and 500 unfiltered queries.
Baseline and candidate were alternated in AB and BA order, five pairs each.

Medians across all ten pairs:

| Metric | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Write 10,000 rows | 1.4443 s | 1.4802 s | +2.49% |
| All 1,000 queries | 0.3424 s | 0.3297 s | -3.71% |
| 500 filtered queries | 0.1567 s | 0.1523 s | -2.78% |
| 500 unfiltered queries | 0.1886 s | 0.1789 s | -5.17% |

The write path is unchanged and its difference is host noise. Because paired
container runs remained noisy, a same-process A/B toggled only the combined
entry point on one snapshot. Ten alternating rounds measured a query median of
0.15311 s for the two-call path and 0.14847 s for the combined path, a 3.03%
reduction. The first 50 query results were identical.

## Resource check

Three-round `tests.container_benchmark` runs used 10,000 rows, 128 dimensions,
1,000 queries, one cycle, and a 128 MiB container limit.

| Metric | Baseline | Candidate |
|---|---:|---:|
| Write `memory.peak` median | 24.65 MiB | 24.79 MiB |
| Query `memory.peak` median | 36.98 MiB | 36.99 MiB |
| Write median | 1.5432 s | 1.7605 s |
| Query median | 1.5401 s | 1.5641 s |

The measured device I/O byte counts are unchanged. The three-round timing
direction conflicts with the paired and same-process measurements, so the local
timing is treated as directional evidence only. A 64 MiB pressure run with
18,000 x 1,024 vectors passed exact/filtered TopK, transaction rollback, restart,
and SIGKILL recovery with `oom=0` and `oom_kill=0`.

## Validation

- Windows `unittest discover`: 172 tests passed.
- Docker 2 CPU, 256 MiB: 172 tests passed.
- Official public JSONL package: 20/20 cases passed.
- Docker 64 MiB lifecycle: read-own-writes, statement atomicity, rollback,
  acknowledged-commit SIGKILL, uncommitted rollback, EOF/SIGTERM restart passed.
- Docker 64 MiB vector pressure: 18,000 x 1,024, no OOM or OOM kill.
- Candidate image:
  `sha256:53caad603f04812bfcba5eaab433089840ee5862fe48945575aa855d2b0de3f1`.

The benchmark and resource probes are local diagnostics, not official T/B/M or
E. Hidden workloads and the formal CPU feature set remain unverified.
