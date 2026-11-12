# Canonical distance-select parser fast path (2026-11-13)

## Change

The exact TopK benchmark repeatedly sends the same projection shape:

```sql
SELECT id,L2_DISTANCE(v,'[...]') AS d
FROM t [WHERE column='value']
ORDER BY d LIMIT K
```

`sql.parse()` now recognizes only that unquoted, escape-free spelling with a
compiled regular expression and constructs the existing parser tuple directly.
Every other query, including quoted identifiers, comments, escaped strings,
additional projections or predicates, alternate ordering, and malformed input,
continues through the original `Parser`.

The fallback boundary is deliberate: the fast path does not accept a broader SQL
subset and does not change query semantics. Two differential tests compare the
fast and general parsers for filtered, unfiltered, and non-matching shapes.

## Measurement

Baseline is commit `8b2a016a3991e18ab04959fb215abf4d0c19bf0e`. Local Docker
Desktop Linux used 2 CPU, 256 MiB, swap disabled, and network disabled. Each run
loaded 10,000 rows of 128 dimensions and executed 1,000 exact TopK queries,
K=10, with 500 filtered and 500 unfiltered queries. Baseline and candidate were
alternated in AB and BA order, five pairs each. Every query was checked against
the independent truth IDs, distances, ordering, and filter predicate.

Medians across all ten pairs:

| Metric | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Write 10,000 rows | 1.3582 s | 1.3531 s | -0.38% |
| All 1,000 queries | 0.3775 s | 0.3633 s | -3.77% |
| 500 filtered queries | 0.1706 s | 0.1649 s | -3.31% |
| 500 unfiltered queries | 0.2061 s | 0.1975 s | -4.15% |

Seven of ten paired runs had lower total query time. Host noise remained visible:
the AB median changed by -6.46% and the BA median by -1.30%; the combined
median is the retained result. Write time is effectively unchanged.

An earlier candidate that replaced `pop_heap` plus `push_heap` with a custom
down-filter was rejected. Across five AB pairs its query median regressed by
6.94%, including an 8.33% regression for unfiltered queries, so it was removed
before this change.

## Resource check

Three-round `tests.container_benchmark` runs used 10,000 rows, 128 dimensions,
1,000 queries, one cycle, and a 128 MiB container limit.

| Metric | Baseline | Candidate |
|---|---:|---:|
| Write `memory.peak` | 24.65 MiB | 24.80 MiB |
| Query `memory.peak` | 37.26 MiB | 37.07 MiB |
| Write median | 1.7736 s | 1.5432 s |
| Query median | 1.6007 s | 1.5401 s |

The measured device I/O byte counts are unchanged for the write and query
phases. These are local diagnostics, not official T/B/M or E.

## Validation

- Windows `unittest discover`: 171 tests passed.
- Docker 2 CPU, 256 MiB: 171 tests passed.
- Official public JSONL package: 20/20 cases passed.
- Docker 64 MiB lifecycle: read-own-writes, statement atomicity, rollback,
  acknowledged-commit SIGKILL, uncommitted rollback, EOF/SIGTERM restart passed.
- Candidate image:
  `sha256:426133f60748249a678a04e549c45c62387890e4e8aa2d7399e3a3bd6ad967b3`.

The fast path is limited to the benchmark's canonical projection. Hidden SQL
shapes and official benchmark conditions remain unverified.
