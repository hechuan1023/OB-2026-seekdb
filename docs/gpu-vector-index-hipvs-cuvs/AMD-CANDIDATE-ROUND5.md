# Round 5: reduce NN-Descent iterations after broader validation

Branch: `amd-cagra-opt-round5`. Parent: round 4 commit
`825a4f4ec699777ed99cd11709147a42879a179f`. Earlier submission branches remain
available. The candidate is a descendant of frozen V5.

## Changes

The uniform default is now intermediate degree 192, final degree 192 and 25
NN-Descent iterations. Round 4 used 35 iterations. There is no dataset, dimension
or evaluation-stage selection. Search parameters remain the original default
policy (itopk at least 512 or four times k, width 4). All vectors remain FP32.
The existing `baseline` profile still selects 128/256/100. Round-4 OBCUVS03
persistence and its OBCUVS02 reader are retained; its deployment/format notes
continue to apply.

The development CLI benchmark now supports `--backend vsag` for CPU comparisons,
reports `build_and_first_query_seconds`, validates row/result counts and uses
bounded-batch FP64 exact-neighbor calculation over the six-decimal FP32 values
actually loaded. Its QPS includes mysql CLI startup. These changes affect our
development utility, not the organizer's fixed evaluator or its inputs.

## Executed development validation, 2026-10-30

All native comparisons were run on the same Radeon Cloud instance, using the
fixed hipVS installation, 16 configured CPU workers and an 8 GiB seekdb memory
budget. The control engine's native sources are unchanged from V5; its bridge
explicitly selects V5 parameters. It is the earlier compiled native engine, not
a separately rebuilt pristine V5 artifact. Official evaluation is still needed.

### Repeated fresh builds: 35 versus 25 iterations

Each case started with an empty runtime directory. Order was 35/25/25/35 at
128 dimensions, then 25/35/35/25 at 256 dimensions. Inputs were the same 250000
rounded Gaussian FP32 vectors; each trial had 256 exact-reference queries.

| Dimension | Mean build + first query, 35 | Mean, 25 | Observed reduction |
|---|---:|---:|---:|
| 128 | 10.761765 s | 9.711870 s | 9.76% |
| 256 | 12.972270 s | 12.226130 s | 5.75% |

25-iteration trials had Recall@10/100 of .989844/.971602 and
.990234/.972344 at 128 dimensions; .950391/.905781 and .947656/.908164 at
256 dimensions. Sampled GPU peaks were about 797 MB and 926 MB respectively,
similar to the 35-iteration trials. Sampling interval was 20 ms. These are
development observations, not official profiler certification.

### Broader paired check: 4096 distinct Gaussian queries

The final compiled candidate was tested with defaults and no OB_CUVS overrides.
Both paths used each of 4096 distinct queries once per k, after 16 warmup calls.
Every returned result had exactly k distinct valid IDs. Candidate snapshots
were readable after process restart.

| Dimension | Control build + first query | Candidate | Speedup |
|---|---:|---:|---:|
| 128 | 19.255352 s | 9.850465 s | 1.955x |
| 256 | 22.548126 s | 12.298432 s | 1.833x |

| Dimension | Metric | Control | Candidate |
|---|---|---:|---:|
| 128 | Recall@10 | .991137695 | .988964844 |
| 128 | Recall@100 | .966977539 | .971477051 |
| 256 | Recall@10 | .952001953 | .948559570 |
| 256 | Recall@100 | .899121094 | .905502930 |
| 128 | Top-10 QPS | 133.993 | 239.735 |
| 128 | Top-100 QPS | 130.740 | 214.367 |
| 256 | Top-10 QPS | 126.810 | 157.918 |
| 256 | Top-100 QPS | 124.510 | 152.456 |

The larger observed Recall@10 drop is .003442383, within .005 on these inputs.
It leaves only about .00156 of margin on that development pair. This does not
establish the hidden-input gate. The weighted development build speedup is about
1.88x, below the 3x full-build-score target. Neither these numbers nor the
public SIFT subset establish full marks.

### Public SIFT development subset

Input source: the [ANN-Benchmarks dataset loader](https://github.com/erikbern/ann-benchmarks/blob/main/ann_benchmarks/datasets.py)
and its `https://ann-benchmarks.com/sift-128-euclidean.hdf5` download. SHA-256:
`dd6f0a6ed6b7ebb8934680f861a33ed01ff33991eaee4fd60914d854a0ca5984`.
The agent extracted train rows 0:250000 and the first 4096 distinct test rows,
preserving their order. No neighbor labels were used; exact neighbors were
recomputed against this subset. Raw integer-valued FP32 vectors were retained.
The subset/order is not asserted identical to official SIFT250K.

| Configuration | Build + first query | Recall@10 | Recall@100 | QPS 10 / 100 |
|---|---:|---:|---:|---:|
| V5 settings/control engine | 18.725692 s | .999951172 | .999968262 | 109.681 / 108.290 |
| Round 4, 35 iterations | 10.796040 s | .999951172 | .999963379 | 176.605 / 166.772 |
| 25 iterations | 10.095400 s | .999926758 | .999958496 | 175.761 / 166.501 |

Each k used all 4096 distinct queries once. Cardinality, valid IDs and restart
readback passed for all three configurations. The initial public query prefix
contained duplicate vectors; the qualification check rejected it before quality
measurement. The final run uses recorded distinct query indices. Its control
build was reused because the base and first query were unchanged.

### CPU/GPU construction comparison

Full lib=vsag HNSW create/index/search also ran on both 250000-row Gaussian
datasets, using m=16, ef_construction=200 and ef_search=64. GPU observations below
come from the broader fresh candidate check above.

| Dimension | CPU HNSW build + first query | GPU CAGRA build + first query |
|---|---:|---:|
| 128 | 127.767423 s | 9.850465 s |
| 256 | 174.112763 s | 12.298432 s |

These engines have different graph construction and achieved different quality.
CPU Recall@10/100 was .362109/.340625 and .298438/.288281 on 256 queries; GPU
quality is listed above. This is a functional construction comparison, not a
quality-matched comparison or the scoring baseline. Scoring uses V5 GPU.

### Other checks and rejected experiments

- Engine and bridge compiled; nine parameter cases, 8000 worker requests,
  production v2/v3 framing tests and real-GPU bitwise FP32 snapshot tests passed.
- Bounded exact-neighbor calculation matched direct pairwise distances at
  k=1, k=10 and k=all, across multiple query batches.
- Degree 224 intermediate graphs lost substantial recall and were rejected.
- An early private script misspelled an environment variable; its intended
  parameter labels were rejected and the test was repeated with actual-parameter
  assertions. Those trials did not change a published default.
- Later non-default search-buffer experiments returned bridge errors and entered
  the existing CPU fallback. Their throughput/quality results are invalid for
  this contest and are excluded. The submitted search defaults remain unchanged.

## Reproduce development CPU/GPU observations

Use a dedicated test instance. This CLI helper drops its named test database.
Run the following once per backend, with unique labels/databases, then repeat
at `--dim 256`. Use identical instance resources and compare the
`build_and_first_query_seconds` field. It generates deterministic Gaussian
inputs; it does not fetch or reproduce the official hidden data.

```bash
python3 docs/gpu-vector-index-hipvs-cuvs/validation/amd_cagra_bench.py \
  --port 2981 --backend vsag --rows 250000 --dim 128 \
  --db amd_cpu128 --label cpu128 --evidence /workspace/amd-evidence
python3 docs/gpu-vector-index-hipvs-cuvs/validation/amd_cagra_bench.py \
  --port 2981 --backend cuvs --rows 250000 --dim 128 \
  --db amd_gpu128 --label gpu128 --evidence /workspace/amd-evidence
```

The detailed tables use persistent libmysqlclient, not CLI QPS. The exact private
controllers, input provenance, evidence JSON and compiled artifacts are retained
in the stage backup. No online service or new product dependency is required.
