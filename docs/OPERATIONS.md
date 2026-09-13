# Operations, concurrency and recovery

Opt-in [native system history](SYSTEM_TIME_HISTORY.md) participates in the same
COMMIT/recovery protocol and physical backup. Its named retention pins are
independent of MVCC/WAL reader registration. Pruning is an explicit bounded
payload-redaction operation, not disk compaction or autonomous recovery.
The separate `compact_system_history(confirm_quiescent=True)` reclaims expired
payload extents/obsolete temporal paths only after native replacement and
checkpoint, with all other processes stopped. Optional temporal indexes share
the history COMMIT, verification and recovery; they are not permission caches.
See [0.0.6 wheel compatibility](V006_COMPATIBILITY.md) before enabling new bits.

[Logical export/import](LOGICAL_TRANSFER.md) creates a separately writable fresh-UUID
store; [physical restore](BACKUP_RESTORE.md) remains an offline same-UUID replacement.
Neither copies live participants into a fork. [FTS operations](FULL_TEXT_SEARCH.md#operations-and-compatibility)
cover the opt-in required capability, verification, generation rebuild, transfer and
older-reader refusal. Creating an FTS index requires compatible binaries on all
writers; it is not merely a process-local configuration change.
[Resumable logical import](LOGICAL_TRANSFER.md#opt-in-resumable-import) is explicitly
opt-in through a private workspace. It recovers native WAL and validates committed
prefixes; never serve its staging directory or reset WAL/locks to force continuation.
The public destination appears only after full verification and atomic promotion.

[Documentation index](README.md) · [Configuration](CONFIGURATION.md)

For cooperative read cancellation/deadlines and explicit orphan-index reclamation,
see [read control and index cleanup](READ_CONTROL_AND_INDEX_CLEANUP.md).
The query-error metric's finite `code` label domain includes `query_cancelled` and
`query_deadline_exceeded`; emitting either must not replace the original outcome
with a metrics configuration error.

## Deployment and ownership

Open one explicit application-owned local directory. The persistent identity is
`grafx.meta`; schema/rows/indexes live in `catalog.dat`, `heap.dat`, `index/`, with
WAL, control records, ledger and quarantine as a coherent database. Never replace
one file with a file from another database or remove WAL/control records to make
an error disappear. `:memory:` is ephemeral and does not demonstrate power-loss durability.

There is no authentication, authorization, encryption or hosted-service boundary
inside the embedded API. The host owns those controls. Keep live storage off
network shares/cloud synchronization and prevent unmanaged replacement/rename.
Use `descriptor_revalidation="strict"` unless the generation-mode prerequisites
are met. Neither mode makes concurrent external file replacement a supported restore.

## Concurrency contract

### ACID scope

Grafx implements ACID transaction properties within one supported local store,
with snapshot isolation and optimistic conflict validation. This describes the
implemented contract, not independent certification or universal serializability.

| Property | Implemented guarantee and boundary | Evidence |
| --- | --- | --- |
| Atomicity | A transaction's changes commit together or roll back; recovery must not promote effects without a valid durable COMMIT. A failure after the durable barrier is not a rollback. | [Commit/rollback protocol tests](../tests/txn/test_commit_protocol.py), [committed redo](../tests/recovery/test_commit_redo.py). |
| Consistency | Supported type, primary-key, endpoint, schema and storage invariants are enforced by the corresponding operations. Arbitrary business invariants are not inferred or enforced automatically. | [Typed query/write contract](QUERY_LANGUAGE.md), [native protocol](architecture/CONTRACT.md). |
| Isolation | Readers use MVCC snapshots; writers must pass optimistic logical and physical validation. Do not infer general predicate locking or serializability for every application invariant. | [Visibility tests](../tests/txn/test_snapshot.py), [OCC tests](../tests/txn/test_occ.py). |
| Durability | A successful durable write acknowledges its WAL barrier; verified recovery handles committed effects. Guarantees depend on supported filesystem/storage behavior. `:memory:` is not power-loss durable. | [WAL ordering and post-barrier outcomes](../tests/txn/test_commit_protocol.py), [recovery tests](../tests/recovery/test_commit_redo.py). |

ACID does not mean distributed transactions, zero possible corruption, automatic
repair of arbitrary damage, or identical isolation levels across products.
Host side effects, other databases and remote calls are outside this transaction.
For read-dependent business rules, validate the specific concurrent workload and
enforcement strategy; do not assume snapshot isolation alone prevents every anomaly.

Independent processes/threads can own concurrent transactions. Each reader sees
its snapshot and each writer uses optimistic validation. A writer lease and an
exclusive commit publication section remain: multiwriter is not lock-free or
linear throughput scaling. Distinct rows may share conflict partitions/physical
pages. Both logical OCC and physical-page validation must pass.

A shared `Database` participant has local protected sections; a long commit may
queue same-handle reads. Services that need overlapping read/write I/O should use
bounded independent participants with owned transactions, not expose one mutable
transaction to every request. Additional handles multiply cache/descriptor memory;
CPU-bound Python work still contends for the GIL. See [integration](INTEGRATION.md).

Keep snapshots short. They retain history/WAL; checkpoint and writer work can wait
or refuse. Reader TTL is not an operator assertion that no reader exists. Recovery,
index maintenance and vacuum have stronger lifecycle constraints than a normal read.

## Commit outcomes and retries

`Transaction.commit()` returns `CommitReport(csn, durable, wrote)`. Empty/read
transactions need not append a COMMIT. On a writing transaction, durable success
requires the WAL barrier. A later failure in page application/publication may still
raise an exception **after the commit is durable**.

| Observation | Required interpretation/action |
| --- | --- |
| Successful writing report with `durable=True` | Acknowledged durable write; do not repeat it |
| Exception details `committed=True`, `durable=True`, `recovery_required=True` | Already committed; stop using the failed handle for ordinary work, arrange bounded recovery/reopen, verify outcome; do not replay the mutation |
| `GrafxWriteConflict` before durable commit | Roll back/retire that transaction and retry the whole logical operation from a fresh snapshot with bounded backoff |
| Lease/storage/buffer retryable refusal | Address contention/resource cause; inspect durable outcome first, then bounded fresh operation where safe |
| Durability-barrier failure, process kill or transport timeout without an observed outcome | Do not infer rollback or success; reconcile with durable state/application idempotency before resubmitting |
| Corruption, incompatible format, unsupported operation or budget error | Do not blind-retry; correct the cause or follow recovery/operator procedure |

Use supported imports `from okto_grafx.errors import GrafxError, GrafxWriteConflict`.
Read `code`, `retryable`, `details` and the transaction's `report`; do not match
exception text. The post-barrier regression explicitly marks its exception
nonretryable. Custom trusted adapters may propagate their own exceptions.

Conservative conflict-only recipe (the operation must not send external messages
or perform non-idempotent host side effects inside the retried transaction):

```python
import random
import time
from okto_grafx.errors import GrafxWriteConflict

def write_with_conflict_retry(db, statement, parameters, attempts=3):
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        txn = None
        try:
            with db.begin("write") as txn:
                txn.execute(statement, parameters)
            return txn.report
        except GrafxWriteConflict as error:
            if (error.details.get("committed") or error.details.get("durable")
                    or (txn is not None and txn.report is not None and txn.report.durable)
                    or attempt + 1 == attempts):
                raise
            time.sleep(random.uniform(0, min(0.05 * 2**attempt, 0.5)))
```

The explicit `db.retry(txn)` door is for an active owned transaction refused by
optimistic validation; it returns a fresh successor, not a replay of statements.
It is not a universal substitute for the outcome policy above.

## Recovery runbook

1. Stop admission of new application work for the affected store; record the
   exception/report and release owned transactions/cursors. Do not keep a failing
   writer alive while deleting its files or extending its lease artificially.
2. A normal writable `connect(..., recovery_policy="replay")` performs recovery
   before returning a usable database. Inspect `db.recovery_report`. The `refuse`
   policy refuses damaged history instead of consenting to truncation; it is not
   a read-only switch or a promise to skip every clean replay operation.
3. `connect(..., read_only=True)` does not replay or repair. Admission requires
   checkpoint-complete state: even a normally acknowledged write newer than the
   checkpoint can refuse with `field="read_only_consistency"`. Open writable,
   recover/checkpoint as appropriate, then reopen read-only. Do not serve an older
   graph as fresh or checkpoint on every request to disguise a lifecycle mismatch.
4. `db.recover()` is explicit mutating maintenance, refuses read-only, and requires
   local quiescence. The engine coordinates with writers; the host must drain its
   work and bound recovery attempts. Do not implement a reconnect/recover loop per
   incoming request. Recovery-required handles stay fenced until safe completion.
5. Replay applies only proved committed effects with database identity, ordering,
   page/checksum/LSN and index authority validated. A safely discardable tail can
   be preserved/truncated; unproved effects, foreign UUID, future/equal-LSN content
   conflict or CRC-invalid authoritative targets can instead refuse before mutation.
6. Inspect recovery findings, ledger/quarantine and a proportional `verify()`;
   validate application-level acknowledged records before reopening admission.
   Repeated recovery should be idempotent, not create another logical mutation.
7. If proof is impossible, preserve the complete directory and reports. Escalate
   to operator restore from a verified quiescent backup or application-authorized
   logical rebuild into a new directory. Grafx has no public general hot restore
   that can make arbitrary corruption transparent. Never call data loss “recovery”.

An application can automate the safe replay/reopen branch with a single bounded
coordinator and honest availability status. Unproved repair/data deletion must not
be automated merely to return HTTP 200. Callback-driven custom storage has its own
trusted-code failure boundaries; [ports](PORTS.md) explains ownership.

`RecoveryReport.outcome` is one of `clean`, `truncated`, `quarantined`, `refused`.
These summarize the pass, not total application health. `clean` can include replay
of intact committed records; `truncated`/`quarantined` require inspecting preserved
evidence and discarded-work counts. A refused open may raise before any usable
`Database`/report is returned: retain the typed exception/details instead of
assuming a missing report means clean. Never reprocess forensic bytes as if they
were an application-approved write.

## Maintenance, backup and upgrades

| Operation | Effect / when to use |
| --- | --- |
| `verify(scope)` | Read/check pages, records, indexes or all; inspect counters and findings. Empty findings alone do not prove that anything was checked. Not a repair. |
| `flush()` | Write dirty cached pages; returns page count. Not an application commit, standalone backup or replacement for checkpoint. |
| `checkpoint()` | Flush/replay proved committed state, publish checkpoint and attempt WAL recycling; reader horizons can retain WAL. |
| `maintenance.status` | Last-observed status, not a global linearizable health certificate. |
| `read_index_status(name)` | Validate active index header and return detached status. |
| `rebuild_index(name)` / `rebuild_vector_index(space)` | Explicit derived-index reconstruction, not repair of corrupt authoritative heap/source data. |
| `inspect_index(name)` | Materializes an entry inventory; do not put unbounded inspection on a hot request. |
| `read_quarantine(name)` / `quarantine_receipts(name)` | Checksum-verified preserved bytes / receipt names; not a restore command. |

The 0.0.5 development line provides `okto_grafx.backup.create_backup` and
`restore_backup`: [full contract, budgets and examples](BACKUP_RESTORE.md).
Capture uses the existing checkpoint fence; writers wait for checkpoint/capture,
including the default temporary-disk spool I/O, but not subsequent artifact output
or verification. `capture_mode="memory"` is the explicit RAM-backed alternative.
Restore requires an offline-original assertion
and a new destination, preserving UUID/commit provenance rather than creating a fork.

For a manual filesystem backup, stop **all** participants, close handles successfully and
copy the complete directory as one quiescent artifact. Preserve hashes, Grafx
version/configuration and identity. Verify/reopen a separate restored copy before
relying on it. Copying only `heap.dat`, an open live directory or selected WAL
segments is not a supported consistent backup procedure.

Before an upgrade, pin/test the target binary on a copy and keep a pre-upgrade
backup. `page_size` must match persisted identity. `partitions_per_table` is adopted
from existing identity rather than reconfigured by reopen. Catalog-v2, heap reclaim
and WAL v2 capabilities can be one-way fences. Durable FTS summaries and hash
generations above 4,096 buckets activate additional required bits in the 0.0.5
continuation. Do not open migrated files with
older binaries; there is no general downgrade API. The CLI's offline control-format
downgrade is a narrowly scoped exception, not a way to undo catalog/WAL capabilities.

All processes accessing the same directory must understand activated capabilities.
Drain/restart after installation; never mix live old code with newly replaced modules.
Use an application adapter to preserve your own stable API across pre-alpha upgrades.

## Observation and maintenance contracts



Properties such as `db.catalog`, `db.indexes`, `db.wal`, `db.storage` and `db.metrics` are frozen
snapshots for schema, inventory and diagnostics. They never retain the storage device, page pool,
WAL, transaction manager or adapter callbacks. Writes go through transactions or explicit gated
database methods (`create_index`, `rehash_index`, `rehash_index_if_needed`,
`ensure_identity_indexes`, `checkpoint`,
`recover`, `flush`, `publish_metrics`); there is no `unsafe=True` escape. `Transaction` exposes
`snapshot`, `mode`, `txn_id`, `active` and `report`, but never its mutable engine context.

`db.maintenance.bloat(table=None)` is a conservative, header-only census at a non-pruning
observation of the checkpoint-capped recyclable horizon; even TTL-stalled reader records remain
pins. It reports ended versions and record-slot bytes that are eligible or retained by that
horizon, but deliberately excludes overflow-page bytes and keeps
`vacuum_safety_established=False`: observing potential bloat neither mutates the database nor
certifies that physical reclamation is safe.

For `bloat` and `vacuum`, `table` accepts `None` (all tables), a unique string
name, or `("node", name)` / `("rel", name)`. A bare ambiguous name refuses before
mutation or capability activation. Each table report retains `table_id`; its
`table` field is a qualified pair only when the current catalog contains both
kinds with that name, otherwise the original string. Code serializing reports
must handle both selector shapes. Qualification does not change the global
reclamation horizon, quiescence requirement or snapshot-floor consequences.

`db.maintenance.vacuum(table=None, *, confirm_quiescent=False, max_versions=None)` is the
separate mutating operation. Vacuum v1 is manual and foreground. It refuses catalog v1,
read-only handles, an open local transaction and every call that does not pass the exact
`confirm_quiescent=True` assertion. That assertion means the operator has stopped **every other
Grafx process and handle**, including an older binary, for the whole call; reader TTL is never
treated as proof of safety. The first call publishes the required `heap_reclaim_v1` capability,
so older builds fail closed, then one WAL-before-data commit atomically advances a durable global
snapshot floor, reconciles ACTIVE indexes, relinks retained chains and removes eligible inline
and overflow-backed versions. Overflow chains are retired only after checking complete payload
coverage and exclusive ownership across every table, including unselected/retained records.
Use `max_versions` to bound removed heap versions per pass; ownership scans and index reconciliation are not
part of that quota. A table filter still advances a heap-global floor and is therefore an
availability choice for the whole database.

The immutable `VacuumReport` distinguishes pages rewritten, tuple-slot bytes removed,
chain relinks, index entries removed and `reclaimed_overflow_pages`. Table reports include
`eligible_overflow_versions`; `skipped_overflow_versions` counts eligible overflow versions not
selected under the pass quota. `complete` covers eligible inline **and overflow** versions in
the selected tables. Retired overflow pages are WAL-logged as empty FREE pages, not silently
removed from disk. The ownership pass is foreground O(total reachable overflow pages), not a
new cost on ordinary queries/commits; `max_versions` is not an IO or total plan-memory limit.
Subsequent overflow writes reuse eligible persisted FREE pages under normal commit
fences, with current-page revalidation. Discovery uses O(1) advisory cursor memory
and scans a fixed heap extent incrementally per participant/reclaim floor. It is
amortized discovery, not a persistent O(1) free-page index. No file truncation occurs.
Vacuum never reassigns slots or `RecordRef`
identities. Restart application processes after the maintenance window so their first transaction
adopts the new capability, floor and index authority. See
[`docs/architecture/MVCC_VACUUM_V1.md`](architecture/MVCC_VACUUM_V1.md).

WAL page-image compression is a separate explicit, one-way activation:

```python
db.maintenance.ensure_identity_indexes()
db.maintenance.enable_wal_page_compression()
```

The first call ensures catalog v2 exists; the second publishes required capability
`wal_record_v2` in a v1-only transaction. Later commits use bounded zlib level 1 only when the
complete page image becomes strictly smaller. Incompressible images and batches that roll to a new
WAL segment retain the exact v1 full-image grammar. `max_wal_batch_bytes` measures the final encoded
records. Current participants adopt the capability at their next protected boundary; older builds
fail closed on the catalog capability or retained WAL v2. There is no disable/downgrade door. See
[`WAL_PAGE_COMPRESSION_V1.md`](architecture/WAL_PAGE_COMPRESSION_V1.md).

## Error taxonomy



Failures produced by the engine and the adapters shipped with Okto Grafx are `GrafxError`
subclasses carrying a machine-readable `code`, a `retryable` flag and located `details`. A custom
adapter is trusted host code: the registry validates its shape without executing it, but does not
translate exceptions it raises later. Such an exception can therefore propagate unchanged.

| Error | `retryable` | Means |
|---|---|---|
| `GrafxWriteConflict` | ✅ | Partitions intersected another commit's — retry with a fresh snapshot |
| `GrafxLeaseTimeout` | ✅ | Another writer held the lease too long |
| `GrafxLeaseStolen` / `GrafxStaleEpoch` | ❌ | This writer was superseded; its writes are refused |
| `GrafxCorruptionDetected` | ❌ | Bytes that cannot be trusted, with the location |
| `GrafxDeviceFull` / `GrafxStorageError` | ✅ | The device refused |
| `GrafxDurabilityBarrierFailed` | ❌ | A barrier failed; do not acknowledge unproved durability or assume rollback; reconcile the outcome |
| `GrafxRecoveryRefused` | ❌ | Recovery would not be safe; the evidence is preserved |
| `GrafxSnapshotReclaimed` | ✅ | Snapshot is older than the durable reclaim floor; open a new transaction, do not advance an existing snapshot |
| `GrafxLedgerError` / `GrafxQuarantineError` | ❌ | Evidence-store operation refused; inspect details and preserve artifacts |
| `GrafxBufferBudgetExceeded` | ✅ | The working set exceeded the budget |
| `GrafxTransactionBudgetExceeded` | ❌ | An enabled statement, transaction or final WAL-batch limit was exceeded before partial persistence |
| `GrafxQueryBudgetExceeded` | ❌ | An enabled row, traversal or logical query-memory limit was exceeded before statement release |
| `GrafxQueryCancelled` | ❌ | A caller signal was observed by a read; owned cursor/autocommit resources are released |
| `GrafxQueryDeadlineExceeded` | ❌ | A cooperative read deadline expired; retry only with an explicitly chosen new budget |
| `GrafxSchemaVersionMismatch` | ❌ | This build cannot read this database |
| `GrafxPortNotConfigured` | ❌ | An incomplete registry, naming every missing slot |
| `GrafxTransactionStateError` | ❌ | The transaction is not in a state that allows this |
| `GrafxQueryError` / `GrafxParseError` / `GrafxPlanError` | ❌ | The statement |
| `GrafxIndexError` | ❌ | An index refused, including a stale one asked to answer |
| `GrafxVectorValidationError`, `GrafxEmbeddingSpaceMismatch`, `GrafxSpaceRetired` | ❌ | Embeddings |
| `GrafxConfigurationError` | ❌ | An option, naming the field |
| `GrafxUnsupportedOperation` | ❌ | Declared not to exist, rather than silently ignored |

## Indexed retired-overflow discovery (0.0.5 development)

`db.maintenance.vacuum(confirm_quiescent=True, index_free_pages=True)` explicitly
activates `heap_free_page_index_v1` (bit 9) and builds a bounded-page immutable
directory of already retired overflow candidates. Existing catalog-v2 activation
and the caller's real quiescence assertion remain prerequisites. Default False
does not upgrade a store; once active, later vacuum calls maintain the directory.
Capability publication without a completed directory is recoverable and keeps
legacy discovery until initialization commits. There is no supported downgrade
by removing the required bit.

Normal writes traverse candidate IDs rather than scanning every heap page. Each
candidate still needs current physical FREE/LSN validation under the ordinary
publication fence; candidates used by another writer are OVERFLOW and skipped.
The directory/root are immutable during allocation: an interrupted pre-COMMIT
attempt cannot lose membership. Local cursor state resets on reopen/new reclaim
floor. Costs are O(requested pages + stale candidates + directory pages), not a
universal O(k) or O(1) guarantee. No online vacuum, truncation or single-writer
application premise is introduced.

Use for churn-heavy heaps with reusable overflow space and many cold participants.
It spends a small fraction of retired pages on the directory and adds an explicit
whole-heap census to quiescent directory construction/verification. It does not
benefit heaps with no eligible overflow pages. Physical backup preserves allocator
metadata; logical transfer uses a fresh heap. See [format and failure boundaries](specs/HEAP_FREE_PAGE_INDEX.md).

---
