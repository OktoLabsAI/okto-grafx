# Public Python API

[Documentation index](README.md) · [Integration](INTEGRATION.md) · [Query types](QUERY_LANGUAGE.md)

## Entry points and supported imports

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
        **options: object) -> Database
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

<!-- GENERATED PUBLIC REFERENCE: do not edit below -->

## Complete facade signatures

Generated from the public facade declarations; no engine instance is opened.
Types in signatures are described below; `DEFAULT_QUERY_CURSOR_BATCH_ROWS` is 256.
Factories, not direct engine constructors, own resource composition.

### Query

A canonical read statement that can open independent snapshot-owning cursors.

#### Query.cursor

```python
cursor(*, batch_size: int=DEFAULT_QUERY_CURSOR_BATCH_ROWS) -> QueryCursor
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
execute(text: str, parameters: Mapping[str, object] | None=None) -> QueryResult
```

Run one statement inside this transaction and return its result.

#### Transaction.executemany

```python
executemany(text: str, parameter_sets: Iterable[Mapping[str, object]]) -> ExecuteManyReport
```

Stage one updating statement for every parameter mapping, atomically as a batch.

#### Transaction.scan_rows_v1

```python
scan_rows_v1(table: str, *, limit: int, cursor: ScanCursorV1 | None=None) -> ScanPageV1
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

#### Maintenance.vacuum

```python
vacuum(table: str | None=None, *, confirm_quiescent: bool=False, max_versions: int | None=None) -> VacuumReport
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
rehash_index_if_needed(name: str, *, overflow_pages_per_bucket: int=1) -> IndexView | None
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
begin(mode: str='write') -> Transaction
```

Open a transaction in `"read"` or `"write"` mode (SPEC-M1 FR-2).

#### Database.retry

```python
retry(transaction: Transaction) -> Transaction
```

Open the successor of a transaction optimistic validation refused (BR-6).

#### Database.transaction

```python
transaction(mode: str='write') -> Iterator[Transaction]
```

Open a transaction as a block, committing on a clean exit and rolling back otherwise.

#### Database.execute

```python
execute(text: str, parameters: Mapping[str, object] | None=None) -> QueryResult
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

#### Database.search_vectors

```python
search_vectors(transaction: Transaction, *, space: str, query: Sequence[float] | VectorValue, k: int, candidate_filter: RecordIdFilter | None=None) -> VectorSearchResult
```

Search vectors under the fixed snapshot of one active transaction.

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
rehash_index_if_needed(name: str, *, overflow_pages_per_bucket: int=1) -> IndexView | None
```

Grow one exact index after a bounded directory-pressure assessment.

#### Database.verify

```python
verify(scope: str='all') -> VerificationReport
```

Walk the database and report every finding, precisely located (SPEC-M1 FR-11).

#### Database.ensure_identity_indexes

```python
ensure_identity_indexes() -> None
```

Persist and activate every exact access path required by endpoint identities.

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

Release everything this database opened, and never corrupt anything doing it (FR-1).

## Result and observation type fields

DTO module paths below are annotation/import locations, not permission to
construct raw engine state. Consume returned instances and documented accessors.
`Lsn`, `Csn` and record/table IDs are integer aliases, not wall-clock times.
`RecordRef` is a physical page/slot identity, not your application primary key.
`Value` is the detached value union described in the query-language reference.

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
