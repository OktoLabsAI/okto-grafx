# SPEC-GX-CAP-9 — Graph projections and algorithms

Status: bounded read-only projections and algorithms implemented in 0.0.5 development; broader scope remains planned.
Date: 2026-09-09. [Consumer contract](../GRAPH_PROJECTIONS.md),
[foundation receipt](../reports/V005_AFTER_69ED311.md) and
[algorithm continuation](../reports/V005_AFTER_7DDE256.md).
Dependencies: GX-CAP-7; existing traversal/query budgets.

The continuation after `a4dd85a` adds retained PageRank transition preparation,
simple-undirected topology reuse and deterministic bounded asynchronous label
propagation. [Usage/charges](../GRAPH_PROJECTIONS.md),
[NetworkX conformance and graph exchange](../GRAPH_EXCHANGE.md).
This does not implement Louvain, persistent projection catalogs or mutation/write
modes. Final grouped acceptance is recorded in the [roadmap](../../ROADMAP.md).

The continuation after `970aa1e` adds retained node identity lookup, local-path
discovery budgets, bucket k-core, opt-in NumPy PageRank, snapshot numeric weights,
Dijkstra and weighted/personalized PageRank. [Detailed contracts](../GRAPH_PROJECTIONS.md)
and [validation receipt](../reports/V005_AFTER_970AA1E.md). Derived state never
grants storage authority; multi-read/write, WAL and durability remain unchanged.

## Normative scope

[Full complementary requirements](../archive/ROADMAP_SOURCES.md#source-grafx-complementary-evolution-plan-codex), §13; §17 GX-CAP-9.
[Full agent requirements](../archive/ROADMAP_SOURCES.md#source-agent-first-evolution-plan-codex) remains incorporated
in full under the [roadmap](../../ROADMAP.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: Snapshot-bound projection catalog; degree/WCC/SCC/PageRank/k-core; stream/stats/mutate/write modes; NetworkX oracle; algorithm work/memory/cancellation controls. Later §13 algorithms remain backlog, not deleted.

## Contract and implementation boundary

Binding decision: [ADR-GX-001-DATABASE-FIRST.md](../architecture/ADR-GX-001-DATABASE-FIRST.md).
Planned owning modules: Public projection facade and DTO; optional algorithms wheel; public transactions only; reuse existing shortest-path work.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

Error taxonomy to specify as public typed outcomes before implementation:
invalid projection/type/orientation, insufficient resources, cancelled iteration, stale/conflicting write target. Names/fields must be reconciled with existing public errors, not added as
generic catch-all wrappers. Cancellation and uncertain durable outcomes stay distinct.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Deterministic small graph fixtures with weighted/directed/parallel/self-loop semantics, no leaked pins, immutable source projection, atomic conflict-aware write through public API.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: **not implemented by GX-CAP-0**; the continuation after `69ed311`
adds detached snapshot projection, degrees and weak components through public scans,
with logical limits, cancellation and an independent reachability oracle. The
continuation after `7dde256` adds projected batched capture, retained immutable CSR,
iterative SCC, BFS reachability/shortest paths, bounded unweighted PageRank and
simple-undirected k-core. Oracles use independent reachability, linear-system and
peeling implementations. Persisted catalog and mutation/write modes remain backlog.
NetworkX conformance was subsequently added for the explicitly listed algorithms
after `a4dd85a`; the original independent-oracle runs are not retroactively labeled
NetworkX runs.
No release, new on-disk capability,
installed Pulse feature, or successful crash matrix is inferred from this document.
Record immutable code SHA, test commands/results, audit and unresolved debt here when
implemented. Next prerequisite is the first unmet dependency, not another scope expansion.
