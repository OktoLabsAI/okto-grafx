# SPEC-GX-CAP-3 — Temporal system time

Status: native bounded system-time slice implemented and locally validated;
full scope remains partial. Original specification: 2026-09-08.

Current implementation: [typed consumer APIs](../SYSTEM_TIME_HISTORY.md),
[native bytes/recovery](SYSTEM_HISTORY_V1.md) and
[round acceptance](../reports/V006_NATIVE_HISTORY_ROUND.md). Atomic current/history,
as-of/versions, historical schema/edges, pins, payload retention, verify and physical
backup are delivered. Temporal indexes, physical compaction, complete temporal
logical transfer and embedding-space timelines remain outside this slice.
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

## Continuation item 8: implementation handoff (September 10)

**Historical boundary at `204bd2e`, superseded by the native follow-up linked above.**
The statements below describe that earlier prototype, not the current implementation.

The preceding repeated-key physical layout is not the history representation.
Its entries are reclaimable, and exact candidates remain tied to heap visibility.
Retaining that index, the decoded-page memo or recycled WAL cannot implement this
contract. No system-time catalog bit, table activation or public history API has
been enabled by that work. Item 8 now has an
[internal event/image-planning prototype](SYSTEM_HISTORY_APPEND_DRAFT.md), but
its native persistence checkpoint remains unimplemented and unexposed.

The existing native integration points are `TransactionManager._write_rows`
(settled row identities and old/new values), `_prepare_journal` followed by
`_materialized_page_delta` (attempt-local physical planning before second OCC),
`_build_records` / `_retarget_commit_batch` (binding the final COMMIT sequence),
and `CommitRedo._validate_native_catalog` (complete preflight before application).
A history append needs the corresponding immutable preparation, complete
publication/coverage proof and retargeting, not a post-commit callback or late
logical intents. Any selected format must also participate in backup, verification
and transfer/refusal; existing ordinary-table copy receipts are not a substitute.

This records the implementation boundary, not an additional performance gate,
new scope, released format or successful temporal crash test. The fixed acceptance
criteria above and ADR-GX-002 remain the authority.
