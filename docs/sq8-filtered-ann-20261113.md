# SQ8 ANN candidates for filtered and unfiltered TopK

Date: 2026-11-13

This experiment adds a self-written scalar-quantized candidate layer for ANN
mode. It is a candidate filter, not the final distance implementation:

- The index stores one global int8 code per float32 component.
- AVX2 scans the quantized codes and retains 16 candidates.
- The existing float32 kernel re-ranks those candidates.
- Returned distances are recomputed with the existing ordered-double kernel.
- Exact mode never enters the SQ8 path.
- Filtered mode scans only positions selected after `WHERE` evaluation.
- Small selections, tombstones, non-128-dimension vectors, and unsupported
  table sizes fall back to the existing exact path.

The SQ8 index is derived from the in-memory snapshot and is invalidated on
vector updates or membership changes. It creates no additional durable files.

## Recall

The test shape was 10,000 rows, 128 dimensions, a 2,000-row equality filter,
K=10, and 400 queries for each of five seeds. Truth was computed by the exact
kernel over the filtered positions.

| Distribution | 16-candidate mean Recall@10 | Worst seed mean | Worst single query |
|---|---:|---:|---:|
| Uniform [-1, 1] | 1.0000 | 1.0000 | 1.0000 |
| Gaussian N(0, 1) | 1.0000 | 1.0000 | 1.0000 |
| 20 clusters + Gaussian noise | 0.9984 | 0.9975 | 0.9000 |

The competition threshold is applied to the average Recall@10 of each query
group, so 16 candidates retained a margin above 0.95 on all tested
distributions. Twelve candidates were rejected because they reached only
0.97975 average recall on the clustered data and produced a 0.7 single-query
recall.

## End-to-end A/B

Seven alternating fresh-container pairs used the JSONL protocol, 10,000 rows,
128 dimensions, 1,000 TopK queries, and 500 filtered plus 500 unfiltered
queries. Every ANN result was checked against exact IDs and distances.

| Metric | Exact median | ANN median | Paired median ANN / exact |
|---|---:|---:|---:|
| Total query time | 1.693487 s | 1.558304 s | 0.931478 |
| Filtered query time | 0.803337 s | 0.769243 s | 0.979540 |
| Unfiltered query time | 0.890150 s | 0.789061 s | 0.896132 |
| cgroup `memory.peak` | 30,244,864 B | 31,469,568 B | 1.043892 |
| cgroup `io.stat` bytes | 0 B | 0 B | 1.0 |

The median total query time fell by about 6.9%, and all 7,000 ANN query results
matched the exact reference. The filtered gain is smaller because the existing
filtered float32 kernel is already efficient at this selectivity. The I/O
median is zero in both modes because the volume and page cache were warm; this
is not a cold-cache or official B measurement. Exact mode now builds no SQ8
data. ANN mode lazily allocates one byte per vector component, adding about
1.28 MiB at this shape; the measured 4.4% peak-memory increase is the explicit
cost of the query-time reduction.

## Validation

- Windows unit suite: 182/182.
- Linux container unit suite at 256 MiB: 182/182.
- Official public JSONL protocol cases: 20/20.
- Public SQL adapter: 40/40 across typed and STDIO paths.
- 64 MiB lifecycle and SIGKILL recovery: 4/4.
- 18,000 x 1,024 pressure run: passed with `oom=0` and `oom_kill=0`;
  the vector file was 73,728,000 bytes under a 67,108,864-byte memory limit.

These are local Docker Desktop diagnostics, not official T/B/M or E values.
