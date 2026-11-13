# Sorted small-K candidate collector

Date: 2026-11-13

Baseline commit: `350d36cf0826e503c5d2478d14810cd3f14fa151`

The native exact TopK kernel retained K+1 candidates in a binary heap and
sorted the heap at the end of every scan. The benchmark uses K=10, so the
kernel keeps 11 candidates. At that size the heap maintenance and final sort
cost more than inserting into a small ordered vector.

The candidate keeps the same float32 distance function, primary-key tie-break,
K+1 boundary margin, exact-distance re-evaluation and Python fallback rules.
Only the candidate container changed: an ascending `std::vector<Candidate>`
replaces the heap. A row that cannot enter the current set is rejected with one
comparison against the worst retained candidate; an improving row is inserted
with `lower_bound` and the excess tail is removed.

## Measurements

All measurements are local Docker Desktop Linux diagnostics, not official
T/B/M or E.

### Native kernel A/B

Both shared libraries were loaded into one process and alternated over the same
10,000-row, 128-dimension snapshot. Each result contains 27 candidates.

| Run | Workload | Baseline median | Candidate median | Change |
| --- | --- | ---: | ---: | ---: |
| 1 | Unfiltered | 155.39 us | 140.77 us | -9.40% |
| 1 | Filtered | 54.51 us | 53.97 us | -0.99% |
| 2 | Unfiltered | 148.54 us | 136.80 us | -7.90% |
| 2 | Filtered | 49.77 us | 49.26 us | -1.02% |

Both runs returned identical positions and distances for 250 randomized
Top-27 queries.

### Full `VectorIndexes.topk` A/B

The full path includes candidate selection, exact distance verification,
deleted-row filtering, row projection and result ordering. Two native libraries
were alternated in one process on the same snapshot and filter cache.

| Run | Workload | Baseline median | Candidate median | Change |
| --- | --- | ---: | ---: | ---: |
| 1 | Unfiltered | 0.152997 s | 0.148197 s | -3.14% |
| 1 | Filtered | 0.053447 s | 0.051533 s | -3.58% |
| 2 | Unfiltered | 0.155041 s | 0.156018 s | +0.63% |
| 2 | Filtered | 0.051968 s | 0.050566 s | -2.70% |

The second unfiltered run is within the observed host variance. Both runs
returned identical full TopK results for 250 queries.

### Same-volume end-to-end runs

One preloaded Docker volume was reused across seven alternating process runs:
10,000 rows, 128 dimensions, 5 filter values and 1,000 exact TopK queries.

| Metric | Baseline median | Candidate median | Change |
| --- | ---: | ---: | ---: |
| Filtered queries | 0.722031 s | 0.680947 s | -5.69% |
| Unfiltered queries | 0.806201 s | 0.743398 s | -7.79% |
| All queries | 1.545080 s | 1.435733 s | -7.08% |
| Query-phase `memory.peak` | 30,289,920 B | 30,158,848 B | -0.43% |

The write I/O delta was unchanged at 6,082,560 bytes in both images. The query
phase added no physical reads or writes.

A separate fresh-volume run showed large correlated host variance: the
candidate appeared 4.5% slower in query time while also appearing 10.2% faster
in write time even though the write code and I/O were unchanged. That run is
not used to claim either a gain or a regression. The same-process A/B,
same-volume runs, unchanged I/O and correctness results are the retained
evidence.

## Correctness and resource validation

- Windows: 175 tests passed.
- Docker 2 CPU / 256 MiB: 175 tests passed.
- Official public JSONL package: 20/20 cases passed.
- 64 MiB lifecycle: read-your-writes, multi-row statement atomicity, rollback,
  acknowledged commit followed by SIGKILL, uncommitted rollback, EOF rollback,
  and SIGTERM restart passed.
- 64 MiB pressure: 18,000 rows, 1,024 dimensions, 73,728,000-byte vector file,
  `oom=0`, `oom_kill=0`; elapsed 11.55 s.
- Randomized native tests cover ties, dimensions around SIMD boundaries,
  mutations, filters, extreme float32 values and exact fallback boundaries.

The candidate changes only the native candidate container. It does not change
SQL, storage, transaction, WAL, recovery, filtering or persistence behavior.
