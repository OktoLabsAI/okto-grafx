# Native system-time history (0.0.6 development)

Opt-in history stores committed row versions separately from recyclable physical
MVCC. Current data, temporal events, schema changes and the commit journal publish
in **one native durable COMMIT**. Both OCC checks, independent participants,
writer fencing, WAL-before-pages and fail-closed recovery remain mandatory.
This is system/transaction time, not valid time or full bitemporal support.

## Enable and query

```python
from okto_grafx import connect, CommitId, TemporalLimits

with connect("./history-example") as db:
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
        tx.execute("CREATE (:Person {id:1, name:'Ada'})")
    db.ensure_identity_indexes()
    db.enable_commit_history()
    db.enable_system_history(("Person",))
    activated = db.commit_history().entries[-1].identity
    initial = db.system_as_of(activated, tables=("Person",))
    rid = initial.rows[0].record_id
    with db.begin() as tx:
        tx.execute("MATCH (p:Person {id:1}) SET p.name='Ada Lovelace'")
    changed = CommitId(db.identity.database_uuid, tx.report.csn)
    assert db.system_as_of(activated, tables=("Person",)).rows[0].values == (1, "Ada")
    versions = db.system_versions("Person", rid)
    assert versions.versions[0].system_to == changed
    instant = db.lookup_commit(changed).timing.ordered_at
    assert db.system_as_of(instant, tables=("Person",)).as_of == changed
```

`enable_system_history(tables)` is explicit, idempotent and one-way. It captures
the existing table rows and schemas in the activation commit, not earlier history.
It requires commit history and catalog identity indexes first. Enable relationship
tables together with both endpoint tables, or after those tables. The bounded
baseline refuses intact when it exceeds the limits below; there is no automatic
partial activation or background backfill. Every later writing COMMIT appends a
batch (possibly empty); read-only/no-op transactions do not create fake commits.

## Read contracts

| API | Meaning |
| --- | --- |
| `db.system_as_of(at, *, tables, limits=TemporalLimits())` | Complete historical graph rows/schema for a qualified `CommitId` or native `Timestamp` |
| `tx.system_as_of(...)` | Same operation, constrained to the owning transaction's committed snapshot |
| `db.system_versions(table, record_id, *, limits=...)` | Ordered retained intervals for one source-store physical row lineage |
| `tx.system_versions(...)` | No future version/closure beyond the owning snapshot; excludes private write intents |

Intervals are `[system_from, system_to)`; `None` is the unclosed end visible to
that read boundary. Delete closes a lineage; recreating an equal PK creates a
different record ID. Relationships keep endpoint **record identities**, not an
automatic link to later rows with equal PKs. As-of relationship selections require
their endpoint tables in `tables`; relationship versions can be requested alone.
Nullable-column additions retain prior schemas; old current rows widen virtually
with NULL at later schema coordinates. Row values remain native typed tuples.

`Timestamp` resolves the greatest tracked commit whose monotonic `ordered_at` is
at/before the request, subject to the read snapshot. This is not the host wall
clock or a promise of strict serializability. Use qualified `CommitId` for exact
replay, provenance and cross-process bookmarks. UUIDs from another store refuse.

Frozen results: `TemporalGraph(as_of, schemas, rows, events_scanned,
encoded_bytes_scanned)`, `TemporalVersion(table, table_id, record_id, values,
schema_version, system_from, system_to)`, and `TemporalVersions(read_commit,
table, record_id, versions, activation, retained_from)`. Schemas are native
`TableDef` objects. DTOs are exported at `okto_grafx`; errors at
`okto_grafx.errors`. These Python methods do not add Cypher time-travel syntax.

## Retention and explicit protection

```python
with connect("./history-example") as db:
    db.pin_system_history("audit-export", activated, tables=("Person",))
    assert db.system_history_pins()[0].name == "audit-export"
    # Pruning beyond activated now refuses. Release only when the consumer is done:
    db.unpin_system_history("audit-export")
    report = db.prune_system_history(changed, tables=("Person",), max_bytes=16 * 1024 * 1024)
    assert report.physical_bytes_reclaimed == 0
```

Pins are durable named metadata with **no TTL**. Repeating an identical name,
coordinate and table set is a no-op; rebinding that name refuses. Unpinning a
missing valid name is a no-op. Names contain 1–128 UTF-8 bytes; at most 1,024 pins
exist. `system_history_pins()` returns sorted frozen `TemporalPin(name, at,
tables)` values. Pins protect logical retained horizons, not WAL or MVCC recycling.
An ordinary read transaction alone is not a durable temporal retention pin.

Pruning advances selected table horizons monotonically. It refuses if any named
pin protects an earlier selected coordinate. Closed versions ending at/before
the horizon have their payload bytes zeroed atomically; current versions,
schemas, record identities, intervals and event framing remain. It uses a bounded
whole-history rewrite plus an empty maintenance batch in the same WAL COMMIT.
Concurrent publications can conflict; retry explicitly after re-reading state.

`TemporalPruneReport(before, tables, commit, redacted_versions, redacted_bytes,
physical_bytes_reclaimed)` reports only newly redacted payloads. A no-op has
`commit=None`. This implementation **does not shrink files or erase old WAL,
backups, filesystem snapshots or physical MVCC copies**. It is not secure erasure
or online compaction. Do not enable it merely to reclaim disk space. Consumers
needing older versions must pin them before an operator advances the horizon.

## Configuration and bounded cost

No new connection option or environment default is introduced.

| Setting | Default / bound | Effect |
| --- | --- | --- |
| `TemporalLimits.max_events` | 100,000; 1–10,000,000 | Aggregate historical events inspected |
| `TemporalLimits.max_bytes` | 64 MiB; 1–2 GiB | Aggregate encoded batch input, not RSS |
| `TemporalLimits.max_rows` | 100,000; 1–1,000,000 | Retained live rows plus collected versions |
| Prune `max_bytes` | 16 MiB; 1–2 GiB | Complete history-file capture ceiling; not native transaction quota override |
| Native history batch | 4,096 events, 16 MiB encoded | Fixed format bound; activation includes schema events |
| One event | 64 KiB schema, 1 MiB values | Fixed format bound |

Reads validate the complete retained chain before returning anything. Work is
linear in retained events plus affected incident relationships, not indexed
time-travel. A batch is independently bounded; decoding one batch can precede
the aggregate-budget refusal. A limit failure returns no partial answer.
History adds write amplification and retained storage, even when only a few tables
are enabled. Native transaction/WAL quotas still apply. There is no automatic
retention, secondary temporal index, valid time, bitemporal diff or public
embedding-space timeline. Vector row values preserve typed bytes/space identity;
this API does not restore retired embedding-space services at a past instant.

## Operations, errors and upgrades

`verify('all'|'pages'|'records')` includes history chain/interval checks and current
row agreement; unreadable or over-budget verification is a finding, not a clean
empty result. `verify('indexes')` does not verify temporal history.
[Physical backup/restore](BACKUP_RESTORE.md) includes history and durable pins,
retaining UUIDs under the existing original-offline requirement. Recovery validates
native commit lineage and exact page images before application; uncertain ACKs
retain the usual reopen/recovery protocol.

[Logical export](LOGICAL_TRANSFER.md) and [catalog copy](CATALOG_COPY.md) refuse
temporal source tables unless explicitly passed `history="current-only"`. This
exports current rows/schema only, with fresh target row identities and no invented
history, horizons or pins. Use physical replacement backup for full preservation.

`GrafxHistoryUnavailable`: coordinate/table was not tracked or activated.
`GrafxHistoryExpired`: coordinate precedes the retained horizon.
Both codes are admitted by the bounded `oktografx_query_errors_total{code=...}`
metric label; an operator can distinguish unavailable from expired history
without treating either as an empty successful query.
`GrafxConfigurationError`: invalid identities, selections, pins or horizon rules.
Native budget, conflict, corruption and retryable publication-change errors remain
distinct; do not convert them to an empty graph or automatically discard data.

Required catalog bit 15 (`system_history_v1`) and qualified WAL-v2 history flags
make unsupported readers/writers refuse. No downgrade is supported. The
[native format](specs/SYSTEM_HISTORY_V1.md) documents framing and recovery; the
[0.0.6 matrix](V006_COMPATIBILITY.md) records actual upgrade evidence separately
from planned platform CI.
