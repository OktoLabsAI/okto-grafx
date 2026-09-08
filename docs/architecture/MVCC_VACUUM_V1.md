# MVCC vacuum v1: quiescent reclamation with a durable snapshot floor

Status: accepted for Okto Grafx 0.0.2 (2026-09-04).

## Decision

Vacuum v1 is an explicit, foreground maintenance operation. It may run only when the operator
passes `confirm_quiescent=True` and has stopped every other Grafx process, including binaries
from before the capability fence. The current process must have no open user transaction. The
assertion is an operational precondition, not a conclusion inferred from reader TTLs: an expired
registry entry cannot prove that a process is not already executing a statement.

The first mutating pass uses two durable transactions:

1. publish catalog-v2 required capability `heap_reclaim_v1`;
2. in one ordinary WAL-before-data commit, advance the heap-wide retained-snapshot floor, free
   eligible version slots and owned overflow payloads, relink retained version chains, and reconcile every ACTIVE
   index at the same horizon.

Catalog v2 and its identity-index authority must already be active. The capability transaction
is idempotent and must complete before any heap floor or slot can disappear. An older binary
rejects the unknown required bit rather than interpreting reclaimed history. Subsequent vacuum
passes skip the already-published capability transaction.

If no heap slot or ACTIVE-index tombstone is removable, the reclaim transaction is a true
zero-write no-op and does not advance the floor. The initial capability publication may still be
the only write of the first call. No checkpoint is required to choose the horizon: inside the
asserted quiescent window vacuum uses the newest globally published commit LSN.

## Durable floor

Heap page zero keeps its existing file-header layout. The legacy state is
`(root_page=NO_PAGE, payload_length=0)`. Once reclamation advances the floor, heap files encode
`(root_page=HEADER_PAGE_INDEX, payload_length=floor_lsn)`. These fields were unused by heap files;
their new meaning is valid only while the catalog requires `heap_reclaim_v1`.

The floor is global to `heap.dat`, positive once set, monotonic, and never above the horizon used
by the reclaiming transaction. Every newly opened transaction checks its selected snapshot
against the floor before returning to the caller. A snapshot below it raises retryable
`GrafxSnapshotReclaimed` with the observed snapshot, floor, file, and `reopen` remedy. A floor
marker without its catalog capability, an invalid marker pair, or a non-monotonic request fails
closed.

## Eligibility and physical effects

A version is eligible only when both birth and end are committed, `xmin <= xmax`, and
`xmax <= horizon`. The 0.0.5 extension also retires overflow-backed versions. Before emitting
any retirement image, it checks all reachable overflow chains across all catalog tables for
exclusive ownership, acyclicity, page kind/slots and exact payload length. This includes
unselected tables and retained versions. Provisional or structurally implausible
lifetimes are retained and remain verifier concerns.

Reclamation uses `Page.free_slot()` followed by in-page compaction. Slot directory entries,
slot IDs, page IDs, table chains, and file length are preserved. A retained version whose
`prev_version` crosses removed history is rewritten to the first retained predecessor, or to
zero. Rewrites and removals are full page images in the existing WAL transaction. ACTIVE index
tombstones at or below the same horizon are removed through existing `INDEX_RECONCILE` records,
which carry and replay their reconciliation watermark.

Only catalog `ACTIVE` generations are query and reconciliation authority. A `STALE` generation
is historical and cannot become `ACTIVE` again. A detached `BUILDING` artifact left by an
interrupted rehash is likewise never resumed: retry allocates a fresh nonce and rescans the
post-vacuum heap before atomically publishing a new generation. This structural lifecycle, not
`committed_high_water()` (which is derived from retained record headers), guarantees that an
authoritative generation was built against the heap state current at its publication. Vacuum
therefore keeps reconciliation records stamped with the vacuum horizon; stamping them with the
later commit CSN would incorrectly claim reconciliation of versions between those two values.

`max_versions`, when supplied, limits heap versions selected in deterministic
table/page/slot order. Reconciliation may remove more index tombstones because every ACTIVE index
of a selected table must be safe before any selected slot disappears. `complete` reports whether
all eligible inline versions were selected; skipped overflow history is reported separately.
`pages_rewritten` counts changed heap data pages and excludes the heap-header floor image.
Selecting one table does not create a table-local floor: the durable floor remains heap-global,
so snapshots below it are rejected for every table.

## Explicit exclusions

This version does not:

- run online, automatically, or in the background;
- derive safety from TTL pruning;
- truncate files or unlink/relink table pages;
- reuse a durable page or slot ID;
- let a retired `RecordRef(page, slot)` resolve to another record;
- reclaim overflow chains;
- weaken the existing first or second OCC pass, WAL barrier, publication-last rule, recovery
  latch, or multiwriter/multireader behavior outside the explicitly quiescent operation.

## Expected performance effect

Freed slots are skipped before `RecordHeader.peek`, and compacted pages no longer retain removed
tuple bytes. This reduces header decoding, tuple materialization pressure, and resident/durable
payload bytes on churned pages. Because page chains and slot-directory entries remain and inserts
stay append-only, scans are still proportional to historical pages/slots; v1 does not claim file
shrinkage, physical identity reuse, or elimination of every `O(history pages)` path.

## Operator contract

Use vacuum only during a maintenance window after stopping every other process that can open the
database. Do not use `confirm_quiescent=True` as a convenience flag while application readers or
writers remain active. Restart application processes after vacuum so their first transaction
adopts the capability, current catalog authority, current heap floor, and current index views.
