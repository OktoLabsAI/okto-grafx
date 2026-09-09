# Indexes and vector search

## Sparse exact hash indexes and repeated keys

`db.create_index("by_status", "Document", ("status",), layout="sparse_hash",
bucket_count=128)` opts an explicit property index into compact pointer pages and
lazy bucket heads. Default `layout="hash"`, automatic indexes, FTS and vectors are
unchanged. Rebuild/rehash retains the selected layout; physical backup and logical
transfer preserve its declaration. Older binaries refuse required capability bit
10. [Format and recovery contract](specs/SPARSE_HASH_DIRECTORIES.md).

Use sparse layout when many buckets would be empty. At 512-byte pages a 65,536-bucket
empty index uses 571 pages, versus 65,537 for eager hash. The first write to a new
bucket requires an additional flush/barrier; do not select it expecting every write
to be faster. Distribution counts only populated chain pages in `pages`, but its
page-work budget also charges pointer reads, including empty buckets.

Repeated-key native bucket scans retain a lazy, per-index decoded-page memo: at
most 64 pages and 1 MiB of conservatively charged logical data. Every reuse compares
the complete current slot images, key, requested reference and decoder identity;
all page/link checks, pre/post generation certificates and heap visibility checks
still execute. No on-disk format or consistency option changes. Changed bytes,
foreign writes and different keys miss the memo. Oversized pages use canonical
decoding without retention. These are fixed implementation bounds, not RSS limits.
Warm repeated queries avoid entry decoding but still visit all chain pages and all
returned candidates. Returning N matches necessarily remains O(N); increasing the
bucket count cannot split one repeated key. A new posting-tree layout remains a
distinct future change, not a claim made by this decoding optimization.

## Cooperative vector read control

`db.search_vectors(reader, space=..., query=..., k=..., timeout_seconds=None,
cancellation=None)` accepts the same positive finite timeout and exact
`CancellationToken` as materialized reads. Defaults preserve ordinary execution.
Native exact candidate reads/scoring, ANN scoring/navigation and cold HNSW builds
between insertions check the signal. A cancelled build publishes no partial picture;
the caller-owned transaction remains open until its context exits. A subsequent
read/write can proceed. No commit is interrupted and no partial ranking is returned.

Use controls for interactive or abandoned work; do not interpret them as a hard OS
deadline. Admission, individual storage/math calls, header capture/sorting and one
construction insertion are not preempted. Custom vector collaborators need the
explicit `search_controlled` capability; requests refuse if it is absent, rather
than silently ignoring a timeout. Hybrid search shares one deadline across sources.

For native inverted text indexes, versioned analyzers, BM25, filters and the closed
search procedure, see [full-text search](FULL_TEXT_SEARCH.md). It uses the existing
exact HASH generation/WAL lifecycle but emits several postings per row; it is not
a scalar equality index or a vector/embedding feature. `rebuild_index`/`rehash_index`
preserve analyzer identity, and `verify('all')` includes posting coverage.
[Hybrid search](HYBRID_SEARCH.md) combines native text and vector windows with
weighted RRF and optional bounded graph evidence; it retains the vector source's
exact/approximate regime and uses one read snapshot throughout.

[Documentation index](README.md) · [Configuration](CONFIGURATION.md) · [Maintenance](OPERATIONS.md)

0.0.5 development adds [quiescent orphan-file inventory and cleanup](READ_CONTROL_AND_INDEX_CLEANUP.md#orphan-index-inventory-and-removal).
This does not retire catalog-owned STALE/BUILDING generations or change query access paths.

## Choosing an access path

Hash exact indexes accelerate equality/compound equality, not general ordering.
`db.create_index(name, table, columns, layout="ordered")` additionally supports
the closed two-column **TIMESTAMP then STRING** ordered layout, with no hash
sizing hints. Eligible keyset/window plans can use it; inspect the actual plan and
statistics, and expect a canonical scan for unsupported shapes. This is not a
general-purpose B-tree API or a persistent public cursor.

For example, after creating a table with `created_at TIMESTAMP, id STRING`, call
`db.create_index("by_created", "Event", ("created_at", "id"), layout="ordered")`.
`create_index` owns a dedicated transaction; do not assume it joins an unrelated
open transaction. Explicit hash `bucket_count` is an integer from 1–65,536;
`expected_cardinality` is 1–4,194,304 and rounds the derived count to a power of two;
both together refuse. The default remains 64. Rehash grows hash
directories, while `rebuild_index` reconstructs a fresh immutable generation.

## Indexes

### Explicit distribution diagnostics and wide directories

`db.index_distribution(name, max_pages=65536, max_entries=1000000,
max_memory_bytes=67108864)` performs a bounded physical census of one certified
active hash generation. It reports `bucket_count`, `entries`, `pages`,
`overflow_pages`, `largest_chain_pages`, `largest_bucket_entries`,
`largest_key_entries`, `dominant_key_fraction`, and `recommendation`, without key
values. Counts include retained physical versions, not just visible live rows.
Budgets fail with `GrafxQueryBudgetExceeded`; this is not a heap integrity audit.
Each diagnostic bound must be an integer in 1..2,147,483,648; booleans refuse.
Its O(pages + entries) cost is explicit maintenance, never a new query/commit hook.

`recommendation` is `inspect_key_skew` when at least 16 entries exist and one key
accounts for at least half, otherwise `consider_growth` for overflow or more than
64 average entries per bucket, otherwise `balanced`. These are heuristics, not
throughput promises. `rehash_index_if_needed(..., check_skew=True)` runs this
diagnostic only after ordinary head-pressure admission, skips growth for dominant
key skew, and propagates exhausted diagnostic budgets. The default `False` keeps
the existing O(bucket_count) decision without a full entry census. Rehash cannot
spread many identical encoded keys across different buckets.

Any retained generation above 4,096 buckets activates required capability
`large_hash_directories_v1` (bit 7). All participants must understand it; the flag
cannot be removed to downgrade. The layout remains eager: 65,536 heads at 8 KiB
consume 512 MiB per index even before entries. Use explicit larger sizing only
after observing pressure; defaults stay 64 and no sparse directory/sharding was
introduced. Old-reader visibility, immutable generation publication and recovery
are unchanged. [Layout contract](specs/LARGE_HASH_DIRECTORIES.md).

### Existing index contracts

- **A declared `PRIMARY KEY` gets an index automatically**, created by the DDL and re-adopted at
  every later open. A keyed read plans an index seek; an unkeyed predicate plans a scan.
- **A relationship table gets an index per endpoint** (`ef_`/`et_`), so traversal expands a
  bounded frontier by lookup instead of reading every edge, switching to one grouped scan when
  the frontier grows past the point where the scan is cheaper.
- **Known bulk loads can size new automatic exact indexes up front.** Pass
  `automatic_index_expected_cardinality=` to `connect()` before catalog-v2 activation or new DDL.
  The hint applies per new PK, endpoint and identity index; it never resizes an existing
  generation. An empty writable catalog is activated to v2 immediately; a non-empty v1 catalog
  requires the explicit `db.ensure_identity_indexes()` migration before further table DDL, so
  the hint is never silently ignored. Count artifacts, not tables: a keyed node owns its PK and,
  when referenced by a relationship, a separate identity index; a relationship owns `ef_` and
  `et_`. Leave the hint unset when the scale is unknown, because full walks cost
  `O(bucket_count + entries)` and an oversized eager directory wastes space and scan time.
- **Dual visibility (CONTRACT §8.7).** An EXACT index returns candidates that are validated against
  the heap under the caller's own snapshot — so the index may be a superset and can never be a wrong
  answer. A PROXIMITY index is versioned with tombstones and a horizon, and its entries are the
  answer.
- **A stale index is never used.** A stale index is a *subset* of the heap, which validation cannot
  repair, so both the planner and the uniqueness check fall back to the scan they did before any
  index existed: slower, and right.
- **Custom exact indexes are durable catalog authority.** Create one transactionally with
  `CREATE INDEX by_email FOR (p:Person) ON (p.email)` or with
  `db.create_index("by_email", "Person", ("email",))`. Ordered compound keys are supported.
  Choose either `bucket_count=` or `expected_cardinality=`; with neither, the default is 64
  buckets. The Python door owns a dedicated write transaction and returns an immutable
  `IndexView` only after the shadow generation and catalog commit are durable.
- **Exact indexes can grow explicitly without replacing a live file.** Call
  `db.rehash_index("by_email", bucket_count=256)` or supply `expected_cardinality=` instead —
  exactly one hint is required, and the resolved count must be strictly larger than the current
  ACTIVE generation. This is foreground maintenance: writers wait during the complete shadow
  scan/build, while readers with an established snapshot may finish. The immediate predecessor is
  retained as STALE; older immutable files remain retained orphans until safe reclamation exists.
  A catalog-v1 automatic index activates v2 and grows in one build, so every process using the
  directory must satisfy the compatibility fence below.
- **Unknown growth can be handled by explicit assisted maintenance.**
  `db.maintenance.rehash_index_if_needed("by_email")` validates the selected physical generation,
  samples only the bounded eager bucket heads and grows at most one `2x` step when average head
  occupancy reaches the canonical 64-entry target or retained overflow reaches the configured
  ratio. It never runs from commit or in a background worker and never walks all entries merely to
  decide by default. `None` means only “no assisted growth was selected now” (or the 65,536-bucket ceiling),
  not that the index is healthy. The eventual foreground shadow build has the same writer-pause,
  OCC, WAL and durability contract as `rehash_index`; do not call it repeatedly without
  reassessing a concurrent refusal or data skew.
- **Catalog-v2 activation is a one-way compatibility fence.** `db.create_index(...)` and the
  explicit idempotent `db.ensure_identity_indexes()` may activate it. Every process that can open
  that database must therefore run a Grafx build that understands catalog v2; rollback uses a
  pre-activation backup or logical export, not an older binary against migrated bytes.

### Embeddings, first class

- `CREATE VECTOR SPACE` declares a dimension, a metric (`cosine`, `l2`, `dot`) and a storage dtype.
- A node table declares a `VECTOR(space)` column, and the index that makes it searchable is created
  with the table.
- `db.search_vectors(reader, space=…, k=…, query=…)` returns the nearest rows visible to an active
  read transaction, reporting the **regime** it answered in (`exact` or `approximate`) and the
  `achieved_k`, so a caller can tell an exhaustive answer from an approximate one. The database
  validates that the transaction is active and belongs to it; no raw transaction context or
  mutable vector engine is exposed.
- A bounded query-language search over one unfiltered node table can use the vector index as a
  complete access path: after a durable frontier proof over the engine-owned immutable snapshot,
  and the exact planned table/column pair, the query layer decodes only the returned
  `VectorHit.ref` rows from the heap (at most `K`, not the table's `N`). The approximate hot path
  therefore avoids the former heap scan; the exact oracle still performs its intentional
  exhaustive heap validation once, but no longer pays for an additional `NodeScan`. Filters,
  joins/traversals, historical/custom snapshots, stale indexes, unbounded searches and uncertain
  sparse cardinality retain the canonical materialised path; staged owner rows keep the existing
  fail-closed RYOW refusal.
  `QueryResult.statistics` reports `vector_direct_accesses` and
  `vector_rows_materialized` when the direct path is selected.

## Embedding example

```python
with db.begin("write") as txn:
    txn.execute("CREATE VECTOR SPACE minilm {dimension: 384, metric: 'cosine'}")
    txn.execute(
        "CREATE NODE TABLE Chunk(id INT64, body STRING, embedding VECTOR(minilm), PRIMARY KEY(id))"
    )

with db.begin("write") as txn:
    txn.execute(
        "CREATE (:Chunk {id: 1, body: 'the text', embedding: $e})",
        {"e": [0.1] * 384},
    )

reader = db.begin("read")
try:
    hits = db.search_vectors(reader, space="minilm", k=10, query=[0.1] * 384)
    print(hits.regime, hits.achieved_k)   # 'exact' or 'approximate', and how many it reached
finally:
    reader.rollback()
```

## Candidate filters, metrics and precision

For enumerated candidates use the immutable exact filter, not a Python callback:

```python
from okto_grafx.domain.vector.filter import RecordIdFilter

# `record_ids` must be Grafx record IDs obtained for this store, NOT user PKs.
candidate_filter = RecordIdFilter.of(record_ids)
with db.begin("read") as reader:
    result = db.search_vectors(
        reader, space="minilm", query=[0.1] * 384, k=10,
        candidate_filter=candidate_filter,
    )
    print(result.regime, result.requested_k, result.achieved_k)
```

An empty candidate set is not “no filter”. Arbitrary predicates/subclasses refuse
at the public boundary; they could run host code while a reader pin is held.
Filter cardinality, snapshot/index freshness and query eligibility influence the
exact/approximate regime. ANN is approximate, not guaranteed full recall; report
`achieved_k`, not a fictitious k hits. Dirty writes affecting the searched vector
table remain fail-closed; perform search in an appropriate clean/read transaction.

Declared metrics are `cosine`, `l2`, `dot`; storage dtype is `float32` or `float64`.
Space/dimension/dtype and finite-component validation remain active under `[accel]`.
The engine does not generate embeddings. NumPy vector arithmetic has a declared
tolerance; exact-vs-approximate search and storage precision are separate decisions.

`vector_exact_scan_threshold`/`vector_ef_search` are runtime controls; construction
knobs and a recall target are not public connection settings. A stale exact index
can fall back to a scan; proximity-index repair has explicit freshness/rebuild
semantics. `rebuild_vector_index(space)` derives from valid heap data, not from
an untrusted external snapshot. For index health use `read_index_status` and
verification, not only the absence of an exception from a query.

## HNSW derived-picture memory

`connect(path, vector_hnsw_memory_budget_bytes=64 * 1024 * 1024)` opts into a
positive logical-byte ceiling for **each** derived HNSW picture, including cold
construction work and warm insertion work. `None` (default) preserves unlimited
admission. It does not change persisted vector/index bytes, approximate recall,
the exact/ANN planner threshold, or the chosen arithmetic adapter.

Use `db.vector_memory_usage("space_name")` to obtain an immutable
`VectorMemoryUsage` with `space`, `limit_bytes`, `cached_entries`,
`cached_logical_bytes`, `peak_requested_bytes`, `budget_refusals` and
`warm_retirements`. This observes the local attached index only: it neither builds
the HNSW graph nor reads device pages or certifies that a cached picture is fresh.
Counts include retained tombstoned versions. Counters reset with index/handle
replacement; requested peak includes refused reservations. A missing space/index
keeps normal typed refusal; a custom collaborator without this observation refuses
with `GrafxUnsupportedOperation`.

The deterministic tariff uses `N` stored entries, `D` dimensions, `M` neighbours
(currently 16), and `E` construction beam (currently 200):

- Resident: `4096 + N * (1024 + 32*D + 32*M*34)` logical bytes.
- Insertion work: resident plus `128*N + 128*E + 64*D`.
- Cold build: insertion work plus `N * (256 + 64*M*34)` for retained headers,
  sort slots and transient link-score capacity.

Maximum-height towers are reserved even for short towers, and compact adapters
receive the same component tariff. This is deliberately conservative, **not RSS**.
One page decoder, Python allocator overhead, query/search buffers and custom math
provider allocations are outside the envelope. Distinct spaces, handles and
simultaneously retained old/replacement pictures have distinct envelopes; adding
participants can multiply memory. Use process/container limits for an RSS policy.

Header admission refuses before materializing an over-budget header collection
or resolving vectors. A cold ANN request exceeding the limit raises
`GrafxQueryBudgetExceeded` with resource `vector_hnsw_memory`, `requested_bytes`
and `limit_bytes`; no partial cache is published and the caller's reader remains
usable. No silent switch to a different recall regime occurs. Exact scans do not
construct HNSW and remain governed by the normal query limits.

If a **durable write** would overgrow a warm picture, that derived cache is retired
and the write succeeds. The next ANN build may refuse; raise the budget or choose
an explicit exact-search policy as appropriate. Do not set a tiny budget expecting
automatic exact fallback or a cap shared by all handles. This policy cannot fail
or undo a proved COMMIT and never weakens WAL/OCC or reader isolation.
