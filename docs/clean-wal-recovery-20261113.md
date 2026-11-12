# Clean-WAL recovery without an unconditional sync

Date: 2026-11-13

## Change

Recovery used to truncate the WAL and call `fsync` on every startup, even when
the last transaction footer was complete and the parsed recovery position was
already the file size. The candidate now performs that durable truncation only
when an incomplete tail is present. A clean WAL is scanned and left unchanged.

The durability rule is unchanged: an incomplete transaction tail is still
truncated and synchronized before the database reports ready. A complete WAL is
already authoritative and needs no startup write.

## Validation

The focused recovery, engine and WAL-order suites passed 44/44. The new test
`test_clean_reopen_does_not_sync_the_wal` patches `os.fsync` during a clean
reopen and requires zero calls.

Seven alternating fresh-container pairs wrote 10,000 rows, created the vector
index, then restarted from the same volume. The startup-to-ready paired median
candidate/baseline ratio was 0.970305 in the first run and 0.986187 in a second
10-pair run. Timing was noisy, but the physical write delta was stable: every
candidate run wrote 6,037,504 bytes instead of 6,041,600 bytes, exactly 4,096
bytes less. In the separate 7-pair main workload, all 7 candidate runs had the
same 4 KiB reduction; the median peak-memory ratio was 0.995433.

Full validation after retaining the change:

- Windows: 183/183 tests.
- Linux Docker 256 MiB: 183/183 tests.
- Official JSONL public package: 20/20 cases.
- SQL adapter: 40/40 tests, 20 cases and 95 SQL assertions per path.
- 64 MiB lifecycle: 4/4 checks, including acknowledged-commit SIGKILL,
  uncommitted rollback, EOF and SIGTERM restart.
- 18,000 rows × 1,024 dimensions under 64 MiB: no OOM, exact and filtered
  TopK correct, long transaction rollback and restart correct.

These are local Docker Desktop diagnostics, not the official T/B/M or E score.

