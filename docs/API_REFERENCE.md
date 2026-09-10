# Public Python API

[Documentation index](README.md) · [Integration](INTEGRATION.md) · [Query types](QUERY_LANGUAGE.md)

## Entry points and supported imports

`db.add_nullable_column(table, column)` uses
`okto_grafx.domain.model.schema.ColumnDef` and native
`okto_grafx.domain.model.value.ValueType`; see [nullable schema evolution](NULLABLE_COLUMNS.md).

`db.views` exposes durable logical read views. Parameter/definition types are
`okto_grafx.views.ViewParameter` and `ViewDefinition`; registry preparation is
explicit. See [logical view usage, bounds and errors](LOGICAL_VIEWS.md).

`okto_grafx.catalog_copy` provides `capture_copy`, `prepare_copy_target`,
`copy_graph` and frozen `CopyLimits`, `CopyTable`, `CopyPackage`, `CopyReceipt`.
[Existing-target copy](CATALOG_COPY.md) is one atomic native transaction with an
indexed durable receipt; its v1 selection/schema/PK restrictions are explicit.

`from okto_grafx import CatalogSession, CatalogPathPolicy, CatalogInfo` provides
[named single-store transactions](CATALOGS_AND_WORKSPACES.md). Optional resolution
uses `okto_grafx.workspace.WorkspacePolicy`, `ResolvedWorkspace` and
`resolve_workspace`. Construction requires explicit `owned` and `policy`;
permissions default to read-only. No distributed write or automatic receipt-ledger
preparation is implied; `apply_copy` requires the explicit copy setup above.

0.0.6 adds `okto_grafx.sqlite_import` (`SQLiteImportLimits`, `read_sqlite_rows`,
`import_sqlite`) for [bounded local SQLite consumption](LOCAL_SQLITE_IMPORT.md),
and `okto_grafx.html_snapshot` (`HtmlSnapshotLimits`, `render_html_snapshot`) for
[offline HTML pictures](HTML_SNAPSHOTS.md). `GraphProjection.topological_order`
returns the frozen `TopologicalOrderResult`; see [algorithms](GRAPH_PROJECTIONS.md).
All new options are operation-local; no durable-format migration is introduced.

`from okto_grafx.projections import project_graph, ProjectionLimits` exposes
[read-only snapshot graph pictures, adjacency, degree/WCC/SCC, paths, PageRank and k-core](GRAPH_PROJECTIONS.md).
`from okto_grafx.arrow import import_arrow_batches, to_arrow_batches` exposes
[optional typed scalar/vector batch interop](EXTENSIONS_AND_ARROW.md). `ArrowVectorType`
is imported from the same module. Import stages one
atomic call inside a caller-owned write transaction; neither function auto-commits.
Full-text prefix mode and relationship fields use the existing native FTS facade;
new connection cache/aggregate ANN limits are listed in [configuration](CONFIGURATION.md).

`from okto_grafx.migrations import SchemaMigration, MigrationReport, migrate_schema`
exposes the [additive application migration contract](SCHEMA_MIGRATIONS.md): exact
checksums, dry-run, atomic per-version DDL and bounded retries. It does not change
the engine file-format migration API. `Database.index_distribution` returns a
bounded physical `IndexDistribution` observation; its type is listed below and
its sizing/skew semantics are in [indexes](INDEXES_AND_VECTORS.md).

Opt-in durable commit provenance on the 0.0.5 development branch is described in
[commit history](COMMIT_HISTORY.md), including activation, metadata, qualified
lookup, snapshot pagination, verification, cost and transfer/restore limitations.

`from okto_grafx.backup import create_backup, restore_backup, BackupReport` exposes
the bounded physical backup/offline replacement workflow. See [backup and restore](BACKUP_RESTORE.md)
for signatures, all parameters, result fields, concurrency and failure guarantees.

`from okto_grafx.transfer import export_graph, import_graph, TransferLimits,
TransferReport, RecordIdMapping` exposes [versioned logical transfer](LOGICAL_TRANSFER.md).
`TextIndexOptions`, `TextSearchLimits`, `TextHit` and `TextSearchResult` are root
exports for `Database.create_text_index` / `search_text`; see [FTS usage](FULL_TEXT_SEARCH.md).
`HybridSearchOptions`, `HybridHit` and `HybridSearchResult` are root exports for
`Database.search_hybrid`; see [hybrid usage](HYBRID_SEARCH.md). Logical import's
optional `resume_directory` retains an operator-owned resumable workspace; no
partial target is published. These are additive 0.0.5 development APIs.

```python
from okto_grafx import (
    connect, DatabaseConfig, Database, Transaction, Query, QueryCursor, QueryResult,
    DatabaseIdentity, ExecuteManyReport, ScanCursorV1, ScanPageV1, ScanRowV1,
    Timestamp, VectorValue, PortRegistry, __version__,
)
from okto_grafx.errors import GrafxError
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.domain.model import Uuid
```

```python
connect(path: str | os.PathLike[str], *, registry: PortRegistry | None = None,
        **options: Unpack[ConnectOptions]) -> Database
```

All options are enumerated with defaults, types and effects in
[configuration](CONFIGURATION.md). `connect` creates a missing writable database;
read-only open does not create one. It accepts a local directory or `:memory:`.
Use `with connect(...) as db:` or close in `finally`. Caller-supplied registry
resources remain caller-owned; see [ports](PORTS.md) for advanced composition via
`okto_grafx.api.build_default_registry`, `open_database` and `assemble_database`.

Do not instantiate `Database`/`Transaction` with private engine collaborators to
integrate an application. Frozen views are observations, not mutable engine doors.

## Lifecycle and return contracts

| Door / value | Contract |
| --- | --- |
| `Database.begin` / `transaction` | Explicit read/write scope; default mode is **write**. Clean context exit commits, exceptional exit rolls back. Pass mode explicitly in application code. |
| `Database.execute` | One materialized autocommit **read**. DML/DDL refuse. |
| `Transaction.execute` | One statement in the owned snapshot/overlay; statement failure restores statement-local staging. |
| `Transaction.executemany` | One no-RETURN updating query, lazy parameter iterable, one batch savepoint. Failure discards this call, not prior staging. Does not itself commit. |
| `QueryResult` | `columns`, detached tuple `rows`, copied `statistics`, optional immutable `plan`. Not a DB-API cursor. Counts/statistics are not an execution-time SLO. |
| `Query` | Snapshots text/parameters; reusable factory for independent read cursors. Not a serialized or indefinitely cached prepared plan. |
| `QueryCursor` | Iterator/context manager; batch size 1–65,536, default 256. `fetchone()` returns row or `None`; `fetchmany(size=None)` returns at most that many rows, `()` at EOF. `close()` is idempotent. |
| `ScanCursorV1` | Opaque, non-serializable, single-use continuation belonging to one read transaction/table/database. Do not construct/copy it or put it in an HTTP token. |
| `ScanRowV1` / `ScanPageV1` | Record ID and values in declared column order; physical relation endpoint columns lead. Parallel relationship occurrences remain separate. |
| `CommitReport` | `csn`, `durable`, `wrote`; may be observable even when commit raises after durability. Follow [outcome handling](OPERATIONS.md#commit-outcomes-and-retries), not automatic retry. |
| `VectorSearchResult` | `hits`, `regime`, `achieved_k`, `requested_k`, `space`, `filter_cardinality`. `k` is requested, not an unconditional result-count promise. |
| `VectorHit` | Record ID, score, physical ref and retired flag. ID is not automatically a user PK; ref is not a portable application identifier. |
| `Database.search_text` / `TextSearchResult` | Native BM25 under a caller-owned reader or autocommit snapshot; complete top-k or typed refusal, versioned analyzer identity, pre-top-k RecordId filter and bounded work. |
| `TransferReport` | Verified logical artifact/import, source snapshot identity, fresh target UUID and current-record mapping. Not physical restore or historical commit replay. |
| `VerificationReport` | Scope, findings and examination counts; no findings is not sufficient if nothing was examined. |
| `RecoveryReport` | Outcome, replay/discard counts, ledger additions, last good LSN and findings; inspect at writable open or explicit recovery. |
| `Database.closed` / `close_complete` | Admission has closed / lower-layer resource release has completed. They differ during overlapping shutdown; do not replace files merely because admission is closed. |

Cursors/transactions are owned, not concurrently consumable or portable between
databases/processes. Detached scalar rows can outlive a handle. `statistics` and
status/view objects are observations, not live mutable dictionaries of engine state
or universal global-health certificates. Do not JSON-serialize arbitrary DTOs with
`repr`; map explicit fields/types at your application boundary.

The appendix lists **every public method/property** of the five consumer facades,
plus result/observation fields and accessors. Internal underscore names are not API.
Detailed index restrictions are in [indexes](INDEXES_AND_VECTORS.md), budgets in
[configuration](CONFIGURATION.md), and mutating maintenance preconditions in
[operations](OPERATIONS.md). A missing component in custom composition raises a
typed unsupported/configuration refusal rather than silently emulating a feature.

## Schema, index and diagnostic views

`catalog` is an immutable schema snapshot; `indexes`/`vectors` expose inventories,
not mutation hooks. `attached_indexes`, `unindexed_tables` and `stale_indexes`
describe open-time adoption, not a forever-current certificate. `read_index_status`
performs an explicit validated header read. `inspect_index` materializes entries.
`recovery_report` is the pass observed at open, not a live health monitor.

`storage`, `clock`, `codec`, `metrics`, `events`, `vector_math`, `coordinator`,
`pool`, `heap`, `wal`, `transactions`, `ledger`, `quarantine` and `queries` expose
bounded observations or inventories with no mutable storage/lease/WAL capability.
Some full inventories and explicit memory estimates still cost proportional work;
avoid repeatedly collecting them in every query request.

## Read execution control and orphan cleanup

`Database.execute`, read `Transaction.execute` and `Query.cursor` accept optional
`timeout_seconds` and a public `CancellationToken`. Defaults preserve uncontrolled
execution; write transactions refuse the options. `Maintenance.cleanup_indexes`
provides an independently revalidated dry-run/removal census under explicit
whole-store quiescence. See [contracts, examples, errors and limits](READ_CONTROL_AND_INDEX_CLEANUP.md).

## Trusted scalar extensions and Arrow interop

Use `from okto_grafx.extensions import ExtensionRegistry, ScalarFunction` and
`connect(..., extensions=registry)` for the explicit per-handle allowlist.
`registry.call_scalar(name, arguments_tuple)` supports direct invocation; the query
door is `udf('namespace.name', ...)`. Registration, value budgets, NULL/type/error
semantics and trust limits are in [Extensions and Arrow](EXTENSIONS_AND_ARROW.md).
`from okto_grafx.arrow import to_arrow_batches` provides optional copied scalar/vector
batches over a materialized result or caller-owned cursor. The same guide defines
all type mappings, budget tariffs, snapshot and close obligations.

<!-- GENERATED PUBLIC REFERENCE: do not edit below -->

## Complete facade signatures

Generated from the public facade declarations; no engine instance is opened.
Types in signatures are described below; `DEFAULT_QUERY_CURSOR_BATCH_ROWS` is 256.
Factories, not direct engine constructors, own resource composition.

### Query

A canonical read statement that can open independent snapshot-owning cursors.

#### Query.cursor

```python
cursor(*, batch_size: int=DEFAULT_QUERY_CURSOR_BATCH_ROWS, timeout_seconds: float | None=None, cancellation: CancellationToken | None=None) -> QueryCursor
```

Open a cursor whose read transaction lives until exhaustion or explicit close.

### QueryCursor

A bounded pull cursor over one fixed MVCC read snapshot.

#### QueryCursor.closed

```python
closed: bool  # read-only property
```

Return whether this cursor has released its snapshot and buffered rows.

#### QueryCursor.statistics

```python
statistics: Mapping[str, int]  # read-only property
```

Return an immutable-shape snapshot of counters observed so far.

#### QueryCursor.fetchone

```python
fetchone() -> tuple[Value, ...] | None
```

Return the next detached row, or `None` after exhaustion.

#### QueryCursor.fetchmany

```python
fetchmany(size: int | None=None) -> tuple[tuple[Value, ...], ...]
```

Return at most `size` detached rows without materialising the remaining result.

#### QueryCursor.close

```python
close() -> None
```

Discard unread rows and release the owned read snapshot idempotently.

### Transaction

One open transaction, as CONTRACT.md section 10 hands it to a caller.

#### Transaction.mode

```python
mode: str  # read-only property
```

Return `"read"` or `"write"`, the mode this transaction was opened in.

#### Transaction.snapshot

```python
snapshot: Snapshot  # read-only property
```

Return the fixed view every read of this transaction sees (SPEC-M1 FR-2).

#### Transaction.txn_id

```python
txn_id: int  # read-only property
```

Return the process-local number of this transaction.

#### Transaction.active

```python
active: bool  # read-only property
```

Return True while this transaction can still commit or roll back.

#### Transaction.report

```python
report: CommitReport | None  # read-only property
```

Return what the commit reported, or None while the transaction is still open.

#### Transaction.execute

```python
execute(text: str, parameters: Mapping[str, object] | None=None, *, timeout_seconds: float | None=None, cancellation: CancellationToken | None=None) -> QueryResult
```

Run one statement inside this transaction and return its result.

#### Transaction.system_as_of

```python
system_as_of(at: CommitId | Timestamp, *, tables: tuple[str, ...], limits: TemporalLimits=TemporalLimits()) -> TemporalGraph
```

Read durable system-time rows under this transaction's snapshot, excluding private writes.

#### Transaction.system_versions

```python
system_versions(table: str, record_id: int, *, limits: TemporalLimits=TemporalLimits()) -> TemporalVersions
```

Read one logical row's intervals visible to this snapshot, not later commits.

#### Transaction.commit_history

```python
commit_history(*, after: CommitId | None=None, limit: int=100) -> CommitHistoryPage
```

Read an ascending bounded history page under this transaction's snapshot.

#### Transaction.lookup_commit

```python
lookup_commit(identity: CommitId) -> CommitCatalogEntry | None
```

Look up a qualified commit visible here; None does not certify legacy absence.

#### Transaction.executemany

```python
executemany(text: str, parameter_sets: Iterable[Mapping[str, object]]) -> ExecuteManyReport
```

Stage one updating statement for every parameter mapping, atomically as a batch.

#### Transaction.scan_rows_v1

```python
scan_rows_v1(table: str, *, limit: int, cursor: ScanCursorV1 | None=None, columns: tuple[str, ...] | None=None, max_batch_bytes: int | None=None, timeout_seconds: float | None=None, cancellation: CancellationToken | None=None) -> ScanPageV1
```

Read one bounded page of physical rows under this transaction's fixed snapshot.

#### Transaction.commit

```python
commit() -> CommitReport
```

Commit this transaction and return the report of CONTRACT.md section 8.5.

#### Transaction.rollback

```python
rollback() -> None
```

Abandon this transaction. Rolling back twice is a no-op, never an error.

### Maintenance

Thin operational facade over the database's existing public maintenance doors.

#### Maintenance.status

```python
status() -> MaintenanceStatus
```

Return a last-observed status snapshot without claiming global linearizability.

#### Maintenance.bloat

```python
bloat(table: str | None=None) -> BloatReport
```

Return a conservative read-only heap-bloat census.

#### Maintenance.cleanup_indexes

```python
cleanup_indexes(*, dry_run: bool=True, confirm_quiescent: bool=False, max_files: int=10000, max_wal_records: int=100000) -> IndexCleanupReport
```

Inventory or reclaim unreferenced native generations with every other handle stopped.

#### Maintenance.vacuum

```python
vacuum(table: str | None=None, *, confirm_quiescent: bool=False, max_versions: int | None=None, index_free_pages: bool=False) -> VacuumReport
```

Run explicit foreground MVCC reclamation under the v1 quiescence contract.

#### Maintenance.checkpoint

```python
checkpoint() -> RecycleReport
```

Delegate checkpointing to :meth:`Database.checkpoint`.

#### Maintenance.verify

```python
verify(scope: str='all') -> VerificationReport
```

Delegate verification to :meth:`Database.verify`.

#### Maintenance.recover

```python
recover() -> RecoveryReport
```

Delegate recovery to :meth:`Database.recover`.

#### Maintenance.rebuild_vector_index

```python
rebuild_vector_index(space: str) -> VectorIndexView
```

Delegate the repair to :meth:`Database.rebuild_vector_index`.

#### Maintenance.ensure_identity_indexes

```python
ensure_identity_indexes() -> None
```

Delegate explicit persistent identity-index activation to the database.

#### Maintenance.enable_wal_page_compression

```python
enable_wal_page_compression() -> None
```

Delegate explicit one-way WAL page-image compression activation.

#### Maintenance.create_index

```python
create_index(name: str, table: str, columns: Sequence[str], *, bucket_count: int | None=None, expected_cardinality: int | None=None, layout: str='hash') -> IndexView
```

Delegate custom exact-index creation to the database.

#### Maintenance.rehash_index

```python
rehash_index(name: str, *, bucket_count: int | None=None, expected_cardinality: int | None=None) -> IndexView
```

Delegate growth-only exact-index rehash to the database.

#### Maintenance.rebuild_index

```python
rebuild_index(name: str) -> IndexView
```

Delegate immutable exact-index reconstruction to the database.

#### Maintenance.rehash_index_if_needed

```python
rehash_index_if_needed(name: str, *, overflow_pages_per_bucket: int=1, check_skew: bool=False) -> IndexView | None
```

Grow one physically pressured exact index by at most one directory step.

#### Maintenance.publish_metrics

```python
publish_metrics() -> None
```

Delegate explicit metric publication to :meth:`Database.publish_metrics`.

### Database

One open database: the object every public entry point of Okto Grafx hands back.

#### Database.path

```python
path: str  # read-only property
```

Return the path this database was opened at, or `":memory:"`.

#### Database.label

```python
label: str  # read-only property
```

Return the bounded label this database reports under, safe as a metric value (TR-7).

#### Database.identity

```python
identity: DatabaseIdentity  # read-only property
```

Return the identity record of section 6.2 that this database carries (FR-1).

#### Database.read_only

```python
read_only: bool  # read-only property
```

Return True when this database refuses to open a write transaction.

#### Database.descriptor_revalidation

```python
descriptor_revalidation: str  # read-only property
```

Return this handle's process-local descriptor revalidation policy.

#### Database.closed

```python
closed: bool  # read-only property
```

Return True once :meth:`close` has run; a closed database refuses every door.

#### Database.close_complete

```python
close_complete: bool  # read-only property
```

Return True once the elected caller finished lower-layer release.

#### Database.metrics_endpoint

```python
metrics_endpoint: str | None  # read-only property
```

Return the URL the metrics of this database are exposed at, or None (FR-14, OR-6).

#### Database.attached_indexes

```python
attached_indexes: tuple[str, ...]  # read-only property
```

Return the name of every index the open sequence attached, in catalog order.

#### Database.unindexed_tables

```python
unindexed_tables: tuple[str, ...]  # read-only property
```

Return the tables whose automatic indexes could not be adopted at open.

#### Database.stale_indexes

```python
stale_indexes: tuple[str, ...]  # read-only property
```

Return the name of every index that opened behind the position this database published.

#### Database.recovery_report

```python
recovery_report: RecoveryReport | None  # read-only property
```

Return the report of the recovery that ran at open, or None when none did (FR-8).

#### Database.maintenance

```python
maintenance: Maintenance  # read-only property
```

Return a thin facade over the database's existing operator operations.

#### Database.storage

```python
storage: StorageView  # read-only property
```

Return immutable storage identity and file-size metadata.

#### Database.clock

```python
clock: ClockView  # read-only property
```

Return clock implementation identity without advancing either clock source.

#### Database.codec

```python
codec: CodecView  # read-only property
```

Return immutable format metadata without exposing page encode/decode doors.

#### Database.metrics

```python
metrics: MetricsView  # read-only property
```

Return whether metrics collection is enabled, without exposing the mutable sink.

#### Database.events

```python
events: ComponentView  # read-only property
```

Return identity-only metadata for the configured event destination.

#### Database.vector_math

```python
vector_math: VectorMathView  # read-only property
```

Return the selected vector arithmetic implementation's immutable name.

#### Database.coordinator

```python
coordinator: CoordinatorView  # read-only property
```

Return participant identity without touching lease, horizon or fencing state.

#### Database.pool

```python
pool: BufferPoolView  # read-only property
```

Return immutable page-cache capacity and residency counters (FR-13).

#### Database.catalog

```python
catalog: CatalogStoreView  # read-only property
```

Return a complete, linearized schema and catalog-layout snapshot.

#### Database.views

```python
views: LogicalViews  # read-only property
```

Return the bounded, persistent read-only logical view API; prepare explicitly.

#### Database.heap

```python
heap: HeapStoreView  # read-only property
```

Return immutable heap layout metadata without row or page mutation doors.

#### Database.wal

```python
wal: WalView  # read-only property
```

Return the WAL state last observed by this handle and its segment inventory (FR-5).

#### Database.transactions

```python
transactions: TransactionManagerView  # read-only property
```

Return immutable transaction configuration, counts and publication state.

#### Database.indexes

```python
indexes: IndexRegistryView  # read-only property
```

Return an immutable secondary-index inventory (FR-12).

#### Database.ledger

```python
ledger: LedgerView  # read-only property
```

Return an immutable ledger inventory (SPEC-M1 FR-9, section 10).

#### Database.quarantine

```python
quarantine: QuarantineView  # read-only property
```

Return an immutable quarantine evidence inventory (FR-10).

#### Database.vectors

```python
vectors: VectorEngineView  # read-only property
```

Return immutable vector-space and index configuration (SPEC-VEC).

#### Database.queries

```python
queries: QueryEngineView  # read-only property
```

Return immutable diagnostics for the composed query engine.

#### Database.begin

```python
begin(mode: str='write', *, metadata: CommitMetadata | None=None) -> Transaction
```

Open a transaction in `"read"` or `"write"` mode (SPEC-M1 FR-2).

#### Database.retry

```python
retry(transaction: Transaction) -> Transaction
```

Open the successor of a transaction optimistic validation refused (BR-6).

#### Database.transaction

```python
transaction(mode: str='write', *, metadata: CommitMetadata | None=None) -> Iterator[Transaction]
```

Open a transaction as a block, committing on a clean exit and rolling back otherwise.

#### Database.execute

```python
execute(text: str, parameters: Mapping[str, object] | None=None, *, timeout_seconds: float | None=None, cancellation: CancellationToken | None=None) -> QueryResult
```

Run one statement in its own read transaction and return its result.

#### Database.query

```python
query(text: str, parameters: Mapping[str, object] | None=None) -> Query
```

Return a reusable canonical read query whose cursors own their snapshots.

#### Database.explain

```python
explain(text: str) -> PlanNode
```

Plan one statement without exposing the mutable query engine.

#### Database.vector_total_memory_usage

```python
vector_total_memory_usage() -> VectorTotalMemoryUsage
```

Return per-handle aggregate ANN reservations; disabled accounting reports zero.

#### Database.vector_memory_usage

```python
vector_memory_usage(space: str) -> VectorMemoryUsage
```

Observe local HNSW cache tariffs without building or proving freshness.

#### Database.search_vectors

```python
search_vectors(transaction: Transaction, *, space: str, query: Sequence[float] | VectorValue, k: int, candidate_filter: RecordIdFilter | None=None, timeout_seconds: float | None=None, cancellation: CancellationToken | None=None) -> VectorSearchResult
```

Search one owned snapshot with optional cooperative read controls.

#### Database.search_hybrid

```python
search_hybrid(reader: Transaction | None=None, *, table: str, index: str | None, query: str, space: str | None, vector: Sequence[float], k: int=20, options: HybridSearchOptions | None=None, filter: RecordIdFilter | None=None, text_limits: TextSearchLimits | None=None, timeout_seconds: float | None=None, cancellation: CancellationToken | None=None) -> HybridSearchResult
```

Fuse text/vector candidate windows with RRF-v1 and optional bounded graph evidence.

#### Database.create_text_index

```python
create_text_index(name: str, table: str, columns: tuple[str, ...], *, options: TextIndexOptions | None=None, bucket_count: int=64) -> IndexView
```

Create a native persisted full-text generation over one to four STRING fields.

#### Database.search_text

```python
search_text(reader: Transaction | None=None, *, index: str, query: str, k: int=20, filter: RecordIdFilter | None=None, limits: TextSearchLimits | None=None, k1: float=1.2, b: float=0.75, timeout_seconds: float | None=None, cancellation: CancellationToken | None=None, prefix: bool=False, phrase: bool=False) -> TextSearchResult
```

Read bounded BM25 hits in a caller-owned reader or a fresh autocommit snapshot.

#### Database.create_index

```python
create_index(name: str, table: str, columns: Sequence[str], *, bucket_count: int | None=None, expected_cardinality: int | None=None, layout: str='hash') -> IndexView
```

Create and atomically publish one custom exact index.

#### Database.rehash_index

```python
rehash_index(name: str, *, bucket_count: int | None=None, expected_cardinality: int | None=None) -> IndexView
```

Grow one exact index through an immutable foreground shadow generation.

#### Database.rebuild_index

```python
rebuild_index(name: str) -> IndexView
```

Rebuild one exact index into a compact, immutable fresh generation.

#### Database.rehash_index_if_needed

```python
rehash_index_if_needed(name: str, *, overflow_pages_per_bucket: int=1, check_skew: bool=False) -> IndexView | None
```

Grow one exact index after a bounded directory-pressure assessment.

#### Database.index_cache_usage

```python
index_cache_usage(name: str) -> KeyPageCacheUsage
```

Return active-index local memo observations; not a cache/freshness certificate.

#### Database.index_distribution

```python
index_distribution(name: str, *, max_pages: int=65536, max_entries: int=1000000, max_memory_bytes: int=64 * 1024 * 1024) -> IndexDistribution
```

Bounded physical HASH distribution, without exposing keys or mutating data.

#### Database.verify

```python
verify(scope: str='all') -> VerificationReport
```

Walk the database and report every finding, precisely located (SPEC-M1 FR-11).

#### Database.add_nullable_column

```python
add_nullable_column(table: str, column: ColumnDef) -> TableDef
```

Atomically append one nullable non-vector column, without rewriting old rows.

#### Database.ensure_identity_indexes

```python
ensure_identity_indexes() -> None
```

Persist and activate every exact access path required by endpoint identities.

#### Database.system_as_of

```python
system_as_of(at: CommitId | Timestamp, *, tables: tuple[str, ...], limits: TemporalLimits=TemporalLimits()) -> TemporalGraph
```

Return a bounded historical graph at a qualified commit or ordered timestamp.

#### Database.system_versions

```python
system_versions(table: str, record_id: int, *, limits: TemporalLimits=TemporalLimits()) -> TemporalVersions
```

Return create/update/delete-bounded intervals for one logical row identity.

#### Database.enable_system_history

```python
enable_system_history(tables: tuple[str, ...]) -> None
```

Atomically opt tables into durable system-time history with their current baseline.

#### Database.pin_system_history

```python
pin_system_history(name: str, at: CommitId, *, tables: tuple[str, ...]) -> None
```

Persist named protection against retention beyond `at` for selected tables.

#### Database.unpin_system_history

```python
unpin_system_history(name: str) -> None
```

Explicitly release a durable temporal pin; an absent valid name is a no-op.

#### Database.system_history_pins

```python
system_history_pins() -> tuple[TemporalPin, ...]
```

List durable temporal pins in name order under a qualified publication read.

#### Database.prune_system_history

```python
prune_system_history(before: CommitId, *, tables: tuple[str, ...], max_bytes: int=16 * 1024 * 1024) -> TemporalPruneReport
```

Atomically redact payloads of versions closed at/before a new retained horizon.

#### Database.enable_commit_history

```python
enable_commit_history() -> None
```

Activate one-way durable provenance after ensure_identity_indexes().

#### Database.commit_history

```python
commit_history(*, after: CommitId | None=None, limit: int=100) -> CommitHistoryPage
```

Read a bounded history page in a new snapshot; use a read transaction for paging.

#### Database.lookup_commit

```python
lookup_commit(identity: CommitId) -> CommitCatalogEntry | None
```

Return a durable entry visible in a new snapshot, or None in the tracked interval.

#### Database.enable_wal_page_compression

```python
enable_wal_page_compression() -> None
```

Persist the compatibility fence before emitting compressed WAL page images.

#### Database.rebuild_vector_index

```python
rebuild_vector_index(space: str) -> VectorIndexView
```

Re-derive one vector index from the heap, and report it only once it is healthy.

#### Database.recover

```python
recover() -> RecoveryReport
```

Run a recovery pass and return its report (SPEC-M1 FR-8).

#### Database.flush

```python
flush() -> int
```

Write every dirty page of this database back to the device and return the count.

#### Database.checkpoint

```python
checkpoint() -> RecycleReport
```

Put the committed state on the platter, publish the checkpoint, and reclaim the log (BR-10).

#### Database.snapshot_metrics

```python
snapshot_metrics() -> MetricsSnapshotView
```

Return the machine-readable current value of every metric this database emitted.

#### Database.publish_metrics

```python
publish_metrics() -> None
```

Publish a configured metrics document without exposing its mutable sink.

#### Database.read_index_status

```python
read_index_status(name: str) -> IndexView
```

Read an active index's validated durable header into an immutable DTO.

#### Database.inspect_index

```python
inspect_index(name: str) -> tuple[IndexEntry, ...]
```

Return immutable entry DTOs from one secondary index.

#### Database.read_quarantine

```python
read_quarantine(name: str) -> bytes
```

Return and checksum-verify the immutable bytes of one quarantine entry.

#### Database.quarantine_receipts

```python
quarantine_receipts(name: str) -> tuple[str, ...]
```

Return immutable restore-receipt names without exposing the quarantine store.

#### Database.close

```python
close() -> None
```

Release owned resources under this database's checksum selection (FR-1).

### CatalogSession

Bounded aliases and pinned native transactions. Python permissions are not an OS sandbox.

#### CatalogSession.open

```python
open(path: str | os.PathLike[str], *, policy: CatalogPathPolicy, read_only: bool=True) -> CatalogSession
```

Open an existing main store; no creation or workspace lookup. Writable opens may recover.

#### CatalogSession.attach_handle

```python
attach_handle(database: Database, *, alias: str, owned: bool, read_only: bool=True) -> None
```

Attach caller-supplied handle; ownership transfers only after successful publication.

#### CatalogSession.attach

```python
attach(path: str | os.PathLike[str], *, alias: str, read_only: bool=True) -> None
```

Open an existing local catalog with read-only default; release failed acquisitions.

#### CatalogSession.catalogs

```python
catalogs() -> tuple[CatalogInfo, ...]
```

Return bounded sorted alias/permission/ownership inventory; omit private paths.

#### CatalogSession.use

```python
use(alias: str) -> None
```

Change the default for future begins only.

#### CatalogSession.begin

```python
begin(mode: str='read', *, catalog: str | None=None, metadata: CommitMetadata | None=None) -> Transaction
```

Return a native transaction permanently owned by the selected store, not the session default.

#### CatalogSession.apply_copy

```python
apply_copy(package: CopyPackage, *, target: str, idempotency_key: str, conflict: str='fail', metadata: CommitMetadata | None=None, limits: CopyLimits | None=None) -> CopyReceipt
```

Apply a detached package using the target permission/lifetime and a pinned native commit.

#### CatalogSession.detach

```python
detach(alias: str) -> None
```

Refuse in-use detach; close owned handles only. main cannot detach.

#### CatalogSession.close

```python
close() -> None
```

Refuse active transactions; otherwise close every owned handle, preserving first failure.

### LogicalViews

Database-owned logical view operations; no separately closable resources or caches.

#### LogicalViews.prepare

```python
prepare() -> None
```

Create the owned registry in one native transaction; repeat adds no commit.

#### LogicalViews.create

```python
create(name: str, *, query: str, parameters_schema: tuple[ViewParameter, ...]=(), replace: bool=False) -> ViewDefinition
```

Validate and atomically create/replace one definition; no automatic OCC retry.

#### LogicalViews.get

```python
get(name: str, *, snapshot: Transaction | None=None) -> ViewDefinition | None
```

Inspect one checked definition at a read snapshot; None means no visible name.

#### LogicalViews.list

```python
list(*, after: str | None=None, limit: int=100, snapshot: Transaction | None=None) -> tuple[ViewDefinition, ...]
```

Bounded lexicographic introspection; reuse one snapshot across stable pages.

#### LogicalViews.drop

```python
drop(name: str) -> bool
```

Atomically remove a definition; absent returns False without a new commit.

#### LogicalViews.execute

```python
execute(name: str, parameters: Mapping[str, object] | None=None, *, snapshot: Transaction | None=None, timeout_seconds: float | None=None) -> QueryResult
```

Execute a validated read definition at one native snapshot, never open a writer.

## Public factory and transfer functions


### okto_grafx.catalog_copy.capture_copy

```python
capture_copy(source: Transaction, *, tables: tuple[str, ...], limits: CopyLimits=CopyLimits(), record_ids: dict[str, tuple[int, ...]] | None=None, history: str='refuse') -> CopyPackage
```

Capture tables or explicit RID subsets from one native read snapshot.

### okto_grafx.catalog_copy.prepare_copy_target

```python
prepare_copy_target(database: Database) -> None
```

Explicitly enable existing provenance capabilities and initialize the ordinary receipt ledger.

### okto_grafx.catalog_copy.copy_graph

```python
copy_graph(package: CopyPackage, target: Database, *, idempotency_key: str, conflict: str='fail', metadata: CommitMetadata | None=None, limits: CopyLimits=CopyLimits()) -> CopyReceipt
```

Copy into pre-existing compatible tables with data+receipt in one native durable commit.

### okto_grafx.workspace.resolve_workspace

```python
resolve_workspace(*, policy: WorkspacePolicy, explicit_store: str | Path | None=None, project_root: str | Path | None=None, cwd: str | Path | None=None, user_store: str | Path | None=None) -> ResolvedWorkspace
```

Resolve explicit store > project root > bounded marker > opted-in cwd. No mkdir/open.

### okto_grafx.html_snapshot.render_html_snapshot

```python
render_html_snapshot(graph: GraphProjection, *, catalog: CatalogView | None=None, limits: HtmlSnapshotLimits=HtmlSnapshotLimits(), cancellation: CancellationToken | None=None) -> str
```

Render detached identities/edges and optional separately captured schema; never write files.

### okto_grafx.sqlite_import.read_sqlite_rows

```python
read_sqlite_rows(path: str | os.PathLike[str], *, allowed_root: str | os.PathLike[str], query: str, columns: tuple[str, ...], types: tuple[str, ...], parameters: tuple=(), limits: SQLiteImportLimits=SQLiteImportLimits(), cancellation: CancellationToken | None=None) -> tuple[dict[str, object], ...]
```

Read one bounded SELECT; SQL NULL stays None; no source connection survives return.

### okto_grafx.sqlite_import.import_sqlite

```python
import_sqlite(transaction: Transaction, statement: str, path: str | os.PathLike[str], *, allowed_root: str | os.PathLike[str], query: str, columns: tuple[str, ...], types: tuple[str, ...], parameters: tuple=(), limits: SQLiteImportLimits=SQLiteImportLimits(), cancellation: CancellationToken | None=None) -> ExecuteManyReport
```

Atomically stage the complete bounded selection; caller owns Grafx commit/rollback.

### okto_grafx.graph_interop.to_networkx

```python
to_networkx(graph: GraphProjection, *, max_memory_bytes: int=64 * 1024 * 1024, cancellation: CancellationToken | None=None) -> MultiDiGraph
```

Copy a detached multigraph with scoped identities and physical edge keys.

### okto_grafx.graph_interop.projection_arrow_batches

```python
projection_arrow_batches(graph: GraphProjection, *, kind: str='nodes', results: tuple | None=None, result_type: str='DOUBLE', batch_rows: int=256, max_batch_bytes: int=16 * 1024 * 1024, cancellation: CancellationToken | None=None) -> Iterator[RecordBatch]
```

Export scoped identities/endpoints and optional aligned scalar results in bounded batches.

### okto_grafx.polars.to_polars

```python
to_polars(source: QueryResult | QueryCursor, *, types: tuple[str | ArrowVectorType, ...], batch_rows: int=256, max_batch_bytes: int=16 * 1024 * 1024, max_rows: int=100000, max_bytes: int=64 * 1024 * 1024) -> PolarsFrame
```

Materialize an explicitly typed frame and retain its native schema separately.

### okto_grafx.polars.import_polars

```python
import_polars(transaction: Transaction, statement: str, frame: PolarsFrame, *, types: tuple[str | ArrowVectorType, ...], max_batch_rows: int=256, max_batch_bytes: int=16 * 1024 * 1024, max_rows: int=1000000, max_batches: int=4096) -> ExecuteManyReport
```

Stage one metadata-bearing eager frame atomically, with caller-owned commit.

### okto_grafx.text_import.read_csv_batches

```python
read_csv_batches(path: str | os.PathLike[str], *, allowed_root: str | os.PathLike[str], columns: tuple[str, ...], types: tuple[str, ...], delimiter: str=',', null_token: str='\\N', limits: TextImportLimits=TextImportLimits(), cancellation: CancellationToken | None=None) -> Iterator[tuple[dict[str, object], ...]]
```

Read header-required UTF-8 CSV with double quotes; close the iterator on early exit.

### okto_grafx.text_import.read_jsonl_batches

```python
read_jsonl_batches(path: str | os.PathLike[str], *, allowed_root: str | os.PathLike[str], columns: tuple[str, ...], types: tuple[str, ...], limits: TextImportLimits=TextImportLimits(), cancellation: CancellationToken | None=None) -> Iterator[tuple[dict[str, object], ...]]
```

Read one exact scalar object per UTF-8 line; missing and duplicate keys are errors.

### okto_grafx.text_import.import_csv

```python
import_csv(transaction: Transaction, statement: str, path: str | os.PathLike[str], *, allowed_root: str | os.PathLike[str], columns: tuple[str, ...], types: tuple[str, ...], delimiter: str=',', null_token: str='\\N', limits: TextImportLimits=TextImportLimits(), cancellation: CancellationToken | None=None) -> ExecuteManyReport
```

Atomically stage one complete CSV file; caller owns transaction, commit and retry.

### okto_grafx.text_import.import_jsonl

```python
import_jsonl(transaction: Transaction, statement: str, path: str | os.PathLike[str], *, allowed_root: str | os.PathLike[str], columns: tuple[str, ...], types: tuple[str, ...], limits: TextImportLimits=TextImportLimits(), cancellation: CancellationToken | None=None) -> ExecuteManyReport
```

Atomically stage one complete JSON Lines file with caller-owned commit/retry.

### okto_grafx.tabular.to_pandas

```python
to_pandas(source: QueryResult | QueryCursor, *, types: tuple[str | ArrowVectorType, ...], batch_rows: int=256, max_batch_bytes: int=16 * 1024 * 1024, max_rows: int=100000, max_bytes: int=64 * 1024 * 1024) -> DataFrame
```

Materialize an explicitly typed Arrow-backed frame; never infer dtypes or close a cursor.

### okto_grafx.tabular.import_pandas

```python
import_pandas(transaction: Transaction, statement: str, frame: DataFrame, *, types: tuple[str | ArrowVectorType, ...], max_batch_rows: int=256, max_batch_bytes: int=16 * 1024 * 1024, max_rows: int=1000000, max_batches: int=4096) -> ExecuteManyReport
```

Stage one Arrow-backed DataFrame atomically; require explicit dtypes and vector metadata.

### okto_grafx.parquet.read_parquet_batches

```python
read_parquet_batches(path: str | os.PathLike[str], *, allowed_root: str | os.PathLike[str], types: tuple[str | ArrowVectorType, ...], max_batch_rows: int=256, max_batch_bytes: int=16 * 1024 * 1024, max_rows: int=1000000, max_batches: int=4096, max_file_bytes: int=256 * 1024 * 1024, max_row_group_bytes: int=64 * 1024 * 1024) -> Iterator[RecordBatch]
```

Read typed batches from one permitted local file; close the iterator on early exit.

### okto_grafx.parquet.import_parquet

```python
import_parquet(transaction: Transaction, statement: str, path: str | os.PathLike[str], *, allowed_root: str | os.PathLike[str], types: tuple[str | ArrowVectorType, ...], max_batch_rows: int=256, max_batch_bytes: int=16 * 1024 * 1024, max_rows: int=1000000, max_batches: int=4096, max_file_bytes: int=256 * 1024 * 1024, max_row_group_bytes: int=64 * 1024 * 1024) -> ExecuteManyReport
```

Stage one complete local Parquet import atomically; never commit or retry for the caller.

### okto_grafx.parquet.write_parquet

```python
write_parquet(source: QueryResult | QueryCursor, path: str | os.PathLike[str], *, allowed_root: str | os.PathLike[str], types: tuple[str | ArrowVectorType, ...], batch_rows: int=256, max_batch_bytes: int=16 * 1024 * 1024, max_rows: int=1000000, max_batches: int=4096, max_file_bytes: int=256 * 1024 * 1024) -> ParquetExportReport
```

Publish a complete new Parquet file atomically without overwrite; caller owns the cursor.

### okto_grafx.arrow.import_arrow_batches

```python
import_arrow_batches(transaction: Transaction, statement: str, batches: Iterable[RecordBatch], *, types: tuple[str | ArrowVectorType, ...], max_batch_rows: int=65536, max_batch_bytes: int=16 * 1024 * 1024, max_rows: int=1000000, max_batches: int=4096) -> ExecuteManyReport
```

Atomically stage typed scalar/vector batches as named parameters, without committing.

### okto_grafx.arrow.to_arrow_batches

```python
to_arrow_batches(source: QueryResult | QueryCursor, *, types: tuple[str | ArrowVectorType, ...], batch_rows: int=256, max_batch_bytes: int=16 * 1024 * 1024) -> Iterator[RecordBatch]
```

Yield copied typed batches; caller owns cursor lifetime and already-emitted batches.

### okto_grafx.projections.project_graph

```python
project_graph(database: Database, reader: Transaction | None=None, *, node_tables: tuple[str, ...], relationship_tables: tuple[str, ...]=(), limits: ProjectionLimits=ProjectionLimits(), cancellation: CancellationToken | None=None, timeout_seconds: float | None=None, weight_columns: dict[str, str] | None=None, default_weight: float | None=None) -> GraphProjection
```

Capture selected tables in one read snapshot, preserving parallel edges and loops.

### okto_grafx.migrations.migrate_schema

```python
migrate_schema(database: Database, migrations: tuple[SchemaMigration, ...], *, namespace: str, dry_run: bool=False, max_attempts: int=3) -> MigrationReport
```

Validate/apply a complete ordered 1..N plan, atomically per version.

### okto_grafx.api.connect

```python
connect(path: str | os.PathLike[str], *, registry: PortRegistry | None=None, extensions: ExtensionRegistry | None=None, **options: Unpack[ConnectOptions]) -> Database
```

Open the database at `path`, creating it when it does not exist yet (SPEC-M1 FR-1).

### okto_grafx.backup.create_backup

```python
create_backup(database: Database, destination: str | os.PathLike[str], *, max_bytes: int=_DEFAULT_MAX_BYTES, max_capture_seconds: float=5.0, capture_mode: str='disk') -> BackupReport
```

Create a verified consistent cut with bounded chunked capture and read-back.

### okto_grafx.backup.restore_backup

```python
restore_backup(backup: str | os.PathLike[str], destination: str | os.PathLike[str], *, confirm_original_offline: bool=False, max_bytes: int=_DEFAULT_MAX_BYTES) -> BackupReport
```

Verify and restore into a NEW directory for offline replacement, never a writable fork.

### okto_grafx.transfer.export_graph

```python
export_graph(database: Database, destination: str | os.PathLike[str], *, limits: TransferLimits | None=None, history: str='refuse') -> TransferReport
```

Stream all current logical schema/rows/vectors from one fixed reader snapshot.

### okto_grafx.transfer.import_graph

```python
import_graph(source: str | os.PathLike[str], destination: str | os.PathLike[str], *, limits: TransferLimits | None=None, resume_directory: str | os.PathLike[str] | None=None) -> TransferReport
```

Verify a logical artifact and publish a separately writable fresh-UUID database.

## Result and observation type fields

DTO module paths below are annotation/import locations, not permission to
construct raw engine state. Consume returned instances and documented accessors.
`Lsn`, `Csn` and record/table IDs are integer aliases, not wall-clock times.
`RecordRef` is a physical page/slot identity, not your application primary key.
`Value` is the detached value union described in the query-language reference.

### TemporalLimits fields

Annotation location: `okto_grafx.domain.temporal.TemporalLimits`.

Aggregate encoded-input, event and live/output-row bounds; not an RSS ceiling.

```python
max_events: int
max_bytes: int
max_rows: int
```

### TemporalVersion fields

Annotation location: `okto_grafx.domain.temporal.TemporalVersion`.

One row lineage interval [system_from, system_to); None is unbounded.

```python
table: str
table_id: int
record_id: int
values: tuple[object, ...]
schema_version: int
system_from: CommitId
system_to: CommitId | None
```

### TemporalGraph fields

Annotation location: `okto_grafx.domain.temporal.TemporalGraph`.

Complete bounded historical rows/schemas for a requested closed table set.

```python
as_of: CommitId
schemas: tuple[TableDef, ...]
rows: tuple[TemporalVersion, ...]
events_scanned: int
encoded_bytes_scanned: int
```

### TemporalVersions fields

Annotation location: `okto_grafx.domain.temporal.TemporalVersions`.

Versions visible to the owning read snapshot, never a claim beyond its boundary.

```python
read_commit: CommitId
table: str
record_id: int
versions: tuple[TemporalVersion, ...]
activation: CommitId
retained_from: CommitId
```

### TemporalPin fields

Annotation location: `okto_grafx.domain.temporal.TemporalPin`.

Durable named retention protection; explicitly released, with no implicit TTL.

```python
name: str
at: CommitId
tables: tuple[str, ...]
```

### TemporalPruneReport fields

Annotation location: `okto_grafx.domain.temporal.TemporalPruneReport`.

Atomic retention result; payload redaction does not reclaim physical file space.

```python
before: CommitId
tables: tuple[str, ...]
commit: CommitId | None
redacted_versions: int
redacted_bytes: int
physical_bytes_reclaimed: int
```

### ViewParameter fields

Annotation location: `okto_grafx.views.ViewParameter`.

An exact native scalar type; all declared arguments are required, even if nullable.

```python
name: str
type: ValueType
nullable: bool
```

### ViewDefinition fields

Annotation location: `okto_grafx.views.ViewDefinition`.

Detached definition; dependencies pair table names with complete schema hashes.

```python
name: str
query: str
parameters_schema: tuple[ViewParameter, ...]
columns: tuple[str, ...]
dependencies: tuple[tuple[str, str], ...]
sha256: str
```

### CopyLimits fields

Annotation location: `okto_grafx.catalog_copy.CopyLimits`.

Whole-package logical bounds, not process RSS or durable transaction quota overrides.

```python
max_rows: int
max_bytes: int
max_row_bytes: int
max_tables: int
```

### CopyTable fields

Annotation location: `okto_grafx.catalog_copy.CopyTable`.

One immutable schema plus native encoded rows; record IDs remain source-qualified.

```python
schema: TableDef
rows: tuple[tuple[int, bytes], ...]
```

### CopyPackage fields

Annotation location: `okto_grafx.catalog_copy.CopyPackage`.

Detached retry input; checksummed, not authenticated or a physical backup.

```python
source_commit: CommitId
tables: tuple[CopyTable, ...]
spaces: tuple[EmbeddingSpaceDef, ...]
sha256: str
```

### CopyReceipt fields

Annotation location: `okto_grafx.catalog_copy.CopyReceipt`.

One proven target commit; replayed=True adds no new target effects.

```python
source_commit: CommitId
target_commit: CommitId
request_sha256: str
rows: int
replayed: bool
skipped_rows: int
```

### CatalogPathPolicy fields

Annotation location: `okto_grafx.catalogs.CatalogPathPolicy`.

Existing allowed roots, no links/UNC, no implicit home expansion or directory creation.

```python
allowed_roots: tuple[str, ...]
max_catalogs: int
max_active_transactions: int
```

#### CatalogPathPolicy.resolve

```python
resolve(path: str | os.PathLike[str], *, existing: bool=True) -> str
```

Validate an explicit path against roots; this never creates or opens a store.

### CatalogInfo fields

Annotation location: `okto_grafx.catalogs.CatalogInfo`.

Detached catalog inventory; store identity is not filesystem authority.

```python
alias: str
database_uuid: bytes
read_only: bool
owned: bool
active_transactions: int
```

### WorkspacePolicy fields

Annotation location: `okto_grafx.workspace.WorkspacePolicy`.

Opt-in discovery within explicit allowed roots, with no environment/home fallback.

```python
paths: CatalogPathPolicy
markers: tuple[str, ...]
max_parent_steps: int
allow_cwd: bool
allow_user_store: bool
project_store_name: str
```

### ResolvedWorkspace fields

Annotation location: `okto_grafx.workspace.ResolvedWorkspace`.

Explicit absolute paths, not open handles; source records which precedence rule won.

```python
root: str
project_store: str
user_store: str | None
source: str
```

### ProjectionLimits fields

Annotation location: `okto_grafx.projections.ProjectionLimits`.

Logical picture/work limits; not process RSS or underlying scan I/O limits.

```python
max_nodes: int
max_edges: int
max_memory_bytes: int
max_work: int
batch_rows: int
max_batch_bytes: int
```

### ProjectionDiagnostics fields

Annotation location: `okto_grafx.projections.ProjectionDiagnostics`.

Capture observations, not physical I/O counts or a freshness certificate.

```python
scan_calls: int
rows: int
max_batch_rows: int
capture_work: int
```

### ProjectionNode fields

Annotation location: `okto_grafx.projections.ProjectionNode`.

A table-qualified physical record identity, not an application primary key.

```python
table: str
record_id: int
```

### ProjectionEdge fields

Annotation location: `okto_grafx.projections.ProjectionEdge`.

One physical relationship; endpoints are offsets in GraphProjection.nodes.

```python
table: str
record_id: int
source: int
target: int
```

### GraphProjection fields

Annotation location: `okto_grafx.projections.GraphProjection`.

Immutable directed multigraph with no handles, pins or durable effects.

```python
database_uuid: bytes
snapshot_lsn: int
nodes: tuple[ProjectionNode, ...]
edges: tuple[ProjectionEdge, ...]
limits: ProjectionLimits
logical_bytes: int
diagnostics: ProjectionDiagnostics | None
adjacency: ProjectionAdjacency | None
lookup: ProjectionLookup | None
weights: tuple[float, ...] | None
pagerank_preparation: PageRankPreparation | None
simple_topology: SimpleTopology | None
```

#### GraphProjection.topological_order

```python
topological_order(*, cancellation: CancellationToken | None=None) -> TopologicalOrderResult
```

Return deterministic Kahn ordering or a typed cycle/blocked result, in O(V+E).

#### GraphProjection.with_pagerank

```python
with_pagerank(*, backend: str='python', weighted: bool=False, cancellation: CancellationToken | None=None) -> GraphProjection
```

Retain immutable transition data for repeated ranking with different seeds.

#### GraphProjection.with_simple_topology

```python
with_simple_topology(*, cancellation: CancellationToken | None=None) -> GraphProjection
```

Retain bounded loop-free undirected neighbors without changing physical edges.

#### GraphProjection.label_propagation

```python
label_propagation(*, max_iterations: int=100, cancellation: CancellationToken | None=None) -> LabelPropagationResult
```

Run asynchronous node-order voting; smallest identity wins ties; no storage writes.

#### GraphProjection.with_lookup

```python
with_lookup(*, cancellation: CancellationToken | None=None) -> GraphProjection
```

Retain a bounded immutable identity lookup, without acquiring storage authority.

#### GraphProjection.with_adjacency

```python
with_adjacency(*, cancellation: CancellationToken | None=None) -> GraphProjection
```

Return a new picture with reusable immutable adjacency, charging its memory.

#### GraphProjection.strongly_connected_components

```python
strongly_connected_components(*, cancellation: CancellationToken | None=None) -> tuple[ProjectionNode, ...]
```

Return deterministic directed SCC labels without recursion or storage reads.

#### GraphProjection.reachable

```python
reachable(source: ProjectionNode, *, direction: str='out', max_depth: int | None=None, max_results: int=100000, cancellation: CancellationToken | None=None) -> tuple[ProjectionNode, ...]
```

Return discovered nodes in BFS order, including source; no silent truncation.

#### GraphProjection.shortest_path

```python
shortest_path(source: ProjectionNode, target: ProjectionNode, *, direction: str='out', max_depth: int | None=None, max_results: int=100000, cancellation: CancellationToken | None=None) -> ProjectionPath
```

Return one unweighted shortest path; equal choices follow physical edge order.

#### GraphProjection.weighted_shortest_path

```python
weighted_shortest_path(source: ProjectionNode, target: ProjectionNode, *, direction: str='out', max_results: int=100000, max_distance: float | None=None, cancellation: CancellationToken | None=None) -> WeightedProjectionPath
```

Find a non-negative minimum-cost path; no hop constraint or implicit weights.

#### GraphProjection.pagerank

```python
pagerank(*, damping: float=0.85, tolerance: float=1e-08, max_iterations: int=100, cancellation: CancellationToken | None=None, backend: str='python', weighted: bool=False, personalization: dict[ProjectionNode, float] | None=None) -> PageRankResult
```

Compute bounded optionally weighted/personalized PageRank with explicit convergence.

#### GraphProjection.k_core

```python
k_core(*, cancellation: CancellationToken | None=None) -> tuple[int, ...]
```

Return simple-undirected core numbers: parallel edges collapse and loops are ignored.

#### GraphProjection.degrees

```python
degrees(*, direction: str='total', cancellation: CancellationToken | None=None) -> tuple[int, ...]
```

Count physical incoming/outgoing occurrences; a self-loop has total degree two.

#### GraphProjection.weakly_connected_components

```python
weakly_connected_components(*, cancellation: CancellationToken | None=None) -> tuple[ProjectionNode, ...]
```

Return each node's component label (minimum table/record identity), including isolates.

### TopologicalOrderResult fields

Annotation location: `okto_grafx.projection_algorithms.TopologicalOrderResult`.

Typed DAG/cycle result. Blocked includes cycle descendants, not just cycle members.

```python
order: tuple[ProjectionNode, ...]
acyclic: bool
blocked: tuple[ProjectionNode, ...]
```

### PageRankPreparation fields

Annotation location: `okto_grafx.projection_algorithms.PageRankPreparation`.

Immutable transition data for one projection, backend and weight mode; no rank state.

```python
backend: str
weighted: bool
sources: tuple[int, ...]
targets: tuple[int, ...]
shares: tuple[float, ...]
dangling: tuple[int, ...]
numeric_buffers: tuple[bytes, ...] | None
logical_bytes: int
```

### SimpleTopology fields

Annotation location: `okto_grafx.projection_algorithms.SimpleTopology`.

Retained loop-free undirected neighbors; physical projection edges remain unchanged.

```python
neighbors: tuple[tuple[int, ...], ...]
logical_bytes: int
```

### LabelPropagationResult fields

Annotation location: `okto_grafx.projection_algorithms.LabelPropagationResult`.

Labels aligned with nodes; convergence means one whole sweep without changes.

```python
labels: tuple[ProjectionNode, ...]
iterations: int
converged: bool
```

### WeightedProjectionPath fields

Annotation location: `okto_grafx.projection_algorithms.WeightedProjectionPath`.

Minimum non-negative cost path, or an explicit unreachable result.

```python
found: bool
distance: float | None
nodes: tuple[ProjectionNode, ...]
edges: tuple[ProjectionEdge, ...]
```

### ProjectionLookup fields

Annotation location: `okto_grafx.projection_algorithms.ProjectionLookup`.

Read-only node identity lookup retained by one detached picture.

```python
positions: Mapping[ProjectionNode, int]
logical_bytes: int
```

### ProjectionPath fields

Annotation location: `okto_grafx.projection_algorithms.ProjectionPath`.

One shortest path within the requested direction/depth, preserving edge identity.

```python
found: bool
nodes: tuple[ProjectionNode, ...]
edges: tuple[ProjectionEdge, ...]
```

### PageRankResult fields

Annotation location: `okto_grafx.projection_algorithms.PageRankResult`.

Scores aligned with projection nodes and explicit convergence status.

```python
scores: tuple[float, ...]
iterations: int
converged: bool
residual: float
```

### ProjectionAdjacency fields

Annotation location: `okto_grafx.projection_algorithms.ProjectionAdjacency`.

Immutable CSR offsets and physical edge positions, in both directions.

```python
out_offsets: tuple[int, ...]
out_edges: tuple[int, ...]
in_offsets: tuple[int, ...]
in_edges: tuple[int, ...]
logical_bytes: int
```

### HtmlSnapshotLimits fields

Annotation location: `okto_grafx.html_snapshot.HtmlSnapshotLimits`.

Display limits; source capture uses independent ProjectionLimits.

```python
max_nodes: int
max_edges: int
max_bytes: int
max_work: int
```

### SQLiteImportLimits fields

Annotation location: `okto_grafx.sqlite_import.SQLiteImportLimits`.

Logical input bounds, including SQLite VM instructions; not a process RSS promise.

```python
max_rows: int
max_bytes: int
max_field_bytes: int
max_work: int
```

### PolarsFrame fields

Annotation location: `okto_grafx.polars.PolarsFrame`.

Frame plus explicit Arrow metadata; do not mutate the frame during consumption.

```python
frame: DataFrame
arrow_schema: Schema
```

### TextImportLimits fields

Annotation location: `okto_grafx.text_import.TextImportLimits`.

Local input and logical batch limits; not a process RSS or transaction-size promise.

```python
batch_rows: int
max_batch_bytes: int
max_rows: int
max_batches: int
max_file_bytes: int
max_record_bytes: int
max_field_bytes: int
max_work: int
```

### ParquetExportReport fields

Annotation location: `okto_grafx.parquet.ParquetExportReport`.

One complete newly published local file; not a database durability receipt.

```python
path: str
rows: int
batches: int
bytes: int
```

### ArrowVectorType fields

Annotation location: `okto_grafx.arrow.ArrowVectorType`.

Explicit store-local vector identity, dimension and native precision for Arrow.

```python
space_ref: int
dimension: int
dtype: str
```

### ScalarFunction fields

Annotation location: `okto_grafx.domain.query.extensions.ScalarFunction`.

Trusted deterministic scalar callback with exact positional types and NULL propagation.

```python
name: str
argument_types: tuple[str, ...]
return_type: str
implementation: Callable[..., object]
max_value_bytes: int
```

#### ScalarFunction.invoke

```python
invoke(arguments: tuple[object, ...]) -> object
```

Call with scalar values only; NULL propagates and callback failures are typed.

### ExtensionRegistry fields

Annotation location: `okto_grafx.domain.query.extensions.ExtensionRegistry`.

Per-handle immutable trusted allowlist; not a sandbox or a plugin loader.

```python
scalars: tuple[ScalarFunction, ...]
trusted: bool
```

#### ExtensionRegistry.call_scalar

```python
call_scalar(name: str, arguments: tuple[object, ...]) -> object
```

Invoke only an exact registered name; no module/path or builtin resolution.

### VectorTotalMemoryUsage fields

Annotation location: `okto_grafx.engine.vector_memory.VectorTotalMemoryUsage`.

Optional per-handle aggregate reservations, including in-flight/held pictures.

```python
limit_bytes: int | None
reserved_bytes: int
pictures: int
peak_reserved_bytes: int
budget_refusals: int
```

### VectorMemoryUsage fields

Annotation location: `okto_grafx.engine.vector_memory.VectorMemoryUsage`.

Local derived-cache observations; no storage read or freshness proof.

```python
space: str
limit_bytes: int | None
cached_entries: int
cached_logical_bytes: int
peak_requested_bytes: int
budget_refusals: int
warm_retirements: int
```

### KeyPageCacheUsage fields

Annotation location: `okto_grafx.engine.key_page_memo.KeyPageCacheUsage`.

Handle-local decoded-page retention and counters, not freshness or RSS.

```python
max_pages: int
max_bytes: int
pages: int
logical_bytes: int
hits: int
misses: int
evictions: int
admission_refusals: int
```

### SchemaMigration fields

Annotation location: `okto_grafx.migrations.SchemaMigration`.

One immutable, contiguous application version and its exact DDL strings.

```python
version: int
statements: tuple[str, ...]
```

#### SchemaMigration.checksum

```python
checksum: str  # read-only property
```

SHA-256 of version and exact text, including whitespace, in canonical JSON v1.

### MigrationReport fields

Annotation location: `okto_grafx.migrations.MigrationReport`.

Observed versions and versions committed by this invocation; dry-run writes none.

```python
namespace: str
dry_run: bool
previously_applied: tuple[int, ...]
applied: tuple[int, ...]
pending: tuple[int, ...]
applied_lsns: tuple[tuple[int, int], ...]
```

### IndexDistribution fields

Annotation location: `okto_grafx.engine.index_distribution.IndexDistribution`.

Physical entries, including retained versions; not live row cardinality.

```python
bucket_count: int
entries: int
pages: int
overflow_pages: int
largest_chain_pages: int
largest_bucket_entries: int
largest_key_entries: int
dominant_key_fraction: float
recommendation: str
```

### HybridSearchOptions fields

Annotation location: `okto_grafx.domain.query.hybrid.HybridSearchOptions`.

RRF-v1 over bounded source candidates; graph work is explicit and optional.

```python
lexical_weight: float
vector_weight: float
rrf_k: int
candidate_k: int
fusion: str
allow_partial: bool
graph_relations: tuple[str, ...]
graph_seeds: tuple[int, ...]
graph_direction: str
graph_hops: int
graph_weight: float
graph_filter: bool
max_graph_edges: int
max_memory_bytes: int
graph_access: str
```

### HybridHit fields

Annotation location: `okto_grafx.domain.query.hybrid.HybridHit`.

One table-qualified identity with source ranks, raw scores and fusion explanation.

```python
table: str
record_id: int
score: float
lexical_rank: int | None
lexical_score: float | None
vector_rank: int | None
vector_score: float | None
graph_distance: int | None
```

### HybridSearchResult fields

Annotation location: `okto_grafx.domain.query.hybrid.HybridSearchResult`.

No hidden partial or approximate source: all dispositions are retained on empty hits.

```python
hits: tuple[HybridHit, ...]
snapshot_commit: int
fusion: str
regime: str
lexical_regime: str
vector_regime: str
lexical_candidates: int
vector_candidates: int
graph_edges_visited: int
source_errors: tuple[tuple[str, str], ...]
lexical_index_built_through_commit: int | None
graph_regime: str
memory_peak_bytes: int
lexical_memory_peak_bytes: int
vector_memory_peak_bytes: int
graph_memory_peak_bytes: int
```

### BackupReport fields

Annotation location: `okto_grafx.backup.BackupReport`.

Verified physical cut; bytes count database payload, not manifest or temporary copies.

```python
destination: str
database_uuid: str
checkpoint_lsn: int
files: int
bytes: int
```

### TransferLimits fields

Annotation location: `okto_grafx.transfer.TransferLimits`.

Artifact/work bounds, not a process RSS cap; checked before publication.

```python
max_bytes: int
max_rows: int
max_row_bytes: int
batch_rows: int
```

### RecordIdMapping fields

Annotation location: `okto_grafx.transfer.RecordIdMapping`.

One current logical identity remap, qualified by the stable table name.

```python
table: str
source_record_id: int
target_record_id: int
```

### TransferReport fields

Annotation location: `okto_grafx.transfer.TransferReport`.

Completed artifact/import evidence, not a mapping of historical commits.

```python
destination: str
source_database_uuid: str
source_snapshot_lsn: int
target_database_uuid: str | None
tables: int
rows: int
bytes: int
manifest_sha256: str
record_id_mapping: tuple[RecordIdMapping, ...]
```

### TextIndexOptions fields

Annotation location: `okto_grafx.domain.index.fulltext.TextIndexOptions`.

Persisted analyzer v1, pinned Unicode rules, locale und and no stop/stem profiles.

```python
analyzer: str
analyzer_version: int
max_token_bytes: int
max_document_characters: int
max_document_tokens: int
field_weights: tuple[float, ...]
normalization: str
case_folding: str
locale: str
stopwords: str
stemming: str
statistics_mode: str
statistics_history_entries: int
prefix_max_characters: int
positions: bool
```

#### TextIndexOptions.derivation

```python
derivation() -> str
```

Encode the complete durable identity as canonical ASCII hex in catalog v2.

### TextSearchLimits fields

Annotation location: `okto_grafx.domain.index.fulltext.TextSearchLimits`.

Per-search work/memory bounds; page IO remains cooperatively interruptible.

```python
max_query_tokens: int
max_postings: int
max_candidates: int
max_explanation_bytes: int
max_memory_bytes: int
max_statistics_wal_records: int
max_statistics_wal_bytes: int
max_expanded_terms: int
```

### TextHit fields

Annotation location: `okto_grafx.domain.index.fulltext.TextHit`.

One snapshot-visible lexical match; no mutable row payload leaks out.

```python
record_id: int
score: float
matched_fields: tuple[str, ...]
matched_terms: tuple[str, ...]
```

### TextSearchResult fields

Annotation location: `okto_grafx.domain.index.fulltext.TextSearchResult`.

Complete bounded BM25 result or a typed refusal, never a partial success.

```python
hits: tuple[TextHit, ...]
regime: str
index_built_through_commit: int
snapshot_commit: int
postings_visited: int
candidates: int
corpus_documents: int
statistics_regime: str
statistics_wal_records: int
```

### ConnectOptions fields

Annotation location: `okto_grafx.api.options.ConnectOptions`.

Optional connect keywords with no defaults or validation duplicated here.

```python
page_size: int
partitions_per_table: int
identity_lease_size: int
buffer_budget_bytes: int
max_open_files: int
recovery_policy: Literal['replay', 'refuse']
lease_ttl_seconds: float
lease_timeout_seconds: float
commit_lock_timeout_seconds: float
reader_stall_threshold_seconds: float
wal_segment_bytes: int
wal_max_bytes: int | None
checkpoint_interval_records: int
max_statement_writes: int | None
max_result_rows: int | None
max_intermediate_rows: int | None
max_traversal_expansions: int | None
max_traversal_paths: int | None
max_transaction_rows: int | None
max_transaction_bytes: int | None
max_wal_batch_bytes: int | None
max_index_build_entries: int | None
automatic_index_expected_cardinality: int | None
metrics: Literal['noop', 'openmetrics', 'json']
metrics_destination: str | None
allow_remote_metrics: bool
codec: Literal['pure', 'numpy']
vector_math: Literal['auto', 'pure', 'numpy']
checksum: Literal['auto', 'pure', 'native']
vector_exact_scan_threshold: int
vector_ef_search: int
vector_hnsw_memory_budget_bytes: int | None
vector_hnsw_total_memory_budget_bytes: int | None
index_key_cache_pages: int
index_key_cache_bytes: int
read_only: bool
descriptor_revalidation: Literal['strict', 'generation']
max_query_value_characters: int
query_memory_budget_bytes: int | None
```

### DatabaseIdentity fields

Annotation location: `okto_grafx.engine.database.DatabaseIdentity`.

Who a database is: the record CONTRACT.md section 6.2 puts on the identity page.

```python
database_uuid: bytes
page_size: int
partitions_per_table: int
created_at_wall: float
granularity_descriptor: str
format_version: int
```

#### DatabaseIdentity.encode

```python
encode() -> bytes
```

Return the identity record exactly as CONTRACT.md section 6.2 lays it out.

#### DatabaseIdentity.decode

```python
decode(raw: bytes) -> DatabaseIdentity
```

Return the identity a record holds, refusing damaged bytes as corruption.

### ScanRowV1 fields

Annotation location: `okto_grafx.engine.database.ScanRowV1`.

One detached stored row in table-column order.

```python
record_id: int
values: tuple[Value, ...]
```

### ScanPageV1 fields

Annotation location: `okto_grafx.engine.database.ScanPageV1`.

One bounded page of detached rows and its optional continuation.

```python
rows: tuple[ScanRowV1, ...]
next_cursor: ScanCursorV1 | None
```

### ExecuteManyReport fields

Annotation location: `okto_grafx.engine.database.ExecuteManyReport`.

Bounded summary of an atomic :meth:`Transaction.executemany` call.

```python
statements: int
statistics: Mapping[str, int]
```

### QueryResult fields

Annotation location: `okto_grafx.engine.query_engine.QueryResult`.

The rows one statement produced, with the plan that produced them.

```python
columns: tuple[str, ...]
rows: tuple[tuple[Value, ...], ...]
plan: PlanNode | None
statistics: Mapping[str, int]
```

#### QueryResult.dictionaries

```python
dictionaries() -> tuple[dict[str, Value], ...]
```

Return the rows as mappings from column name to value.

#### QueryResult.describe

```python
describe() -> str
```

Return a short en-US summary of this result.

### VectorHit fields

Annotation location: `okto_grafx.engine.vector_engine.VectorHit`.

One neighbour of a similarity search.

```python
record_id: RecordId
score: float
ref: RecordRef
retired: bool
```

### VectorSearchResult fields

Annotation location: `okto_grafx.engine.vector_engine.VectorSearchResult`.

The answer to one similarity search, with how it was produced.

```python
hits: tuple[VectorHit, ...]
regime: str
achieved_k: int
requested_k: int
space: str
filter_cardinality: int | None
```

### StorageFileView fields

Annotation location: `okto_grafx.engine.public_views.StorageFileView`.

Immutable size metadata for one file in the storage namespace.

```python
name: str
size_bytes: int
page_size: int
```

#### StorageFileView.page_count

```python
page_count: int  # read-only property
```

Return how many complete configured pages fit in this file.

### StorageView fields

Annotation location: `okto_grafx.engine.public_views.StorageView`.

Read-only inventory of the storage device at the instant the view was requested.

```python
name: str
page_size: int
files: tuple[StorageFileView, ...]
descriptor_cache_hits: int | None
descriptor_cache_misses: int | None
descriptor_cache_evictions: int | None
```

#### StorageView.list_files

```python
list_files(prefix: str='') -> tuple[str, ...]
```

Return captured file names starting with `prefix` in stable order.

#### StorageView.exists

```python
exists(file: str) -> bool
```

Return whether `file` existed when this snapshot was built.

#### StorageView.file_size

```python
file_size(file: str) -> int
```

Return the captured byte length of `file`.

#### StorageView.page_count

```python
page_count(file: str) -> int
```

Return the captured complete-page count of `file`.

### ClockView fields

Annotation location: `okto_grafx.engine.public_views.ClockView`.

Immutable identity of the clock implementation, without advancing either source.

```python
implementation: str
```

### CodecView fields

Annotation location: `okto_grafx.engine.public_views.CodecView`.

Page codec per instance and the effective process-wide checksum provider.

```python
format_version: int
page_size: int
implementation: str
process_checksum_implementation: str
```

### MetricsView fields

Annotation location: `okto_grafx.engine.public_views.MetricsView`.

Immutable metric capability summary; values remain available on `Database` itself.

```python
enabled: bool
```

### MaintenanceStatus fields

Annotation location: `okto_grafx.engine.public_views.MaintenanceStatus`.

Last-observed operational status without unavailable estimates or live capabilities.

```python
wal_bytes: int
checkpoint_lag_lsn: int | None
recovery_required: bool
stale_indexes: tuple[str, ...]
heap_bloat_bytes: int | None
oldest_reader_age: float | None
```

### TableBloatReport fields

Annotation location: `okto_grafx.engine.public_views.TableBloatReport`.

Conservative heap-bloat census for one table at one recyclable horizon.

```python
table: str
table_id: int
data_pages: int
slot_directory_entries: int
free_slots: int
stored_versions: int
ended_versions: int
horizon_eligible_versions: int
horizon_retained_versions: int
horizon_eligible_slot_bytes: int
horizon_retained_slot_bytes: int
overflow_versions: int
horizon_eligible_overflow_versions: int
```

### BloatReport fields

Annotation location: `okto_grafx.engine.public_views.BloatReport`.

Detached aggregate of a read-only, header-only heap-bloat census.

```python
recyclable_horizon_lsn: int
vacuum_safety_established: bool
tables: tuple[TableBloatReport, ...]
data_pages: int
slot_directory_entries: int
free_slots: int
stored_versions: int
ended_versions: int
horizon_eligible_versions: int
horizon_retained_versions: int
horizon_eligible_slot_bytes: int
horizon_retained_slot_bytes: int
overflow_versions: int
horizon_eligible_overflow_versions: int
```

### TableVacuumReport fields

Annotation location: `okto_grafx.engine.public_views.TableVacuumReport`.

Detached physical effects of one quiescent vacuum pass over one table.

```python
table: str
table_id: int
pages_scanned: int
eligible_inline_versions: int
reclaimed_versions: int
reclaimed_slot_bytes: int
relinked_versions: int
skipped_overflow_versions: int
eligible_overflow_versions: int
reclaimed_overflow_pages: int
```

### VacuumReport fields

Annotation location: `okto_grafx.engine.public_views.VacuumReport`.

Outcome of one manual, process-quiescent MVCC vacuum operation.

```python
horizon_lsn: int
reclaim_floor_before: int
reclaim_floor_after: int
capability_activated: bool
wrote: bool
complete: bool
tables: tuple[TableVacuumReport, ...]
pages_rewritten: int
reclaimed_versions: int
reclaimed_slot_bytes: int
relinked_versions: int
skipped_overflow_versions: int
indexes_reconciled: int
index_entries_removed: int
reclaimed_overflow_pages: int
```

### MetricsSnapshotView fields

Annotation location: `okto_grafx.engine.public_views.MetricsSnapshotView`.

A detached immutable mapping used at every level of a metrics snapshot.

```python
entries: tuple[tuple[str, object], ...]
```

### ComponentView fields

Annotation location: `okto_grafx.engine.public_views.ComponentView`.

Identity-only view for a component with no safe collaborator-level read API.

```python
role: str
implementation: str
```

### QueryEngineView fields

Annotation location: `okto_grafx.engine.public_views.QueryEngineView`.

Immutable query-planning diagnostics that do not retain the query engine.

```python
implementation: str
skipped_indexes: tuple[str, ...]
```

### VectorMathView fields

Annotation location: `okto_grafx.engine.public_views.VectorMathView`.

Immutable identity of the selected vector arithmetic implementation.

```python
name: str
```

### CoordinatorView fields

Annotation location: `okto_grafx.engine.public_views.CoordinatorView`.

Participant identity without reading or maintaining coordination records.

```python
participant: str
implementation: str
```

#### CoordinatorView.owner_id

```python
owner_id() -> str
```

Return the participant identity captured with this view.

### BufferPoolView fields

Annotation location: `okto_grafx.engine.public_views.BufferPoolView`.

Captured buffer-pool capacity and residency counters.

```python
page_size: int
budget_bytes: int
capacity_pages: int
used_bytes_value: int
db_label: str
retained_bytes_estimate_value: int
retained_bytes_estimator: str
```

#### BufferPoolView.used_bytes

```python
used_bytes() -> int
```

Return the resident byte count captured with this view.

#### BufferPoolView.retained_bytes_estimate

```python
retained_bytes_estimate() -> int
```

Return the versioned retained-memory estimate captured with this view.

### CatalogView fields

Annotation location: `okto_grafx.engine.public_views.CatalogView`.

Immutable catalog definition snapshot with read-compatible lookup helpers.

```python
table_definitions: tuple[TableDef, ...]
space_definitions: tuple[EmbeddingSpaceDef, ...]
```

#### CatalogView.tables

```python
tables() -> tuple[TableDef, ...]
```

Return captured tables in numeric identity order.

#### CatalogView.spaces

```python
spaces() -> tuple[EmbeddingSpaceDef, ...]
```

Return captured embedding spaces in numeric identity order.

#### CatalogView.has_table

```python
has_table(name: str) -> bool
```

Return whether a captured table has `name`.

#### CatalogView.has_space

```python
has_space(name: str) -> bool
```

Return whether a captured embedding space has `name`.

#### CatalogView.table

```python
table(name: str) -> TableDef
```

Return a captured table by name.

#### CatalogView.table_by_id

```python
table_by_id(table_id: int) -> TableDef
```

Return a captured table by numeric identity.

#### CatalogView.space

```python
space(name: str) -> EmbeddingSpaceDef
```

Return a captured embedding space by name.

#### CatalogView.space_by_id

```python
space_by_id(space_id: int) -> EmbeddingSpaceDef
```

Return a captured embedding space by numeric identity.

#### CatalogView.is_empty

```python
is_empty() -> bool
```

Return whether no table or embedding space was captured.

### CatalogStoreView fields

Annotation location: `okto_grafx.engine.public_views.CatalogStoreView`.

Immutable metadata and catalog snapshot for the catalog store.

```python
file: str
chunk_capacity: int
catalog: CatalogView
```

### HeapStoreView fields

Annotation location: `okto_grafx.engine.public_views.HeapStoreView`.

Immutable layout metadata for the heap store.

```python
file: str
max_tables: int
inline_capacity: int
```

### WalView fields

Annotation location: `okto_grafx.engine.public_views.WalView`.

Immutable state snapshot of the write-ahead log.

```python
last_lsn: int
directory: str
descriptor: str
segment_bytes: int
damage: ScanFailure | None
append_uncertain: bool
segment_inventory: tuple[SegmentInfo, ...]
total_bytes_value: int
```

#### WalView.segments

```python
segments() -> tuple[SegmentInfo, ...]
```

Return the segment inventory captured with this view.

#### WalView.total_bytes

```python
total_bytes() -> int
```

Return the live log size captured with this view.

### TransactionManagerView fields

Annotation location: `okto_grafx.engine.public_views.TransactionManagerView`.

Immutable transaction configuration and publication snapshot.

```python
partitions_per_table: int
commit_lock_timeout: float
lease_timeout: float
reader_stall_threshold: float | None
refresh_interval: float
open_transactions: int
recovery_required: bool
state: CommitState | None
```

#### TransactionManagerView.partition_of

```python
partition_of(table_id: int, key: bytes) -> int
```

Return the deterministic conflict partition for `table_id` and `key`.

#### TransactionManagerView.published_state

```python
published_state() -> CommitState
```

Return the commit state captured with this view.

#### TransactionManagerView.published_lsn

```python
published_lsn() -> int
```

Return the published commit LSN captured with this view.

### IndexView fields

Annotation location: `okto_grafx.engine.public_views.IndexView`.

Immutable public metadata for one committed secondary index.

```python
name: str
file: str
visibility: IndexVisibility
definition: IndexDefinition
stale: bool
stale_reason: str | None
built_through_lsn: int | None
reconciled_through_lsn: int | None
missing_targets: int
columns: tuple[str, ...]
automatic: bool | None
generation_state: str | None
active_nonce: int | None
expected_cardinality: int | None
```

#### IndexView.table_id

```python
table_id: int  # read-only property
```

Return the committed table id covered by this index.

#### IndexView.table_name

```python
table_name: str  # read-only property
```

Return the committed table name covered by this index.

#### IndexView.positions

```python
positions: tuple[int, ...]  # read-only property
```

Return key positions in their declared compound-key order.

#### IndexView.key_derivation

```python
key_derivation: str  # read-only property
```

Return the stable key-derivation contract.

#### IndexView.bucket_count

```python
bucket_count: int  # read-only property
```

Return the active physical generation's bucket count.

#### IndexView.layout

```python
layout: IndexLayout  # read-only property
```

Return the durable physical organization selected by the catalog.

### IndexRegistryView fields

Annotation location: `okto_grafx.engine.public_views.IndexRegistryView`.

Immutable inventory of registered secondary indexes.

```python
registered: tuple[IndexView, ...]
published_lsn: int
```

#### IndexRegistryView.indexes

```python
indexes() -> tuple[IndexView, ...]
```

Return the captured indexes in stable name order.

#### IndexRegistryView.index

```python
index(name: str) -> IndexView
```

Return one captured index by case-insensitive name.

### LedgerView fields

Annotation location: `okto_grafx.engine.public_views.LedgerView`.

Immutable inventory of forensic ledger entries.

```python
file: str
damage: DamagedTail | None
captured_entries: tuple[LedgerEntry, ...]
depth_by_origin: tuple[tuple[str, int], ...]
```

#### LedgerView.entries

```python
entries() -> tuple[LedgerEntry, ...]
```

Return every captured entry, oldest first.

#### LedgerView.depth

```python
depth() -> tuple[tuple[str, int], ...]
```

Return captured pending counts as immutable key-value pairs.

#### LedgerView.list

```python
list(*, origin_class: object=None, reason: object=None, limit: int=100, offset: int=0) -> tuple[LedgerEntry, ...]
```

Filter the captured entries without touching the ledger store.

#### LedgerView.inspect

```python
inspect(entry_id: int) -> LedgerEntry
```

Return one captured ledger entry by identity.

#### LedgerView.provenance

```python
provenance(entry_id: int) -> LedgerPayload
```

Decode the immutable provenance captured with one ledger entry.

#### LedgerView.export

```python
export(entry_id: int) -> bytes
```

Return preserved bytes from the captured, already-verified ledger entry.

### QuarantineView fields

Annotation location: `okto_grafx.engine.public_views.QuarantineView`.

Immutable inventory of quarantined evidence without restore or write capabilities.

```python
directory: str
captured_entries: tuple[QuarantineEntry, ...]
captured_inventory: tuple[QuarantineInventoryItem, ...] | None
```

#### QuarantineView.list

```python
list() -> tuple[QuarantineEntry, ...]
```

Return every quarantine entry captured with this view.

#### QuarantineView.count

```python
count() -> int
```

Return how many quarantine entries were captured with this view.

#### QuarantineView.inventory

```python
inventory() -> tuple[QuarantineInventoryItem, ...]
```

Return the complete evidence inventory captured with this view.

#### QuarantineView.inspect

```python
inspect(name: str) -> QuarantineEntry
```

Return one captured quarantine entry by name.

### VectorIndexView fields

Annotation location: `okto_grafx.engine.public_views.VectorIndexView`.

Immutable identity and search configuration of one committed vector index.

```python
name: str
file: str
space_id: int
space_name: str
dimension: int
metric_of_space: DistanceMetric
storage_dtype: str
ef_search: int
stale: bool
stale_reason: str | None
built_through_lsn: int | None
```

### VectorEngineView fields

Annotation location: `okto_grafx.engine.public_views.VectorEngineView`.

Immutable inventory and configuration of the vector subsystem.

```python
exact_scan_threshold: int
space_definitions: tuple[EmbeddingSpaceDef, ...]
registered_indexes: tuple[VectorIndexView, ...]
```

#### VectorEngineView.spaces

```python
spaces() -> tuple[EmbeddingSpaceDef, ...]
```

Return captured embedding-space definitions.

#### VectorEngineView.space

```python
space(name: str) -> EmbeddingSpaceDef
```

Return one captured embedding-space definition by name.

#### VectorEngineView.indexes

```python
indexes() -> tuple[VectorIndexView, ...]
```

Return captured vector indexes in stable space-name order.

#### VectorEngineView.index

```python
index(space_name: str) -> VectorIndexView
```

Return the captured vector index of one embedding space.

### IndexCleanupFile fields

Annotation location: `okto_grafx.engine.index_cleanup.IndexCleanupFile`.

One inventoried index artifact and the reason it is retained or removable.

```python
file: str
bytes: int
reason: str
```

### IndexCleanupReport fields

Annotation location: `okto_grafx.engine.index_cleanup.IndexCleanupReport`.

A bounded census; removed/deferred names describe only this cleanup call.

```python
dry_run: bool
files: tuple[IndexCleanupFile, ...]
candidate_bytes: int
removed: tuple[str, ...]
deferred: tuple[str, ...]
wal_records_examined: int
```

### Snapshot fields

Annotation location: `okto_grafx.domain.txn.snapshot.Snapshot`.

A fixed view of the database: the LSN a transaction opened at, and what it may see.

```python
read_lsn: Lsn
```

#### Snapshot.visible

```python
visible(xmin: Csn, xmax: Csn) -> bool
```

Return True when a version created at xmin and ended at xmax belongs to this view.

### CommitId fields

Annotation location: `okto_grafx.domain.txn.commit_identity.CommitId`.

Store-qualified, non-sentinel logical commit reference with store-local ordering.

```python
database_uuid: bytes
sequence: int
```

#### CommitId.to_token

```python
to_token() -> str
```

Canonical bounded transport spelling, independent of Python repr/pickle.

#### CommitId.parse

```python
parse(token: str) -> CommitId
```

Refuse alternate spellings instead of ambiguously normalizing an identity.

### CommitTime fields

Annotation location: `okto_grafx.domain.txn.commit_identity.CommitTime`.

Immutable observed/ordered instants; never a clock or lease authority.

```python
observed_at: Timestamp
ordered_at: Timestamp
clock_adjusted: bool
```

### MetadataLimits fields

Annotation location: `okto_grafx.domain.txn.commit_metadata.MetadataLimits`.

Configurable admission limits inside fixed hard ceilings, independent of I/O.

```python
max_bytes: int
max_attributes: int
max_key_bytes: int
max_string_bytes: int
max_depth: int
max_values: int
```

### CommitMetadata fields

Annotation location: `okto_grafx.domain.txn.commit_metadata.CommitMetadata`.

A fully detached canonical value; repr deliberately contains no supplied data.

```python
actor: str | None
origin: str | None
correlation_id: str | None
reason: str | None
attributes: Mapping[str, MetadataValue]
```

#### CommitMetadata.canonical_bytes

```python
canonical_bytes: bytes  # read-only property
```

Canonical admission bytes and commit-catalog nested metadata body v1.

### CommitCatalogEntry fields

Annotation location: `okto_grafx.domain.txn.commit_catalog.CommitCatalogEntry`.

Immutable verified value of one record, not a store handle or physical proof.

```python
identity: CommitId
timing: CommitTime
metadata_bytes: bytes | None
kind: CommitKind
```

#### CommitCatalogEntry.metadata

```python
metadata: CommitMetadata | None  # read-only property
```

The decoded immutable metadata; bytes/keys/content are absent from repr.

#### CommitCatalogEntry.encode

```python
encode() -> bytes
```

Encode captured and revalidated values, not unchecked mutable host slots.

### CommitHistoryPage fields

Annotation location: `okto_grafx.domain.txn.commit_history.CommitHistoryPage`.

Ascending commits; history at/below activation is explicitly untracked.

```python
database_uuid: bytes
activation_sequence: int
read_sequence: int
entries: tuple[CommitCatalogEntry, ...]
has_more: bool
```

### CommitMapping fields

Annotation location: `okto_grafx.domain.txn.commit_transfer.CommitMapping`.

Qualified source-to-target logical import mapping, not a durability receipt.

```python
source: CommitId
target: CommitId
```

### CommitImport fields

Annotation location: `okto_grafx.domain.txn.commit_transfer.CommitImport`.

Metadata to pass to target.begin; mapping is recovered from the target entry.

```python
source: CommitId
metadata: CommitMetadata
```

#### CommitImport.mapping

```python
mapping(target: CommitCatalogEntry) -> CommitMapping
```

Validate a returned/decoded target record's atomically persisted source link.

### RecordRef fields

Annotation location: `okto_grafx.domain.ids.RecordRef`.

Physical location of one record version: the page that holds it and the slot inside that page.

```python
page: PageIndex
slot: SlotId
```

#### RecordRef.encode

```python
encode() -> int
```

Pack this reference into a single integer as `(page << 16) | slot`.

#### RecordRef.decode

```python
decode(raw: int) -> RecordRef
```

Unpack an integer produced by :meth:`encode` back into a reference.

### RecycleReport fields

Annotation location: `okto_grafx.domain.wal.replay.RecycleReport`.

What one pass of horizon based recycling reclaimed, kept, and had to leave for later.

```python
horizon_lsn: Lsn
recycled: tuple[str, ...]
deferred: tuple[str, ...]
retained: tuple[str, ...]
reclaimed_bytes: int
lag_segments: int
reader_present: bool
```

### RecoveryFinding fields

Annotation location: `okto_grafx.domain.recovery.report.RecoveryFinding`.

One thing recovery observed, with enough location to act on it.

```python
kind: str
detail: str
file: str
offset: int
length: int
lsn: Lsn
page: int
entry_id: int
quarantine: str
```

### RecoveryReport fields

Annotation location: `okto_grafx.domain.recovery.report.RecoveryReport`.

The frozen result of one recovery pass (CONTRACT.md section 8.6).

```python
outcome: str
records_replayed: int
records_discarded: int
ledger_entries_created: int
last_good_lsn: Lsn
findings: tuple[RecoveryFinding, ...]
```

#### RecoveryReport.findings_of

```python
findings_of(kind: str) -> tuple[RecoveryFinding, ...]
```

Return every finding of one kind, so a caller need not filter by hand.

### FindingLocation fields

Annotation location: `okto_grafx.domain.verify.findings.FindingLocation`.

Where a finding is, with the five coordinates section 8.6 names.

```python
file: str
page: int
slot: int
lsn: Lsn
index: str
```

#### FindingLocation.describe

```python
describe() -> str
```

Return the location as one en-US phrase, for a message that has to read well.

### VerificationFinding fields

Annotation location: `okto_grafx.domain.verify.findings.VerificationFinding`.

One disagreement the walk met, with its location and an en-US explanation.

```python
kind: str
location: FindingLocation
detail: str
```

### VerificationReport fields

Annotation location: `okto_grafx.domain.verify.findings.VerificationReport`.

The result of one verification walk: what was checked, and what disagreed.

```python
scope: str
findings: tuple[VerificationFinding, ...]
pages_checked: int
records_checked: int
index_entries_checked: int
files_checked: tuple[str, ...]
```

#### VerificationReport.clean

```python
clean: bool  # read-only property
```

Return True when the walk ran and found nothing to report.

#### VerificationReport.findings_at

```python
findings_at(kind: str, file: str, page: int) -> tuple[VerificationFinding, ...]
```

Return every finding of one kind about one page of one file.

#### VerificationReport.findings_of

```python
findings_of(kind: str) -> tuple[VerificationFinding, ...]
```

Return every finding of one kind, so a caller need not filter by hand.

### ColumnDef fields

Annotation location: `okto_grafx.domain.model.schema.ColumnDef`.

One column of a table: a name, a stored type, nullability and, for vectors, its space.

```python
name: str
type: ValueType
nullable: bool
vector_space: str | None
```

#### ColumnDef.is_vector

```python
is_vector: bool  # read-only property
```

Return True when this column stores an embedding.

### TableDef fields

Annotation location: `okto_grafx.domain.model.schema.TableDef`.

A node table or a relationship table, with its columns in their stored order.

```python
table_id: int
name: str
kind: str
columns: tuple[ColumnDef, ...]
primary_key: str | None
from_table: str | None
to_table: str | None
schema_version: int
schema_layouts: tuple[tuple[int, int], ...]
```

#### TableDef.arity

```python
arity: int  # read-only property
```

Return the number of columns a tuple of this table must carry.

#### TableDef.endpoint_columns

```python
endpoint_columns: tuple[ColumnDef, ...]  # read-only property
```

Return the reserved endpoint columns, which only a relationship table has.

#### TableDef.property_columns

```python
property_columns: tuple[ColumnDef, ...]  # read-only property
```

Return the columns the user declared, without the layout's own.

#### TableDef.source_of

```python
source_of(values: Sequence[Value]) -> RecordId
```

Return the row identity an edge tuple starts at.

#### TableDef.target_of

```python
target_of(values: Sequence[Value]) -> RecordId
```

Return the row identity an edge tuple ends at.

#### TableDef.column

```python
column(name: str) -> ColumnDef
```

Return the column with that name.

#### TableDef.column_positions

```python
column_positions: Mapping[str, int]  # read-only property
```

Return the immutable, precomputed position of each stored column.

#### TableDef.column_index

```python
column_index(name: str) -> int
```

Return the position of the column with that name in the stored tuple.

### EmbeddingSpaceDef fields

Annotation location: `okto_grafx.domain.model.schema.EmbeddingSpaceDef`.

One embedding space: the identity a vector column points at (SPEC-VEC TR-2).

```python
space_id: int
name: str
dimension: int
metric: DistanceMetric
normalized: bool
storage_dtype: str
state: str
created_at_wall: float
```

#### EmbeddingSpaceDef.is_active

```python
is_active: bool  # read-only property
```

Return True while the space still accepts writes.

#### EmbeddingSpaceDef.value_type

```python
value_type: ValueType  # read-only property
```

Return the value type vectors of this space encode to.

#### EmbeddingSpaceDef.retired

```python
retired() -> EmbeddingSpaceDef
```

Return the same space in the retired state, which is the only change it allows.

### Timestamp fields

Annotation location: `okto_grafx.domain.model.value.Timestamp`.

A point in time as whole microseconds since the Unix epoch.

```python
micros: int
```

#### Timestamp.from_wall

```python
from_wall(seconds: float) -> Timestamp
```

Build a timestamp from a Unix epoch reading in seconds, rounded to microseconds.

#### Timestamp.to_wall

```python
to_wall() -> float
```

Return this instant as Unix epoch seconds.

### Uuid fields

Annotation location: `okto_grafx.domain.model.value.Uuid`.

A 128-bit identifier carried as its sixteen raw bytes.

```python
raw: bytes
```

#### Uuid.hex

```python
hex: str  # read-only property
```

Return the thirty-two hexadecimal digits of this identifier, without separators.

#### Uuid.canonical

```python
canonical() -> str
```

Return the canonical 8-4-4-4-12 hyphenated form of this identifier.

#### Uuid.from_hex

```python
from_hex(text: str) -> Uuid
```

Build an identifier from its hexadecimal form, with or without hyphens.

### VectorValue fields

Annotation location: `okto_grafx.domain.model.value.VectorValue`.

An embedding together with the space it belongs to and the precision it is stored in.

```python
values: tuple[float, ...]
space_ref: int
dtype: str
```

#### VectorValue.dimension

```python
dimension: int  # read-only property
```

Return the number of components this vector carries.

#### VectorValue.value_type

```python
value_type: ValueType  # read-only property
```

Return the value type this vector encodes to, which follows its dtype.

### RecordIdFilter fields

Annotation location: `okto_grafx.domain.vector.filter.RecordIdFilter`.

The filter of an enumerated set of records, which knows its cardinality exactly.

```python
record_ids: frozenset[int]
```

#### RecordIdFilter.of

```python
of(record_ids: Iterable[int]) -> RecordIdFilter
```

Build a filter from any iterable of record identifiers.

#### RecordIdFilter.cardinality

```python
cardinality: int  # read-only property
```

Return the exact number of records this filter admits.

#### RecordIdFilter.admits

```python
admits(record_id: RecordId) -> bool
```

Return True when this record is one of the enumerated ones.
