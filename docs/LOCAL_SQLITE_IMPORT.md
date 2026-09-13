# Bounded SQLite ingestion (0.0.6)

Collection-root `StoredType` entries are accepted in `types` alongside scalar
names. Their cells require SQL TEXT containing [exact collection JSON](COLLECTION_JSON.md),
or SQL NULL subject to root nullability. No BLOB/INTEGER/REAL inference is allowed.
Descriptors are privately cloned; malformed nested values fail while reading the
bounded source, before Grafx staging. Native encoded collection bytes additionally
charge the existing max_work allowance. No SQLite write or distributed transaction
is introduced.

[Documentation index](README.md) · [API reference](API_REFERENCE.md)

This is a local ingestion adapter, **not SQL federation, CDC or a distributed
transaction**. A native SQLite SELECT runs read-only with one statement snapshot;
all accepted rows are detached and the source connection is closed before Grafx
atomic staging starts. It never commits the caller's Grafx transaction.

```python
from okto_grafx import connect
from okto_grafx.sqlite_import import import_sqlite, SQLiteImportLimits

with connect('./graph') as db:
    with db.begin('write') as tx:
        tx.execute('CREATE NODE TABLE Item(id INT64, body STRING, PRIMARY KEY(id))')
    with db.begin('write') as tx:
        report = import_sqlite(
            tx, 'CREATE (:Item {id:$id, body:$body})', 'source.sqlite',
            allowed_root='/absolute/local/imports',
            query='SELECT id, body FROM items WHERE id >= ? ORDER BY id',
            parameters=(1,), columns=('id', 'body'), types=('INT64', 'STRING'),
            limits=SQLiteImportLimits(max_rows=10000),
        )
```

For inspection without Grafx use `read_sqlite_rows(...)`, returning a tuple of
plain parameter dictionaries. `import_sqlite` returns the existing
`ExecuteManyReport`. The SQL column names/order must match `columns` exactly;
1..256 unique names, with one declared type each. SQL parameters are a tuple of
at most 256 builtin scalar values; no connection, extension or host callbacks are
accepted. NULL is always `None`, never a magic string.

| Declared type | SQLite source contract |
| --- | --- |
| INT64 | INTEGER in signed 64-bit range; no text coercion |
| DOUBLE | finite REAL or exactly convertible INTEGER |
| BOOL | INTEGER 0/1 only |
| STRING | TEXT, bounded UTF-8 |
| BYTES | BLOB, not base64 text |
| UUID | canonical lowercase UUID text, converted to Grafx Uuid |
| TIMESTAMP | INTEGER UTC microseconds, converted to Grafx Timestamp |
| DECIMAL | TEXT containing the canonical `{"type":"decimal","coefficient":"1234500","precision":12,"scale":4}` object; exact 123.4500 |
| DATE / LOCALTIME / TIME / LOCALDATETIME / DATETIME / DURATION | TEXT containing the corresponding explicit JSON temporal tag object |

Temporal values use the [exact tagged field grammar](LOCAL_TEXT_IMPORT.md#native-temporal-fields-006-development),
not SQLite date functions, host adapters or string inference. For example a DATE
TEXT cell is `{"type":"date","epoch_day":"19782"}`; its declared type must be
`DATE`. SQL NULL remains NULL; the text `null`, bare ISO date, BLOB, extra/missing
fields, duplicate JSON keys, wrong tags and noncanonical coordinates refuse.
Wide coordinates, nanosecond precision and recorded zone/offset identity survive
without timezone lookup. Only DATETIME's `zone` coordinate may itself be NULL.
No source schema inference, date affinity or automatic Grafx table creation is added.

DECIMAL uses the [same exact tagged grammar as CSV/JSONL](LOCAL_TEXT_IMPORT.md#native-decimal-fields-006-development).
Declare `types=("DECIMAL",)` for that column. Native coefficient, precision and
scale are retained; SQLite INTEGER/REAL, numeric-affinity inference, untagged
decimal text and host adapters are not implicit DECIMAL conversions. Use TEXT to
avoid prior SQLite numeric-affinity rounding. SQL NULL stays None. Typed Grafx
targets still require an exact assignment to their declared p/s; ANY retains the
offered metadata. There is no source/target schema inference or new configuration.

All temporal/decimal/collection decoding finishes while reading the bounded SQLite selection. A later
invalid cell closes the source connection and fails before any of this import's
Grafx staging; previously staged caller statements remain unchanged. Native target
schema/range/transaction checks still run when the validated rows are staged.

`SQLiteImportLimits` defaults: `max_rows=10000`, `max_bytes=16777216`,
`max_field_bytes=65536`, `max_work=1000000`. All are positive integers <=2^31;
field size is additionally <=65536. Byte accounting includes encoded values,
column names and fixed per-field overhead, not a guaranteed process RSS ceiling.
`max_work` charges SQLite VM instructions in quanta of 100 plus row/field handling;
staging cancellation uses a separate max_work allowance, while Grafx's normal
transaction/query budgets still apply. SQL length is <=65536, width <=256, source
lock wait is 1 second. Limits cause typed refusal, never truncated import success.
Cooperative `CancellationToken` is checked during reading and staging; it cannot
preempt every blocked filesystem call or cancel an already durable commit.

The source file must already exist directly within explicit `allowed_root`.
Traversal, UNC paths and detected links are refused using the existing local-file
guard. The local directory must remain operator-controlled: path checks are not
a sandbox against hostile concurrent filesystem namespace replacement. SQLite
handles its own journaling/locks; no `immutable=1` shortcut is used. Read-only SQL
does not promise an absence of SQLite WAL shared-memory coordination artifacts.
Only SELECT/READ/recursive operations and a small safe builtin function allowlist
are admitted (`count,min,max,sum,avg,coalesce,ifnull,lower,upper,trim,abs,length`).
Writes, ATTACH, PRAGMA queries, extension loading and arbitrary functions refuse.
No source schema or rows are intentionally modified.

Read/type/budget failures happen before any staging. Native `executemany` retains
whole-call atomicity: a failed row discards this import's intents while preserving
earlier transaction work. The caller still decides commit/rollback. A later retry
may see a newer SQLite snapshot; there is no atomicity across both databases.
For larger transfers use explicit application checkpoints/idempotency rather than
silently increasing limits or partially committing a supposedly atomic call.
