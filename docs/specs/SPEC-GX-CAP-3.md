# SPEC-GX-CAP-3 — Temporal system time

Status: specified / implementation not certified. Date: 2026-09-08.
Dependencies: GX-CAP-1; capability manifest; maintenance baseline.

## Normative scope

[Full complementary requirements](../archive/ROADMAP_SOURCES.md#source-grafx-complementary-evolution-plan-codex), §7 (T1); §17 GX-CAP-3.
[Full agent requirements](../archive/ROADMAP_SOURCES.md#source-agent-first-evolution-plan-codex) remains incorporated
in full under the [roadmap](../../ROADMAP.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: Node/relationship table opt-in; atomic current/history effects; as-of commit and timestamp APIs; versions; historical traversal; manual retention; temporal verify.

## Contract and implementation boundary

Binding decision: [ADR-GX-002-TEMPORAL.md](../architecture/ADR-GX-002-TEMPORAL.md).
Planned owning modules: Domain temporal model; engine history/query/recovery/retention; API/schema/verify; runtime composition.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

Error taxonomy to specify as public typed outcomes before implementation:
invalid temporal coordinates, unavailable/expired history horizon, wrong store, corrupt intervals/endpoints, unsupported temporal capability. Names/fields must be reconciled with existing public errors, not added as
generic catch-all wrappers. Cancellation and uncertain durable outcomes stay distinct.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Create/update/delete/recreate, no gaps/overlaps across crash/recovery, independent MVCC vacuum, endpoints/lineage coherence, pinned retention, published write amplification and growth.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: **not implemented by GX-CAP-0**. No release, new on-disk capability,
installed Pulse feature, or successful crash matrix is inferred from this document.
Record immutable code SHA, test commands/results, audit and unresolved debt here when
implemented. Next prerequisite is the first unmet dependency, not another scope expansion.
