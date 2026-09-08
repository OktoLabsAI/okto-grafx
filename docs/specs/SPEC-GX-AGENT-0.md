# SPEC-GX-AGENT-0 — Optional Agent/MCP preview

Status: specified / implementation not certified. Date: 2026-09-08.
Dependencies: GX-CAP-1 + GX-CAP-2; graph/vector baseline.

## Normative scope

[Full complementary requirements](../archive/ROADMAP_SOURCES.md#source-grafx-complementary-evolution-plan-codex), §16; §17 GX-AGENT-0; agent-first §§1–11, 14–16, 18–23 (AGENT-0..5).
[Full agent requirements](../archive/ROADMAP_SOURCES.md#source-agent-first-evolution-plan-codex) remains incorporated
in full under the [roadmap](../../ROADMAP.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: Separate agent/workspace/MCP wheels; versioned system schema; injected identity/session/provenance; semantic idempotency; memory/claim/evidence primitives; minimal stdio tools/resources; project/user policy; two-harness demo. Detailed namespace/governance AGENT-5 remains mandatory, not erased by the preview label.

## Contract and implementation boundary

Binding decision: [ADR-GX-001-DATABASE-FIRST.md](../architecture/ADR-GX-001-DATABASE-FIRST.md).
Planned owning modules: Only optional product packages and public generic APIs; generic gaps require their own database contracts first.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

Error taxonomy to specify as public typed outcomes before implementation:
invalid profile/scope, denied capability, idempotency payload mismatch, schema mismatch, malformed protocol input, bounded payload. Names/fields must be reconciled with existing public errors, not added as
generic catch-all wrappers. Cancellation and uncertain durable outcomes stay distinct.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Non-forgeable author in configured process threat model, shared durable project state, no raw path in tools, idempotent retries, core import isolation; detailed AGENT-0..5 gates remain independently required.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: **not implemented by GX-CAP-0**. No release, new on-disk capability,
installed Pulse feature, or successful crash matrix is inferred from this document.
Record immutable code SHA, test commands/results, audit and unresolved debt here when
implemented. Next prerequisite is the first unmet dependency, not another scope expansion.
