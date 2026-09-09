# SPEC-GX-CAP-6 — Explainable hybrid retrieval

Status: bounded native v1 implemented in 0.0.5 development; acceptance tracked below. Date: 2026-09-08.
Dependencies: GX-CAP-5; real M2 HNSW access path.

## Normative scope

[Full complementary requirements](../archive/ROADMAP_SOURCES.md#source-grafx-complementary-evolution-plan-codex), §10; §17 GX-CAP-6.
[Full agent requirements](../archive/ROADMAP_SOURCES.md#source-agent-first-evolution-plan-codex) remains incorporated
in full under the [roadmap](../../ROADMAP.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: RRF/weighted RRF; candidate union/intersection; bounded graph boost/filter; complete source score breakdown; explicit fallback/partial regimes; recall/conformance harness.

## Contract and implementation boundary

Binding decision: [ADR-GX-005-SEARCH.md](../architecture/ADR-GX-005-SEARCH.md).
Owning modules: `domain/query/hybrid.py` (options/result DTOs), `engine/hybrid.py`
(fusion and bounded graph evidence), `Database.search_hybrid` (public API).
No model provider lives in the core.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

The implemented error taxonomy reuses configuration, transaction-state,
unsupported-operation, query-budget, cancellation/deadline and corruption errors.
Missing-source partial results require explicit policy; source corruption and
incompatible reader ownership never become partial success. No generic catch-all
wrapper or new error code is introduced. See the consumer contract for exact bounds
and the non-preemptive native vector phase.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Compare lexical-only/vector-only/fused results, deterministic ties, no filter leakage, cold/warm p99 and RSS disclosure, exact/approximate and partial regime tests.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: the [typed consumer contract](../HYBRID_SEARCH.md) is implemented
on top of native FTS/vector sources with no persisted hybrid format. The
[four-item receipt](../reports/V005_SEARCH_RESUME_CHECKPOINT.md) records tests,
measurements and limitations. Single-table/single-vector-binding and bounded
relationship-scan semantics are explicit; no general expansion or universal ANN
recall is claimed. No release or installed Pulse feature follows from this status.
