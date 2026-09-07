# Ordered cursor access 0.0.4 — accepted design

Status: **accepted for implementation on `feature/v0.0.4`**

This document freezes the finite design for CURSOR-1. It follows the adversarial comparison in
`FABLE_PERFORMANCE_GRAFX.md`, Claude's `CURSOR1_DESIGN.md`, the independent Codex review, and the
consensus recorded in Nexus trace `trc_c2b021d3d44c4ec69ab96cdbbf8fc818`.

The problem is concrete: the Okto Pulse Knowledge Graph query orders every node by
`(created_at DESC, id DESC)` and returns 500 rows, but every HTTP page still scans and filters all
visible node rows. The measured board scanned 2,069--2,184 rows on every page. The work therefore
grows as `O(P*N)` for `P` pages and `N` nodes.

## Decision

Grafx 0.0.4 will add a persistent, exact, ordered secondary-index layout and a narrowly proved
planner path for the Pulse ordering. Each node table may own an index on exactly
`(TIMESTAMP, STRING)`. The physical order is ascending; a descending range walk plus a lazy
`T`-way merge across the indexed node tables implements the Pulse order. The cursor predicate is
an exclusive upper bound. Each selected entry remains only a candidate and is revalidated from
the heap under the caller's snapshot.

The first implementation boundary is intentionally closed:

- exact indexes only;
- node tables only;
- two columns, with the first declared `TIMESTAMP` and the second declared `STRING`;
- `ORDER BY n.<timestamp> DESC, n.<string> DESC` with a terminal bounded `LIMIT`;
- optional exclusive keyset predicate in the exact Pulse form;
- no `SKIP`, `DISTINCT`, aggregate, write pipeline, `UNION`, proximity/vector behavior, arbitrary
  range language, or covering projection;
- complete canonical fallback unless every participating table has a healthy, fresh ordered
  generation and the transaction has no dirty table that can affect the result.

This is not a promise of a general SQL/Cypher B-tree optimizer in 0.0.4.

## Rejected alternatives

### Retaining the public `QueryCursor`

One cursor scanned the graph once and made its second 500-row delivery fast, but measured
1.658 s before the first delivery and retained a read transaction across the user's HTTP think
time. That holds the snapshot/vacuum horizon and is incompatible with the service boundary.

### Detached in-process spine

A detached `(created_at, id, label)` list would avoid a retained reader, but the first page would
perform today's full scan/sort plus up to 500 point lookups. Pulse background writers also advance
the global `read_lsn` frequently, invalidating the spine between pages and restoring `O(P*N)`.
It therefore adds complexity while making the most visible page strictly more expensive.

### Immutable spine rebuilt after writes

An immutable persistent spine makes the first page cheap only while it is fresh. Any node write
makes it ineligible and requires an `O(N)` rebuild. Continuous ingestion therefore moves rather
than removes the scaling problem.

## Logical and catalog format

`IndexLayout` is part of an index definition and definition digest:

- logical spelling `hash` preserves every existing index byte and behavior;
- logical spelling `ordered` selects this design;
- the index header uses numeric tags `1`/`2`, while the catalog uses `0`/`1` so the former
  reserved zero byte remains byte-exact for every existing hash definition.

The catalog remains at format version 2. Its existing required-capability bitset gains
`ordered_secondary_indexes_v1`. The metadata byte that catalog v2 currently requires to be zero
encodes the layout tag. The capability is mandatory whenever at least one ordered logical index
exists. This ordering is load-bearing: a 0.0.3 reader encounters the unknown required capability
before it parses an ordered index record, so it raises a typed schema-version refusal and never
mistakes the new artifact for a hash index or for corruption.

The generation's existing `bucket_count` field remains encoded for binary stability. It must be
the sentinel value `1` for `ORDERED`; it has no tuning meaning. `bucket_count` and
`expected_cardinality` sizing options are refused for ordered DDL in 0.0.4.

An ordered index is created explicitly. It is not added automatically to every Grafx database.
Okto Pulse Community creates the index idempotently for each physical node table that declares
the required columns. Pulse Core remains unaware of Grafx and of the physical index.

## Order-preserving key

The ordered key is a new derivation; the hash index's `index_key` is not ordered and must never be
compared as if it were.

Each component starts with `0x00` for a non-null value and `0xff` for `NULL`, matching the current
`_sort_key` rule that null sorts last in ascending order. A timestamp encodes its signed
microsecond value as big-endian unsigned after flipping the sign bit. A string encodes valid
UTF-8 with `0x00` escaped as `0x00 0xff` and terminates with `0x00 0x00`; this is prefix-free and
preserves Python/Unicode scalar order. Stored strings already reject unencodable surrogates, and
the ordered derivation must raise the same typed refusal.

The persisted tree key is `(ordered_values, RecordRef)`. `RecordRef` provides physical uniqueness
for historical versions but is not a public query tie-breaker. Across tables the lazy merge uses
catalog `table_id` order for equal `(created_at, id)`, reproducing the stable order of
`AllNodesScan`. The Pulse cursor remains `(created_at, id)`; this change does not invent a hidden
public cursor component.

A hostile differential corpus must prove that the encoded order equals `_sort_key` for nulls,
negative/positive timestamp extremes, equal timestamps, empty/prefix strings, non-ASCII strings,
and duplicate logical keys with distinct historical references.

## Physical layout and atomic publication

The index file keeps page 0 as the static file/index header. Ordered files declare index-header
format 3 and layout `ORDERED`. Hash files remain readable and writable under their current
formats.

New page types are distinct from `INDEX_HASH` and `INDEX_HNSW`:

- `INDEX_ORDERED_ROOT` for the two root descriptors;
- `INDEX_ORDERED_INTERNAL` for separator/child records;
- `INDEX_ORDERED_LEAF` for ordered `(key, entry)` records.

Pages 1 and 2 are independent root-descriptor pages. They are deliberately not two slots in one
page: one torn page write must not destroy both copies. A descriptor contains at least the format,
artifact nonce, monotonically increasing generation, root page, height, entry count,
`applied_through_lsn`, and a payload checksum in addition to the common page checksum. The newest
complete valid descriptor wins; one damaged or interrupted older/newer descriptor is ignored,
while two invalid descriptors refuse fail-closed.

Tree pages are immutable after publication. A transaction applies its complete ordered-index
batch to a private copy-on-write path, coalesces repeated mutations, and assigns fresh append-only
page numbers. It never changes a page reachable from either published root descriptor.

The publication order for all ordered indexes touched by one commit is:

1. validate the ordinary first OCC and construct the same logical `IndexChange` WAL effects;
2. append the complete WAL batch and pass the WAL durability barrier;
3. build/write all new COW pages for the batch;
4. flush all new tree pages and pass one grouped data durability barrier;
5. write the older root-descriptor page with the new root and `applied_through_lsn`;
6. flush all new root descriptors and pass one grouped root durability barrier;
7. publish commit state through the existing durable protocol;
8. perform the ordinary second OCC/publication validation and cleanup.

No barrier is executed per entry or per tree page. A failure before root publication leaves only
unreachable append-only pages. A failure after a root becomes durable is replay-safe because the
same descriptor carries the watermark. No success is acknowledged before the existing commit
state protocol completes.

During the small root-before-commit-state window another process may observe an index root ahead
of the published database high-water. This is permitted only for exact ordered indexes: new
entries are rejected by heap snapshot validation, ended entries remain in the tree until the
reader horizon permits reclamation, and therefore the root is a superset for every live older
snapshot. A proximity index may not use this rule.

## MVCC, redo and reclamation

Ordered entries reuse the exact-index candidate rule. They never decide visibility alone. The
heap version is resolved and its ordered key is re-derived under the transaction snapshot before
a row is returned. `dead_csn` is retained for safe reclamation, not used as a replacement for heap
validation.

`INSERT`, `TOMBSTONE`, `REMOVE`, and `RESET` remain idempotent by `(index, key, ref)`. Recovery
skips a committed ordered batch only when a selected valid root descriptor has
`applied_through_lsn` at or beyond the batch's commit position. Reapplying a batch below that
watermark must not allocate pages. A crash before publication may allocate a second unreachable
COW attempt, but it cannot change answers.

Version 1 is append-only. Unreachable pages are reclaimed only by a compacting rebuild into a
new generation with a fresh artifact nonce. The existing `BUILDING -> ACTIVE -> STALE`
catalog-generation protocol publishes the compact generation. The former generation is deleted
only after the existing reader/vacuum horizon proves no live statement can still be walking it.
Until that proof, it remains intact. A configurable growth quota causes a typed maintenance
signal or canonical query fallback; it never silently stops maintaining a selected index.

## Planner and executor proof

The planner selects `OrderedNodeMerge` only when all of the following are proved from the parsed
and analyzed plan, never from query text:

1. the source is one polymorphic `AllNodesScan` variable over node tables;
2. order keys are the same variable's declared/polymorphic `TIMESTAMP` and `STRING` properties,
   both descending;
3. a terminal `LIMIT` has already passed the ordinary parameter/type/range validation;
4. an optional keyset bound is the exact strict Pulse predicate and its timestamp conversion and
   string parameter retain their established eager refusals;
5. every table that can satisfy the residual predicate has one ACTIVE ordered definition on the
   corresponding positions, with a valid generation certificate and sufficient freshness; a
   polymorphic table may be omitted only when three-valued predicate analysis proves that a
   missing property makes its complete WHERE result never true;
6. no relevant table is dirty in the transaction and no custom collaborator lacks the ordered
   range capability;
7. projection and residual filters are in the existing proved-total subset, so stopping after
   `LIMIT` cannot suppress an expression refusal that the canonical bounded sort would raise.

Each table produces a reverse iterator below the exclusive upper bound. The executor keeps one
candidate per table in a heap, giving `O(T + (K+S) log T)` comparisons and `O(T)` merge memory,
where `S` is the number of stale/invisible/filter-rejected candidates encountered before `K`
rows survive. It does not materialize `T*K` rows. A page-0/root generation change during an
iterator invalidates the whole attempt; no already-published prefix falls back silently. The
statement retries from its original exclusive bound under the normal bounded retry policy or
raises the typed retryable refusal.

If any proof fails before iteration, the complete canonical `AllNodesScan -> Filter -> Project ->
Sort -> Limit` plan runs. After an ordered iterator has emitted a candidate internally, damage,
generation drift, or unsupported behavior raises; it never switches to a scan after a partial
prefix.

## Implementation milestones

1. **OIX-0 — contract and discrimination (implemented):** `IndexLayout`, catalog
   capability/layout encoding, index-header/page/root-descriptor codecs, old-reader refusal tests;
   no planner selection. The established hash digest and catalog metadata bytes remain frozen,
   and `HashIndex` refuses an ordered definition until the dedicated store is attached.
2. **OIX-1 — bulk build and read (implemented):** immutable page builder, complete structural
   verifier, reverse bounded walk, heap revalidation and generation certificates. The engine
   publishes a new nonced artifact in three durable phases — tree, both roots, then static page
   0 — and reopens it through fresh header/root observations. Statement reads use lazy page
   loading, retry the complete attempt on certified root drift and re-read every candidate from
   the heap under the snapshot before returning it. The planner still cannot select this path;
   that waits for transactional maintenance in OIX-2.
3. **OIX-2A — COW batch and physical publication (implemented):** a complete logical batch is
   partitioned down the immutable tree, copies/re-packs each affected leaf and ancestor at most
   once, shares untouched children and allocates the reachable replacement pages contiguously.
   After the caller's WAL barrier, the store publishes `COW pages -> grouped data barrier ->
   alternate root -> grouped root barrier`; the selected root watermark makes replay idempotent
   without repeated allocation.  Root-write refusal leaves the former root authoritative, while
   a write-that-landed-then-raised is recovered by the fresh certificate and watermark.  The
   focused differential/failure corpus passes 16 tests.
4. **OIX-2B — transactional integration (implemented and matrix-verified):** the ordered store
   now implements the ordinary secondary-index staging contract, participates in registry/table
   watermarks, publishes one COW generation per live transaction and coalesces each store's WAL
   subsequence into one recovery publication. Exact equality uses a bounded tree seek and remains
   heap-validated by `IndexManager`; detached catalog generations bulk-build directly. Textual
   `OPTIONS layout = ordered` and Python `layout="ordered"` activate only the narrow
   TIMESTAMP+STRING contract and refuse hash sizing. `rebuild_index()` bulk-builds and verifies a
   compact fresh nonce before catalog rotation, retaining the former generation as `STALE`.
   The ordered-specific matrix now passes 17 crash/recovery and 8 real-process cases. It exposed
   and pinned a damaged-newest-root defect: replacement now installs a complete non-header root
   without first decoding the disposable target, while recovery refuses to advance a surviving
   damaged root past its table high-water after the corresponding WAL has been recycled.
5. **OIX-3 — query path (implemented; accumulated checkpoint executed):** the planner recognizes only
   a polymorphic all-node scan ordered by the table primary key after one TIMESTAMP column, with
   both keys descending, a bounded LIMIT, no SKIP/DISTINCT/aggregation and a proved-total
   residual predicate. Every participating table must expose a catalog-authorized ACTIVE exact
   ordered generation on those positions. The executor validates every capability and cursor
   value before opening a certificate, then holds one heap-revalidated candidate per table in a
   best-first heap. It stops without prefetching beyond K, closes and rechecks every root/header
   certificate before publishing the private result prefix, and never falls back after a stream
   starts. Dirty owner tables and every near miss retain the complete canonical pipeline. The
   focused corpus proves first-page/cursor equivalence, partial-capability fallback, owner RYOW,
   reduced scan counts, Unicode/tied values through the seeded data, and early-close root drift.
   It also proves conservative polymorphic pruning: Pulse's internal `BoardMeta` table may be
   omitted because its missing filtered properties make the whole conjunction non-true, while a
   missing property below an unsafe `OR` retains the canonical scan. The broader crash/multiprocess
   matrix passed with the integrated ordered/index/query checkpoint: 96 cases on 2026-09-07.
   The same-snapshot real-board comparison returned identical first/second 500-node pages with
   657/512 examined candidates instead of 2,411 each in the canonical scan. Residual observations
   O1–O3 from the prior review were reconciled in the bounded handoff: O1 is a marginal replay
   publication cost and O3 is the captured-registry contract. O2 is fixed in `a52940d`: unwritten
   COW pages are exempt only behind a fresh, complete, stable proof of both recoverable trees;
   missing/failed proof preserves all original findings. The integrated verifier/crash/recovery
   slice passed 152 tests, including reachable damage under a warm handle.
6. **OIX-4 — Pulse Community (implemented):** idempotent per-table index creation/migration,
   Community-only capability use (`40ed1a3`), 62 integration tests, and real API/UI validation.
   The 14 Discovery cards were also exercised after the native optional-hop addition. No live
   query, index or generation required a destructive migration during this checkpoint.

`NODE-IN-SEEK` is an independent low-risk optimization and useful primitive, but it is not
reported as CURSOR-1. It is implemented without changing disk format: only the exact standalone
`n.<primary-key> IN $parameter` shape can use the multi-key exact-index door; all near misses,
dirty-table reads and incomplete key encodings retain the canonical scan.

## Acceptance and crash matrix

Focused tests accompany each milestone. The accumulated long recovery and multiprocess suites
run after OIX-2 and again at the integrated OIX-4 checkpoint, not after every codec patch.

Required evidence includes:

- byte-exact v2 hash-catalog compatibility and a typed 0.0.3 refusal for the new capability;
- corruption of each root copy independently, both copies, internal separators, child pointers,
  leaf order, duplicate entries, nonce/digest and page type;
- interruption before/after WAL barrier, each data/root flush and barrier, root replacement,
  commit-state publication, second OCC and compact-generation activation;
- crash-loop replay with constant `applied_through_lsn`, proving no repeated page growth;
- two writers in separate processes plus readers pinned before, during and after root publication;
- update of either indexed column, delete/recreate, rollback, conflict, rehash/rebuild, vacuum and
  cold reopen;
- differential canonical-versus-ordered results, order, error class/details and budgets for empty,
  one-row, multi-table, null, non-ASCII, tied-key and high-tombstone cases;
- structural counts showing the first and later 500-row pages no longer scan all `N` nodes;
- one real Pulse API/UI run with identical node/edge digest and honest first-page/continuation
  timing. Wall time is reported, while correctness and removal of `O(P*N)` remain mandatory.

No milestone may weaken multi-reader/multi-writer participation, either OCC validation, snapshot
isolation, writer fencing, WAL ordering, durability, recovery, consistency, or fail-closed
corruption behavior.
