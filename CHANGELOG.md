# Changelog

All notable changes to Okto Grafx are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/) — with the caveat that below 0.1.0 anything may change,
including the on-disk format.

## [Unreleased]

### Changed

- **Configuration is now canonical and enforced at the first public boundary.** Every accepted
  scalar is copied to an exact built-in value before it can reach persisted identity/WAL metadata
  or an adapter. Buffer budgets must hold both store working sets; WAL segments are constrained to
  the reader's 256-byte through 1-GiB domain, including exceptional batches; malformed
  Unicode/oversized OpenMetrics ports, hostile `PathLike` objects, invalid or concurrently changed
  registries, and forged/uninitialised configuration instances receive typed refusals before a
  database root is created. The process-global `checksum` selector remains effective with a
  caller-supplied registry.
- **Transactions now have four opt-in hard admission budgets, all defaulting to `None`.**
  `max_statement_writes` counts logical writes held by one statement; `max_transaction_rows`
  counts retained `row_intents`; `max_transaction_bytes` charges encoded row tuples, staged
  logical-record `encoded_length()` values and retained page-image generations. Ordinary page
  replacement charges by delta; a rollback preimage remains charged while its statement mark is
  live. `max_wal_batch_bytes` sums the complete record batch including
  `COMMIT` and excluding `SEGMENT_HEADER`, and is checked before WAL append. Overruns raise the
  non-retryable `GrafxTransactionBudgetExceeded`; refusal neither truncates the WAL nor persists a
  partial statement.
- **Queries now have two opt-in positive row-admission limits, both defaulting to `None`.**
  `max_result_rows` counts the public terminal incrementally and refuses row N+1 before retaining
  it, consuming the rest of the stream or calling `context.release()`. `max_intermediate_rows`
  counts each non-terminal physical operator separately for the whole execution; the public
  terminal is charged only as result, while a terminal with no public columns is intermediate.
  Overruns raise non-retryable `GrafxQueryBudgetExceeded` without truncating or releasing partial
  writes. These limits are not cumulative and do not provide RSS, streaming, deadline, traversal
  or spill budgets. Sort, aggregate, distinct and eager operators can retain up to the configured
  rows or states before their first yield; payload bytes, internal structures and auxiliary scans
  remain outside this slice.
- **Record and byte thresholds now drive automatic WAL maintenance.** After a durable write commit
  and schema settlement, writable databases checkpoint when `last_committed_lsn - checkpoint_lsn`
  reaches `checkpoint_interval_records` or when the optional `wal_max_bytes` high-water is crossed.
  The byte setting is a soft post-commit trigger: reader pins, atomic batches and deferred recycling
  may retain more without unsafe truncation, and a latch prevents futile checkpoint storms until the
  WAL falls below the threshold. The decision remains single-flight; a refused or late-failing
  checkpoint stays pending for the next write, while maintenance and diagnostic failures can never
  turn the already-durable commit into an apparent transactional failure. Read transactions and
  empty write transactions do not run maintenance, and explicit `Database.checkpoint()` remains
  available.
- **The calibrated HNSW search beam is now an operational database option.**
  `vector_ef_search` defaults to `320`, is bounded to `1..1_048_576`, reaches every vector index
  assembled or reattached by that database handle, and is exposed by the detached vector index
  view. The setting changes derived search effort only; it neither changes the exact/approximate
  regime boundary nor writes a new on-disk format.
- **The inert `vector_recall_target` runtime option was removed.** Recall is an offline benchmark
  result owned by `bench.harness.gate --recall-target`, not a guarantee one database handle can
  enforce without an exact oracle. Passing the former keyword to `connect()` now produces a typed
  migration refusal; runtime HNSW effort remains configurable through `vector_ef_search`. No
  target-to-beam formula was invented.
- **`Database` no longer exposes mutable engine collaborators, and `Transaction.context` was
  removed.** Composition properties now return detached frozen schema, inventory and diagnostic
  snapshots with no `inner`, callback or raw-object backdoor. Vector reads use
  `Database.search_vectors(transaction, ...)`; planning, index inspection, quarantine reads and
  metrics publication have explicit database methods. Invalid `Database.retry()` calls now prove
  type, ownership, active state and retry eligibility before schema bookkeeping can move.

### Fixed

- **Statement rollback now restores exact page staging.** `staging_mark()` retains its
  `tuple[int, int, int]` signature while sealing exact internal snapshots of page images, their
  provenance proofs, write partitions and charged bytes. Marks are matched by exact object
  identity and released on successful handover. `discard_since()` restores a replaced image and
  additions whose key sorts before an older key, closing both count-based rollback gaps without
  persisting part of a refused statement.
- **The test tree now has a zero-diagnostic Ruff baseline enforced in CI.** The cleanup exposed
  and repaired a non-string map fixture where Python collapsed the distinct-looking keys `1` and
  `True`, removed a dead platform-family resolver that referenced a nonexistent helper, and made
  deferred `PageIndex`/`StepClock` annotations resolvable at runtime. Ruff is pinned in the
  development extra and runs once in a dedicated Linux job before inherited lint debt can hide a
  future defect.
- **Verification now refuses a heap identity counter that can reuse a persisted id.** The records
  walk reads page 0 and every heap record header straight from the storage device, so a resident
  cache image, an ended or provisional version, or an orphan page cannot hide the physical high
  water. `record_id_counter` identifies the exact directory slot when `next_record_id` is equal to
  or below the greatest decodable id; gaps and counters ahead of empty tables remain valid. Every
  physical heap page without an exact readable descriptor now produces a located
  `page_descriptor_missing`, while an exact owner absent from page 0 produces a located
  `orphan_page`; neither malformed ownership bytes nor short orphan headers can certify the file
  clean or invent a counter association. Duplicate directory extents are unreadable rather than
  arbitrarily selected, and extents/pages whose table id is absent from the catalog are located as
  unreadable/orphaned instead of disappearing from the catalog-driven walk.
- **A checksum provider could forge equality and be installed without returning an integer.** The
  installer and native adapter now require and copy an exact unsigned 32-bit result before every
  comparison and runtime use, contain ordinary provider failures, cover the largest legal page in
  the acceptance corpus, and leave the previous safe implementation installed on any refusal.
  Injected callables are oracle-checked at runtime by default; the closed native-provider list
  remains accelerated after corpus validation, with an explicit opt-in to runtime verification.
- **Vector search: a search arriving while the HNSW graph was being built could answer from a
  fragment** (Codex audit, P0.5). The derived graph was published before it was filled, so two
  searches meeting on the first use raced: one answered three of eight rows with `stale` False
  and `achieved_k` reported as if complete, and a build that failed part-way could erase another
  thread's complete build. The graph, its maps and the log position it reflects are now ONE
  snapshot (graph, maps, mark), built in locals and published atomically by one reference assignment under a guard
  the assembly hands in; a search captures it once; a build in flight is waited for rather than
  raced; a failed build drops its locals and nothing else.
- **Vector search: a commit could certify a warm graph that another process had left behind.**
  `commit()` noted its own changes into the warm graph and stamped it with the header's
  `built_through_lsn` -- already past another process's commit -- so this process then answered
  without that process's rows, `stale` False, `verify()` clean (the warm half of LESSONS L22).
  A commit now certifies the picture only when it verified, before the store moved, that the
  picture was current; otherwise it retires the picture and the next search rebuilds.
- Regressions: `tests/vector/test_first_use_concurrency.py` (three deterministic interleavings
  through the public door, the `VectorMath` port as the lever) and
  `tests/vector/test_warm_graph_across_processes.py` (two interpreters).
- **Recovery and commit completion now share one fail-closed WAL protocol.** Startup recovery,
  checkpoint completion and the post-COMMIT path select only transactions with one unambiguous
  terminal COMMIT, validate the complete page/index replay plan before its first mutation, apply
  effects idempotently, and publish `control/commit.state` only after completion. A participant
  that fails after durable COMMIT latches `recovery_required` and cannot begin, flush, checkpoint
  or certify an index until the authoritative WAL gap has been completed.
- **Heap, index, WAL and commit reports now carry one exact commit number.** Live heap frames keep
  the reserved, permanently invisible `PROVISIONAL_CSN` until the WAL barrier succeeds. The WAL
  plans the real terminal LSN including a segment-header roll, heap page images are committed only
  in private copies, logical index staging is retargeted atomically, and append revalidates the
  plan before writing its first byte. Refused attempts therefore cannot leak a usable ghost row,
  even when eviction and cleanup both fail.
- **WAL uncertainty is sticky and recovery barriers are explicit.** A failed append either restores
  its exact physical and in-memory preimage or closes the append door with `append_uncertain`.
  Recovery and gap completion force every segment intersecting the authoritative LSN range before
  page/index apply or publication, independently of the ordinary pending-flush cache. A tail that
  grows after a partial damaged observation is rescanned from byte zero instead of interpreting
  the completing checksum suffix as a new record.
- **Startup can no longer truncate a live writer's in-flight WAL append.** The entire recovery
  observation and repair pass takes the same cross-process commit section as append plus barrier;
  read-only startup takes that section only to prove, byte-identically, that the checkpoint covers
  every complete commit. Torn tails, missing segments, damaged first effects, restored old
  `commit.state`, catalog/index lag and crash-at-every-recovery-write have dedicated regressions.
- **Lifecycle cleanup is exhaustive for every exception class.** `Database.close()` and assembly
  unwind attempt every release even when a host adapter raises `RuntimeError`, `KeyboardInterrupt`
  or `SystemExit`, then preserve the first failure; cleanup can no longer strand later locks or
  handles merely because the first closer was foreign.

## [0.0.1] — 2026-08-23

First published release. **Pre-alpha**: the on-disk format, the public API and the query surface may
all change, and there is no migration path yet.

### Added

- **Embedded graph database** with a public API (`connect`, `Database`, `Transaction`,
  `QueryResult`) and an `oktografx` command line whose exit codes are a contract.
- **Multi-process, multi-thread reads and writes** (D1). A commit is refused only when its
  partitions genuinely intersect another's, never because another writer exists.
- **Snapshot isolation** with optimistic concurrency control. Readers never block writers; writers
  never block readers.
- **Process coordination on the filesystem**: leases with epochs, monotonic liveness, exclusive
  sections, reader registration, and takeover of a dead writer with stale-epoch rejection.
- **Write-ahead log**: segmented, self-describing records with CRC-32C, an LSN mark index, barriers
  a commit waits on, and recycling bounded by the checkpoint and the oldest live reader.
- **Recovery at open**: idempotent replay, truncation of a torn tail at the last intact record, and a
  forensic ledger and quarantine that preserve the evidence rather than discarding it.
- **openCypher (Kùzu dialect)**: DDL for node, relationship and vector-space definitions; `CREATE`,
  `MATCH`, `WHERE`, `RETURN`, `MERGE`, `SET`, `DELETE`, traversal with bounded hop ranges,
  `ORDER BY`, `SKIP`, `LIMIT`, `DISTINCT`, `WITH`, aggregates, and parameters.
- **A declared `PRIMARY KEY` is indexed automatically**, created by the DDL and re-adopted at every
  later open. A keyed read plans an index seek; the uniqueness check reads the index rather than
  scanning the table.
- **Relationship tables are indexed per endpoint** (`ef_`/`et_`): traversal expands a bounded
  frontier by index lookup and switches to one grouped edge scan past a fan limit, with identical
  answers in both regimes. The old cost — every edge of the table read per frontier node — is gone.
- **Schema changes are transactions.** A `CREATE ... TABLE`/`VECTOR SPACE` builds on a
  per-transaction working catalog and stages its page images — the file header included, closing a
  recorded durability gap — so a rollback leaves nothing anywhere and a crash cannot lose a
  committed schema. A table declared inside a transaction is visible to other transactions once the
  transaction commits, not before.
- **Dual index visibility** (CONTRACT §8.7): EXACT indexes return candidates validated against the
  heap under the caller's snapshot; PROXIMITY indexes are versioned with tombstones and a horizon.
- **A stale index is never used**, by the planner or by the uniqueness check — it is a subset of the
  heap, which validation cannot repair, so both fall back to a scan.
- **Embeddings as a first-class column type**: vector spaces with a dimension, metric and storage
  dtype; an index created with the table that declares the column; and a search that reports the
  regime it answered in and the `k` it achieved.
- **Verification**: `verify(scope)` walks pages, records and indexes and reports located findings,
  with counts, so a caller can tell "nothing was wrong" from "nothing was checked".
- **Observability**: a frozen metric catalogue with no-op, OpenMetrics and JSON sinks, and a
  sanitised, bounded event sink.
- **A host-supplied metrics sink cannot break the engine**: every recording call is contained, so
  a sink that raises — even after a commit's barrier — leaves the commit truthful and the rows
  single, and `publish` stays typed. `metrics="json"` now writes its file at `close()`.
- **`Database.unindexed_tables`** reports, at open, the tables whose automatic indexes declined —
  the open-time twin of statement-time `skipped_indexes`.
- **Seven ports with default adapters** (`storage`, `clock`, `coordinator`, `codec`, `metrics`,
  `events`, `vector_math`), a fail-closed registry that names every missing slot in one error, and an
  enforced import boundary that keeps `domain/` and `engine/` mechanism-free.
- **Optional accelerators** behind `[accel]`: a native CRC-32C proved byte-identical to the reference
  before it is installed, and numpy vector math selected only by explicit configuration.
- **Windows and POSIX as equal citizens** (D9), with a suite that requires a platform-specific test
  to declare its counterpart and every skip to be attributed.
- **Performance documentation with in-tree instruments**: [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)
  records measured numbers with their machine, build and load conditions —
  `tools/measure_concurrency.py` (multi-process read/write latency under load, correctness-gated)
  and `tools/measure_traversal.py` (traversal shapes with index-vs-scan equality) reproduce them.
- **Documentation**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
  [`docs/PORTS.md`](docs/PORTS.md), the frozen `docs/architecture/CONTRACT.md`, the component
  register, the lessons, and the punch list of known gaps.

### Known limitations

- The on-disk format is not stable and there is no migration path.
- Performance on Windows does not meet the D5 ceilings; POSIX with `[accel]` does. Windows
  control-file publication costs ~16.5 ms against ~0.13 ms on Linux. The measurements and the
  analysis are in `docs/architecture/COMPONENTS.md`.
- Traversal with an unbound target resolves landings by one scan of the landing table per
  traversal; edges are indexed, landings are not yet.
- `DETACH DELETE` and relationship deletion are not implemented, and refuse rather than pretend.
- A `MATCH` cannot bind a row the same transaction created; a row's identity is allocated by the
  commit.
- There is no read-your-own-writes within a transaction.
- Two tables whose names differ only by case can share one index file name; the second goes without
  an index rather than failing, and is reported.
- Known gaps are recorded in `docs/architecture/PUNCHLIST.md` rather than left implied.

[0.0.1]: https://github.com/OktoLabsAI/okto-grafx/releases/tag/v0.0.1
