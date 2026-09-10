# Internal system-history append prototype — not an activated format

September 10, 2026; continuation item 8, **partial implementation only**.
Owning code: `engine/system_history_store.py`. This is an internal image planner
and event codec, not a `Database` API, writable store, WAL extension or completed
GX-CAP-3 persistence checkpoint. No required capability bit is allocated and
`system-history.dat` deliberately remains outside native redo's admitted targets.
Do not invoke it as an application persistence API or write its images to a store.

## Implemented, detached behavior

`HistoryChange` captures native `TableDef`, logical RecordId, create/update/delete
code and typed tuple values. Delete has no value payload. Full historical table schema
is encoded with every change; tuple decoding does not need the current table schema
or heap. This does not yet capture the lifecycle of embedding-space catalogs.
One settled change per `(table_id, RecordId)` is admitted per batch. These are
supplied identities: the planner does not prove create/recreate or edge lineage.

`SystemHistoryStore.prepare` reads a supplied root image and captures immutable
event bytes. `PreparedHistoryAppend.bind(sequence)` creates full checksummed page
images for the final store-qualified CommitId, with invariant page cardinality.
It performs no host I/O. Rebinding captures a new terminal sequence in both root
and chunks. This is the intended bridge to native WAL batch-size retargeting,
not permission to skip that coordinator's validation.

`read_batches` verifies complete framing, schema/value codecs, store UUID,
monotonic sequence, chunk ordering/coverage, per-batch SHA-256 and chained root
digest before returning a result. All pages also retain native CRC-32C and
slotted-page validation. A stale root, missing/reordered chunk or future page LSN
refuses. Checksums detect damage; they are not authentication against a malicious
party that can rewrite all data and hashes.

The caller must supply an independently proved expected sequence and physical
extent. The root itself cannot certify that those inputs came from native durable
authority. Memory-provider tests do not establish a database crash guarantee.

`validate_append_images` now validates a complete proposed transition: the
store-qualified terminal must advance, the predecessor root is read exactly once,
only page zero and consecutive new chunks may change, and canonical regeneration
binds every image to that predecessor, activation, ordinal and terminal. A missing
chunk is never filled from resident storage. Changed hashes, a consistent but wrong
chain, an old-page overwrite, a foreign file, duplicate images and future stamps
refuse before any write. Image and byte bounds are checked; work depends on the
append, not retained history length. Input coordinates are checked before I/O.

This does **not** certify a supplied CommitId as committed, prove correspondence
with the transaction's current rows, or permit native replay. In particular, an
already-applied root is not accepted as its own predecessor: interrupted replay
still needs native COMMIT/checkpoint/coverage proof and an independently recovered
predecessor. Native admission remains disabled rather than guessing that proof.

## Prototype bytes and fixed bounds

All integer fields are little endian; these bytes are **not release-frozen**.
Pages are native META slotted pages, exactly one live slot, zero flags/reserved,
even sequence, no page-chain link. The slot's record has one of these shapes:

| Record | Fields |
| --- | --- |
| Root, page 0 | `GXHYHD01` (8 bytes), store UUID (16), activation u64, last sequence u64, batch count u64, next unused page u64, chained digest (32) |
| Chunk, consecutive pages starting at 1 | `GXHYBL01` (8), UUID (16), sequence u64, zero-based batch u32, part u32, part count u32, complete payload length u32, prior chain digest (32), payload digest (32), chunk bytes |
| Batch payload | change count u32, then repeated length u32 plus change bytes |
| Change | operation u8 (1=create, 2=update, 3=delete), RecordId u64, schema length u32, value length u32, native binary table schema plus prior-layout count u16 and `(version u16, width u16)` pairs, native tuple bytes |

The chain digest is SHA-256 of prior digest + sequence u64 + payload SHA-256.
Empty batches remain explicit; an initialized empty root has activation=last
sequence, no batches, next page=1 and zero digest. This prototype therefore has
at least root+one chunk per appended commit. No write-performance benefit is
claimed; the native implementation must report its actual write amplification.

Fixed input limits: 4,096 changes/batch, 64 KiB schema/change, 1 MiB encoded
values/change, 16 MiB batch. Read defaults: 65,536 pages, 32 MiB aggregate encoded
payload and 4,096 changes. Page and encoded-byte bounds are not RSS guarantees.
These are internal operation arguments, not exposed connection configuration.
Invalid inputs retain typed configuration/schema errors; damaged images are
`GrafxCorruptionDetected`; exhausted byte/change read budgets are
`GrafxQueryBudgetExceeded`. No partial batch list is returned on failure.

## Required before native exposure

This list preserves the already-approved contract, rather than expanding it:

1. Freeze the actual persisted capability, file framing/admission and one-way
   table activation, including the baseline for pre-existing rows.
2. Capture settled native effects and publish current/history atomically through
   both OCC checks, WAL, exact COMMIT rebinding, quota and complete redo preflight.
3. Validate interval continuity, delete/recreate and endpoint lineage; expose typed
   snapshot/time-travel outcomes only when these invariants are enforced.
4. Integrate verify, physical backup/restore, logical transfer/refusal and retention
   boundaries, independently of MVCC reclamation and recycled WAL.
5. Execute real native crash cuts, concurrent participants and repeated recovery.

The 34 focused tests in `tests/txn/test_system_history_store.py` exercise detached
bytes, bounds, rebinding, supplied lineage/schema preservation and corrupted
pictures, plus complete append transitions and their refusal paths. Two native
`CommitRedo` tests also prove this unadmitted file refuses before an otherwise
valid page prefix moves, even with a supplied COMMIT and compressed/uncompressed
framing. They are **not** tests of automatic history recording in a database.
See [CAP-3](SPEC-GX-CAP-3.md) and [the binding temporal ADR](../architecture/ADR-GX-002-TEMPORAL.md).
