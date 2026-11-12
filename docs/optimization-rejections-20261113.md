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

## Retained changes

- Canonical multi-row INSERT parser fast path: commit `46937676b`.
- TopK JSONL small-result single write: commit `630daca84`.
- Cgroup-aware OpenMP exact TopK for large unfiltered scans: commit `c9a79d8e4`.
