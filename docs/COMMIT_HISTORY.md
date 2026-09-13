# Durable commit history (0.0.5 development)

Commit provenance alone does not retain graph row versions. The separate
0.0.6 [system-time history](SYSTEM_TIME_HISTORY.md) capability now provides
opt-in native row history, typed as-of/versions, pins and retention on top of this
journal. It is a one-way required-capability upgrade, never activated implicitly.

[API reference](API_REFERENCE.md) · [Roadmap](../ROADMAP.md)

This capability is opt-in. Existing databases and ordinary connections do not
automatically activate it. It adds append-only journal IO and storage to each
writing commit; it is provenance, not a performance optimization. The overall
N4 milestone has [local regression and integration evidence](reports/V005_N3_N4_ACCEPTANCE.md).
Do not infer release or cross-platform certification from these development APIs.

## Activation and consumption

```python
from okto_grafx import connect, CommitId, CommitMetadata

with connect("./graph") as db:
    db.ensure_identity_indexes()
    db.enable_commit_history()  # explicit, one-way; repeating is a no-write no-op
    with db.begin("write", metadata=CommitMetadata(
        actor="my-app:indexer", origin="my-app", reason="create schema",
        correlation_id="run-123", attributes={"batch": 1},
    )) as tx:
        tx.execute("CREATE NODE TABLE Item(id INT64, PRIMARY KEY(id))")

    assert tx.report.wrote and tx.report.durable
    identity = CommitId(db.identity.database_uuid, tx.report.csn)
    info = db.lookup_commit(identity)
    assert info.metadata.actor == "my-app:indexer"

    with db.begin("read") as reader:
        page = reader.commit_history(limit=100)
        while True:
            for entry in page.entries:
                print(entry.identity.to_token(), entry.kind, entry.timing.ordered_at)
            if not page.has_more:
                break
            page = reader.commit_history(after=page.entries[-1].identity, limit=100)
```

`db.transaction(..., metadata=...)` also accepts metadata. `db.execute()` remains
a **read-only convenience**; use a writing transaction to mutate data. The example
creates its schema once; it is not an idempotent initialization script.

## Guarantees and boundaries

- Metadata is captured and revalidated before begin IO, then published atomically
  with data. Caller mutation after begin cannot alter the captured bytes; a
  `db.retry(tx)` successor carries them. Canonical bytes define the value, not
  host-mutated display fields. Unsupported types/encodings refuse.
- A read transaction refuses metadata. No-write transactions and rollback create
  no journal record, even when metadata was supplied. Do not construct a new
  commit identity from a report unless `wrote` and `durable` are true.
- The activation commit is an explicit legacy boundary. No actor, time or record
  is invented for earlier commits. `activation_sequence` on every history page
  identifies this boundary. `None` from lookup is absence **in tracked history and
  this snapshot**, not proof that an earlier legacy operation never happened.
- Identities contain the database UUID and COMMIT sequence. They survive WAL
  segment rolls, recovery and checkpoint/recycling. Cross-store lookup refuses;
  ordering unrelated stores is undefined. Sequence gaps are valid.
- `observed_at` is sampled wall clock; `ordered_at` advances monotonically by at
  least one microsecond. Ties/regressions set `clock_adjusted`. Overflow refuses
  before durable publication. These APIs do not implement graph time travel.
- History pages are ascending, limited to **1..1000** entries, and return
  `read_sequence`, `activation_sequence`, `entries` and `has_more`. Paging on one
  read transaction keeps the same snapshot. Separate `db.commit_history()` calls
  open new snapshots and may include intervening commits.
- Lookup uses O(log N) directory seek; paging seeks then decodes at most limit+1
  records. Maximum metadata can make a 1000-entry page large: choose smaller pages
  when memory matters. Full verification scans all history, with bounded working
  memory. There is no automatic retention/truncation of the audit journal.
- Reads take no new global reader/writer lock. Each observation proves qualified
  current publication before and after reading. Pages beyond publication are
  never accepted. Four changing/in-flight observations produce retryable
  `unsupported_operation` with `field=commit_history_observation`; retry the
  **read**, not an uncertain data write. Stable corruption remains fail-closed.
- A post-durability exception does not prove rollback. Preserve the transaction's
  report and query its qualified identity after recovery; never blindly replay
  a business operation. Metadata/correlation_id is **not** an idempotency key.

## Configuration, privacy and observability

`MetadataLimits` defaults and hard bounds are in the generated
[API reference](API_REFERENCE.md) and [capability specification](specs/SPEC-GX-CAP-1.md).
Default complete encoding limit: 16 KiB; hard limit: 64 KiB. Actor, origin,
correlation_id and reason are optional strings; attributes is a bounded canonical
tree of small primitive values. Input is copied and exposed immutably.

Actor strings are caller-supplied provenance, **not authenticated identity**.
Do not include credentials, prompts, personal content or full graph payloads.
Repr, errors and default telemetry do not contain metadata content; the database
does not automatically recognize secrets embedded in permitted strings.

Optional metrics count locally acknowledged outcomes, not a replay of all durable
history: `oktografx_commits_with_metadata_total`,
`oktografx_commit_metadata_bytes_total`,
`oktografx_commit_id_high_watermark_count`. The final `_count` follows the existing
metric naming contract. They contain no actor/correlation labels; sink failures
cannot change commit outcomes. Exact identities come from typed history, not a
floating-point monitoring gauge. Constructor-time budget refusals are exceptions,
not per-database metrics (there may be no database yet).

`db.verify("all")`, `"records"` and `"pages"` include journal logical integrity
when active. A failed or moving observation produces a finding, never a clean
empty result. `"indexes"` remains index-only. Verification does not repair pages.

## Coordinated logical transfer and restore

The source entry has a versioned/checksummed `encode()` envelope; decode with
`okto_grafx.domain.txn.commit_catalog.decode_commit_catalog_entry`. Unknown versions,
corrupted records and invalid metadata refuse. This is not raw WAL serialization.

```python
from okto_grafx import CommitId, prepare_commit_import

transfer = prepare_commit_import(source_entry)
with target.begin("write", metadata=transfer.metadata) as tx:
    # Copy the selected graph payload through ordinary typed/Cypher writes.
    tx.execute("CREATE (:Item {id:$id})", {"id": 1})
target_id = CommitId(target.identity.database_uuid, tx.report.csn)
mapping = transfer.mapping(target.lookup_commit(target_id))
```

The target must be a separately initialized store with its own UUID and enabled
history. `mapping` contains `source` and `target` qualified identities. The source
token is atomically persisted as `metadata.attributes["grafx_source_commit"]`,
so it is recoverable even if the importer crashes before recording a sidecar.
Source attributes are nested under `source_attributes`, avoiding key collisions;
actor/origin/reason/correlation are preserved. Added bytes/depth must fit configured
`MetadataLimits` passed as `limits=`. Over-budget transfer refuses before target
begin. `preserve_metadata=False` explicitly retains only the source mapping for
privacy/size; it never silently drops metadata. Source claims remain unauthenticated.

These are **coordinated transfer hooks**, not a generic graph dump/restore engine:
the caller owns snapshot selection, graph payload streaming, batch boundaries and
schema compatibility. One source record maps to one target writing transaction;
multiple sources require explicit consumer batching/mapping policy. Metadata does
not make repeated import idempotent. Re-export retains the new qualified identity
and can preserve earlier provenance inside bounded nested source attributes.

Copying an active directory is not a consistent backup. A physical restore of
the same identity must replace the offline original, never run simultaneously as
an independent writable copy. Do not edit UUIDs or journal bytes to fork a store.
For a logical fork, initialize a fresh target UUID and use the mapping hooks above.
The tests cover a logical transfer into a fresh target and a checkpointed, closed
physical copy reopened read-only with identical history. Manual OS-level copying
cannot be detected globally by this embedded library; it is not an authorized
way to create independent writers for one UUID.

The 0.0.5 [physical backup API](BACKUP_RESTORE.md) now provides a bounded, consistent
checkpoint cut and verified offline replacement restore. It preserves journal and
qualified commit identities; destination verification is outside the source fence.
It does not replace the logical-fork mapping contract above.
