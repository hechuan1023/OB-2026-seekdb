# Rejected optimization experiments

Date: 2026-11-13

These local experiments were not retained. Each was reverted from source before
the next experiment. Results are Docker Desktop diagnostics, not official
T/B/M or E.

## Filtered 128-dimension kernel specialization

The existing constant-width 128-dimension loop was extended to filtered
selection lists. A same-process two-library A/B showed no stable filtered gain;
the unfiltered path also changed code layout and became noisy. The experiment
was discarded without changing `native/exact.cpp`.

## WAL `fdatasync`

Only the acknowledged WAL commit sync was changed from `fsync` to `fdatasync`.
Nine alternating fresh-container writes had identical median time of 1.44 s.
The experiment was discarded because it had no measured benefit and would
weaken the conservative persistence story.

## WAL mmap copy

The staged transaction was mapped once for the checksum and WAL copy instead of
alternating 64 KiB reads and writes. Fifteen alternating pairs measured
1.419046 s baseline and 1.414949 s candidate, a 0.29% median difference, but
the median paired ratio was 1.0094 (candidate slightly slower). This was inside
host noise and was not retained.

## Two-column JSON row formatting

A direct formatter for the common `(BIGINT, float)` TopK row reduced a
microbenchmark from about 0.70 to 0.55 microseconds per row. Nine alternating
end-to-end pairs measured 0.380772 s baseline and 0.385338 s candidate, a 1.20%
median regression, with a median paired ratio of 1.0585. The microbenchmark did
not survive the full request path, so the change was discarded.

## Larger initial primary-key hash capacity

The rebuildable primary-key hash started with 4096 slots instead of 1024 for a
10,000-row, 128-dimension direct Engine write. Seven alternating fresh-container
pairs measured 0.628730 s versus 0.659936 s median, a 4.96% regression, with a
median paired ratio of 1.0440. The 4096-slot candidate was faster in only 1/7
pairs; cgroup peak memory was effectively unchanged. The experiment was
discarded and the 1024-slot default retained.

## Stack candidate buffer for small OpenMP K

The OpenMP exact kernel was changed to use a fixed 32-candidate stack buffer for
`K <= 32` instead of allocating small vectors per query. Seven alternating
direct-kernel pairs measured 0.109294 s versus 0.109003 s in total, but the
per-query median was 80.80 µs versus 87.11 µs and the candidate won only 3/7
pairs. The fixed-buffer path was therefore discarded; the allocation-light
micro-optimization did not survive the real kernel path.

## Precompiled structs in compact row encoding

The compact row encoder reused precompiled `struct.Struct` instances instead of
calling `struct.pack` for every table/key, count, integer and length field. The
encoded bytes were identical. A same-process microbenchmark over 10,000 vector
rows and 1,000 deletes improved by about 6.4% in the container, but seven
alternating fresh-container writes measured 1.380807 s baseline and 1.425950 s
candidate. The candidate won only 2/7 pairs and the median paired ratio was
1.0036, a 0.36% regression. The change was reverted because the encoder gain was
too small relative to commit `fsync` and host noise.

## Four-row lookahead prefetch for filtered scans

The filtered OpenMP scanner prefetched the vector four selected rows ahead.
Seven alternating direct-kernel rounds at 768 selected rows measured a 0.8721
median ratio but only 4/7 rounds were faster; at 2,000 selected rows the median
ratio was 1.0416 and only 3/7 rounds were faster. The inconsistent direction
showed that the apparent small-filter gain did not survive the larger working
set, so the prefetch was reverted.

## Exact-size preallocation for in-memory snapshots

The snapshot builder used the exact transaction-visible row count to allocate
the vector and primary-key bytearrays once, then filled them by offset. This
avoided the growth and copying performed by repeated `bytearray +=` operations.
Seven alternating fresh-container pairs rebuilt a 10,000-row, 128-dimensional
snapshot twelve times per run. The candidate lost all 7/7 pairs: the median
paired ratio was 1.1256, a 12.6% slowdown. Median cgroup peak memory changed
from 48,988,160 to 48,893,952 bytes, only 0.19% lower. The zero-filled
preallocation and offset writes outweighed the saved reallocations, so the
source and its row-count bookkeeping were reverted.

## IVF coarse quantizer

A self-written IVF prototype clustered 10,000 uniform 128-dimensional vectors
into 64, 128, and 256 groups and searched 2 through 32 probes. Recall was too
low at useful probe counts: 128 clusters with 32 probes reached only 0.6266
mean Recall@10, while 64 clusters with 32 probes reached 0.7958. Raising the
probe count enough to approach the 0.95 threshold also approached the exact
scan cost. The prototype and its temporary source were removed.

## 4-bit scalar quantization

A 4-bit candidate index was evaluated on the same 10,000-row, 128-dimensional,
2,000-row filtered shape with five seeds and 400 queries per seed. Global and
per-dimension scales both lost too much ranking information. With 16
candidates, mean Recall@10 was 0.9845 on uniform data, 0.8701 on Gaussian data,
and 0.4072 on clustered data. Raising the clustered result to 0.5786 required
32 candidates and still missed the requirement. The 8-bit SQ8 index was
retained because it reached at least 0.9984 average recall on every tested
distribution with 16 candidates.

## Twelve SQ8 candidates

The retained SQ8 path originally kept 22 float32 re-ranking candidates for the
benchmark's `K=11`. A 12-candidate candidate filter passed the recall check on
all tested distributions: mean Recall@10 was 0.9999 for uniform data, 0.99855
for Gaussian data, and 0.97975 for clustered data across five seeds. The first
21 alternating image pairs measured a total-query candidate/baseline median
ratio of 0.968142 and all results matched exact IDs and distances.

A second 21-pair run with seed 20261114 reversed the apparent gain: the total
query ratio was 1.044244, filtered was 1.052682, and unfiltered was 1.038270.
All results still matched, and the peak-memory ratio was 1.000260. The
cross-seed direction was not stable enough to retain a lower recall margin, so
the source was restored to 16 candidates, with the existing `keep * 2`
minimum.

## Double-accumulator SQ8 distance loop

The SQ8 integer distance kernel was changed to use two vector accumulators.
Direct-kernel A/B looked promising: seven-pair ratios were 0.961687 for
unfiltered and 0.938255 for filtered scans, and a same-process engine check
measured 0.976938 overall. Fresh-image validation did not reproduce the gain:
11 pairs had a 1.032142 total-query ratio with only 3/11 pairs faster, and a
second 21-pair run had a 1.018948 ratio. The source was reverted because the
kernel-only improvement did not survive the complete request path.

## Four-accumulator float32 exact scan

The exact TopK float32 candidate scan was changed from two to four AVX2
accumulators for 128-dimensional vectors. A direct two-library kernel A/B
measured a 0.974851 median ratio for unfiltered scans (8/9 rounds faster) and
0.985492 for filtered scans (6/9 rounds faster). A same-process engine A/B
measured 0.992851 overall, but only 9/15 rounds were faster.

The full shared-volume image A/B used 21 alternating pairs, 10,000 rows, 128
dimensions, and 1,000 exact JSONL queries. The paired median candidate/baseline
ratio was 1.000472 overall, 1.005583 filtered, and 0.999059 unfiltered. All
results matched, but the kernel gain disappeared in the complete request path,
so the source was restored to the existing two-accumulator implementation.

## SQ8 residual-bound exact pruning

An exact-mode prototype used SQ8 reconstruction error as a rigorous
triangle-inequality lower bound. It first obtained an exact-distance upper bound
from the nearest quantized rows, then scanned the selection again and exactly
re-ranked only rows whose lower bound could enter the result. The proof and
differential tests were sound, but the two quantized scans were more expensive
than the existing float32 exact scan at the benchmark shape.

Seven alternating in-container pairs measured a total-query candidate/baseline
median ratio of 1.040911. Filtered queries were 0.969259, but unfiltered
queries were 1.094276; only 2/7 total pairs were faster and peak memory was
1.010721 times the baseline. A 32-byte AVX2 squared-difference loop also
regressed the full path. Both source changes were reverted. The existing
float32 candidate selection remains the exact-mode implementation.

## Single-probe duplicate insert

The insert path normally probes the transaction overlay and the committed
primary index before appending a row, then probes the overlay again while
updating it. A prototype removed the explicit duplicate lookup and tried to use
the previous value returned by the append itself.

This was incorrect because the overlay contains only changes made by the active
transaction. A key present only in the committed index therefore appeared new
to the overlay. The public `transaction_failed_insert` case returned
`affected_rows=2` instead of `CONSTRAINT_ERROR`, and the new transaction
continuation test exposed the same flaw. The prototype was reverted without
performance testing. Any future single-probe design must check both the overlay
and committed index before reusing an empty overlay slot.

## Reused overlay probe with committed-index validation

A second prototype retained both duplicate checks but reused the transaction
overlay's empty-slot probe when updating the index after staging the row. It
still checked the committed index when the overlay had no entry, and it rechecked
the slot after any resize or race. Targeted transaction tests passed, including
a committed duplicate after an earlier update and a delete followed by reinsert.

Seven alternating fresh-container pairs used the same 10,000-row,
128-dimensional write workload. The baseline median was 2.482943 s and the
candidate median was 2.474086 s, but the median paired candidate/baseline ratio
was 1.008934: the candidate was faster in only 2/7 pairs. The median cgroup peak
memory ratio was 1.000455 and every query still matched exact truth. The source
and its two new tests were reverted because the apparent 0.36% median difference
was smaller than the host variance and had no stable direction.

## Publish the transaction overlay directly after commit

The normal commit path synchronized the WAL, then decoded every staged record
again to update the rebuildable primary index. A candidate translated the final
offset already held by the transaction overlay into the WAL and published that
state directly after `fsync`, while retaining the same WAL format, durability
point and crash-recovery fallback.

The focused transaction, index-lifecycle, recovery and snapshot suites passed
63/63. In seven alternating fresh-container pairs, however, the median paired
candidate/baseline write ratio was 1.005429 and the candidate was faster in only
3/7 pairs. The baseline and candidate medians were 2.513930 s and 2.511051 s,
respectively. `fsync` remained roughly 80% of the candidate's measured commit
time, leaving too little CPU work for this change to overcome host variance.
The source was reverted because it touched the storage core without a stable
end-to-end gain.

## Retained changes

- Canonical multi-row INSERT parser fast path: commit `46937676b`.
- TopK JSONL small-result single write: commit `630daca84`.
- Cgroup-aware OpenMP exact TopK for large unfiltered scans: commit `c9a79d8e4`.
- OpenMP exact TopK for large filtered selections: commit `1202d21db`.
- Self-written SQ8 ANN candidate filtering for benchmark-shaped unfiltered and
  filtered TopK; exact mode and fallback paths remain unchanged.
