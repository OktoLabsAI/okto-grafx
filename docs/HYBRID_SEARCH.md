# Native hybrid search

[Documentation index](README.md) · [FTS](FULL_TEXT_SEARCH.md) · [Vectors](INDEXES_AND_VECTORS.md) · [API](API_REFERENCE.md)

Available in 0.0.5 development. `Database.search_hybrid` combines native lexical
and vector candidate windows with weighted reciprocal rank fusion (RRF-v1), on
**one owned reader snapshot**. Optional bounded graph evidence boosts or filters
those candidates. It does not generate embeddings, call an LLM or persist a new
search store. Python is the supported door; no hybrid Cypher/CALL syntax exists.

## Working example

```python
from okto_grafx import connect, HybridSearchOptions
from okto_grafx.domain.vector.filter import RecordIdFilter

with connect(':memory:') as db:
    with db.begin() as tx:
        tx.execute("CREATE VECTOR SPACE semantic {dimension:2, metric:'cosine'}")
        tx.execute('CREATE NODE TABLE Doc(id INT64, body STRING, v VECTOR(semantic), PRIMARY KEY(id))')
        tx.execute('CREATE REL TABLE Related(FROM Doc TO Doc)')
        tx.execute("CREATE (:Doc {id:1,body:'durable wal',v:[1.0,0.0]})")
        tx.execute("CREATE (:Doc {id:2,body:'wal recovery',v:[0.8,0.2]})")
        tx.execute('MATCH (a:Doc {id:1}), (b:Doc {id:2}) CREATE (a)-[:Related]->(b)')
    db.create_text_index('doc_text', 'Doc', ('body',))
    db.ensure_identity_indexes()  # required only for graph evidence
    with db.begin('read') as reader:
        lexical = db.search_text(reader, index='doc_text', query='durable')
        seed = lexical.hits[0].record_id  # native RecordId, not application PK
        found = db.search_hybrid(reader, table='Doc', index='doc_text',
            query='wal', space='semantic', vector=(1.0,0.0), k=2,
            options=HybridSearchOptions(candidate_k=20, graph_relations=('Related',),
                graph_seeds=(seed,), graph_filter=True, graph_weight=0.5))
        assert len(found.hits) == 2 and found.regime == 'complete'
        allowed = RecordIdFilter.of([seed])
        filtered = db.search_hybrid(reader, table='Doc', index='doc_text',
            query='wal', space='semantic', vector=(1.0,0.0), filter=allowed)
        assert [hit.record_id for hit in filtered.hits] == [seed]
```

Signature: `search_hybrid(reader=None, *, table, index, query, space, vector, k=20,
options=None, filter=None, text_limits=None, timeout_seconds=None, cancellation=None)`.
An omitted reader is opened and closed by Grafx. A supplied reader must be an
active read transaction of this handle; writes/foreign/closed readers refuse.
All source execution and graph evidence stay on its snapshot, including old readers
while another participant commits. No read operation builds an index or writes data.

The target is one node table. The FTS index must cover that table. The vector space
must bind to **exactly one vector column, on that table**: native RecordIds are
table-qualified, while vector-space results do not carry a table discriminator.
Multiple bindings are refused rather than post-filtered after top-k. Create separate
spaces when different tables need independent retrieval. To disable a source, set
its weight to zero; pass `index=None` or `space=None` if unused, and `vector=()` for
lexical-only search. At least one text/vector source must remain enabled.

## Options

`HybridSearchOptions` is immutable and per call, never a `connect` option:

| Field | Default | Meaning / when to change |
| --- | --- | --- |
| `lexical_weight` | 1.0 | Finite 0..1000; favor exact terminology or zero to disable text. |
| `vector_weight` | 1.0 | Finite 0..1000; favor semantic neighbors or zero to disable vectors. |
| `rrf_k` | 60 | Integer 1..100000; larger values flatten rank differences. Not result k. |
| `candidate_k` | 100 | Integer 1..10000; maximum hits requested from each source. Larger windows can improve recall but increase source/fusion cost. Require 1 <= result k <= candidate_k. |
| `fusion` | `union` | `union` or `intersection` of available enabled source windows. Intersection can be empty even with good individual results. |
| `allow_partial` | False | Only missing logical FTS index/vector binding can be reported as unavailable. True uses remaining enabled sources and marks partial. Never swallows corruption, stale indexes, missing engines, budget/deadline or validation failures. |
| `graph_relations` | `()` | Unique tuple of up to 16 relationship table names, each joining target table to itself. Explicit allowlist. |
| `graph_seeds` | `()` | Up to 10000 positive native RecordIds; must each have exactly one visible node. Duplicates do not multiply boost. Required with relations for graph boost/filter. |
| `graph_direction` | `out` | `out`, `in`, `both`; direction of relationship evidence. |
| `graph_hops` | 1 | 1..8 BFS hops. Seeds have distance 0; shortest permitted distance wins. |
| `graph_weight` | 0.0 | Finite 0..1000; adds weight/(rrf_k + distance + 1) for reachable candidates. Zero disables boost, not an explicit graph_filter. |
| `graph_filter` | False | Keep only base text/vector candidates reachable from seeds; does not create graph-only hits. |
| `max_graph_edges` | 10000 | 1..1000000; scan counts all visible scanned relationships; incident mode counts unique visible `(table, relationship ID)` encountered from the frontier, including parallel and filtered-out edges. Exceeding refuses, never truncates traversal. |
| `max_memory_bytes` | 33554432 | 1..2³¹ aggregate logical operation envelope: simultaneous fusion, lexical/vector temporary work and graph work. Not process RSS or resident engine/vector cache size. See accounting below. |
| `graph_access` | `auto` | `auto` uses certified endpoint indexes when all selected paths exist; absent paths select scan before lookup. `scan` explicitly retains the canonical full-relation path for comparison/legacy budget behavior. Corruption or refusal after index adoption never triggers fallback. |

`filter` is an immutable `RecordIdFilter`; derive allowed IDs from your application's
policy using the same reader. Both sources apply it before their own top-k. For
graph evidence, seeds and **both endpoints of every traversable edge** must be allowed;
paths cannot traverse excluded nodes to boost included hits. All edge endpoints are
validated through native exact identity/heap certificates. Missing identity indexes
refuse: prepare them with `ensure_identity_indexes()` outside the read operation.
Duplicate/missing visible endpoints refuse; no dangling edge becomes evidence.

Graph defaults to indexed BFS over the allowlisted tables: only endpoint buckets
for the current frontier are probed, under native pre/post certificates and heap
visibility/key/endpoint validation. Collisions, skew and retained versions can still
increase work; this is not a universal O(neighborhood) or constant-time promise.
The explicit/fallback scan builds bounded adjacency in O(selected relationships).
Both paths preserve shortest-hop results, cycles, self-loops and parallel-edge
semantics. Unvisited disconnected relationships are not a whole-graph integrity audit;
use `verify` for that. Graph disabled means zero graph scanning.
The edge counter is not a physical-page/I/O ceiling: scans can also examine retained
invisible MVCC versions. Page decoding and a single native scan call are not
preempted by the outer cooperative deadline.

## Results, ranking and failure policy

For each candidate: `lexical_weight/(rrf_k+lexical_rank) +
vector_weight/(rrf_k+vector_rank) + graph_boost`, omitting unavailable terms. Ranks
are one-based in each filtered source window. Final ties use ascending RecordId.
RRF does not add incomparable raw BM25 and vector scores. With one source and no
graph boost it preserves that source order. It ranks a finite union/intersection,
not the global RRF top-k of every document beyond the configured windows.

`HybridHit`: table, record_id, score (fused), lexical_rank/lexical_score,
vector_rank/vector_score and graph_distance. Missing evidence is None, not zero.
`HybridSearchResult`: hits, snapshot_commit, fusion (`rrf_v1_union` or
`rrf_v1_intersection`), regime (`complete`/`partial`), lexical_regime, vector_regime,
lexical_candidates, vector_candidates, graph_edges_visited, source_errors and
lexical_index_built_through_commit (None when disabled/unavailable), `graph_regime`
(`disabled`, `scan`, `incident_index`) and logical `memory_peak_bytes`,
`lexical_memory_peak_bytes`, `vector_memory_peak_bytes`, `graph_memory_peak_bytes`.
Phase peaks are not simultaneous and must not be summed as actual peak RSS. The complete
status concerns source availability, **not exact ANN recall**. Preserve vector
regime/fallback explanations; empty hits do not erase source disposition.
Candidate counts describe the returned source windows, not total matching rows in
the table. Use the ordinary counting API when the UI needs a corpus total.

`source_errors` contains (`lexical`, `missing_index`) and/or (`vector`,
`missing_space_binding`) only. If all enabled sources are unavailable, even explicit
partial mode refuses with `GrafxUnsupportedOperation`. An enabled but valid empty
source is not missing. Use partial only when your application can expose degraded
retrieval; do not enable it to mask corruption or authorization failures.

`text_limits` supplies `TextSearchLimits` to the lexical source with default BM25
k1=1.2/b=0.75. Vector work follows native connection settings, including
`vector_exact_scan_threshold`, `vector_ef_search` and vector budgets. The aggregate
logical envelope reserves 1024 bytes per fusion window slot, 64 per allowed ID,
128 per seed/frontier/edge, plus lexical accounting (including bounded WAL proof
reservation), vector candidate components and ANN navigation containers. Native
source limits still apply independently. Engine caches, a cold HNSW picture and its
construction state, page/decoder buffers, allocator overhead, sorting temporaries
and caller-owned result retention are not an RSS-capped memory pool. HNSW pictures
have a separate optional `vector_hnsw_memory_budget_bytes` admission policy; see
[its independent scope and tariffs](INDEXES_AND_VECTORS.md#hnsw-derived-picture-memory). Inspect native
cache/vector budgets separately. Source reservations are released between phases.

Cancellation/deadlines share one control across lexical, vector and graph work,
including native vector candidate/scoring/navigation loops and between cold HNSW
insertions. They cannot preempt a blocking storage/math call, initial admission,
native header capture/sorting or one HNSW construction insertion. A vector phase
that returns after deadline yields a typed
deadline error, not late successful hits. No write/COMMIT is cancelled.

Invalid options/bindings/readers, exhausted budgets and corruption preserve the
existing typed error taxonomy. No broad retry or automatic repair is hidden here.
No persisted fusion format, embedding provider, probabilistic reranker, cross-table
graph expansion or application ContextPack API is introduced.
