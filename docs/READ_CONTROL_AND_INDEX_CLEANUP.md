# Read control and orphan-index cleanup

[API reference](API_REFERENCE.md) · [Operations](OPERATIONS.md) · [Roadmap](../ROADMAP.md)

These are **0.0.5 development** APIs, not a claim of a published release or an
upgrade of your installed application. Neither changes the disk format.

## Cooperative cancellation and deadlines

```python
from okto_grafx import CancellationToken, connect
from okto_grafx.errors import GrafxQueryCancelled, GrafxQueryDeadlineExceeded

with connect("./graph") as db:
    token = CancellationToken()
    # A different application thread may call token.cancel().
    try:
        result = db.execute("MATCH (n:Person) RETURN n.id",
                            timeout_seconds=2.0, cancellation=token)
    except (GrafxQueryCancelled, GrafxQueryDeadlineExceeded):
        # No partial QueryResult; this autocommit read's snapshot has been released.
        result = None

    query = db.query("MATCH (n:Person) RETURN n.id")
    with query.cursor(batch_size=128, timeout_seconds=5.0) as rows:
        for row in rows:
            print(row)
```

| Option / outcome | Contract |
| --- | --- |
| `timeout_seconds=None` | No execution deadline. A finite positive exact `int`/`float` opts in; booleans, zero, negatives, NaN and infinity are rejected. |
| `cancellation=None` | No signal. Otherwise pass an exact `CancellationToken`; arbitrary callbacks and subclasses are refused. |
| `CancellationToken.cancel()` | Idempotent, irreversible, process-local signal. Share between Python threads, not processes. It does not itself close the database/cursor or wait for completion. Use a new token for a new uncancelled request. |
| `token.cancelled` | Read-only observation of the signal. |
| `GrafxQueryCancelled` / `query_cancelled` | A cooperative check observed cancellation; subclass of `GrafxQueryError`, not automatically retryable. Cancellation takes precedence when both conditions are observed together. |
| `GrafxQueryDeadlineExceeded` / `query_deadline_exceeded` | A check observed the monotonic execution deadline; subclass of `GrafxQueryError`, not automatically retryable. Choose a new budget explicitly. |

The deadline begins at **`Database.execute`, `Transaction.execute`, or `Query.cursor`**,
not at reusable `Database.query` preparation. Every opened cursor receives a fresh
deadline. Its budget includes time between consumer fetches and buffered delivery;
an expired cursor does not continue serving buffered rows. A closed cursor stays
closed and repeated close/fetch remains idempotent. Previously returned batches
cannot be recalled.

Checks occur at execution/fetch boundaries, operator streams, scan counters and
traversal work admission. Clock polling is amortized over up to 64 work observations;
the default path creates no control object and does not poll the deadline clock.
This is **cooperative**, not a wall-clock service-level guarantee: waiting for a
lock, one storage/adapter call, parsing/planning, a native vector kernel or another
non-yielding primitive cannot be preempted. The read refuses at the next check.
Existing lock timeouts, work/memory budgets and corruption checks remain separate.
An idle cancelled cursor releases resources when next consumed or explicitly
closed, not from a background watchdog. Always use a context manager or `finally`.

On cancellation/deadline failure, an autocommit read or cursor releases its owned
transaction, buffers and nested iterator/spill resources. A read transaction opened
explicitly by the caller remains the caller's responsibility and may execute a
new uncontrolled read or roll back. Cleanup failures remain attached to the original
failure; do not infer clean shutdown if the device itself refuses cleanup.

Controls on **write transactions are rejected before statement execution**, even
for a read-shaped statement. They are not accepted by `executemany`, maintenance,
native `search_vectors`/physical scan doors, or commit. They never cancel durable
commit publication. No database-wide cancellation state, forced thread termination,
writer serialization or change to multi-reader/multi-writer guarantees is added.

Use this for abandoned UI requests, bounded read jobs and early stream termination.
Do not use it to guarantee instant interruption, kill commits, repair corruption,
or replace independent reader participants in a concurrent host integration.

## Orphan-index inventory and removal

```python
from okto_grafx import connect

# Stop ALL other handles/processes, including readers and older binaries first.
with connect("./graph") as db:
    preview = db.maintenance.cleanup_indexes(confirm_quiescent=True)
    for item in preview.files:
        print(item.file, item.bytes, item.reason)
    # Review the preview. This call independently re-proves the candidate set.
    report = db.maintenance.cleanup_indexes(
        confirm_quiescent=True, dry_run=False,
        max_files=10000, max_wal_records=100000,
    )
    print(report.removed, report.deferred)
```

`dry_run=True` is the default and removes no artifact. **Both modes require a
writable handle, no open local user transaction, and exact
`confirm_quiescent=True`.** This is the operator's assertion that every other
handle/process is stopped, not a detection feature. Reader lease expiry is never
proof of quiescence. The local participant and commit/WAL sections serialize
inventory, proof and reclamation. Normal durable-view admission may finish an
already-provable commit gap; preview is not a read-only forensic opening.

The complete catalog and strict retained-WAL scan are inspected before the first
delete. All catalog states (ACTIVE, STALE, BUILDING) and currently registered
artifacts remain protected. Retained page-write and logical-index references are
protected even below the checkpoint and even if their effects would not currently
be replayed. If older catalog page images remain, **all unowned generation files
are retained conservatively**; an unmapped logical index record does the same.
An ordinary checkpoint and subsequent WAL recycling may make a later inventory
more permissive, but cleanup does not force rollover, rewrite or truncate the WAL.
Malformed WAL envelopes/reference payloads and unknown record types refuse rather
than silently shorten the dependency proof. This inventory is not a replacement
for full page-image verification or `db.verify("all")`.

Only exact native `index/g_<16 lower-case hex>.idx` names with a nonzero nonce and
displaced `index_orphan/<32 lower-case hex>[.<4 lower-case hex>].idx` names can become
candidates. Canonical legacy names, arbitrary files, nested paths and unrecognized
spellings are preserved. Do not store application-owned data under native names.

| `IndexCleanupFile.reason` | Meaning |
| --- | --- |
| `catalog_or_registered` | Owned by the current catalog or this participant's registry. |
| `retained_wal` | Explicit retained page/index reference. |
| `retained_catalog_wal` | Historical catalog images prevent proving generation independence. |
| `unresolved_logical_wal` | An index WAL name has no current physical mapping. |
| `unrecognized_preserved` | Not an eligible native orphan spelling. |
| `orphan` | No ownership/dependency found under the stated quiescence contract. |

`IndexCleanupReport` contains `dry_run`, the tuple `files`, `candidate_bytes`,
`removed`, `deferred` and `wal_records_examined`. `candidate_bytes` is the eligible
file-size sum, not a promise of bytes immediately returned by the filesystem.
`removed` means the storage adapter reported reclamation; `deferred` means it did
not, including Windows sharing restrictions. Re-inventory before retrying.

`max_files` defaults to 10,000 and `max_wal_records` to 100,000; each accepts an
exact integer in 1..1,000,000. Exceeding either refuses before any deletion. These
are work/report bounds, **not an RSS cap**: the storage inventory API itself returns
a complete tuple and the WAL reader decodes bounded segments. Cache retirement
visits resident/retired frames, not every physical page of every candidate file.

All candidates are checked for local page holders, including retired-but-still-held
frames, before deletion. Proven orphan cache frames and allocation hints are
discarded without writeback, so a later flush cannot recreate an orphan. No live
catalog, index generation or user record is modified. Individual file reclamation
is not a multi-file transaction: failure can leave a safely removed prefix, and a
new call re-proves the remaining files. There is no undelete facility; take a backup
if you need forensic copies. Process interruption cannot turn an orphan into a
catalog-owned file while the quiescence assertion holds.

This is **not** online garbage collection, catalog-generation retirement, vacuum,
file truncation or automatic cleanup on every commit. It does not solve the
4,096-bucket directory limit or remove STALE generations still in the catalog.
