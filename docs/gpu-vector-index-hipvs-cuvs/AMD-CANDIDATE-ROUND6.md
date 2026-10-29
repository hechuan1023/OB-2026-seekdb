# Round 6: smaller graphs with the library's MULTI_CTA search

Branch: `amd-cagra-opt-round6`, based on round 5 commit
`defd649358c23a30e884a3accde5bb59119a5d90`. Earlier submission branches remain
unchanged. The frozen V5 ancestor is retained.

## Implementation and scope

Uniform defaults are now final/intermediate graph degrees 128/128 and 15
NN-Descent iterations. Search retains 1024 candidates and uses the fixed
hipVS library's MULTI_CTA implementation. The bridge still calls
`cuvsCagraSearch`; the installed library is unmodified. FP32 inputs, stored
vectors, SQL parameters and ID mapping are unchanged. Wider searches recover
recall from a cheaper graph.

The installed factory search policy is SINGLE_CTA with itopk=64. V5 raised the
frontier to at least 512 and search width to 4. On this fixed hipVS build,
SINGLE_CTA rejects the configured 1024 frontier. The bridge now selects
MULTI_CTA when an explicitly configured frontier exceeds 512. The `baseline`
profile retains its original 128/256/100 build settings, automatic 512-floor
frontier and SINGLE_CTA policy. Historical fast/aggressive profiles also keep
their original automatic frontier policy. Explicit environment values continue
to take precedence.

The round-4 OBCUVS03 format and OBCUVS02 reader are retained. Build and deploy
the matching bridge, as described in the round-4 report. A separate buffer-cache
prototype had insignificant or inconsistent throughput gains and was excluded;
its changes are not in this commit.

## Executed checks, 2026-10-30

Native comparisons ran over SSH on the same Radeon Cloud instance, using the
fixed hipVS installation, 16 configured CPU workers and an 8 GiB seekdb budget.
Every case used an initially empty runtime. Order: control/candidate at 128
dimensions, candidate/control at 256 dimensions. The control is the earlier
native engine with V5 source behavior and explicit V5 bridge settings; it is
not a newly rebuilt pristine baseline artifact. Official scoring remains separate.

### Gaussian development data, 250000 rows and 4096 distinct queries

Each query was used once per k after 16 warmup calls. Exact neighbors were
computed from the six-decimal FP32 values actually loaded. Every result had k
distinct IDs in range. Candidate snapshots were readable after process restart.

| Dimension | Control build + first query | Candidate | Speedup |
|---|---:|---:|---:|
| 128 | 19.380270 s | 6.883758 s | 2.815x |
| 256 | 21.774167 s | 9.409278 s | 2.314x |

| Dimension | Metric | Control | Candidate |
|---|---|---:|---:|
| 128 | Recall@10 | .990332031 | .990454102 |
| 128 | Recall@100 | .966491699 | .976975098 |
| 256 | Recall@10 | .951367188 | .962744141 |
| 256 | Recall@100 | .899118652 | .928491211 |
| 128 | Top-10 QPS | 134.999 | 1225.476 |
| 128 | Top-100 QPS | 130.519 | 804.408 |
| 256 | Top-10 QPS | 126.515 | 1067.132 |
| 256 | Top-100 QPS | 124.756 | 770.672 |

Both recall measures improved on these pairs. The weighted development build
speedup is about 2.50x, below the 3x full-build-score target. The data are synthetic,
not the formal hidden set; these results do not establish full marks.

| Dimension | Control GPU peak | Candidate GPU peak | Control / candidate peak RSS |
|---|---:|---:|---:|
| 128 | 734203904 B | 730935296 B | 2655568 / 2191660 KiB |
| 256 | 862388224 B | 859615232 B | 3639756 / 3370204 KiB |

Assigned-device VRAM and process RSS were sampled every 20 ms across build and
queries. Both sampled VRAM ratios are below 1.0, hence below 1.25 on these runs.
Sampling is observational and does not certify an official profiler peak.

### Public SIFT development subset

Input provenance, selected 250000 train rows, 4096 distinct test rows and source
SHA-256 are recorded in the [round-5 report](AMD-CANDIDATE-ROUND5.md). The same
subset was used, with exact neighbors recomputed locally rather than reading
published labels. It is not asserted identical to official SIFT250K ordering.

| Configuration | Build + first query | Recall@10 | Recall@100 | Top-10 / Top-100 QPS |
|---|---:|---:|---:|---:|
| V5 settings/control | 19.024623 s | .999902344 | .999975586 | 111.458 / 109.264 |
| Candidate defaults | 7.075068 s | .999926758 | .999960938 | 347.503 / 313.499 |

All cardinality, identity-range and restart checks passed. The largest observed
recall decrease is about .00001465 for SIFT Recall@100, within .005 on this pair.
Raw Gaussian and SIFT results are separate workloads and are not mixed into a
claimed official score.

### Compatibility, compilation and profiler qualification

- Full engine/bridge compilation passed; 11 production parameter cases passed.
- Existing 8000-request worker and v2/v3 framing checks passed again.
- Real GPU snapshot dataset round trips at dimensions 17, 128 and 256 preserved
  all FP32 bits. Original v2 read support remains in the unchanged adaptor.
- Native SQL at 1024 rows passed with both lib=vsag and the new lib=cuvs defaults.
  GPU error/fallback checks passed in the SIFT/basic-SQL run.
- Direct fixed-library searches with 1, 2 and 8 queries, varying Top-10/100,
  returned valid distinct IDs and the expected self matches. Archived logs from
  both Gaussian candidate runs contained no bridge search errors or lazy CPU
  fallback builds.
- The full CPU construction comparison remains in the round-5 report: unchanged
  native CPU code built 250000-row indexes in 127.767423/174.112763 s. Current GPU
  observations are 6.883758/9.409278 s. CPU and GPU achieved different recall;
  this is a functional construction comparison, not the scoring baseline.
- A local ROCProfiler SDK launch was attempted. Both the candidate and the old
  control aborted in `ObTimer::init` before database initialization completed.
  No kernel trace was obtained, so this is not counted as profiler success.
  Logs for both failures are retained; official GPU-path profiling remains pending.

## Reproduction and remaining work

Use the bridge and native build commands from the project guide. Ordinary
candidate startup needs no OB_CUVS environment overrides. For a development
control, set `OB_CUVS_PROFILE=baseline` in the observer environment and use the
old control engine. Do not use the new engine as a pristine V5 query control:
it also contains the earlier reusable query worker.

The CLI helper supports CPU/GPU comparisons as described in round 5. It includes
mysql process overhead, whereas the tables above use persistent libmysqlclient.
Exact private controllers, source provenance, evidence and binaries are in the
stage backup. Final hidden quality, external GPU-path qualification, formal
memory measurement and official score are still unverified.
