# Indexes and vector search

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
open transaction. Hash `bucket_count` is a power of two from 1–4,096;
`expected_cardinality` is 1–262,144; both together refuse. Rehash grows hash
directories, while `rebuild_index` reconstructs a fresh immutable generation.

## Indexes

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
  decide. `None` means only “no assisted growth was selected now” (or the 4,096-bucket ceiling),
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
