# MiniVectorDB development guidance

- Read `docs/minivectordb-requirements.md`, `README.md`, and `docs/validation.md` before changes.
- Upstream public SQL cases are in `mysqltest/`; do not modify their inputs or expected results to make tests pass.
- Official commit f4d4e2665 supplies `STDIO_PROTOCOL.md` and `protocol_check/`. Docker defaults to JSONL; the local SQL adapter remains available for historical tests. Request search modes must preserve the active process and transaction.
- Storage, execution, transactions, and vector indexes must be implemented here. Do not use existing database engines or vector index libraries.
- All database data, logs, indexes, and temporary files belong under the configured data directory (default `/data`).
- Preserve statement atomicity inside explicit transactions and acknowledge commits only after durable WAL synchronization.
- Use `python -m unittest discover -v` for the correctness suite. Add independent counterexamples for behavior changes, especially transaction and recovery changes.
- Record real commands and results. Never invent student reasoning, user dialogue, experiments, official test results, or a benchmark score in `agent/conversation.json`.
- Do not treat local Windows or tracemalloc results as Linux cgroup validation.
- Apply `docs/performance-acceptance.md` to every performance change. Do not present or promote a candidate as an overall improvement when a measured dimension has a reproducible regression; improve or revert it, and label insufficiently measured dimensions as unresolved.
