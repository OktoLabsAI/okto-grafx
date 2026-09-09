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
`import_graph(source, destination, *, limits=None)` owns the private target and its
write batches. Both return frozen `TransferReport` values. Imports come from
`okto_grafx.transfer`; they are not instance methods or CLI subcommands.

Reports include destination, source UUID/snapshot LSN, target UUID (only on import),
table/row counts, artifact bytes and SHA-256 of the captured manifest. Import also
returns `record_id_mapping`: frozen `RecordIdMapping(table, source_record_id,
target_record_id)` entries. Table names qualify IDs; a record ID is not your user PK.

## What is preserved or remapped

| Contents | Logical import behavior |
| --- | --- |
| Schema | Node/relationship tables, declared types, nullability, PKs, endpoint table names and embedding spaces. Immutable schema version 1 is supported; other versions refuse. |
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

Resumption is **restart from a completed immutable artifact**, not mid-stream or
mid-import continuation. An artifact can be imported into several new directories;
each gets its own UUID. Export cannot resume an expired read cursor. Online merge
into an existing graph and in-place upgrades are not supported.

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
caps, resumability guarantees or timing SLOs; peak memory includes Python objects,
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
