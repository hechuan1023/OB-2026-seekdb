# Canonical multi-row INSERT parser fast path

Date: 2026-11-13

Baseline commit: `45d9319955d0d12711ca0f3543d8b88d836bf1c2`

The benchmark loader sends 100-row INSERT statements with three columns:
BIGINT primary key, simple VARCHAR, and a JSON vector literal. The generic
tokenizer and parser accounted for about 11% of the local write profile.

The candidate recognizes only this canonical shape:

- unquoted table identifier;
- `VALUES` without an explicit column list;
- exactly three values per row;
- integer primary key;
- strings without an escaped quote;
- optional trailing whitespace and semicolon.

Every other INSERT shape falls back to the generic parser. The shortcut returns
the same AST as the generic parser for accepted rows, so type checks, duplicate
keys, vector parsing, statement atomicity, WAL writing and commit durability
remain in the existing execution and storage paths.

## Measurements

All measurements are local Docker Desktop Linux diagnostics, not official
T/B/M or E.

### Parser A/B

The same 100 canonical INSERT statements were parsed alternately with the
generic parser and the shortcut in one process.

| Metric | Generic parser | Fast path | Change |
| --- | ---: | ---: | ---: |
| 100 INSERT parses, median | 0.061220 s | 0.020951 s | -65.78% |
| Minimum | 0.057405 s | 0.019074 s | -66.77% |
| Maximum | 0.067096 s | 0.021627 s | -67.77% |

An earlier whole-statement lazy regular expression was rejected before
retention: it took 0.924126 s for the same parser workload because of
backtracking. The retained implementation matches only the fixed prefix and
scans row bodies incrementally.

### End-to-end write A/B

Seven alternating fresh-volume runs used 10,000 rows, 128 dimensions, 100 rows
per INSERT and one durable commit per batch.

| Metric | Baseline median | Candidate median | Change |
| --- | ---: | ---: | ---: |
| Write time | 1.541580 s | 1.535905 s | -0.37% |
| Write-phase `memory.peak` | 24,641,536 B | 24,608,768 B | -0.13% |
| Write I/O | 6,082,560 B | 6,082,560 B | unchanged |
| All queries | 1.458545 s | 1.457309 s | -0.08% |
| Query-phase `memory.peak` | 36,048,896 B | 36,052,992 B | +0.01% |

The end-to-end write difference is much smaller than the parser difference
because fsync and storage work dominate each batch. The result is retained
because parsing is materially cheaper while write time, I/O, memory, query
time and correctness show no regression.

## Correctness and resource validation

- Windows: 177 tests passed.
- Docker 2 CPU / 256 MiB: 177 tests passed.
- Official public JSONL package: 20/20 cases passed.
- 64 MiB lifecycle: read-your-writes, multi-row statement atomicity, rollback,
  acknowledged commit followed by SIGKILL, uncommitted rollback, EOF rollback,
  and SIGTERM restart passed.
- 64 MiB pressure: 18,000 rows, 1,024 dimensions, 73,728,000-byte vector file,
  `oom=0`, `oom_kill=0`; elapsed 12.73 s.
- Added AST-equivalence tests for canonical INSERT and fallback tests for
  explicit column lists, extra values, escaped quotes and quoted identifiers.
