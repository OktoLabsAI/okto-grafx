# SPEC-GX-CAP-7 — Extension SPI and UDFs

Status: specified / implementation not certified. Date: 2026-09-08.
Dependencies: GX-CAP-0; typed values and cursors.

## Normative scope

[Complementary roadmap](../../GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md), §11; §17 GX-CAP-7.
[Agent-first roadmap](../../AGENT_FIRST_EVOLUTION_PLAN_CODEX.md) remains incorporated
in full under the [main plan](../../EVOLUTION_PLAN_CODEX.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: Versioned manifest/registry; scalar/aggregate UDF; read-only procedures/table functions; analyzer/algorithm providers; privileged admin contract; allowlist and trust documentation; external consumer fixture.

## Contract and implementation boundary

Binding decision: [ADR-GX-006-EXTENSION-TRUST.md](../architecture/ADR-GX-006-EXTENSION-TRUST.md).
Planned owning modules: Public SPI DTOs; engine registry policy; runtime explicit provider loading; external consumer tests.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

Error taxonomy to specify as public typed outcomes before implementation:
incompatible manifest, duplicate name, denied load/capability, provider exception, cancellation; preserve native transaction refusal. Names/fields must be reconciled with existing public errors, not added as
generic catch-all wrappers. Cancellation and uncertain durable outcomes stay distinct.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Public-only imports, incompatible provider refusal, exception/outcome isolation, typing/documentation CI, deterministic function conformance and concurrent load/unload lifecycle.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: **not implemented by GX-CAP-0**. No release, new on-disk capability,
installed Pulse feature, or successful crash matrix is inferred from this document.
Record immutable code SHA, test commands/results, audit and unresolved debt here when
implemented. Next prerequisite is the first unmet dependency, not another scope expansion.
