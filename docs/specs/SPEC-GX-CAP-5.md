# SPEC-GX-CAP-5 — Native full-text search

Status: specified / implementation not certified. Date: 2026-09-08.
Dependencies: Existing index lifecycle/freshness, budgets and planner; GX-CAP-0.

## Normative scope

[Full complementary requirements](../archive/ROADMAP_SOURCES.md#source-grafx-complementary-evolution-plan-codex), §9; §17 GX-CAP-5.
[Full agent requirements](../archive/ROADMAP_SOURCES.md#source-agent-first-evolution-plan-codex) remains incorporated
in full under the [roadmap](../../ROADMAP.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: Persisted FTS definitions; standard/keyword/code_identifier/whitespace analyzers; BM25; transactional update/tombstones; typed search and procedure; analyzer rebuild/versioning; cold/warm corpus.

## Contract and implementation boundary

Binding decision: [ADR-GX-005-SEARCH.md](../architecture/ADR-GX-005-SEARCH.md).
Planned owning modules: Domain analyzer/index model; engine index lifecycle/search/recovery/planner; API/verify; adapter implementations only behind ports.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

Error taxonomy to specify as public typed outcomes before implementation:
unsupported analyzer/version, invalid query/filter, resource exhaustion, stale/incomplete generation, corrupt postings/statistics. Names/fields must be reconciled with existing public errors, not added as
generic catch-all wrappers. Cancellation and uncertain durable outcomes stay distinct.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Snapshot correctness, no silently partial/stale answers, deterministic ranking, exact technical terms, memory/work budgets, concurrent rebuild, full crash/fault and analyzer-upgrade conformance.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: **not implemented by GX-CAP-0**. No release, new on-disk capability,
installed Pulse feature, or successful crash matrix is inferred from this document.
Record immutable code SHA, test commands/results, audit and unresolved debt here when
implemented. Next prerequisite is the first unmet dependency, not another scope expansion.
