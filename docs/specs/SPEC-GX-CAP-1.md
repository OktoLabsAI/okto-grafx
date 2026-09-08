# SPEC-GX-CAP-1 — Commit identity and provenance

Status: specified / implementation not certified. Date: 2026-09-08.
Dependencies: M1 typed API; GX-CAP-0.

## Normative scope

[Complementary roadmap](../../GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md), §6; §17 GX-CAP-1.
[Agent-first roadmap](../../AGENT_FIRST_EVOLUTION_PLAN_CODEX.md) remains incorporated
in full under the [main plan](../../EVOLUTION_PLAN_CODEX.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: CommitId; bounded immutable CommitMetadata; commit lookup/catalog; replay-idempotent recovery; typed views; metrics and verification; coordinated logical export/import hooks.

## Contract and implementation boundary

Binding decision: [ADR-GX-003-COMMIT-IDENTITY.md](../architecture/ADR-GX-003-COMMIT-IDENTITY.md).
Planned owning modules: Domain commit DTO/codec; engine transaction/recovery/catalog orchestration; runtime clock/identity ports; public API/verify; logical transfer hooks.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

Error taxonomy to specify as public typed outcomes before implementation:
commit identity/type/range, metadata type/byte/key bounds, outcome ambiguity, missing identity, corrupt catalog, unsupported capability. Names/fields must be reconciled with existing public errors, not added as
generic catch-all wrappers. Cancellation and uncertain durable outcomes stay distinct.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Prove multiprocess ordering, metadata atomicity, clock tie/regression handling, all new crash cuts, full regression and mutation tests. Source §6 requirements all remain mandatory.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: **not implemented by GX-CAP-0**. No release, new on-disk capability,
installed Pulse feature, or successful crash matrix is inferred from this document.
Record immutable code SHA, test commands/results, audit and unresolved debt here when
implemented. Next prerequisite is the first unmet dependency, not another scope expansion.
