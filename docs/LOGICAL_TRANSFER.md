# Logical export and import

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
target_record_id)` entries. Table names qualify IDs; a record ID is not your user PK.

## What is preserved or remapped

| Contents | Logical import behavior |
| --- | --- |
| Schema | Node/relationship tables, declared types, nullability, PKs, endpoint table names and embedding spaces. Export of certified nullable-column evolution emits the complete current layout as destination schema version 1, without source physical layout history. Other unsupported schema versions still refuse. [Evolution contract](NULLABLE_COLUMNS.md). |
| Rows | Every snapshot-visible physical occurrence, including parallel edges, self-loops, empty tables, nulls, bytes, lists/maps, timestamps, UUID values and vector precision. Floating values travel through the tagged binary value codec, not lossy JSON numbers. |
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
compares every target row against its expected remapped value digest, runs
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
