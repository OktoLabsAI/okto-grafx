# Logical export and import

**Native node labels (0.0.6 development):** format 4 now preserves complete
per-version membership, including implicit, explicit-empty and multi-label sets,
through export/import, private batches, resumed prefixes and cold readback.
Source tests pass; final installed-wheel and paired-Pulse qualification are
separate checkpoints. See the [format and consumption contract](#node-label-artifact-format-4).

Development typed collection columns preserve their complete `stored_type` JSON
descriptor in schema manifests. Fresh and resumable imports validate native
rows against that exact descriptor before destination/workspace effects; no
rescaling decimals, filling missing stored STRUCT fields or discarding array
length is allowed. Stores without the label capability retain formats 1/2/3;
existing limits remain, and readers lacking this
column keyword must refuse. [Typed collection contract](specs/TYPED_COLLECTIONS_V1.md).

Automatic vector index names are reconstructed against the imported target's
physical identities. `TableDef.vector_identity_names` is a physical catalog
policy, not a logical manifest property; source and target may legitimately
choose different names while preserving space contracts, values and endpoints.
See [durable owner transport qualification](specs/VECTOR_OWNER_NAMES_V1.md).

**Independent graph namespaces (0.0.6 development):** when a node name overlaps
a physical or logical relationship name, export uses `okto-grafx-logical-3`
unless native labels require format 4.
Schema entries retain their kind and table ID; row objects bind source table IDs,
and custom-index declarations include `table_kind`. Formats 1/2 are not accepted
with overlapping names or format-3 index metadata. Other exports retain their
existing format. Old importers refuse the new format rather than guessing.
The [installed-wheel qualification](reports/LOGICAL_TRANSFER_WHEEL_QUALIFICATION.md)
tests actual archived 0.0.5/0.0.6 importers: ordinary/resumable format refusals
leave every artifact file and destination-parent entry unchanged. Format-1
backward import is qualified with their default 256-row batches, **not** arbitrary
batch sizes: both old importers have a reproduced tiny-batch identity-floor defect.
The current importer passes one-row batches; use it for bounded multi-batch imports.

Import and crash-resume inventories resolve `(kind, name, record_id)` independently,
and endpoints always resolve node identities. `RecordIdMapping.kind` is populated
with `"node"` or `"rel"` in every returned mapping; consumers must not key mappings
only by `(table, source_record_id)`. Target IDs are freshly allocated respecting
each table's durable identity floor and need not form a dense sequence. Resumption
still proves the exact committed prefix and supports retry after lost publication
acknowledgment. No namespace or ID spelling is rewritten in the source.

**0.0.6 flexible-model integration:** logical artifacts now preserve relationship
groups (typed or automatic), flexible node/edge property maps and empty node labels.
Without overlapping namespaces these artifacts use `okto-grafx-logical-2`;
ordinary typed, ungrouped exports retain format 1. Readers without format 2 refuse
before creating an import destination. Overlap takes precedence over format 2;
native label format 4 takes precedence over both.

**Native temporal properties (0.0.6 development):** all six
[temporal value families](TEMPORAL_VALUES.md) round-trip in typed columns, nested
ANY values and flexible node/edge properties. Typed temporal schema installation
activates catalog v2; stored values retain exact coordinates, nanos and recorded
offset/zone, without a timezone-provider lookup. Exact-index declarations rebuild
on the imported rows. This uses native value frames, not ISO string coercion, and
requires a reader that recognizes the new types/tags. It does not transfer system
history or imply compatibility with older wheels.

**Native DECIMAL properties (0.0.6 development):** typed node/relationship columns
export `decimal_precision` and `decimal_scale` in each DECIMAL column definition.
These fields are absent for other types; ordinary artifact schema bytes do not
gain null placeholders. Precision/scale, exact coefficient and nested ANY/flexible
metadata survive import, one-row batches, endpoint remapping, index rebuilding,
reopen and interrupted/resumed publication. Typed DECIMAL schema installation
activates catalog v2; native value admission publishes `decimal_values_v1` with
the relevant schema/row commit. There is no float/text conversion.

DECIMAL alone does not change the format-1/2/3 graph-model selection: this is
the new native DECIMAL type/tag 18, not a new graph-model format. **Use a reader
that supports DECIMAL**; the format number alone does not imply support for all
native types. Initial complete row/schema validation precedes creation of a private
database or resume workspace. Malformed frames and valid frames whose p/s differs
from a typed column refuse, even if a checksum is recomputed or exact rescaling
would fit. Import never normalizes incompatible stored metadata as an assignment.
The source artifact must remain trusted/immutable, as for all logical imports.
See [consumer evidence and remaining installed-reader qualification](reports/FP6_DECIMAL_CONSUMER_QUALIFICATION.md).

**0.0.6 temporal-source rule:** `export_graph(..., history="refuse")` is the
default. A database with native system-time history requires explicit
`history="current-only"`. The manifest still declares current-state-only; rows
and schema transfer, but temporal events, horizons and pins do not. No destination
is promoted on refusal. Use [physical backup](BACKUP_RESTORE.md) to preserve full
history for offline replacement; see [temporal operations](SYSTEM_TIME_HISTORY.md).

[Documentation index](README.md) · [API reference](API_REFERENCE.md) · [Physical backup](BACKUP_RESTORE.md)

Available in the **0.0.5 development source**, not a claim of a published release.
Use logical transfer to create a separately writable database or migrate logical
contents across supported physical layouts. Use physical backup/restore for an
offline replacement retaining the original UUID. Neither operation overwrites an
existing destination or silently repairs authoritative corruption.

## Python workflow

```python
from tempfile import TemporaryDirectory
from pathlib import Path
from okto_grafx import connect
from okto_grafx.transfer import export_graph, import_graph, TransferLimits

with TemporaryDirectory() as directory:
    root = Path(directory)
    with connect(root / "source") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Note(id INT64, body STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:Note {id:1, body:'durable graph'})")
        exported = export_graph(db, root / "package")
    imported = import_graph(root / "package", root / "fork")
    assert imported.source_database_uuid == exported.source_database_uuid
    assert imported.target_database_uuid != imported.source_database_uuid
    assert imported.rows == 1
    with connect(root / "fork") as fork:
        assert fork.execute("MATCH (n:Note) RETURN n.body").rows == (("durable graph",),)
```

`export_graph(database, destination, *, limits=None)` owns one read transaction.
`import_graph(source, destination, *, limits=None, resume_directory=None)` owns the private target and its
write batches. Both return frozen `TransferReport` values. Imports come from
`okto_grafx.transfer`; they are not instance methods or CLI subcommands.

Reports include destination, source UUID/snapshot LSN, target UUID (only on import),
table/row counts, artifact bytes and SHA-256 of the captured manifest. Import also
returns `record_id_mapping`: frozen `RecordIdMapping(table, source_record_id,
target_record_id, kind)` entries. Kind and table name qualify IDs; a record ID is
not your user PK.

## Node-label artifact format 4

When the source catalog requires `node_labels_v1`, export selects
`okto-grafx-logical-4` automatically, even if current nodes have empty or implicit
sets. No connection option or flag is needed. Earlier artifacts remain readable;
older importers that lack format 4 must refuse before creating a destination or
resume workspace. A graph format number does not imply support for every stored
property type; the reader must support those native types too.

Each node schema contains canonical `extra_node_labels` (a JSON array, possibly
empty); relationship schemas must not contain this field. These append-only
candidates are preserved as schema metadata, not substituted for any node's actual
membership. Custom indexes have explicit `table_kind`. Every object descriptor
contains boolean `node_labels`, true for node tables and false for relationships.
Unknown/missing flags, model mismatches and label metadata under an earlier format
refuse. The manifest is checksummed, not authenticated; do not edit it to downgrade.

Node rows retain the existing uint64-record-ID/uint32-length envelope. Format 4
adds one presence byte inside the payload: 0 precedes an ordinary value tuple for
implicit membership; 1 precedes a complete bounded GXL1 label prefix and then the
value tuple. Thus explicit empty remains distinct from a named table's implicit
base label. Case-sensitive Unicode is preserved without normalization. Relationship
rows retain their two unsigned endpoint IDs and property tuple, with no label
prefix. The presence byte and prefix count toward row/artifact byte bounds and
object digests. Invalid order, duplicates, malformed text, unadmitted names and
trailing bytes refuse even with recomputed checksums.

The importer installs captured candidates and native capability bit 28 through its
schema transaction before staging rows, including when candidates are empty.
Private batches preserve exact native membership metadata; this is not the
existing-target copy API's permitted implicit-representation normalization. Final
readback compares membership **and** remapped values. Resume performs the same
comparison for every committed prefix, so changing only a node's labels is not
accepted as an already imported row. Candidate/schema discrepancies also refuse.
The workspace lock, fresh target UUID, record/endpoint remapping, native WAL/OCC,
verification and no-replace directory promotion are unchanged.

Export still owns one committed snapshot. A concurrent membership update using
already admitted candidates does not leak newer labels into it; candidate growth
is schema change and causes `schema_changed` refusal before artifact promotion.
Retained history still requires `history="current-only"`; source historical
versions/pins are not transferred. Existing `TransferLimits` bound the same full
operation; this feature introduces no new configuration or unbounded retry.
See [source evidence and remaining package qualification](specs/NODE_LABELS_V1.md#fresh-store-transfer-format-4).

## What is preserved or remapped

| Contents | Logical import behavior |
| --- | --- |
| Schema | Node/relationship tables, declared types, nullability, PKs, endpoint table names and embedding spaces. Export of certified nullable-column evolution emits the complete current layout as destination schema version 1, without source physical layout history. Other unsupported schema versions still refuse. [Evolution contract](NULLABLE_COLUMNS.md). |
| Rows | Every snapshot-visible physical occurrence, including parallel edges, self-loops, empty tables, nulls, bytes, lists/maps, timestamps, UUID values and vector precision. Floating values travel through the tagged binary value codec, not lossy JSON numbers. |
| Node labels | Complete native per-version membership and candidate schema metadata via format 4. Implicit/explicit empty/nonempty states survive import and resume; labels do not change node or edge-endpoint identity. |
| Graph identity | A fresh database UUID. New numeric table/space IDs follow target allocation; current node/edge RecordIds are remapped and returned in the report. Relationship endpoints and nested vector space references are rewritten. User PKs, strings, UUID properties and arbitrary application references are **not** rewritten. |
| Embedding spaces | Names, dimensions, metric, precision, normalization declaration, original creation time and retired state. Import writes data through active-space validation, then restores retirement before publication. |
| Indexes | Automatic indexes reconstructed from schema. Active custom hash/ordered and full-text declarations recreated with fresh physical generations; FTS analyzer identity/weights preserved. Non-active custom declarations refuse rather than disappear. |
| Excluded | Physical pages, WAL, locks/readers, old generations, deleted/MVCC history, commit journal and past commit metadata, forensic/quarantine history, application configuration and runtime caches. The manifest explicitly says `current-state-only`; its source LSN is provenance, not target commit identity. |

Node/edge RecordIds are unsigned 64-bit identities. The existing native relationship
endpoint column currently accepts signed INT64; logical transfer does not expand
that heap contract. Application references outside native endpoints need the returned
mapping or stable application keys.

Hash directory bucket counts are restored explicitly. An original
`expected_cardinality` sizing hint is recorded as provenance in the artifact but
is not reinstated as the target index's hint; it does not change stored keys or results.

## Concurrency, publication and retry

Export streams pages of rows from one fixed reader snapshot; it does not hold the
commit fence throughout the copy. Writers may commit concurrently. A schema check
brackets snapshot acquisition and a fresh final schema observation detects concurrent
DDL. A changed logical schema causes refusal, not a mixed-schema package. The reader
pin remains until export finishes and can delay ordinary WAL/history reclamation.

Export writes to a private sibling, barriers each object, rereads the manifest and
all row streams, and only then promotes the complete artifact. Import first checks
the entire manifest and all object streams, imports nodes before relationships in
ordinary bounded transactions inside a private sibling, checks the streams again,
compares every target row against its expected remapped value/membership digest, runs
`verify('all')`, checkpoints, closes and verifies a read-only reopen. Only then is
the database directory promoted with the shared atomic **no-replace** operation.
WAL, both OCC checks, durability and ordinary constraints remain active during import.

A batch failure cannot expose a partial graph at the requested destination. Normal
exception cleanup removes only that attempt's private temporary directory; a process
crash can leave an `.incomplete-*` sibling. It is not a published database and should
not be used as a resume checkpoint. Inspect/remove only the abandoned attempt after
confirming its process is stopped. Source files and an existing destination remain
untouched. If publication completed but the caller did not receive the report,
inspect the destination before retrying; an existing directory is never overwritten.

Without `resume_directory`, retry means restart from the completed immutable artifact.
An artifact can be imported into several new directories;
each gets its own UUID. Export cannot resume an expired read cursor. Online merge
into an existing graph and in-place upgrades are not supported.

## Opt-in resumable import

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from okto_grafx import connect
from okto_grafx.transfer import export_graph, import_graph, TransferLimits

with TemporaryDirectory() as temporary:
    root = Path(temporary)
    with connect(root / 'source') as source:
        with source.begin() as tx:
            tx.execute('CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))')
            tx.execute('CREATE (:N {id:1})')
        export_graph(source, root / 'artifact')
    report = import_graph(root / 'artifact', root / 'target',
                          resume_directory=root / 'resume',
                          limits=TransferLimits(batch_rows=1))
    # The same call also recovers a lost publication acknowledgement.
    assert import_graph(root / 'artifact', root / 'target',
                        resume_directory=root / 'resume') == report
```

Choose a dedicated private workspace with an existing parent on the destination
volume. Source artifact, workspace and target must be disjoint, not ancestors of
one another. Keep all three trusted and the artifact immutable. The workspace lock
excludes a second importer; it does not change the database's multiwriter protocol.
Never open/write its private `database` directory from another program.

Retry the same source, destination and workspace after a process interruption or
ordinary error. Native WAL recovery decides which batches committed. The importer
revalidates every artifact stream and proves that existing rows, schema and index
declarations form exact source prefixes before appending missing rows. Fresh native
RecordIds may contain lease gaps after a crash; the returned mapping, not numeric
density, is authoritative for the copy. Nodes are mapped before endpoints. Resume
does not reinsert committed batches, but still performs O(artifact + imported rows)
validation and final verification. It is not constant-time restart or stream seeking.
Limits may change between attempts (for example a smaller batch) if the full
artifact still satisfies them. No target physical setting is inherited from the source.

The small `resume.json` contains format `grafx-resume-1`, artifact manifest hash,
absolute destination, source UUID, private target UUID, phase (`loading`/`ready`)
and publication LSN. It is atomically replaced after barriers, **not** a second
commit journal. COMMIT/WAL and exact readback prove rows. Incomplete, foreign,
duplicate-field or mismatched metadata refuses. A crash before the initial marker
exists leaves an ambiguous unpublished workspace: inspect/abandon it and choose a
new workspace; do not infer ownership and automatically delete it.
Finalization is idempotent for already-retired embedding spaces: it preserves the
retired state instead of retiring twice or reactivating it. An unexpectedly retired
target space whose artifact declares it active refuses before appending data.

Only verified, checkpointed and read-only-reopened stores are promoted atomically
without replacement. The marker/workspace remains after success. Repeating the call
can return the same report only when that target UUID, committed LSN and full content
still match; an unrelated or subsequently modified target refuses. Once the report
is accepted, close any importing process and manually remove only the identified
workspace if desired. Do not delete the published target, source or artifact.
No automatic cleanup, export resumption, merge into existing data, arbitrary
workspace relocation or authentication against a malicious filesystem owner is offered.

## Limits and errors

`TransferLimits` is per-operation, not a `connect` option:

| Field | Default | Meaning / use |
| --- | --- | --- |
| `max_bytes` | 268,435,456 | Total encoded row streams plus manifest. Raise explicitly for larger trusted artifacts after sizing storage/memory. |
| `max_rows` | 100,000 | Total rows across tables; bounds identity maps and validation work. |
| `max_row_bytes` | 8,388,608 | One encoded row; at most `max_bytes` and u32. Avoid oversized values/batches on constrained hosts. |
| `batch_rows` | 256 | Export scan page/import write batch; at most `max_rows`. Lower to reduce transaction retention, increase only after measuring write/quota cost. |

The manifest additionally has a fixed 4 MiB ceiling. Payload IO is streaming, but
schema, identity maps and expected row hashes remain in memory up to these bounds.
Import row batches still obey engine transaction/buffer limits. These are not RSS
caps or timing SLOs; peak memory includes Python objects,
temporary encodings and engine buffers. Imports use the current default target
physical configuration; they do not copy source connection settings.

Invalid limits raise `GrafxConfigurationError`; invalid artifacts/versions/checksums,
row counts, endpoints, schema races and verification failures raise typed refusals
(normally `GrafxRecoveryRefused`, with `reason`). Schema, storage, quota and index
errors preserve their existing typed taxonomy. Native host failures during final
directory publication can propagate as `OSError`, as in physical restore. Never
turn a refusal into a successful empty graph.

Malformed manifest low-level type/key/JSON/frame errors are normalized to
`GrafxRecoveryRefused` with `operation=logical_transfer`, `reason=artifact_invalid`
in both normal and resumable import. Preflight validates before creating a target
or resume workspace. This does not suppress existing Grafx typed errors or OS
failures. The [installed type-artifact qualification](reports/FP6_TYPE_WHEEL_QUALIFICATION.md#logical-artifacts-and-the-resumable-error-correction)
also records an archived importer's raw-exception limitation separately from the
current corrected contract; no lossy downgrade is used for unsupported schemas.

Checksums detect accidental changes, not a malicious author replacing both manifest
and objects. Keep the artifact in a trusted directory that other principals cannot
rewrite. No pickle or executable query text is loaded from an artifact.

## Artifact v1 format

`manifest.json` is UTF-8 JSON with unique keys and the exact top-level fields:
`format='okto-grafx-logical-1'`, `value_codec='grafx-value-v1'`, `source_uuid`
(32 lowercase hex digits), `snapshot_lsn`, `history='current-state-only'`, `schema`
and `objects`. Schema contains tables, spaces and custom index declarations; object
records carry table ID, canonical `rows/00000000.bin` path, bytes, rows and SHA-256.
Paths are generated in table order, never taken as arbitrary filesystem paths.
Unknown versions, extra top-level/object fields, duplicate keys, wrong coverage,
truncation, count/digest mismatch and trailing row payload bytes are refused.

Each object concatenates rows: little-endian `record_id:u64 | payload_length:u32 |
payload`. Node payloads contain exactly the schema arity of tagged Value-v1 values.
Relationship payloads start with `_from:u64 | _to:u64`, followed by tagged property
values (not a second encoding of endpoint columns). Value-v1 freezes existing type
tags, nested containers, UUID/timestamp and vector dtype/space reference encoding;
no page header, physical address or physical catalog serialization is included.

## Artifact v2: flexible models and relationship groups

Format 2 retains the exact top-level fields, Value-v1 row framing, checksum and
budget rules above. It adds explicit model metadata, not executable DDL:

- Flexible tables include exact booleans `flexible_properties` and `unlabeled`.
  Their native non-null `_properties` ANY map is preserved, including nested value
  tags. Missing/NULL-property and nonfinite-storage rules remain unchanged.
- When logical groups exist, `schema.relationship_types` is a list of objects
  with exactly `name` and `members`. `members` names the physical relationship
  tables, not source table IDs. Duplicate/missing members, node members, conflicting
  type names, repeated endpoint pairs or incompatible property/model schemas refuse
  through native catalog validation. Unknown fields and malformed containers refuse.
- Format 1 cannot carry these model fields/group authority. A version downgrade
  is refused; no silent interpretation of physical members as separate logical types.

Example group metadata: `{"name":"R","members":["_gx_rel_00000003","_gx_rel_00000005"]}`.
Physical table names are retained for the existing mapping/index contract, but
table IDs and record IDs are allocated in the fresh destination. Nodes precede
edges. Logical groups are attached using remapped member IDs through the native
schema journal in the same transaction as all imported table definitions. A late
group-attachment error rolls back that schema, not only the group. Row batches
remain private until complete readback, verification, checkpoint/reopen and promotion.

Stored type admission is validated during the initial artifact scan, before a
private database/workspace is created; a checksummed transient NaN value is not a
valid persisted property. Resume compares full logical membership and model flags,
in addition to the existing exact row-prefix/index proofs. Repeated imports receive
independent database identities. Imported automatic types can continue growing
endpoint pairs; explicit typed groups keep their closed schema constraints.

Upgrade order: use a reader that supports format 2 before importing the new
artifacts. Pre-release development artifacts that placed flexible flags in format
1 must be re-exported with this build; relabeling their version is not an upgrade
procedure. Ordinary format-1 artifacts remain supported. No artifact is modified
in place by import.

This copies **current state**, not retained history or copy/promotion receipts into
an existing catalog. No new tuning parameter or change to reader/writer concurrency,
WAL/durability or destination-overwrite rules is introduced. Evidence is in
`tests/api/test_flexible_graph_transfer.py` and the typed/flexible real-process
crash cuts in `tests/api/test_transfer_resume.py`.
