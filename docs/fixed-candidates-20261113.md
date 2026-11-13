# Fixed-capacity exact TopK candidate buffer

Date: 2026-11-13

This experiment reduces per-query allocation and vector bookkeeping in the
native exact TopK candidate collector. It does not change SQL semantics,
filtering, transaction visibility, WAL durability, recovery, or the final
ordered-double distance verification.

## Change

- The native float32 candidate scan now keeps up to 32 candidates in a
  fixed-size stack buffer.
- The existing sorted-vector collector remains the fallback for `K + 1 > 32`.
- The OpenMP path uses one fixed buffer per worker and merges the small
  per-worker sets in the same distance/primary-key order.
- Candidate distances, exact returned distances, primary-key tie-breaking,
  filtering, and the Python boundary fallback are unchanged.
- The regression test covers `K = 32` and `K = 33` so both the fixed and
  generic paths are exercised.

## Same-snapshot native A/B

Both shared libraries were loaded into one process and alternated on the same
10,000-row, 128-dimensional snapshot. Each workload ran 1,000 queries with
`K = 11`, two OpenMP workers, and exact result comparison.

| Workload | Baseline | Candidate | Paired median ratio | Candidate faster | Mismatches |
| --- | ---: | ---: | ---: | ---: | ---: |
| Unfiltered seed 1 | 76.968 us | 74.719 us | 1.026007 | 783/1000 | 0 |
| Filtered 2,000 rows seed 1 | 21.603 us | 19.932 us | 1.096730 | 950/1000 | 0 |
| Unfiltered seed 2 | 79.942 us | 78.989 us | 1.026813 | 797/1000 | 0 |
| Filtered 2,000 rows seed 2 | 20.706 us | 19.291 us | 1.081562 | 939/1000 | 0 |
| Unfiltered seed 3 | 71.525 us | 69.831 us | 1.023828 | 786/1000 | 0 |
| Filtered 2,000 rows seed 3 | 20.888 us | 19.261 us | 1.082769 | 948/1000 | 0 |

The paired ratio is baseline time divided by candidate time. All returned
positions and exact distances matched.

## End-to-end JSONL A/B

The full host-side JSONL workload used 10,000 rows, 128 dimensions, 1,000
queries, and alternating fresh containers. Every query was checked against an
independent exact reference.

| Seed | Pairs | Query total ratio | Filtered ratio | Unfiltered ratio | Candidate faster |
| --- | ---: | ---: | ---: | ---: | ---: |
| 20261110 | 5 | 0.975373 | 0.987096 | 0.964252 | 3/5 |
| 20261113 | 5 | 0.994360 | 1.005558 | 0.983681 | 3/5 |
| 20261114 | 7 | 0.994753 | 0.998236 | 0.991374 | 4/7 |

The three run-median query ratios are below one. The filtered result is close
to neutral in the latter two seeds; the native kernel A/B shows the stronger
and more repeatable signal. Physical write bytes were unchanged at
6,037,504 bytes in the paired runs. Peak-memory ratios were approximately
0.995 to 1.000, with no systematic increase.

## Validation

- Windows full suite: `186/186`.
- Docker 2 CPU / 256 MiB full suite: `186/186`.
- Official public JSONL package: `20/20`.
- 64 MiB lifecycle: read-your-writes, multi-row statement atomicity, rollback,
  acknowledged commit followed by SIGKILL, uncommitted rollback, EOF and
  SIGTERM restart all passed.
- 64 MiB pressure: 18,000 rows x 1,024 dimensions, 73,728,000-byte vector
  file, `oom=0`, `oom_kill=0`, committed state restored and temporary files
  cleaned.
- A three-round container benchmark rerun after host noise settled measured
  write `1.493 s`, query `1.556 s`, and recovery `0.593 s`; this is a local
  diagnostic, not official T/B/M or E.

Candidate image:

```text
minivectordb:fixed-candidates-20261113
sha256:3ecfa8bfdac067063fc5d04ec19e27de134ae051dbe7150421b87a8af437fd19
```

## Boundary

The fixed buffer is capped at 32 candidates. Larger K values retain the
existing vector collector, so the optimization cannot change the large-K
fallback. The local measurements do not predict the official performance
score.
