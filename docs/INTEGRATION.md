# Integration recipes

New 0.0.5 development integration recipes: [hybrid retrieval](HYBRID_SEARCH.md),
[logical transfer and crash resumption](LOGICAL_TRANSFER.md)
for verified fresh-store copies, and [full-text search](FULL_TEXT_SEARCH.md) for
native lexical retrieval. Both use ordinary transaction ownership; FTS-specific
options belong to the database integration layer, not a consumer's domain model.

[Documentation index](README.md) · [Start here](GETTING_STARTED.md) · [Operations](OPERATIONS.md)

This is the synchronous embedded API. Read the operational contract before deploying workers.

## Services and independent participants

Grafx does not provide a native async connection, HTTP server or SQLAlchemy/DB-API
driver. Put synchronous calls behind an application adapter. For an async server,
offload blocking work from the event loop and bound both submitted work and open
handles. Keep every transaction owned by one operation; do not interleave request
lifecycles on a shared transaction.

For parallel reads beside writes, use a bounded set of independently opened
`Database` handles/participants, owned by worker lanes. A single handle's local
protected sections can queue reads behind a long commit. More handles also multiply
page budgets and descriptors, and do not remove the Python GIL or exclusive commit
publication. Initialize each process's handles in that process, not by inheriting
open objects across `fork` or pickling them.

Minimal async offload pattern for an **existing initialized database**:

```python
import asyncio
from okto_grafx import connect

def read_count(path):
    # Writable admission can perform required WAL recovery. The query itself
    # still uses the read-only autocommit door, db.execute().
    with connect(path) as db:
        return db.execute("MATCH (p:Person) RETURN count(*)").rows[0][0]

async def read_count_async(path, slots):
    # slots = asyncio.Semaphore(2), created/owned within the service event loop.
    async with slots:
        task = asyncio.create_task(asyncio.to_thread(read_count, path))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            # A cancelled waiter has NOT cancelled native disk I/O. Retain this
            # slot until worker cleanup finishes; production shutdown must drain it.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue  # Repeated request cancellation still cannot stop I/O.
                except BaseException:
                    break
            try:
                task.result()  # Observe failure; never leave an unobserved worker.
            except BaseException as failure:
                raise cancelled from failure
            raise
```

The connection is not a filesystem-level read-only capability: the host must
allow safe recovery at admission. `read_only=True` requires checkpoint-complete
state and can refuse even an acknowledged recent commit not yet checkpointed.
Do not use a fresh read-only open on every live write-heavy request and silently
serve stale data when it refuses. Initialize/reuse the appropriate participants
through the application's coordinated lifecycle.

This simple recipe opens/closes per request to make ownership explicit; cold-open
cost can dominate. For production, reuse lane-owned handles and drain all tracked
jobs before shutdown/replacement. This recipe drains even repeated cancellation;
a production service should retain its own job registry through shutdown. A request
timeout is not proof that a writer stopped or rolled back. Recovery admission is a
separate coordinated lifecycle, not an exception handler that resets graph files.

Handle/application pool settings belong to the integrating application. They are
not hidden Grafx `connect()` options. With Pulse, put those specifics in Community
adapters; preserve a backend-neutral Core port.

## Application DTOs, pagination and agents

Map explicit projected columns to your own DTOs. Convert `Timestamp`/`Uuid`/vectors
deliberately for JSON; graph paths and opaque backend IDs are not cross-database
identities. Never persist internal `RecordRef` as an application key.

For HTTP pagination, use a stable application ordering with a unique tie-breaker
and a documented consistency policy. `SKIP`/`LIMIT` or a keyset query on a fresh
transaction does not preserve a previous page's snapshot. Physical `ScanCursorV1`
and `QueryCursor` cannot be serialized as continuation tokens. A service can own a
bounded snapshot session, or document that each page is a fresh snapshot; closing
idle sessions is necessary to avoid pinning history indefinitely.

Use the narrow ordered `(TIMESTAMP, STRING)` index where the query shape qualifies;
it is not a guarantee that every query with `LIMIT` is O(page size). Check
`db.explain()`/result statistics and preserve the scan fallback.

Agents can use this same library or the [JSON CLI](CLI.md). The host must enforce
authentication, capabilities, read/write policy and destructive-action consent;
there is no built-in optional-product MCP API until its roadmap milestone ships.

## Batch ingestion and resource policy

Choose a transaction size that fits configured row/byte/WAL limits and your atomicity
requirements. `executemany` amortizes parsing, not the need for durable commit.
Do not implement “retry” by committing a prefix after a quota error. A batch does
not generate embeddings, bypass indexes or disable verification. Use external
idempotency/source identity for retry across application/transport boundaries.

### Atomic bulk writes

Use `Transaction.executemany()` when many parameter mappings apply to the same updating query:

```python
with db.begin("write") as txn:
    report = txn.executemany(
        "CREATE (:Person {id: $id, name: $name, city: $city})",
        (
            {"id": row.id, "name": row.name, "city": row.city}
            for row in incoming_rows
        ),
    )
print(report.statements, report.statistics)
```

The iterable is consumed lazily, canonicalized one mapping at a time and applied in its original
order. Grafx parses the fixed text once, but replans each item against the transaction's current
overlay so later items see earlier writes correctly. The report is deliberately small: it carries
only the executed statement count and summed statistics, never input payloads, plans or
`QueryResult` objects.

The batch is one savepoint inside the caller-owned transaction. If iteration, parameter binding,
planning, a write budget or execution fails at any item, every change made by that
`executemany()` call is discarded. Work staged before it remains intact, and the caller may catch
the typed error, continue using the transaction and commit that earlier work. A successful call
does not commit by itself; the surrounding transaction still uses the ordinary WAL, OCC and
durability path exactly once when it commits.

This door accepts one updating query with no `RETURN`. Reads, DDL, `UNION` and writes that return
rows use `execute()` instead. It requires a write transaction even for an empty iterable, and all
existing statement, transaction-byte/row and final WAL-batch budgets continue to apply.

### Bounded physical scans

Integrations that need a lossless logical export can page stored rows without materializing an
`ORDER BY` query. The cursor stays inside one active read transaction, so every page observes the
same MVCC snapshot:

```python
with db.begin("read") as txn:
    cursor = None
    while True:
        page = txn.scan_rows_v1("Chunk", limit=256, cursor=cursor)
for row in page.rows:
            print(row.record_id, row.values)  # values follow TableDef.columns
        cursor = page.next_cursor
        if cursor is None:
            break
```

Relationship values start with `_from` and `_to`, and duplicate/parallel occurrences are returned
separately. `ScanCursorV1` is opaque, non-serializable, single-use and cannot cross a transaction,
table or database. This is a physical scan primitive for adapters, not a portable backup format
or bulk import API.

### Streaming query results

`execute()` remains the convenient materialised result. For a large read result, a query cursor
keeps one MVCC snapshot and detaches at most one bounded batch at a time:

```python
query = db.query("MATCH (c:Chunk) RETURN c.id, c.body")
with query.cursor(batch_size=256) as rows:
    for chunk_id, body in rows:
        consume(chunk_id, body)
```

`Query` copies its text and parameters when it is created and may open independent cursors.
`QueryCursor` accepts only read plans with `RETURN`; writes continue through `execute()` so early
cursor close can never commit a prefix. The cursor owns and releases its read transaction on EOF,
explicit `close()` or context-manager exit. It never retains a page pin or page-access section
between pulls, is not concurrently consumable, and bounds each iterator refill to `batch_size`
(default 256, hard maximum 65,536). The internal operators named by the plan can still be blocking;
streaming the terminal does not by itself make an unbounded sort, distinct or group bounded.

## Substituting an adapter

The registry checks protocols structurally. This minimal no-op metric implementation
demonstrates binding/lifetime, not a production metrics collector. Custom adapters
are trusted host code; exceptions may propagate without Grafx reclassification.

<!-- okto-grafx-doc-test -->

```python
from contextlib import nullcontext
from okto_grafx import DatabaseConfig, connect
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports

class CountingMetrics:
    """Any object with the MetricsSink members is a MetricsSink."""
    enabled = True
    def register(self, descriptor): ...
    def increment(self, name, value=1.0, labels=None): ...
    def observe(self, name, value, labels=None): ...
    def set_gauge(self, name, value, labels=None): ...
    def time(self, name, labels=None): return nullcontext()
    def snapshot(self): return {}

config = DatabaseConfig(path=":memory:", metrics="noop")
registry = build_default_registry(config)
registry.bind("metrics", CountingMetrics())
try:
    with connect(":memory:", registry=registry) as db:
        assert db.metrics.enabled
finally:
    release_ports(registry)  # Supplied registry remains caller-owned after db.close().
```

See [ports](PORTS.md) for the full adapter matrix and further executable examples.
