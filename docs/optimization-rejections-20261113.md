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

## OpenMP active wait policy

`OMP_WAIT_POLICY=ACTIVE` was tested as a Docker-only runtime change for the
two-CPU exact TopK path. An initial 6-pair in-container run had a 0.973129
query median ratio, but only 4/6 pairs were faster. A second 10-pair run with a
different seed had a 0.968989 median ratio and only 6/10 faster pairs.

The decision test used the complete Docker JSONL pipe with 1,000 queries per
round. Seven alternating pairs had a candidate/baseline query median ratio of
1.012415, and the candidate was faster in only 3/7 pairs. Median write and peak
memory ratios were 0.994806 and 0.999659, while every query still matched exact
truth. The local in-container direction did not survive the full request path,
so the Dockerfile environment change was reverted.

## Python optimized mode

`PYTHONOPTIMIZE=1` was tested as a Docker-only way to remove unused docstrings
and reduce startup memory. The implementation contains no runtime assertions,
so the change did not alter an internal correctness check.

Seven alternating fresh-container pairs used the short 10,000-row write and
query workload. Median write time was 4.70% slower, median cgroup peak memory
was 1.34% higher, and the candidate reduced memory in 0/7 pairs. Short-query
timing also regressed. The Dockerfile change was reverted.

## Dot-product distance with precomputed row norms

A direct AVX2 kernel prototype replaced each squared-difference reduction with
`row_norm + query_norm - 2 * dot(row, query)` and precomputed float32 row norms.
On the 10,000-row, 128-dimensional, 1,000-query scan shape, two alternating
timings measured 0.118174/0.128662 s for the current reduction and
0.110286/0.130010 s for the dot form. The direction reversed between runs, and
the algebra would also require a broader floating-point error proof before it
could safely select exact candidates. The prototype was removed.

The dot form was revisited on the current `a1e13a5cf` baseline with a
standalone 10,000-row, 128-dimensional, 1,000-query kernel benchmark. It used
float32 row norms and the same sorted K+1 candidate collector. Three runs
measured candidate/baseline median ratios of 1.019549, 1.014944, and 1.021161,
so the nominally shorter distance loop was consistently slower once the
per-row norm load and reconstruction arithmetic were included. The source was
not changed.

## Reused ctypes output buffers

The exact TopK wrapper allocated three small arrays for positions, selection
distances, and exact distances on every query. A private reuse path returned
bounded `memoryview` slices for the synchronous engine consumer while the
public native method kept its allocating behavior. An isolated 2,000-query
wrapper benchmark was 3.06% faster in the median and won 15/15 rounds.

That gain did not survive the complete engine path. A same-process 31-round
engine A/B alternated the allocating and reuse paths per query on one snapshot,
checked every ID and distance, and measured a candidate/baseline median ratio
of 1.039938 overall, 1.146935 for filtered queries, and 0.998315 for
unfiltered queries. The candidate won only 15/31 total-query rounds. Python
`memoryview` indexing in the candidate-verification loop outweighed the saved
array initialization. The source and its focused test were reverted.

## Native primary-key output

The first key-returning prototype still called the position-returning kernel
and copied each selected position to its primary key. A second prototype wrote
the ranked primary keys and exact distances directly from the candidate loop,
removing the position buffer and second key lookup in both the single-thread and
OpenMP paths. A focused differential test compared every key, selection
distance, and exact distance with the existing kernel.

A same-process 61-round engine A/B alternated the two paths per query on one
10,000-row, 128-dimensional snapshot and checked every result. The paired
candidate/baseline median ratio was 1.022118 overall, 1.109157 for filtered
queries, and 0.982864 for unfiltered queries. The candidate won only 30/61
total-query rounds and 30/61 filtered rounds. The unfiltered gain did not
survive the filtered path, so the ABI additions, Python routing, focused test,
and source changes were reverted.

## Specialized three-column row encoding

The write profile showed compact row encoding at about 82 ms for 10,000 rows.
A specialized path for the benchmark's `(BIGINT, VARCHAR, array('f'))` row
shape combined the fixed headers into two `struct.Struct` calls. Its bytes were
identical to the generic encoder, and a container microbenchmark reduced
10,000-row encoding from 15.56 ms to 7.14 ms, winning 31/31 rounds.

The gain did not survive the complete durable write path. Twenty-one
alternating fresh-container pairs used the JSONL protocol, 10,000 rows, 128
dimensions, 100-row commits, and restarted verification of every row. The
paired candidate/baseline write median ratio was 1.001728 and the candidate won
only 10/21 pairs. Physical writes were identical at 6,025,216 bytes in every
run. Median cgroup peak memory was 1.30% lower and the candidate was lower in
18/21 pairs, but this did not justify retaining a format-specific encoder when
the primary write metric had no stable improvement. The source and test were
reverted.

## Binary protocol transport

The JSONL transport was replaced with an equivalent binary stream to test
whether text encoding and decoding dominated the end-to-end query path. The
binary candidate was slower in every alternating pair. It was reverted because
the protocol did not provide a stable gain and would add an unnecessary
compatibility surface.

## Four-bit SQ8 candidate filtering

A four-bit scalar-quantized candidate index was tested with the same
10,000-row, 128-dimensional scan shape. With 22 candidates, Recall@10 was
0.3492 filtered and 0.2350 unfiltered. Raising the candidate window to 352
improved recall to 0.9468 and 0.7824, but the unfiltered group still missed the
0.95 requirement and total query time regressed by 9.4%. The prototype was
removed.

## Smaller SQ8 candidate window

The retained eight-bit SQ8 path used roughly 22 candidates for the benchmark's
`K=11`. A 16-candidate window was expected to reduce exact re-ranking work.
Five alternating image pairs measured a candidate/baseline total-query ratio of
1.016952, and the candidate was faster in only 1/5 pairs. The smaller window
was reverted because it reduced recall margin without producing a stable
latency benefit.

## Reduced committed-record cache

The committed-record cache was reduced from 16 MiB to 8 MiB and then to 12 MiB
to trade page-cache memory for additional reads.

- At 8 MiB, the peak-memory ratio was 0.950745, but the total-query ratio was
  1.111212 and the candidate was faster in only 1/5 pairs.
- At 12 MiB, the peak-memory ratio was 0.993446, but the total-query ratio was
  1.043872 and the candidate was faster in only 2/5 pairs.

Both changes were reverted. The memory reduction did not compensate for the
query regression.

## Reuse transaction-stage records after WAL sync

After the durable WAL sync, the commit path decoded staged records to update
the rebuildable primary index. A candidate reused the records already held by
the transaction stage to publish that index directly. Twenty-one alternating
durable-write pairs measured a baseline median of 1.4323261 seconds and a
candidate median of 1.4743245 seconds, about 2.9% slower. Physical writes were
identical. The source was reverted because the storage-core change had no
stable benefit.

## Partial-dimension SQ8 codebooks

Two partial-dimension SQ8 prototypes were evaluated to reduce index memory and
scan work while retaining exact re-ranking:

- A 112-dimensional codebook produced Recall@10 of 0.8288 filtered and 0.7498
  unfiltered.
- A 120-dimensional codebook produced Recall@10 of 0.9438 filtered and 0.9004
  unfiltered, with 520 queries failing the local result check.

Both prototypes were reverted. They did not meet the required recall threshold
and therefore could not be considered performance candidates.

## Fixed-spelling SELECT parser

The benchmark's canonical `SELECT id,L2_DISTANCE(...) AS d ... ORDER BY d
LIMIT ...` statement was recognized with fixed string searches before the
existing regex parser. The fallback parser and accepted SQL language were
unchanged. In the container, parsing the canonical statement fell from
3.65 microseconds to 1.66 microseconds.

Two alternating image runs with different seeds and 1,000 queries per round
could not reproduce a stable end-to-end gain. The 11-pair paired median
candidate/baseline query ratios were 0.981930 and 0.999328; the second run had
the candidate faster in only 6/11 pairs. All IDs and distances matched. The
source was reverted because the microbenchmark gain was below the full
JSONL-pipe variance.

## No-semantic-interposition native build

The native library was rebuilt with `-fno-semantic-interposition` to test
whether GCC could remove avoidable calls in the exact TopK path. A direct
same-process comparison over 1,000 queries measured candidate/baseline median
ratios of 0.998029 unfiltered and 0.995432 filtered; the candidate won only
523/1,000 and 537/1,000 queries.

Two alternating image runs with different seeds and 1,000 queries per round
did not confirm a stable end-to-end gain. Their paired median total-query
ratios were 0.994172 and 1.000022; the second run had the candidate faster in
only 3/7 pairs. All results matched. The compiler flag was reverted because
the measured change was inside host variance.

## OpenMP processor binding

The container environment was changed to `OMP_PROC_BIND=close` and
`OMP_PLACES=cores` so the two exact-scan workers would stay on adjacent cores
instead of being scheduled across the eight CPUs visible inside the Docker
Desktop cgroup. Three alternating image runs used different seeds and 1,000
queries per round.

The first two paired median candidate/baseline total-query ratios were 0.963865
and 0.970876, but a third seed produced 1.009294 and the candidate was faster
in only 3/7 pairs. The direction was not stable across seeds, so the
environment change was reverted. The official CPU topology and cpuset are
unknown, making a host-specific binding policy a poor submission default.

## Cached distance-query plan

For tables with at least 4,096 rows and the benchmark's exact
`id, L2_DISTANCE(v, ...) AS d` projection, a cached plan bypassed repeated
column and alias binding. It still delegated filtering, exact TopK,
verification and fallback to the existing vector index path. Smaller tables
and every other SQL shape used the original executor.

Two alternating image runs with different seeds and 1,000 queries per round
produced opposite results. The paired median total-query ratios were 0.975965
and 1.059155; the second run had the candidate faster in only 3/7 pairs. In a
separate same-process run the candidate was also slower. All IDs and distances
matched. The source was reverted because the small Python binding reduction did
not survive the complete JSONL path.

## Partial-dimension exact TopK checkpoints

The exact float32 candidate scan was first changed to test a conservative
partial-distance lower bound after 32, 64, and 96 dimensions. The full JSONL
path produced a paired candidate/baseline median ratio of 1.018692, so the
extra branches and threshold loads outweighed the pruning work and the variant
was reverted.

A single checkpoint after 64 dimensions was retained. Once the candidate set
is full, a row is abandoned only when its non-negative partial squared distance
exceeds the current K-th squared distance by 0.1% plus a small absolute margin.
Rows near the boundary receive the full scan, and the existing float32 exact
kernel and ordered-double verification still determine the returned results.
Across three seeds and seven alternating end-to-end pairs per seed, all 21
result sets matched and the median of run-median candidate/baseline total-query
ratios was 0.989624; physical I/O was unchanged and peak-memory ratios were
approximately 1.0001 to 1.0007. See `partial-bound-exact-20261113.md`.

## Scalar TopK threshold

The next prototype kept the current K-th distance in a scalar outside the
scan loop instead of reading `ranked.back()` and checking the container size
for every row. The direct kernel A/B was nearly neutral: the unfiltered paired
median candidate/baseline ratio was 0.994439 with the candidate faster in 6/9
rounds, while the filtered ratio was 1.002895 with the candidate faster in only
2/9 rounds.

Three end-to-end seeds used seven alternating pairs each. The paired median
total-query ratios were 0.982253, 0.979337, and 1.020172; the third seed
regressed in total, filtered, and unfiltered queries. All result sets matched,
physical I/O did not change materially, and peak-memory ratios stayed near
1.000 to 1.006. The direction was not stable enough to retain, so the source
was reverted and the experiment is recorded here.

## Retained changes

- The 64-dimension exact TopK partial-distance checkpoint is split out of the
  AVX2/FMA hot loop. Across five seeds and 35 alternating end-to-end pairs, all
  results matched, every run-median total-query ratio was below 1, and the
  median of those ratios was 0.984013; see
  `split-checkpoint-topk-20261113.md`.
- The 128-dimension exact TopK candidate scan uses one conservative 64-dimension
  partial-distance checkpoint; see `partial-bound-exact-20261113.md`.
- Canonical multi-row INSERT parser fast path: commit `46937676b`.
- TopK JSONL small-result single write: commit `630daca84`.
- Cgroup-aware OpenMP exact TopK for large unfiltered scans: commit `c9a79d8e4`.
- OpenMP exact TopK for large filtered selections: commit `1202d21db`.
- Self-written SQ8 ANN candidate filtering for benchmark-shaped unfiltered and
  filtered TopK; exact mode and fallback paths remain unchanged.
- The SQ8 candidate scan now uses precomputed integer row norms and
  `row_norm + query_norm - 2 * dot(row, query)`. This is exact for the
  quantized integer codes and avoids the rejected float32 version's
  floating-point error and per-row norm-load concerns; see
  `sq8-dot-20261113.md`.
- Clean-WAL recovery skips the unconditional startup truncate/`fsync`; see
  `clean-wal-recovery-20261113.md`.

## Sequential snapshot reads for committed single-table indexes

The vector snapshot builder was changed to read every committed single-table
index through `snapshot_rows()` instead of the committed-row cache. The intent
was to avoid retaining full vector rows in the random-access cache while the
snapshot already holds the same vectors.

Twelve repeated snapshot rebuilds measured a median of 25.7-26.8 ms with the
existing path and 27.3-30.2 ms with sequential reads. Three end-to-end seeds
with five alternating pairs each showed a stable peak-memory reduction of about
6%, but durable writes regressed in every seed (paired median ratios 1.0095 to
1.0278) and query direction changed across seeds. The change was reverted
because the memory reduction did not justify the write and rebuild regression.

## WAL block reservation with `fallocate`

Linux `fallocate(FALLOC_FL_KEEP_SIZE)` was used to reserve 16 MiB of WAL blocks
without changing the logical file size. An isolated 100-commit probe improved
from 0.352-0.405 s to 0.334-0.375 s across three pairs, but the gain did not
survive the complete database path.

Three seeds with five AB pairs each produced mixed write directions. Seven
additional reverse-order BA pairs measured a 1.0074 candidate/baseline write
ratio, while the earlier AB run measured 0.9943. Query direction reversed with
execution order, showing that the apparent query change was host noise. Peak
memory increased by about 0.08% in the reverse-order run. The reservation code
was reverted because it did not provide a stable end-to-end improvement.

## Persistent native worker for exact TopK

A custom native pool kept one worker thread alive between queries while the
caller scanned the other half of the table. The prototype used the same
float32 candidate kernel, sorted merge, exact-distance verification and
primary-key tie-break as the retained OpenMP path.

On 1,000 alternating same-process queries, every returned position and exact
distance matched. The pool was nevertheless slower than the existing OpenMP
path: 0.112887 s versus 0.076698 s overall (median paired ratio 1.846), with
filtered queries at 2.301 and unfiltered queries at 1.421. The condition
variable wake-up and completion barrier cost more than OpenMP's reused team.
The prototype was left outside the source tree.

## Four-candidate SIMD exact-distance verification

Exact verification was changed to evaluate four selected rows in parallel with
AVX2 double lanes. Each lane retained the original ordered addition per row,
so all candidate positions, float candidate distances and returned double
distances were bitwise identical to the scalar verifier.

The extra non-contiguous vector loads outweighed the parallelism. On 1,000
alternating same-process queries, total kernel time increased from 0.055896 s
to 0.059750 s (median paired ratio 1.193), filtered queries increased to 1.267,
and unfiltered queries were neutral at 0.9989. The prototype was not retained.
