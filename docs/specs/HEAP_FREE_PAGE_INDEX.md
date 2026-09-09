# Indexed discovery of retired overflow pages

Approved next-round item 3; implemented and locally validated in the
[acceptance receipt](../reports/V005_NEXT_EIGHT_PROGRESS.md). The authority is
the [roadmap](../../ROADMAP.md), not a new maintenance mode inferred from TTLs.

## Format and activation contract

Opt-in `maintenance.vacuum(index_free_pages=True, confirm_quiescent=True)` first
publishes `heap_free_page_index_v1` (catalog bit 9), requiring `heap_reclaim_v1`.
Default False does not upgrade a store; an already activated index is maintained
by subsequent vacuum operations. No downgrade by clearing a bit is supported.

In the heap's page-0 header, page flag bit 0 declares initialization and
`next_page` points to the first immutable directory page or NO_PAGE. The FileHeader's
existing root/payload reclaim-floor fields and table-directory slots are unchanged.
Directory pages use META, zero flags/reserved fields and one payload slot:
`<8sQI>` (`GRFXFPI1`, reclaim floor, count) followed by strictly increasing u32
candidate page IDs. `next_page` links directory pages. Links are bounded and
acyclic; directory pages cannot also be candidates, and candidates cannot repeat.
Ordinary FREE pages remain empty/terminal and identifiable by their proved
committed page LSN. Activation
without initialization is a legitimate crash cut; its header has no new flag/link
and keeps legacy discovery until the first indexed vacuum commits.

Vacuum's existing quiescent ownership proof remains mandatory. A detached plan
indexes already retired pages and newly retired pages in physical order. A small
subset of retired pages holds the directory itself (up to 114 IDs per directory
page at 512-byte page size); no new heap extent is necessary. Directory pages are
reused by the next quiescent rebuild. Updates to directory payloads,
root/initialized flag, reclaimed versions/index effects and snapshot
floor belong to the same ordinary WAL transaction. No live/retained chain is freed
merely because a directory says so. Initial construction and maintenance census
remain O(heap pages), outside normal allocation.

## Allocation and recovery

Ordinary allocations advance only a bounded **local cursor**, never the root.
Each candidate is revalidated as empty FREE with a committed LSN no newer than
the current durable view. Candidates consumed by previous writers are now OVERFLOW
and are skipped. Foreign types, malformed/future pages and invalid directory
pointers refuse. Reuse retains the normal full-page WAL/OCC payload path. A crash
before COMMIT cannot remove directory membership. Reopen/new reclaim floor resets
local traversal. A refused attempt can skip a candidate locally until that reset,
but cannot remove its durable membership.

Cost is O(k + stale candidates + directory pages) rather than a scan of every
heap page. A newly opened participant can revisit already-consumed candidates;
this is **not** an O(k) allocator or a universal O(1) promise. Directory verification
and quiescent construction remain proportional to heap size.

The first prototype used a mutable free-list head. A real process-death test
before COMMIT exposed an incomplete list even though rows remained intact. That
prototype was replaced before delivery: immutable candidate membership avoids
making pre-WAL heap materialization responsible for transactional list popping.

Independent readers retain snapshots. No single-writer application premise,
online vacuum, truncation, cross-store allocator or unchecked hint is introduced.
Device-level verification must validate root/links, coverage, candidate shape and
committed-page horizon. Physical backup preserves the structure; logical transfer
creates a fresh heap and does not copy allocation metadata. Pre/post-commit crash,
foreign writers, rollback under eviction, quota, stale/ABA pointer and malformed
directory tests are required before delivery.
