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
| `db.system_diff(before, after, *, tables, limits=..., max_changes=100_000)` | Bounded retained same-store schema/node/relationship/property diff |
| `tx.system_diff(...)` | Both pictures constrained to one owning read snapshot |

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

## System-time graph diff

`db.system_diff(activated, changed, tables=("Person",))` returns a frozen
`okto_grafx.temporal_diff.TemporalDiff`. For a caller-owned reader, use its
`system_diff` method or `diff_graph(reader, before, after, tables=...)` from that
module. Coordinates must be same-store `CommitId` values with `before <= after`.
Expired/not-yet-active tables refuse; this is not valid-time or bitemporal diff.

The result contains `before`, `after`, `schemas`, `rows`, `events_scanned` and
`encoded_bytes_scanned`. `TemporalSchemaChange(table, before, after)` preserves
native schemas even when tables are empty. `TemporalRowChange(table, table_kind,
record_id, operation, before, after, properties)` uses `added`, `removed` or
`updated` and contains `TemporalVersion` values or None. Equal-PK delete/recreate
is separate removed/added lineage, not an update. Endpoint property names are
native `_from` and `_to`, carrying source record IDs.

`TemporalPropertyChange(name, before_present, after_present, before, after)`
distinguishes an absent column from a present NULL and compares native typed bytes.
No row change is manufactured merely because an interval acquired a later closure.
Schema/row/property entries all count toward `max_changes`; the two captured
pictures share `TemporalLimits` input and row budgets. No partial diff is returned.
An identical pair still validates the requested historical picture. Schemas/rows
are ordered by native IDs and properties by name. No database mutation occurs.

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
`commit=None`. Pruning **does not shrink files or erase old WAL,
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
| `TemporalLimits.access_path` | `"auto"`; `auto`, `scan`, `index` | Select optional temporal tree or validated complete scan |
| `system_diff.max_changes` | 100,000; 1–1,000,000 | Aggregate schema/row/property output entries |
| Index build / compaction `max_bytes` | 16 MiB; 1–2 GiB | Complete capture bound, independent of native WAL/transaction quotas |
| Prune `max_bytes` | 16 MiB; 1–2 GiB | Complete history-file capture ceiling; not native transaction quota override |
| Native history batch | 4,096 events, 16 MiB encoded | Fixed format bound; activation includes schema events |
| One event | 64 KiB schema, 1 MiB values | Fixed format bound |

The `scan` path validates the complete retained chain before returning anything.
Its work is linear in retained events plus affected incident relationships. A
batch is independently bounded; decoding one batch can precede
the aggregate-budget refusal. A limit failure returns no partial answer.
History adds write amplification and retained storage, even when only a few tables
are enabled. Native transaction/WAL quotas still apply. There is no automatic
retention, valid time, bitemporal diff or public
embedding-space timeline. Vector row values preserve typed bytes/space identity;
this API does not restore retired embedding-space services at a past instant.

## Persistent temporal access path

Call `db.enable_system_history_index(max_bytes=16 * 1024 * 1024)` explicitly after
history activation. It returns True for a committed build, False when already
active. Activation fully validates/captures retained history and publishes a
copy-on-write authenticated B+tree in the **same native history file and COMMIT**.
Every subsequent writing commit maintains it, including schema/retention changes.
Keys are `(table ID, record ID, commit)`; record zero is historical schema, not a
heap reference. Closed intervals survive ordinary physical MVCC reclamation.

With `TemporalLimits(access_path="auto")`, eligible versions/as-of requests use
the qualified native root. A store without the capability uses the complete,
validated scan. `scan` explicitly requests that scan and cross-checks the entire
index against events when present; `index` refuses if not enabled. Corrupt/missing
enabled-index evidence never silently falls back or returns empty success.
Both paths retain the same journal pre/post publication observation, UUID,
snapshot, activation, horizon, schema and endpoint rules.

One-lineage versions read the relevant ordered range, not all batches. As-of
reconstruction seeks each selected lineage's predecessor at the target commit,
then jumps to the next lineage. Cost is proportional to selected historical
lineages and tree height/value bytes, **not just current output rows**: deleted
or future-born lineages still cost bounded candidates. It avoids decoding every
old update. No whole-history decode is required by eligible ordinary reads;
full verification/build/recovery are separate obligations, not an authority cache.

Every temporal read captures the current catalog chain within the same journal
observation, without replacing an older participant's schema view or caching
authority. This keeps activation/retention policy current even for a handle
opened before a foreign activation. Catalog page bytes share `max_bytes` with
history work; cost includes current schema metadata, independently of history size.
Indexed `max_bytes` charges actual visited page bytes (including repeat visits),
and `max_events` charges decoded events plus as-of lineage candidates. Thus scan
and index counters/costs need not match despite identical graph contents. Row
and output limits still apply. Activation and updates add data duplication and
copy-on-write storage/WAL cost. Keep the index disabled for rare temporal queries
or very tight write/storage budgets; use it for frequent lineage/as-of reads.
This is a one-way required bit **17**, `system_history_index_v1`, dependent on
system history. Old builds refuse; there is no automatic migration on connect.

## Quiescent physical history compaction

After explicitly pruning and stopping every other Grafx process:

```python
with connect("./history-example") as db:
    result = db.compact_system_history(confirm_quiescent=True,
                                       max_bytes=16 * 1024 * 1024)
    print(result.physical_bytes_reclaimed)
```

No local transaction may remain open. The confirmation is an operator assertion,
not a conclusion inferred from reader TTL. Compaction removes zero-padded expired
payload extents and, when indexed, rebuilds one compact temporal tree baseline
instead of retaining old copied search paths. It does **not** remove historical
schema/lineage/commit framing, advance horizons or release pins. It is foreground,
bounded maintenance, not online/no-pause compaction or secure erasure.

A complete native full-image COMMIT publishes the new logical extent; checkpoint
and validation precede physical truncation. Before COMMIT the old picture remains.
After COMMIT a crash may leave unused tail bytes, but the new qualified logical
extent remains authoritative and readable/recoverable. A subsequent explicit
compaction can finish that reclaim. WAL/backup/snapshot files are not erased.
Physical backup/restore carries the qualified extent and native capabilities.

`TemporalCompactionReport(commit, physical_bytes_before, physical_bytes_after,
physical_bytes_reclaimed)` is a frozen root export. A build that would not reduce
the file is a no-op (`commit=None`); a previously committed unused tail can still
be removed. First actual replacement sets required bit **18**,
`system_history_compaction_v1`. Future writes may reuse only space beyond the
qualified logical extent. Unproved future LSNs or foreign pages still refuse.
Budget, conflict, corruption, durability and uncertain-ACK failures propagate;
do not assume an exception means no COMMIT. Reopen/recover before retrying.

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
