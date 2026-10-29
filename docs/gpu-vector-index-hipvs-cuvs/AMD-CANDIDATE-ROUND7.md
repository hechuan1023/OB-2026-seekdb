# Round 7: development measurements exceed the 3x build target

Branch: `amd-cagra-opt-round7`. Parent:
`60de6f30afb441c8947b29d9eab5d979cb23eb9f`. All earlier submission branches are
retained. This remains a descendant of frozen V5.

## Change

Uniform defaults are final/intermediate graph degrees 64/64, eight NN-Descent
iterations and a 4096-candidate MULTI_CTA search. The wider fixed-library search
recovers recall from the cheaper graph. No dataset identity, dimension, query
identity or evaluation phase selects a different policy. FP32 data, SQL
parameters, ID mapping and the fixed hipVS library are unchanged.

The round-6 MULTI_CTA selection for configured frontiers above 512 remains.
`OB_CUVS_PROFILE=baseline` still selects the original 128/256/100 graph,
automatic 512-floor frontier and SINGLE_CTA search. Historical profiles and
explicit overrides keep their established precedence. Existing OBCUVS03
persistence and OBCUVS02 reading remain in the unchanged native adaptor.

## Paired native SQL validation, 2026-10-30

Tests ran directly over SSH on one Radeon Cloud instance with the fixed hipVS,
16 configured CPU workers and an 8 GiB seekdb memory budget. Each build used an
initially empty runtime; order was control/candidate for 128 dimensions and
candidate/control for 256 dimensions. The control is the earlier native engine
with V5 source behavior and explicit V5 bridge settings, not a separately
rebuilt pristine V5 artifact. Data are development inputs, not the hidden set.

Every k used 4096 distinct single-row queries once, after 16 warmup calls.
Exact-reference neighbors were computed against all 250000 loaded rows. All
returned results had exactly k distinct valid IDs. Candidate snapshots were
readable after full process restart. Archived candidate logs contained no bridge
search errors or lazy CPU fallback builds.

### Gaussian development inputs

| Dimension | Control build + first SQL query | Candidate | Speedup |
|---|---:|---:|---:|
| 128 | 19.342066 s | 5.375961 s | 3.598x |
| 256 | 22.124586 s | 7.678629 s | 2.881x |

The 40%/60% weighted geometric build speedup is **3.149x**. This exceeds the 3x
full-build-score line on these development inputs. It is not an official score.

| Dimension | Metric | Control | Candidate |
|---|---|---:|---:|
| 128 | Recall@10 | .991088867 | .995751953 |
| 128 | Recall@100 | .967092285 | .988652344 |
| 256 | Recall@10 | .951635742 | .980468750 |
| 256 | Recall@100 | .898825684 | .960270996 |
| 128 | Top-10 QPS | 132.353 | 1401.019 |
| 128 | Top-100 QPS | 127.928 | 921.463 |
| 256 | Top-10 QPS | 124.876 | 1247.575 |
| 256 | Top-100 QPS | 122.448 | 847.924 |

Both recall metrics improved on both pairs. Query throughput is above both
the .90 regression gate and 1.5x full-query-score line on these inputs.

| Dimension | Control GPU peak | Candidate GPU peak | VRAM ratio | Control / candidate peak RSS |
|---|---:|---:|---:|---:|
| 128 | 734208000 B | 665624576 B | .90659 | 2687088 / 1858436 KiB |
| 256 | 862380032 B | 794165248 B | .92090 | 3599260 / 2974108 KiB |

GPU memory and process RSS were sampled every 20 ms during build and queries.
Both observed VRAM ratios are below 1.25. Sampling does not replace formal
peak-memory profiling.

### Public SIFT development subset

The [round-5 report](AMD-CANDIDATE-ROUND5.md) records the public input URL,
SHA-256, 250000 selected train rows and 4096 distinct test rows. Inputs remained
FP32; exact neighbors were recomputed for the selected subset. Its ordering and
IDs are not asserted identical to official SIFT250K.

| Configuration | Build + first query | Recall@10 | Recall@100 | Top-10 / Top-100 QPS |
|---|---:|---:|---:|---:|
| V5 settings/control | 18.756492 s | .999926758 | .999956055 | 111.625 / 108.031 |
| Candidate defaults | 5.185870 s | .999951172 | .999968262 | 374.683 / 335.841 |

SIFT build speedup is 3.617x. Combining this public subset at weight .4 with the
256-dimensional Gaussian proxy at .6 gives 3.156x. This is another development
observation, not a substitution for the formal hidden dataset. All SIFT result
integrity, GPU-error and restart checks passed.

## Compatibility and qualification

- Full engine and bridge compilation passed, along with 11 actual parameter
  resolver cases, the existing 8000-request worker test and v2/v3 framing checks.
- Real-GPU snapshot round trips at dimensions 17, 128 and 256 preserved FP32 bits.
- Native 1024-row SQL create/index/search passed for both lib=vsag and lib=cuvs.
  Direct fixed-library searches with 1, 2 and 8 queries and Top-10/100 returned
  distinct valid IDs and expected self matches.
- The prior full CPU comparison remains applicable to the unchanged native CPU
  code: 250000-row HNSW construction plus first query took 127.767423/174.112763 s
  at 128/256 dimensions. Current GPU observations are 5.375961/7.678629 s. The
  engines achieved different recall; this is not the scoring baseline. Details
  and reproducible CPU/GPU CLI commands are in round 5.
- Launching under the local ROCProfiler SDK aborted in `ObTimer::init` for both
  the old control and candidate, before database initialization. Attaching the
  local tool after startup succeeded but produced no kernel records, including
  a repeat with explicit tracing configuration. This does **not** count as GPU
  profiler qualification. All logs are retained; formal GPU-path profiling is
  still unverified.

## Submission and remaining verification

Build and deploy this branch's matching bridge and native engine. Normal
candidate startup requires no OB_CUVS environment overrides. Keep separate
runtime directories for builds; do not reuse a prebuilt graph in a build trial.
The development controllers, source provenance, evidence JSON, binaries and
public input subset are preserved in the stage backup. Earlier reports describe
the unchanged persistence format and source/interface compatibility.

The recorded development inputs pass the tested integrity, recall, throughput
and sampled-resource gates and exceed the full-score performance lines. Formal
hidden-input quality, exact profiler limits, GPU-path qualification and the
official score remain unverified. Freeze this version for submission rather
than assuming that lowering iterations further guarantees a better result.
