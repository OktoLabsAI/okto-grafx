# Changelog

All notable changes to Okto Grafx are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/) — with the caveat that below 0.1.0 anything may change,
including the on-disk format.

## [Unreleased]

### Added

- Added an explicitly selected, per-database `codec="numpy"` page adapter backed by NumPy from
  `[accel]`. It preserves page format v1 byte-for-byte, uses the pure codec for small directories
  and as the sole authority for invalid-image refusals, and reports both page-codec and effective
  CRC-32C implementation through `database.codec`.
- Added explicit, one-way `db.maintenance.enable_wal_page_compression()` activation. A catalog-v2
  required capability is published in a v1-only transaction before later commits may store
  strictly-smaller zlib level-1 full-page images in `WRITE_PAGE` v2. Bounded inflation, closed
  type/flag semantics, mixed-writer adoption and typed downgrade refusal preserve fail-closed
  recovery; incompressible images and segment-roll batches retain the v1 grammar.
- Added manual, foreground `db.maintenance.vacuum(...)` for process-quiescent MVCC reclamation.
  A one-way catalog-v2 capability guards a durable monotonic snapshot floor; one ordinary
  WAL-before-data commit relinks retained chains, removes eligible inline version slots and
  reconciles ACTIVE indexes at the same horizon. The operation requires the exact
  `confirm_quiescent=True` operator assertion, supports a deterministic `max_versions` bound,
  rejects stale snapshots with retryable `GrafxSnapshotReclaimed`, and deliberately excludes
  overflow reclamation, file truncation and durable page/slot/`RecordRef` reuse.
- Added `db.maintenance.bloat(table=None)`, an immutable, read-only and header-only census of
  ended heap versions at a non-pruning observation of the checkpoint-capped recyclable horizon.
  The report distinguishes horizon-eligible from retained ended lifetimes, states its
  byte-accounting limits and never presents the observation as authorization to vacuum.
- Added durable equality-only custom indexes through transactional `CREATE INDEX` and
  `Database.create_index()`. Ordered compound keys, explicit bucket counts and deterministic
  expected-cardinality sizing share one planner and one catalog-v2 shadow-build protocol. The
  Python door returns a detached ACTIVE `IndexView` with a certified nonce and freshness
  horizons only after durable publication.
- Added explicit growth-only `Database.rehash_index()` and maintenance delegation for exact
  indexes. A complete immutable shadow becomes ACTIVE only after OCC and its durability barrier;
  the immediate predecessor becomes STALE, recovery converges to one complete authority, and
  long-lived/read-only handles adopt foreign generation changes at their next fresh read boundary.
- Added explicit `Database.rehash_index_if_needed()` and maintenance delegation. Its advisory
  probe proves physical generation identity, examines only the bounded eager head pages plus the
  scalar physical page count, and requests at most one `2x` growth step through the existing
  foreground rehash protocol. It is never automatic/background, never scans all index entries to
  decide, and does not weaken WAL, OCC or multi-process reader/writer semantics.
- Added explicit, idempotent `Database.ensure_identity_indexes()` activation for catalog-v2
  primary-key, relationship-endpoint and unsigned record-identity access paths. Catalog v2 is a
  one-way mixed-fleet fence; vector/proximity indexes remain schema-derived.
- Added snapshot-owning, read-only query cursors through `db.query(...).cursor()`. Iteration pulls
  detached results in bounded batches without materialising the public terminal; early close can
  never stage a write and always releases the reader transaction.
- Added `Transaction.executemany()` for streaming parameterized DML batches. It parses one fixed
  statement once, returns only aggregate counters and preserves the existing transaction/WAL/OCC
  path. A failure at any item rolls all batch staging back to its initial savepoint even when the
  caller catches the error and later commits other work.
- Added the opt-in `query_memory_budget_bytes` limit. Blocking sort, result-DISTINCT and
  grouping/aggregation use safe, versioned adapter-owned external merge runs under deterministic
  logical-byte accounting; aggregate DISTINCT, stable mixed-value/NaN ordering, cleanup on every
  exit and the existing row limits remain intact. The default `None` preserves the previous
  execution paths.

### Changed

- Exact vector searches fed by a small, engine-sealed materialized candidate set now authenticate
  only the selected heap rows and their index buckets. Any incomplete proof, metadata drift, NULL,
  deletion, duplicate or wrong reference falls back to the canonical full scan before ranking; a
  public filter or custom snapshot cannot activate the path. With fixed bucket count the cost is
  `Θ(U·E/B)`, not `O(K)`; corruption outside visited candidates remains the responsibility of a
  full scan or `verify`.
- V2 index-generation nonce allocation now reuses the immutable nonce already certified by each
  registered definition. Legacy nonce-zero artifacts still open and validate their physical
  header, and collisions retain the same bounded refusal. A 64-generation component sample moved
  from roughly `352 ms` to `61 ms`; end-to-end DDL moved from `1,085 ms` to `789 ms`.
- A local-equality Cartesian pushdown prototype was removed after adversarial tests proved that it
  could suppress predicate errors, persistent inner-scan failures and query-budget refusals, in
  the latter case allowing a write that the canonical plan refused. A subsequent bounded replay
  prototype was also removed: under buffer pressure it could hide corruption introduced before a
  later physical pass and commit writes that A63 requires to fail closed. Regressions now freeze
  both refusal surfaces while the canonical nested scans remain in force.
- Exact index seeks no longer change conjunction semantics by promoting a residual term into a
  standalone predicate. The planner rechecks the original conjunction over hits and declines a
  seek when an observable term precedes its equality; safe leading equalities, including a later
  pattern following total equalities on already-bound rows, keep the indexed path.
- Empty exact-index generations now use an ephemeral first-fit directory while they are built.
  The durable index pages remain byte-identical to the canonical allocator, including removes and
  tombstones, while reset/detached builds avoid repeatedly walking empty buckets and data pages.
- Recovery port discovery no longer eagerly invokes dynamic attribute fallback for concrete
  implementations. Native cold recovery therefore performs one authoritative WAL walk; proxy and
  custom wrappers retain the existing dynamic fallback and typed refusal behavior.
- Transaction materialization groups immutable MVCC page stamps once per physical page and retargets
  those groups after publication. This removes repeated page-by-intent inspection without changing
  row order, WAL effects, OCC validation or conflict semantics.
- Same-handle content-only heap commits retain extent-directory, tail and endpoint-locator caches.
  Structural topology changes and foreign read views still invalidate them using an ephemeral
  structural signature that is never a durability authority.
- Split and monolithic checkpoints now reuse one exact reader-horizon observation for both reader
  presence and recycling decisions, avoiding a duplicate coordinator scan without weakening the
  conservative recycling horizon.
- Primary-key uniqueness now folds transaction and statement intent suffixes incrementally rather
  than reducing the complete accumulated list for every row. Public list rewrites and rollbacks
  force a canonical rebuild; a seeded differential covers numeric equality, NaN, mutable keys,
  table/transaction isolation and exact refusal parity.
- Prepared plans now use the immutable serialized catalog image, ACTIVE index picture and dirty
  table set as their authority key without also requiring Python object identity. Byte-identical
  catalog adoption across transaction boundaries therefore reuses the same bounded plan entry,
  while any schema, generation freshness or owner-overlay change still splits it.
- WAL-only read boundaries retain the resident catalog for a same token, a proven own publication
  or a complete catalog-free CE-3 interval. Initial views, foreign DDL, checkpoint movement,
  declined proofs and direct/legacy compositions keep the conservative refresh; the complete
  ACTIVE index projection is memoized only for the lifetime of that catalog authority.
- Exact one-hop traversals directly below a streaming `LIMIT` may use fresh endpoint indexes even
  with a scan frontier, allowing the consumer to stop before grouping the complete relationship
  table. Blocking operators, ranges, optional/union/write shapes and unavailable or stale indexes
  retain the canonical grouped scan.
- Recovery now coalesces repeated full-page effects only for page-only WAL replays whose embedded
  page LSN sequence is unambiguous. Full preflight still validates every superseded image, mixed
  page/index effects keep their original order, and recovery establishes per-file data durability
  after index completion and before publishing `commit.state`.
- Local two-slot control reads fuse the exact-name existence observation with their bounded read.
  Warm descriptors are still matched by physical identity, case-only aliases still fail closed,
  and custom/fault wrappers retain the original two-door behavior unless their concrete type
  explicitly opts in. The frozen `StorageDevice` port is unchanged.
- Recovery reuses its passage-bound, content-checked page preflight instead of decoding each page
  image repeatedly. A proof cannot cross replay objects, modes or recovery passages; a changed
  record is revalidated before mutation, and a page projection from a mixed replay remains
  sequential rather than becoming accidentally coalescible.
- Buffer-pool dirty work is selected from a per-file candidate set and revalidated at use time,
  avoiding full resident-frame scans in `flush`, `modified_pages`, `has_dirty_pages` and fresh
  read-view checks. The candidate index covers pinned, doomed, evicted and directly applied pages;
  it does not change write-back or WAL authority.
- Heap extent-directory lookups retain a defensive `table_id -> slot` hint. Every hit verifies the
  current slot's table-id before decode or overwrite, stale hints fall back to the canonical scan,
  and replay/epoch movement invalidates the memo. Cold scans use zero-copy slot views and decode
  only the matching extent.
- Raised the lazy, per-database descriptor-cache default from 128 to 256 (and the direct local
  adapter default from 64 to 256) to avoid LRU churn for the measured 141-file Pulse working set.
  `max_open_files` remains configurable per instance and should be lowered when several large
  databases share a descriptor-constrained process.
- Bumped the development version to `0.0.2` and started the bounded performance round governed by
  `GRAFX_PERFORMANCE_ROUND_FINAL.md`.
- Added a separate, versioned `python-v2` estimate of Python memory retained by the buffer pool
  without changing its nominal page admission budget. Descriptor-cache hits, misses and
  capacity-driven evictions are now available as unlabelled metrics and immutable storage-view
  counters; composed metric callbacks run only after the local storage and enclosing buffer-pool
  guards are released. The v2 estimator also covers bounded cold-load reservations and dirty
  frames detached for eviction.
- Cold buffer misses are single-flight per physical `(file, page)` and run storage read plus codec
  decode outside the global pool guard. In-flight loads count against capacity, failures wake
  waiters without being cached, distinct pages can load concurrently, and epoch movement prevents
  a late stale result from being published. Dirty eviction follows the same callback-free phase
  boundary without changing on-disk bytes, WAL, nominal LRU admission or concurrency guarantees.

### Fixed

- Made catalog capability/tag lookup tables immutable and restored the pure-core import gate:
  physical generation-name validation no longer needs `re`, while deterministic bounded `zlib`
  remains explicitly admitted as part of the durable WRITE_PAGE-v2 grammar.

## [0.0.1] — 2026-09-01

First public release. **Pre-alpha**: the on-disk format, the public API and the query surface may
all change, and there is no migration path yet.

### Added

- **Pulse whole-node payload replacement is pinned at the public boundary.** One
  `MATCH ... SET` replaces every adapter-owned mutable field while retaining `id` and
  `source_session_id`, the exact incoming/outgoing/self-loop/parallel edge multiset and every
  relationship property. Owner/outsider visibility, rollback, optimistic conflict, zero-match
  no-op, commit, cold reopen and `verify()` are covered without delete/recreate or a new API.
- **A write transaction now reads its own relationships.** `CREATE` accepts an endpoint bound to
  a node an earlier statement of the same transaction created, carrying that node's owner-local
  identity, and traversal answers from the owner's combined view: relationships this transaction
  created, relationships it updated or ended, and endpoint nodes it created, updated or ended --
  with multiplicity, direction, self-loops and exact properties. `DELETE r` cancels the one
  pending relationship it names; `DETACH DELETE` cancels every pending relationship incident on
  the node and leaves the rest standing. Pending relationship inserts force one grouped scan plus
  the overlay because endpoint indexes contain only committed edges. Update/delete-only overlays
  can keep using fresh indexes, while a start node created by this transaction takes the scan path
  because its private identity has no index encoding. An edge also declares the two endpoint rows
  it depends on, so a transaction that deletes one of them and a transaction that creates the edge
  no longer both commit -- the refusal arrives at the first validation, before a row is written or
  a page allocated for it. Two same-statement shapes remain typed refusals rather than guesses: an
  endpoint that very statement is creating, and a `DETACH DELETE` of a node an edge held by that
  same statement points at.
- **Relationship endpoints may now name nodes staged by the same transaction at the transaction
  substrate.** `stage_row_insert()` returns an authenticated owner-local `PendingRowRef`; only the
  two endpoint slots of a relationship INSERT may carry it. Commit reduces the full intent set,
  proves ownership, order, node kind and declared endpoint role, plans every durable record ID and
  resolves the endpoint tuple before its first heap mutation. Exact byte budgets include pending
  endpoints, explicit IDs cannot collide with the batch or reuse any physical version (including
  ended versions), and no pending token can enter heap rows, indexes or WAL.
- **Properties of committed relationships now participate in the owner-only transaction
  overlay.** A later `MATCH` in the same transaction reads the latest staged `SET`, including
  when parallel edges connect the same pair, while other transactions keep their committed
  snapshot. Commit, rollback and optimistic conflicts retain their existing all-or-nothing
  behavior. The layout-owned `_from` and `_to` columns are immutable and receive a typed
  refusal.
- **Write transactions now have an owner-only node overlay.** A later statement in the same
  transaction can `MATCH`, read, `SET`, `MERGE` or `DELETE` a node staged earlier. Inserts,
  updates and deletes are reduced through the same intent view used by commit; a dirty table
  withholds exact indexes and uses scan+overlay, including primary-key changes. Private
  `PendingRowRef` identities are authenticated by owner/table/object identity and never appear in
  public results, heap rows, indexes or WAL. Vector search over a dirty table remains a typed,
  pre-mutation refusal.
- **`DETACH DELETE` and relationship deletion are implemented.** `DELETE r` ends a relationship a
  `MATCH` bound and leaves both endpoints standing; the planner now registers a matched
  relationship variable in its table map exactly as a written one, which is what the refusal was
  missing. `DETACH DELETE` ends every relationship incident on a node — incoming, outgoing,
  self-loops, several at once — together with the node in one commit, or leaves all of them
  standing if the statement refuses. Incidence is matched POSITIONALLY against each relationship
  table's declared `FROM` and `TO`: record numbers are allocated per table, so the same number
  names different rows on the two sides and an edge is only ever incident on the side its own
  table names. A plain `DELETE` is unchanged and deliberately so: it ends the node and leaves its
  relationships on the pages as rows no traversal will follow.

### Changed

- **Descriptor identity revalidation now has a strict default and one bounded performance opt-in
  (ST-2).** `descriptor_revalidation="strict"` retains CF-12's identity proof on every cached hit;
  `"generation"` amortizes it only for canonical heap, catalog, valid index and valid twelve-digit
  WAL names while control, malformed and unknown names remain strict. Full invalidations advance a
  process-local adapter generation, file invalidation drops one stamp, and same-token, proved-own
  and valid CE-3 partial views do not advance it. Bounded changed names, fresh certificate reads and
  fenced page-0 CAS checks still reprove only their directed file. Generation mode requires a
  directory exclusively managed by Grafx/Pulse because external replacement of a whitelisted name
  may otherwise remain undetected until invalidation or reopen. Normal Grafx operation does not
  republish live heap or catalog names; any future live paged-name republication must add a directed
  identity proof at its fence/certificate or fail closed to strict. Memory mode is inert, custom
  registries are not reconfigured, and the effective process-local selection is observable through
  the read-only `Database.descriptor_revalidation` property without entering the persisted
  `DatabaseIdentity`. WAL, OCC, durability, multiwriter/multireader and BR-10 semantics are
  unchanged.
- **Two-slot control reads coalesce their fixed three-page image without weakening ST-2.** Control
  names remain strict in both descriptor modes, but one logical image read now takes one bounded
  `read_log` descriptor acquisition rather than three separate page acquisitions. Short v1 and
  oversized legacy images are length-confirmed, v2 retries refuse both short and oversized
  replacements, and header, CRC and generation validation are unchanged. A publication still
  performs independently proved read, page-write and durability-barrier calls.
- **Cross-writer DDL now proves the exact physical artifact before WAL.** Index headers carry a
  non-zero artifact nonce in format v2 while v1 remains readable and is upgraded only on writable
  startup under the commit fence. Durable foreign artifacts are synchronized only after the first
  OCC succeeds; exact definitions, registry claims and vector-map epochs make speculative adoption
  and rollback compensable without deleting another writer's canonical bytes. Incompatible
  orphans are quarantined and any pre-WAL mismatch is a retryable write conflict.
- **The second OCC has a narrow materialization baseline.** The first OCC still validates every
  logical and pre-staged interest at the transaction's original snapshot. Only newly discovered
  physical pages materialized from the current durable view use that view for the second OCC; late
  logical interests, overlap and drift fail closed. WAL order and durability are unchanged.
- **Normal checkpoints release writers while data files cross their durability barriers (CN-2).**
  Phase A freezes and flushes an immutable target under the lease and commit section; phase B runs
  only the captured data barriers without either fence; phase C reacquires authority, replays the
  suffix, publishes no further than the barriered target and recycles only afterwards. Failures
  publish nothing, and index rebuild claim/clear remains monolithic.
- **Reader horizon publication is now per database participant, not per transaction (E-CE2-1).**
  The first transaction opens one standing registration; later begins inside the configured
  refresh interval publish nothing, and commit/rollback no longer unregister it. A deferred,
  forward-only pin follows the oldest open snapshot, advances on due ticks and before checkpoint
  recycling, recreates a file pruned by another participant, and is withdrawn only by database
  close. Interval-zero compatibility still republishes when another transaction remains open,
  and fallible clock reads precede first durable publication so a failed begin cannot orphan an
  own pin. This removes one control-record publication from warm short operations while
  preserving multi-process readers, writers, CF-2 snapshot ordering and BR-10 WAL retention.
- **Nullable vector columns now use a sparse durable index.** A `NULL` embedding creates no
  vector-index entry or WAL effect; transitions to and from `NULL` insert or tombstone only the
  populated side.  Empty sparse commits still certify index coverage, while rebuild, exact
  verification, cold reopen and vector search consistently omit `NULL` rows.
- **Public recovery and metrics facades now expose their concrete detached result types.**
  `Database.recovery_report` is `RecoveryReport | None`, `Database.recover()` returns
  `RecoveryReport`, and `Database.snapshot_metrics()` returns `MetricsSnapshotView`; runtime
  type-hint tests pin all three and a strict consumer probe verifies them.
- **The custom-adapter contract and every public example now match the executable registry.**
  Examples use `PortRegistry.bind()` and `require_complete()`, release caller-owned registries,
  and are executed from `README.md` and `docs/PORTS.md` by the test suite. Registry validation is
  explicitly structural only: custom adapters are trusted host code, so their runtime exceptions
  may propagate unchanged; the engine and shipped adapters continue to use the `GrafxError`
  taxonomy.
- **Configuration is now canonical and enforced at the first public boundary.** Every accepted
  scalar is copied to an exact built-in value before it can reach persisted identity/WAL metadata
  or an adapter. Buffer budgets must hold both store working sets; WAL segments are constrained to
  the reader's 256-byte through 1-GiB domain, including exceptional batches; malformed
  Unicode/oversized OpenMetrics ports, hostile `PathLike` objects, invalid or concurrently changed
  registries, and forged/uninitialised configuration instances receive typed refusals before a
  database root is created. The process-global `checksum` selector remains effective with a
  caller-supplied registry.
- **OpenMetrics destinations are loopback-only unless the caller explicitly opts out.**
  `DatabaseConfig.allow_remote_metrics` is a boolean that defaults to `False` and is valid only
  with `metrics="openmetrics"`. Without that override, `metrics_destination` accepts only literal
  IP addresses for which `ipaddress.ip_address(host).is_loopback` is true, for example IPv4
  `127/8` or IPv6 `::1`; hostnames, including `localhost`, are refused without DNS resolution. A remote address or
  hostname requires `allow_remote_metrics=True` and emits one `RuntimeWarning` when each publisher
  starts. IPv6 destinations use `[::1]:port`, bind `::1` with `AF_INET6`, and report a bracketed
  endpoint URL. The override changes bind consent only; it adds no authentication, TLS or firewall.
- **Transactions now have four opt-in hard admission budgets, all defaulting to `None`.**
  `max_statement_writes` counts logical writes held by one statement; `max_transaction_rows`
  counts retained `row_intents`; `max_transaction_bytes` charges encoded row tuples, staged
  logical-record `encoded_length()` values and retained page-image generations. Ordinary page
  replacement charges by delta; a rollback preimage remains charged while its statement mark is
  live. `max_wal_batch_bytes` sums the complete record batch including
  `COMMIT` and excluding `SEGMENT_HEADER`, and is checked before WAL append. Overruns raise the
  non-retryable `GrafxTransactionBudgetExceeded`; refusal neither truncates the WAL nor persists a
  partial statement.
- **Queries now have two opt-in positive row-admission limits, both defaulting to `None`.**
  `max_result_rows` counts the public terminal incrementally and refuses row N+1 before retaining
  it, consuming the rest of the stream or calling `context.release()`. `max_intermediate_rows`
  counts each non-terminal physical operator separately for the whole execution; the public
  terminal is charged only as result, while a terminal with no public columns is intermediate.
  Overruns raise non-retryable `GrafxQueryBudgetExceeded` without truncating or releasing partial
  writes. These limits are not cumulative and do not provide RSS, streaming, deadline, traversal
  or spill budgets. Sort, aggregate, distinct and eager operators can retain up to the configured
  rows or states before their first yield; payload bytes, internal structures and auxiliary scans
  remain outside this slice.
- **Record and byte thresholds now drive automatic WAL maintenance.** After a durable write commit
  and schema settlement, writable databases checkpoint when `last_committed_lsn - checkpoint_lsn`
  reaches `checkpoint_interval_records` or when the optional `wal_max_bytes` high-water is crossed.
  The byte setting is a soft post-commit trigger: reader pins, atomic batches and deferred recycling
  may retain more without unsafe truncation, and a latch prevents futile checkpoint storms until the
  WAL falls below the threshold. The decision remains single-flight; a refused or late-failing
  checkpoint stays pending for the next write, while maintenance and diagnostic failures can never
  turn the already-durable commit into an apparent transactional failure. Read transactions and
  empty write transactions do not run maintenance, and explicit `Database.checkpoint()` remains
  available.
- **The calibrated HNSW search beam is now an operational database option.**
  `vector_ef_search` defaults to `320`, is bounded to `1..1_048_576`, reaches every vector index
  assembled or reattached by that database handle, and is exposed by the detached vector index
  view. The setting changes derived search effort only; it neither changes the exact/approximate
  regime boundary nor writes a new on-disk format.
- **The inert `vector_recall_target` runtime option was removed.** Recall is an offline benchmark
  result owned by `bench.harness.gate --recall-target`, not a guarantee one database handle can
  enforce without an exact oracle. Passing the former keyword to `connect()` now produces a typed
  migration refusal; runtime HNSW effort remains configurable through `vector_ef_search`. No
  target-to-beam formula was invented.
- **`Database` no longer exposes mutable engine collaborators, and `Transaction.context` was
  removed.** Composition properties now return detached frozen schema, inventory and diagnostic
  snapshots with no `inner`, callback or raw-object backdoor. Vector reads use
  `Database.search_vectors(transaction, ...)`; planning, index inspection, quarantine reads and
  metrics publication have explicit database methods. Invalid `Database.retry()` calls now prove
  type, ownership, active state and retry eligibility before schema bookkeeping can move.

### Fixed

- **`DETACH DELETE` no longer acknowledges a node deletion while overlooking an incident
  relationship staged by the same transaction.** A relationship an earlier statement staged is
  cancelled with the node; one the SAME statement is still holding refuses before any of the
  statement's effects reach the transaction, because it is not yet an intent a delete could
  name. Unrelated pending relationships do not block the detach. Repeating the same pending node
  in one `DELETE` is also idempotent instead of refusing after the first name cancels the held
  insert.
- **A row a statement ends more than once is no longer ended, counted and charged more than
  once.** The end of a row is now recorded for the whole STATEMENT and checked against what the
  transaction has already staged, keyed by the stored version an end is actually written to. It
  used to be recorded per ROW of the pipeline, so a Cartesian that handed the same node over
  three times reported three deletions of one node and held three writes for it; under
  `max_statement_writes` that spent the budget on repeats and refused a statement for exceeding a
  limit it never needed. A `DETACH DELETE` following an earlier `DELETE` of one of the same
  relationships was the same fault across statements: both read one snapshot, and a snapshot
  cannot see either one's uncommitted work.

- **Statement rollback now restores exact page staging.** `staging_mark()` retains its
  `tuple[int, int, int]` signature while sealing exact internal snapshots of page images, their
  provenance proofs, write partitions and charged bytes. Marks are matched by exact object
  identity and released on successful handover. `discard_since()` restores a replaced image and
  additions whose key sorts before an older key, closing both count-based rollback gaps without
  persisting part of a refused statement.
- **The test tree now has a zero-diagnostic Ruff baseline enforced in CI.** The cleanup exposed
  and repaired a non-string map fixture where Python collapsed the distinct-looking keys `1` and
  `True`, removed a dead platform-family resolver that referenced a nonexistent helper, and made
  deferred `PageIndex`/`StepClock` annotations resolvable at runtime. Ruff is pinned in the
  development extra and runs once in a dedicated Linux job before inherited lint debt can hide a
  future defect.
- **Verification now refuses a heap identity counter that can reuse a persisted id.** The records
  walk reads page 0 and every heap record header straight from the storage device, so a resident
  cache image, an ended or provisional version, or an orphan page cannot hide the physical high
  water. `record_id_counter` identifies the exact directory slot when `next_record_id` is equal to
  or below the greatest decodable id; gaps and counters ahead of empty tables remain valid. Every
  physical heap page without an exact readable descriptor now produces a located
  `page_descriptor_missing`, while an exact owner absent from page 0 produces a located
  `orphan_page`; neither malformed ownership bytes nor short orphan headers can certify the file
  clean or invent a counter association. Duplicate directory extents are unreadable rather than
  arbitrarily selected, and extents/pages whose table id is absent from the catalog are located as
  unreadable/orphaned instead of disappearing from the catalog-driven walk.
- **A checksum provider could forge equality and be installed without returning an integer.** The
  installer and native adapter now require and copy an exact unsigned 32-bit result before every
  comparison and runtime use, contain ordinary provider failures, cover the largest legal page in
  the acceptance corpus, and leave the previous safe implementation installed on any refusal.
  Injected callables are oracle-checked at runtime by default; the closed native-provider list
  remains accelerated after corpus validation, with an explicit opt-in to runtime verification.
- **Vector search: a search arriving while the HNSW graph was being built could answer from a
  fragment** (Codex audit, P0.5). The derived graph was published before it was filled, so two
  searches meeting on the first use raced: one answered three of eight rows with `stale` False
  and `achieved_k` reported as if complete, and a build that failed part-way could erase another
  thread's complete build. The graph, its maps and the log position it reflects are now ONE
  snapshot (graph, maps, mark), built in locals and published atomically by one reference assignment under a guard
  the assembly hands in; a search captures it once; a build in flight is waited for rather than
  raced; a failed build drops its locals and nothing else.
- **Vector search: a commit could certify a warm graph that another process had left behind.**
  `commit()` noted its own changes into the warm graph and stamped it with the header's
  `built_through_lsn` -- already past another process's commit -- so this process then answered
  without that process's rows, `stale` False, `verify()` clean (the warm half of LESSONS L22).
  A commit now certifies the picture only when it verified, before the store moved, that the
  picture was current; otherwise it retires the picture and the next search rebuilds.
- Regressions: `tests/vector/test_first_use_concurrency.py` (three deterministic interleavings
  through the public door, the `VectorMath` port as the lever) and
  `tests/vector/test_warm_graph_across_processes.py` (two interpreters).
- **Recovery and commit completion now share one fail-closed WAL protocol.** Startup recovery,
  checkpoint completion and the post-COMMIT path select only transactions with one unambiguous
  terminal COMMIT, validate the complete page/index replay plan before its first mutation, apply
  effects idempotently, and publish `control/commit.state` only after completion. A participant
  that fails after durable COMMIT latches `recovery_required` and cannot begin, flush, checkpoint
  or certify an index until the authoritative WAL gap has been completed.
- **Heap, index, WAL and commit reports now carry one exact commit number.** Live heap frames keep
  the reserved, permanently invisible `PROVISIONAL_CSN` until the WAL barrier succeeds. The WAL
  plans the real terminal LSN including a segment-header roll, heap page images are committed only
  in private copies, logical index staging is retargeted atomically, and append revalidates the
  plan before writing its first byte. Refused attempts therefore cannot leak a usable ghost row,
  even when eviction and cleanup both fail.
- **WAL uncertainty is sticky and recovery barriers are explicit.** A failed append either restores
  its exact physical and in-memory preimage or closes the append door with `append_uncertain`.
  Recovery and gap completion force every segment intersecting the authoritative LSN range before
  page/index apply or publication, independently of the ordinary pending-flush cache. A tail that
  grows after a partial damaged observation is rescanned from byte zero instead of interpreting
  the completing checksum suffix as a new record.
- **Startup can no longer truncate a live writer's in-flight WAL append.** The entire recovery
  observation and repair pass takes the same cross-process commit section as append plus barrier;
  read-only startup takes that section only to prove, byte-identically, that the checkpoint covers
  every complete commit. Torn tails, missing segments, damaged first effects, restored old
  `commit.state`, catalog/index lag and crash-at-every-recovery-write have dedicated regressions.
- **Lifecycle cleanup is exhaustive for every exception class.** `Database.close()` and assembly
  unwind attempt every release even when a host adapter raises `RuntimeError`, `KeyboardInterrupt`
  or `SystemExit`, then preserve the first failure; cleanup can no longer strand later locks or
  handles merely because the first closer was foreign.

### Initial pre-alpha baseline

The initial baseline below is included in the same `0.0.1` release; later entries above record the
stabilization and Pulse-compatibility work completed before publication.

#### Added

- **Embedded graph database** with a public API (`connect`, `Database`, `Transaction`,
  `QueryResult`) and an `oktografx` command line whose exit codes are a contract.
- **Multi-process, multi-thread reads and writes** (D1). A commit is refused only when its
  partitions genuinely intersect another's, never because another writer exists.
- **Snapshot isolation** with optimistic concurrency control. Readers never block writers; writers
  never block readers.
- **Process coordination on the filesystem**: leases with epochs, monotonic liveness, exclusive
  sections, reader registration, and takeover of a dead writer with stale-epoch rejection.
- **Write-ahead log**: segmented, self-describing records with CRC-32C, an LSN mark index, barriers
  a commit waits on, and recycling bounded by the checkpoint and the oldest live reader.
- **Recovery at open**: idempotent replay, truncation of a torn tail at the last intact record, and a
  forensic ledger and quarantine that preserve the evidence rather than discarding it.
- **openCypher (Kùzu dialect)**: DDL for node, relationship and vector-space definitions; `CREATE`,
  `MATCH`, `WHERE`, `RETURN`, `MERGE`, `SET`, `DELETE`, traversal with bounded hop ranges,
  `ORDER BY`, `SKIP`, `LIMIT`, `DISTINCT`, `WITH`, aggregates, and parameters.
- **A declared `PRIMARY KEY` is indexed automatically**, created by the DDL and re-adopted at every
  later open. A keyed read plans an index seek; the uniqueness check reads the index rather than
  scanning the table.
- **Relationship tables are indexed per endpoint** (`ef_`/`et_`): traversal expands a bounded
  frontier by index lookup and switches to one grouped edge scan past a fan limit, with identical
  answers in both regimes. The old cost — every edge of the table read per frontier node — is gone.
- **Schema changes are transactions.** A `CREATE ... TABLE`/`VECTOR SPACE` builds on a
  per-transaction working catalog and stages its page images — the file header included, closing a
  recorded durability gap — so a rollback leaves nothing anywhere and a crash cannot lose a
  committed schema. A table declared inside a transaction is visible to other transactions once the
  transaction commits, not before.
- **Dual index visibility** (CONTRACT §8.7): EXACT indexes return candidates validated against the
  heap under the caller's snapshot; PROXIMITY indexes are versioned with tombstones and a horizon.
- **A stale index is never used**, by the planner or by the uniqueness check — it is a subset of the
  heap, which validation cannot repair, so both fall back to a scan.
- **Embeddings as a first-class column type**: vector spaces with a dimension, metric and storage
  dtype; an index created with the table that declares the column; and a search that reports the
  regime it answered in and the `k` it achieved.
- **Verification**: `verify(scope)` walks pages, records and indexes and reports located findings,
  with counts, so a caller can tell "nothing was wrong" from "nothing was checked".
- **Observability**: a frozen metric catalogue with no-op, OpenMetrics and JSON sinks, and a
  sanitised, bounded event sink.
- **A host-supplied metrics sink cannot break the engine**: every recording call is contained, so
  a sink that raises — even after a commit's barrier — leaves the commit truthful and the rows
  single, and `publish` stays typed. `metrics="json"` now writes its file at `close()`.
- **`Database.unindexed_tables`** reports, at open, the tables whose automatic indexes declined —
  the open-time twin of statement-time `skipped_indexes`.
- **Seven ports with default adapters** (`storage`, `clock`, `coordinator`, `codec`, `metrics`,
  `events`, `vector_math`), a fail-closed registry that names every missing slot in one error, and an
  enforced import boundary that keeps `domain/` and `engine/` mechanism-free.
- **Optional accelerators** behind `[accel]`: a native CRC-32C proved byte-identical to the reference
  before it is installed, and numpy vector math selected only by explicit configuration.
- **Windows and POSIX as equal citizens** (D9), with a suite that requires a platform-specific test
  to declare its counterpart and every skip to be attributed.
- **Performance documentation with in-tree instruments**: [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)
  records measured numbers with their machine, build and load conditions —
  `tools/measure_concurrency.py` (multi-process read/write latency under load, correctness-gated)
  and `tools/measure_traversal.py` (traversal shapes with index-vs-scan equality) reproduce them.
- **Documentation**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
  [`docs/PORTS.md`](docs/PORTS.md), the frozen `docs/architecture/CONTRACT.md`, the component
  register, the lessons, and the punch list of known gaps.

#### Known limitations

- The on-disk format is not stable and there is no migration path.
- Performance on Windows does not meet the D5 ceilings; POSIX with `[accel]` does. Windows
  control-file publication costs ~16.5 ms against ~0.13 ms on Linux. The measurements and the
  analysis are in `docs/architecture/COMPONENTS.md`.
- Traversal with an unbound target resolves landings by one scan of the landing table per
  traversal; edges are indexed, landings are not yet.
- `DETACH DELETE` and relationship deletion are not implemented, and refuse rather than pretend.
- A `MATCH` cannot bind a row the same transaction created; a row's identity is allocated by the
  commit.
- There is no read-your-own-writes within a transaction.
- Two tables whose names differ only by case can share one index file name; the second goes without
  an index rather than failing, and is reported.
- Known gaps are recorded in `docs/architecture/PUNCHLIST.md` rather than left implied.

[0.0.1]: https://github.com/OktoLabsAI/okto-grafx/releases/tag/v0.0.1
