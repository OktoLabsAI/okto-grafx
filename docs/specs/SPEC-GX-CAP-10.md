# SPEC-GX-CAP-10 — Application schema evolution and views

Status: specified / implementation not certified. Date: 2026-09-08.
Dependencies: Commit metadata; schema/catalog epochs; backup/export; changefeed for incremental refresh.

## Normative scope

[Full complementary requirements](../archive/ROADMAP_SOURCES.md#source-grafx-complementary-evolution-plan-codex), §§14–15; §17 GX-CAP-10.
[Full agent requirements](../archive/ROADMAP_SOURCES.md#source-agent-first-evolution-plan-codex) remains incorporated
in full under the [roadmap](../../ROADMAP.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: S1 metadata-safe migrations; checksum/versioned migration ledger and dry run; resumable S2 subset; logical views; manual materialized refresh; explicit stale/source commit metadata. Full S2/S3 and incremental-refresh prerequisites remain tracked.

## Contract and implementation boundary

Binding decision: [ADR-GX-001-DATABASE-FIRST.md](../architecture/ADR-GX-001-DATABASE-FIRST.md).
Planned owning modules: Domain schema/view DTOs; engine catalog/query/maintenance; public facade; existing physical migration reused.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

Error taxonomy to specify as public typed outcomes before implementation:
migration checksum/version conflict, unsupported online rewrite, invalid/cyclic view, stale dependency, resource limit. Names/fields must be reconciled with existing public errors, not added as
generic catch-all wrappers. Cancellation and uncertain durable outcomes stay distinct.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Crash-safe migrations, old-reader/plan epoch handling, atomic generation swaps, retained history decodability, n-1/n fixtures, migration retry/checksum conflict, no silent incompatible reads.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: **not implemented by GX-CAP-0**. No release, new on-disk capability,
installed Pulse feature, or successful crash matrix is inferred from this document.
Record immutable code SHA, test commands/results, audit and unresolved debt here when
implemented. Next prerequisite is the first unmet dependency, not another scope expansion.
