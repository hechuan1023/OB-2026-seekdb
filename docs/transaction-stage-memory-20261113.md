# Transaction stage memory fast path

Date: 2026-11-13

This experiment reduces the duplicate write work between a transaction stage
and the durable WAL. It is a local Docker Desktop diagnostic, not an official
T/B/M/E result.

## Change

The baseline `191915b12` wrote every transaction record to
`/data/transaction.stage` and then copied the complete stage into the WAL at
commit. The candidate uses `tempfile.SpooledTemporaryFile` with a 512 KiB
memory threshold:

- small transactions stay in memory;
- larger transactions spill to an anonymous temporary file under the configured
  data directory;
- commit still writes the complete payload to the WAL, publishes the footer,
  calls `fsync`, and only then acknowledges success;
- after commit or rollback, the stage is replaced with an empty in-memory
  buffer so a large transaction does not tax later small transactions;
- startup and close remove legacy `transaction.stage` and
  `transaction-stage-*` leftovers.

The WAL is still the only authoritative committed state. A crash before the
WAL footer and sync discards the uncommitted stage, as before.

## Validation

| Check | Result |
| --- | --- |
| Windows full regression | 181/181 passed |
| Docker 2 CPU / 256 MiB full regression | 181/181 passed |
| Official public JSONL cases | 20/20 passed |
| 64 MiB lifecycle and SIGKILL recovery | 4/4 passed |
| 18,000 x 1,024 vector pressure | passed; `oom=0`, `oom_kill=0` |
| Large-stage rollover test | passed; stage spilled under `/data`, then returned to memory |

The pressure probe reported a 4,148,000-byte transaction stage and
`memory_peak_bytes=67,108,864`; the probe and database child share the cgroup,
so this is a resource correctness check rather than a standalone benchmark.

## Performance

Fourteen alternating fresh-container JSONL pairs used 10,000 rows, 128
dimensions, 100-row commits, 1,000 TopK queries, and independent truth checks:

| Metric | Baseline median | Candidate median | Paired median change | Candidate wins |
| --- | ---: | ---: | ---: | ---: |
| Write | 1.4314 s | 1.4128 s | -1.08% | 8/14 |
| Query | 0.25965 s | 0.26030 s | +0.45% | 7/14 |
| Filtered query | 0.11075 s | 0.11070 s | -0.94% | 8/14 |
| Unfiltered query | 0.14940 s | 0.15100 s | +1.38% | 7/14 |
| Peak memory | 103,346,176 B | 103,387,136 B | +0.05% | 4/14 |
| `/data` writes | 6,017,024 B | 6,017,024 B | identical | — |

The query and memory differences are inside host noise; the candidate does not
change the query or vector-index code. The write improvement is small because
the required commit `fsync` remains the dominant cost. The candidate is retained
only because it preserves durability and I/O, has no attributable regression,
and reduces avoidable stage writes.

## Limits

- The 512 KiB threshold is a local choice; official memory and transaction-size
  limits are still not frozen in the public package.
- The stage is not durable before commit by design; uncommitted transactions
  must disappear after a crash.
- This is not an ANN index and does not reduce the exact vector scan.
