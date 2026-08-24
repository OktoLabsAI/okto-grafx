# Changelog

All notable changes to Okto Grafx are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/) — with the caveat that below 0.1.0 anything may change,
including the on-disk format.

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
