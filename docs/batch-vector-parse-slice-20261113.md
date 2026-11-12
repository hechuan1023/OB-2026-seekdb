# Batch vector parser copy removal

Date: 2026-11-13

The native batch vector parser fills one flat `array('f')` and previously
returned each row through `array('f', flat[start:start + size])`. The slice is
already an independent `array('f')`, so the outer constructor copied every
vector a second time. The candidate returns the slice directly.

The accepted language, float32 conversion, native parser, fallback behavior,
and stored row type are unchanged. A regression assertion mutates one returned
vector and checks that the adjacent vector remains independent.

## Write A/B

Each run used alternating fresh containers, the JSONL protocol, 10,000 rows,
128 dimensions, 100-row INSERT batches, and exact verification after loading.
The two 21-pair runs used different data seeds.

| Seed | Pairs | Baseline write median | Candidate write median | Paired ratio median | Candidate faster | Peak-memory ratio |
|---|---:|---:|---:|---:|---:|---:|
| 20261110 | 21 | 2.553020 s | 2.525619 s | 0.993030 | 13/21 | 0.999886 |
| 20261113 | 21 | 2.462634 s | 2.446444 s | 0.991992 | 12/21 | 0.999774 |

The measured write improvement is small, about 0.7% to 0.8%, but points in the
same direction in both seeds. Query results remained exact, and the change
removes a real allocation and copy without altering durable data.

## Validation

- Windows unit suite: 182/182.
- Linux container unit suite at 256 MiB: 182/182.
- Official public JSONL protocol: 20/20 cases.
- Public SQL adapter: 40/40 across typed and STDIO paths.
- 64 MiB lifecycle and SIGKILL recovery: 4/4.
- 18,000 x 1,024 pressure run: passed with `oom=0` and `oom_kill=0`;
  the 73,728,000-byte vector file exceeded the 67,108,864-byte memory limit.

These are local Docker Desktop diagnostics, not official T/B/M or E values.
