# Batch vector parsing for multi-row INSERT (2026-11-13)

## Change

A multi-row INSERT previously called the native vector parser once per vector.
The new `mv_parse_vectors` entry point parses all vector literals in a statement
with one ctypes call and writes their float32 values into one flat buffer.

The optimization is used only when:

- the statement contains more than one row;
- every row has the expected number of values;
- every value in the vector column is a string literal; and
- every vector passes the same strict JSON-number parser.

If any condition fails, `Engine.insert` uses the original per-value conversion
path. This preserves error ordering, error codes, statement atomicity, and the
accepted vector language. The old `mv_parse_vector` symbol remains available.

## Measurement

Baseline is commit `1190b089c` (combined native TopK selection and exact
verification). Local Docker Desktop Linux used 2 CPU, 256 MiB, swap disabled,
and network disabled. Each end-to-end run loaded 10,000 rows of 128 dimensions
in 100-row batches and executed 1,000 exact TopK queries. Baseline and candidate
were alternated in AB and BA order, five pairs each.

Medians across all ten pairs:

| Metric | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Write 10,000 rows | 1.4104 s | 1.3821 s | -2.01% |
| All 1,000 queries | 0.3429 s | 0.3397 s | -0.95% |
| 500 filtered queries | 0.1524 s | 0.1585 s | +4.04% |
| 500 unfiltered queries | 0.1902 s | 0.1827 s | -3.94% |

Seven of ten paired writes were faster. Query code did not change; its mixed
direction is treated as host noise. Five alternating write-stage profiles
isolated the intended effect: `convert/vector` fell from 0.73-0.91 ms per batch
to 0.13-0.15 ms per batch. End-to-end write remains dominated by durable commit
and `fsync`, so the total improvement is smaller.

## Resource check

Three-round `tests.container_benchmark` runs used 10,000 rows, 128 dimensions,
1,000 queries, one cycle, and a 128 MiB container limit.

| Metric | Baseline | Candidate |
|---|---:|---:|
| Write `memory.peak` median | 23.65 MiB | 23.61 MiB |
| Query `memory.peak` median | 35.29 MiB | 35.55 MiB |
| Write median | 1.7605 s | 1.5803 s |
| Query median | 1.5641 s | 1.5381 s |

Write and query device write-byte counts are unchanged. The first read sample
increased by 155,648 bytes; later samples reported no extra device reads. A
64 MiB pressure run with 18,000 x 1,024 vectors passed exact/filtered TopK,
transaction rollback, restart, and SIGKILL recovery with `oom=0` and
`oom_kill=0`.

## Validation

- Windows `unittest discover`: 173 tests passed.
- Docker 2 CPU, 256 MiB: 173 tests passed.
- Official public JSONL package: 20/20 cases passed.
- Docker 64 MiB lifecycle: read-own-writes, statement atomicity, rollback,
  acknowledged-commit SIGKILL, uncommitted rollback, EOF/SIGTERM restart passed.
- Docker 64 MiB vector pressure: 18,000 x 1,024, no OOM or OOM kill.
- Candidate image:
  `sha256:efd876761c92dc0ab2b53116279ac2f16b24e402385461f116aedcca5e54389c`.

These are local diagnostics, not official T/B/M or E. Hidden workloads and
formal resource limits remain unverified.
