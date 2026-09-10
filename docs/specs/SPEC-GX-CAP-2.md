# SPEC-GX-CAP-2 — Catalog sessions and workspace scopes

Status: bounded slices implemented and locally validated; full scope remains partial.
Original specification: 2026-09-08; implementation status reconciled 2026-09-10.
Dependencies: M1 lifecycle; GX-CAP-0; GX-CAP-1 for promotion receipts.

September 10, 2026 checkpoint: the 0.0.6 development line implements the
non-persisting `CatalogSession` and optional bounded workspace resolver. See
[consumer contract](../CATALOGS_AND_WORKSPACES.md) and
[active execution status](../../ROADMAP.md#approved-capability-continuation-after-4ee4d2e).
The full CAP-2 scope is **not complete**. The subsequent bounded existing-target
[copy/receipt slice](../CATALOG_COPY.md) is implemented; arbitrary subgraph
selection by arbitrary predicates, automatic endpoint closure, merge policies and
anonymous-node copy remain pending. The latest follow-up also delivers explicit
selected RIDs, skip-node conflicts and catalog/workspace JSON CLI inventory.
[Latest acceptance](../reports/V006_NATIVE_HISTORY_ROUND.md) supersedes the initial
checkpoint for those named slices, not for the entire CAP-2 specification.
[Local acceptance](../reports/V006_CATALOG_WORKSPACE_ACCEPTANCE.md): 437 affected
regression tests passed; full repository/POSIX/release matrices are not inferred.

## Normative scope

[Full complementary requirements](../archive/ROADMAP_SOURCES.md#source-grafx-complementary-evolution-plan-codex), §8; §17 GX-CAP-2.
[Full agent requirements](../archive/ROADMAP_SOURCES.md#source-agent-first-evolution-plan-codex) remains incorporated
in full under the [roadmap](../../ROADMAP.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: Read-only attach/detach; catalog-pinned begin; safe store UUID identity; generic workspace resolver; project/user conventions; explicit idempotent copy/promotion receipts; path policies; CLI status/list.

## Contract and implementation boundary

Binding decision: [ADR-GX-004-ATTACHED-CATALOGS.md](../architecture/ADR-GX-004-ATTACHED-CATALOGS.md).
Planned owning modules: Public catalog-session API; runtime lifecycle; optional workspace package; existing logical transfer surface.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

Error taxonomy to specify as public typed outcomes before implementation:
ambiguous/duplicate/reserved alias, denied path, read-only mutation, in-use detach, mismatched idempotency input, independent-source failure. Names/fields must be reconciled with existing public errors, not added as
generic catch-all wrappers. Cancellation and uncertain durable outcomes stay distinct.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Enforce single-catalog writes and no cross-store physical edges; prove snapshot identification, two-process resolver conformance, lifecycle cleanup, and copy retries across target-commit crashes.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: **not implemented by GX-CAP-0**. No release, new on-disk capability,
installed Pulse feature, or successful crash matrix is inferred from this document.
Record immutable code SHA, test commands/results, audit and unresolved debt here when
implemented. Next prerequisite is the first unmet dependency, not another scope expansion.
