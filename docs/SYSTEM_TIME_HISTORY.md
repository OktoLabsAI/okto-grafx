# Native system-time history (0.0.6 development)

Native [multiple-label history](specs/NODE_LABELS_V1.md#native-retained-history-integration)
preserves per-version membership through activation, COMMIT, scan/index reads,
retention, recovery and physical backup/restore. Label-only changes create a
historical row version on the same identity; property-only changes inherit the
actual native membership. The earlier temporary history refusals are removed.
This is development-source support, not final package/Pulse qualification.

Typed LIST/MAP/ARRAY/STRUCT schemas retain full descriptors in historical schema
images. Native values, nullable append, updates, deletion/recreation, indexed/scan
time travel and physical backup/restore keep their existing retention and identity
rules. This does not synthesize history from before activation or add valid-time
semantics. [Type and format contract](specs/TYPED_COLLECTIONS_V1.md).

Opt-in history stores committed row versions separately from recyclable physical
MVCC. Current data, temporal events, schema changes and the commit journal publish
in **one native durable COMMIT**. Both OCC checks, independent participants,
writer fencing, WAL-before-pages and fail-closed recovery remain mandatory.
This is system/transaction time, not valid time or full bitemporal support.

The six [native temporal property families](TEMPORAL_VALUES.md) are retained as
values, including nested ANY/flexible maps, wide years, nanoseconds and recorded
zone identities. As-of/diff reads using scan or the history index preserve old/new
values after reopen and physical backup/restore. A temporal property is not itself
a system-history timestamp and does not change commit ordering or retention policy.

Native [DECIMAL values](specs/DECIMAL_VALUES_V1.md) also retain exact coefficient,
precision and scale in typed columns and nested ANY/flexible properties. Historical
schemas retain decimal column parameters, including a nullable column appended
after activation: older rows remain NULL under that later schema, while earlier
as-of reads retain their original schema. Updates, deletion/recreation lineage,
as-of/version/diff reads, scan/index paths and physical backup/restore preserve
these distinctions. Real-process cuts before COMMIT and after durable COMMIT but
before page application test that current rows and history recover to one outcome.
No history setting or retention/ordering policy changes; this is property-value
support, not valid-time semantics. [Qualification](reports/FP6_DECIMAL_CONSUMER_QUALIFICATION.md).

## Enable and query

For physical tables with the same name, qualify each selector with its kind:
`tables=(("node", "R"), ("rel", "R"))`. This syntax is accepted by
`enable_system_history`, `system_as_of`, `system_diff`, `pin_system_history` and
`prune_system_history`; `system_versions(("rel", "R"), record_id)` selects one
relationship lineage. Unique physical names remain valid strings. Duplicate
resolved selections and ambiguous bare names refuse; full historical graph reads
still require both endpoint node tables.

`TemporalPin.tables` returns qualified pairs for ambiguous names and strings for
unique names, so its inventory can be passed back as a selection. Historical row
identity remains `(table_id, record_id)`, never `table` name alone. Both scan and
index paths, endpoint reconstruction, pins, pruning, compaction and verification
use kind-aware resolution. No history event format or retention policy changes:
catalog capability `graph_namespaces_v1` already fences old readers/writers.

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
| `db.system_diff(before, after, *, tables, limits=..., max_changes=100_000)` | Bounded retained same-store schema/node/relationship/property/label diff |
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
encoded_bytes_scanned, relationship_types)`, `TemporalVersion(table, table_id, record_id, values,
schema_version, system_from, system_to, logical_type, node_labels)`, and `TemporalVersions(read_commit,
table, record_id, versions, activation, retained_from)`. Schemas are native
`TableDef` objects. DTOs are exported at `okto_grafx`; errors at
`okto_grafx.errors`. These Python methods do not add Cypher time-travel syntax.

### Historical node labels

`TemporalVersion.node_labels` is immutable native membership metadata, matching
the public raw scan contract. `None` means implicit membership: the historical
physical table name for a normal node table, or no labels for an `unlabeled`
table. `()` means an explicitly empty set even in a named table. A nonempty tuple
is the complete canonical case-sensitive set; do not add the physical table name
to it. Relationships always have `node_labels=None` and retain their own type.
Resolve the implicit case using the matching `TemporalGraph.schemas` entry by
`table_id`, not a current logical-label query or catalog candidate union.

```python
def historical_labels(version, graph):
    schema = next(s for s in graph.schemas if s.table_id == version.table_id)
    if schema.kind != "node":
        return ()
    if version.node_labels is not None:
        return version.node_labels
    return () if schema.unlabeled else (schema.name,)
```

Label candidate growth is append-only historical schema metadata; it does not
change table/record identity or increment the physical tuple schema version.
Appending a nullable property still increments that schema version. Neither
operation rewrites earlier membership or rebinds relationship endpoints.
Old reader transactions see no future version or closure; temporal methods on a
write transaction still exclude its private intents. Idempotent label changes
create no extra row version. Delete/recreate retains separate lineages even for
the same PK. Complete label frames count toward history and transaction quotas.

## System-time graph diff

For flexible entities, row values retain the native `_properties` map slot. A
property-map update is represented losslessly as a change to that slot, not as
flattened per-key changes; user keys cannot collide with physical edge endpoints.

`db.system_diff(activated, changed, tables=("Person",))` returns a frozen
`okto_grafx.temporal_diff.TemporalDiff`. For a caller-owned reader, use its
`system_diff` method or `diff_graph(reader, before, after, tables=...)` from that
module. Coordinates must be same-store `CommitId` values with `before <= after`.
Expired/not-yet-active tables refuse; this is not valid-time or bitemporal diff.

The result contains `before`, `after`, `schemas`, `rows`, `events_scanned` and
`encoded_bytes_scanned`. `TemporalSchemaChange(table, before, after)` preserves
native schemas even when tables are empty. `TemporalRowChange(table, table_kind,
record_id, operation, before, after, properties, labels_added, labels_removed)` uses `added`, `removed` or
`updated` and contains `TemporalVersion` values or None. Equal-PK delete/recreate
is separate removed/added lineage, not an update. Endpoint property names are
native `_from` and `_to`, carrying source record IDs. `labels_added` and
`labels_removed` are canonical tuples, empty for relationships. They compare
actual logical membership, including implicit/explicit empty states, not physical
owners. A label-only mutation is `updated` with `properties=()`; additions and
removals report the labels of the added/removed identity respectively.

`TemporalPropertyChange(name, before_present, after_present, before, after)`
distinguishes an absent column from a present NULL and compares native typed bytes.
No row change is manufactured merely because an interval acquired a later closure.
Schema/row/property entries and each added/removed label count toward `max_changes`; the two captured
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
An expired version's explicit membership payload is redacted with its properties.
A structural marker preserves the original event framing, including for explicit
empty sets; it does not retain the erased membership. Historical schema/candidate
names remain metadata, so pruning must not be treated as erasing label names from
the database. Compaction preserves this distinction and the retained versions.
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
| `system_diff.max_changes` | 100,000; 1–1,000,000 | Aggregate schema/row/property entries and individual added/removed labels |
| Index build / compaction `max_bytes` | 16 MiB; 1–2 GiB | Complete capture bound, independent of native WAL/transaction quotas |
| Prune `max_bytes` | 16 MiB; 1–2 GiB | Complete history-file capture ceiling; not native transaction quota override |
| Native history batch | 4,096 events, 16 MiB encoded | Fixed format bound; activation includes schema events |
| One event | 64 KiB schema, 1 MiB values | Fixed format bound, including encoded candidate/membership metadata |

The `scan` path validates the complete retained chain before returning anything.
Its work is linear in retained events plus affected incident relationships. A
batch is independently bounded; decoding one batch can precede
the aggregate-budget refusal. A limit failure returns no partial answer.
History adds write amplification and retained storage, even when only a few tables
are enabled. Native transaction/WAL quotas still apply. There is no automatic
retention, valid time, bitemporal diff or public
embedding-space timeline. Vector row values preserve typed bytes/space identity;
this API does not restore retired embedding-space services at a past instant.

## Flexible models and historical relationship groups

Opted-in flexible tables retain their `TableDef.flexible_properties` and
`TableDef.unlabeled` flags in historical schemas. Grouped relationships retain the
logical type name in every historical schema/row/delete/redaction event. This is
stored temporal metadata, not a name inferred from today's physical table names.
`TemporalVersion.logical_type` is the grouped relationship name, or None for nodes
and ordinary ungrouped relationships. `TemporalGraph.relationship_types` contains
`RelationshipTypeDef(name, table_ids)` for **selected historical members**, including
empty members. It is not an inventory of unselected tables or future group members.
Both new DTO fields have empty defaults for ordinary historical results.

Enable history using physical table names, including the endpoint tables:

```python
with connect("./flex-history") as db:
    with db.begin("write") as tx:
        tx.execute("CREATE(a {v:'start'})-[:R {v:1}]->(b {v:2})")
    db.enable_commit_history()
    tables = tuple(t.name for t in db.catalog.catalog.tables())
    db.enable_system_history(tables)
    at = db.commit_history().entries[-1].identity
    graph = db.system_as_of(at, tables=tables)
    assert graph.schemas[0].unlabeled
    assert graph.relationship_types[0].name == "R"
```

As before, opt-in is per physical table. Growing a flexible relationship group
does not automatically activate history for its new member or backfill earlier
commits. Explicitly enable the new member and its endpoints. Asking for it at a
pre-activation coordinate refuses. Querying the prior selected tables preserves
the old membership instead of borrowing the current expanded group.

### Durable model admission and compatibility

Activating history for a flexible/grouped table requires catalog-v2 bit **22**,
`system_history_models_v1`, dependent on system-history bit 15. Native activation
adds it in the same durable COMMIT as the baseline. Unknown readers/writers refuse
the catalog before applying recovery effects; supporting flexible graph bit 21
alone is insufficient. Ordinary typed, ungrouped history keeps its existing bytes
and does not activate this capability.

Historical schema bytes retain the table definition and u16 prior-layout count
followed by `(u16 version, u16 arity)` pairs. When model metadata exists, the schema
ends with `GXHM01 | flags:u8 | name_length:u16 | name:ASCII`, little-endian lengths.
Flag bit 0 means flexible properties; bit 1 means unlabeled. The optional name is
the logical group name (1–128 bytes), never the physical member name. Unknown
flags, wrong lengths, invalid model/name combinations, redundant empty trailers,
noncanonical bytes and incompatible event/model transitions refuse. Existing
schema/event/batch byte bounds include this metadata. Redaction and compaction
preserve it; both scan and indexed reads expose the same model and lineage.

Recovery and public reads compare immutable model metadata against qualified
catalog authority without filling missing fields from that catalog. Experimental
histories written before this fix may have lost model information; they are not
silently upgraded. Flexible/grouped history without bit 22 refuses catalog admission.
Do not manually set that bit or rewrite a trailer: missing historical evidence
needs an explicitly reviewed restore/migration, not guessed reconstruction.

Physical backup/restore preserves this history and pins. Logical transfer remains
explicit `history="current-only"` and carries current model/groups, not historical
events. Whole-statement rollback, independent snapshots/OCC, WAL-before-pages and
the single durable publication remain unchanged. There is no new tuning flag.

## Persistent temporal access path

Historical schemas also retain `TableDef.vector_identity_names` when automatic
vector indexes use physical owner names (catalog capability 25). Such metadata
uses `GXHM02` with flag bit 2 set; otherwise existing `GXHM01` bytes are unchanged.
Decode validates exact framing, canonical encoding and the immutable catalog
model. Missing evidence is corruption, not inferred from an index filename.
This adds no history configuration toggle. See [format and recovery qualification](specs/VECTOR_OWNER_NAMES_V1.md).

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
