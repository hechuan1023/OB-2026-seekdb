# K+1 exact TopK candidate optimization (2026-11-13)

## Change

The float32 native selector previously retained `K+16` candidates before exact
verification. The revised path retains only `K+1` candidates and trusts them
only when the `(K+1)`-th squared float32 distance is outside the conservative
float32 error band around the `K`-th candidate. Otherwise it falls back to the
full double-precision kernel. The returned rows are still re-evaluated in double
precision and sorted by distance and primary key.

The JSONL transport also accepts the canonical benchmark request shape without
constructing a generic JSON object. Non-canonical requests continue through
`json.loads`.

## Measurement

Local Docker Desktop Linux, 2 CPU, 256 MiB, swap disabled, network disabled.
Each run used 10,000 rows, 128 dimensions, 1,000 exact TopK queries, K=10, with
500 filtered and 500 unfiltered queries. Baseline and candidate were alternated
in both AB and BA order, five pairs each. All query IDs, distances, ordering, and
filter predicates matched the independent truth set; restart content checks
passed.

Medians across ten pairs:

| Metric | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Write 10,000 rows | 1.47525 s | 1.45560 s | -1.33% |
| All 1,000 queries | 0.40460 s | 0.36615 s | -9.50% |
| 500 filtered queries | 0.18195 s | 0.17000 s | -6.57% |
| 500 unfiltered queries | 0.22140 s | 0.19920 s | -10.03% |
| Thin ORDER BY reference | 98.1 us | 103.9 us | +5.91% |

The thin reference is not a TopK measurement and its direction varied with host
noise. A separate three-round resource run measured candidate write/query
`memory.peak` at 23.39/35.28 MiB and the same write and query I/O byte counts as
the baseline. These are local measurements, not official T/B/M or E.

## Validation

- Docker 256 MiB: 169 tests passed.
- Official public JSONL package: 20/20 cases passed.
- Docker 64 MiB lifecycle: read-own-writes, statement atomicity, rollback,
  acknowledged-commit SIGKILL, uncommitted rollback, EOF/SIGTERM restart passed.
- Candidate image: `sha256:8727cd31533a4a9c53f136757e5d0220330dd26bdd9139b89a86247d69080499`.

The local host had background container load; the paired medians are evidence of
the change direction, not a formal score. Hidden cases and official benchmark
conditions remain unverified.
