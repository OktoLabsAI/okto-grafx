# Okto Grafx roadmap

**Single active product backlog — reconciled September 8, 2026.**
Baseline: source `0.0.4`, checkpoint `485ea074bf2b9e174200b44bb74c7adb54c1ecab`
on `feature/gx-cap-1`; latest recorded Pulse measurement is a different build,
`0.0.4@fa8f188`. See [performance](docs/PERFORMANCE.md).

This roadmap includes **new capabilities, corrective work, known limitations,
operational hardening and developer experience**. It replaces the execution
authority of the former evolution/agent/performance/round plans. It does not
reopen completed work, authorize production data changes or imply release approval.

## Index

- [Rules and status vocabulary](#rules-and-status-vocabulary)
- [Current delivery boundary](#current-delivery-boundary)
- [Known limitations and corrective work](#known-limitations-and-corrective-work)
- [Remaining performance work](#remaining-performance-work)
- [Database capabilities and dependencies](#database-capabilities-and-dependencies)
- [Optional agent product](#optional-agent-product)
- [Completed foundations](#completed-foundations)
- [Legacy requirement register](#legacy-requirement-register)
- [Deferred and rejected directions](#deferred-and-rejected-directions)
- [Acceptance and maintenance](#acceptance-and-maintenance)

## Rules and status vocabulary

1. Preserve legitimate multi-process/multi-thread read/write access, snapshots,
   both OCC validations, writer fencing, WAL order, durability and fail-closed
   validation. Performance does not authorize weaker guarantees.
2. Keep the database generic. Grafx-specific Pulse behavior belongs in Community
   adapters/composition; Pulse Core remains backend-agnostic.
3. Fix demonstrated defects first. Prefer high-impact bounded work over repeated
   marginal profiling. Group regression tests after cohesive small changes;
   semantic/durability failures are blockers, historical timing ratios are not.
4. One active status here; specs define contracts and reports supply evidence.
   A specification, internal codec or passing isolated test is not a shipped API.
5. No release date or next version is promised by a row below. Publishing 0.0.4
   remains a joint operator action. Later feature/version allocation is explicit.

| Status | Meaning |
| --- | --- |
| Implemented checkpoint | Named implementation/evidence exists; scope is bounded, not universal certification |
| Partial | Some foundation exists; remaining work is mandatory before consumer exposure |
| Planned | Accepted product direction, not currently callable |
| Open limitation | Present constraint or unclosed acceptance requirement |
| Historical / revalidate | Older finding retained; do not assert it still reproduces without checking current source |
| Deferred / rejected | Not an active implementation task; reopening needs evidence/decision |

## Current delivery boundary

| ID | Status | Deliverable / exit condition |
| --- | --- | --- |
| REL-004 | Pending joint release | Finish documentation validation and agreed release review; confirm candidate/branch, then PR/merge/tag/build/install checks and PyPI publication with the operator. No upload is implied here. |
| DOC-1 | Implemented in this documentation refactor | One README entry point, public API/configuration/query/operations references, measured-performance table, one roadmap and a preserved source archive. Links, examples, API/config field coverage and preservation hashes are checked; [validation receipt](docs/reports/DOCUMENTATION_REFACTOR_2026_09_08.md). |
| CAP-1B | Implemented checkpoint | `6b5163e`: native journal preflight is connected to page application/checkpoint; UUID, activation/COMMIT coverage, target/resident LSN and file extents validated. 2,433 tests / 77.27 s recorded; no new typing diagnostics in the isolated comparison. This is recovery correctness, not a latency benchmark. |
| GX-CAP-1 remainder | Partial; no automatic emission/public history API | Complete writer journal staging/publication, public lookup/verification, metrics and logical-transfer semantics before exposing generic commit provenance. See [spec](docs/specs/SPEC-GX-CAP-1.md). Do not install/activate unfinished capability paths in Pulse. |
| PULSE-BENCH | Reserved workload | Latest recorded authorized run consumed one spec; 19 remain reserved. Do not consolidate more, redrive, rebuild or reset data to improve this document. New live runs need a deliberate workload decision. |

## Known limitations and corrective work

| ID / legacy mapping | Status and impact | Required work / acceptance |
| --- | --- | --- |
| OPS-1 / §8.1 | Planned: no generic public consistent hot-backup/restore API | Snapshot/file inventory, checksums, reader-horizon retention, interrupt-safe copy, restore into a new directory, identity/provenance handling, verify/reopen and crash tests. Never copy/replace live files as an alleged hot backup. |
| OPS-2 / §8.2 | Partial: physical scan primitives and consumer-owned logical transfer exist; no general versioned export/import product | Versioned schema/types/nodes/parallel edges/vectors, resumability, compatibility and identity remapping; reject unsupported formats before partial promotion. |
| OPS-3 / P1.4, §8.3 | Partial: manual foreground vacuum v1 | Safe overflow/page reclamation, file-space reduction and future online operation remain. Current vacuum requires real quiescence, does not truncate files/reuse identities, and skips overflow history. Any online extension needs reader/lifetime proofs and crash/reopen tests. |
| OPS-4 / P1.8 | Open limitation: buffer and query budgets are not process RSS caps | Account/measure all retained engine state, vector memory, concurrent handles and temporary encodings. Preserve deterministic refusal; publish a realistic peak-memory envelope, not an RSS promise derived from nominal page bytes. |
| OPS-5 / P1.12 | Partial: explicit growth/rebuild exists | Hash directories cap at 4,096 buckets; skew/overflow and retained immutable orphan generations remain. Define bounded reclamation and larger-scale indexing without in-place generation replacement. |
| OPS-6 / P1.11 | Open limitation: physical conflicts and exclusive publication | Remove only proven redundant publication/page work. Disjoint logical rows may still conflict; do not promise linear writer scaling or replace multiwriter with an application-wide single-writer premise. |
| OPS-7 / P2.6, P2.9 | Open DX/operational constraints | `connect(**options)` lacks fully explicit keyword autocomplete; checksum selection is process-global. Improve discoverability/isolation where justified, without silently changing provider or byte semantics. |
| OPS-8 / §8.5 | Partial: Query/cursor API exists | Reusable `Query` is not a durable prepared plan or HTTP token. General prepared statements/plan-cache invalidation, deadlines/cancellation and broader streaming need explicit semantics. |
| OPS-9 / P1.16 | Deferred configuration capability | Expose HNSW construction knobs only with persistent identity/defaults, cold/incremental parity expectations and a controlled 8,192×384 recall profile. `vector_ef_search` already configures runtime beam; it is not construction tuning. |
| OPS-10 / P2.8 | Acceptance obligation | Exercise supported Python versions/platforms, wheels/sdists, optional accelerators, version refusal and upgrade fixtures. A local Windows run is not a claim that every matrix row passed. |
| OPS-11 / query gaps | Open subset boundary | No full Cypher compatibility, arbitrary UNION/OPTIONAL combinations, general CALL/procedures or arbitrary schema ALTER/DROP contract. Extend through explicit query specs, budgets, null/ordering/error and read/write overlay tests. |
| OPS-12 / recovery | Open operational limit, not an auto-repair promise | CRC-invalid authoritative pages, foreign database identity, future/conflicting LSN, unsupported capability and unproved effects remain fail-closed. Product recovery must distinguish safe replay, evidence-preserving refusal and operator restore. |
| OPS-13 / historical static and mutation debts | Historical / revalidate | Retain all original survivors and component findings in the source register. Reproduce against current source before reopening; close with a named discriminating test, not a broad “all fixed” assertion. |
| PULSE-OPS | Consumer debt, not engine defect by default | Latest live report still lists historical Global/policy DLQ, canonical debt, stale diagnostics and incomplete runtime-budget attribution. Keep visible; generic Grafx changes do not automatically resolve Pulse policy/source-authority data. |

## Remaining performance work

This is a finite residual queue, not a requirement to rediscover every historic
micro-optimization. [Measurements](docs/PERFORMANCE.md) distinguish component,
native API, HTTP/MCP and UI boundaries.

| Priority / ID | Status / next bounded action | Success evidence |
| --- | --- | --- |
| 1 / PERF-WRITE | Open: attribute complete consolidation and Global delivery cost | Capture already-deployed phase observations in the next authorized real run; separate admission, graph commit, SQLite/outbox, verification and scheduling. Latest 10.726/54.952 s do not identify native-only cost. |
| 2 / PERF-COLD | Open: full KG cold-load and remaining relationship fan-out | Profile selected UI/API route with fixed result digest and error table; separate cold handle/index admission, per-layout work, verification and rendering. Do not label early 0.0.4 samples as final-source latency. |
| 3 / BATCH-REL-1 | Partial: transaction-local PK/identity batches and grouped endpoint verification implemented | Remaining per-layout statement fan-out may share bounded preparation only when independent statement errors, snapshots and budgets remain observable. Re-profile after landed batching before new API work. |
| 4 / CONC-CPU-1 | Open: CPU/GIL limits after legitimate I/O parallelism | Compare bounded independent participants, decode/expression cost and memory before adding processes/native kernels. No inference that more handles implies linear throughput. |
| 5 / PERF-SCALE | Partial: ordered cursor OIX work, index growth and vector access paths landed | Measure increasing N, fan-out, skew and churn on selected real query shapes. Canonical fallback may still be O(N); preserve it where completeness/validation requires it. |
| 6 / PERF-MEM | Partial: bounded caches, spill, vector-free landings and retained metrics/history improvements landed | Measure peak memory with realistic handle count and indexed writes; logical counters are not RSS. Do not discard validation to reduce allocations. |
| Future / AUTH-BUNDLE | Deferred, explicitly not needed to unblock current work | Possible cache/bundle of authority requires a separate security/invalidations design. The selected protocol remains complete pre-validation → operation with the real 30 s budget → post-validation/close, every phase fail-closed. Do not expand the authority surface merely to pass a gate. |

Completed Wave 0–3, OIX-0–3, source-reference seeks, query-expression reuse,
vector-free relationship landings, grouped endpoint/count checks, bucket batching,
recovery-floor reuse and telemetry sampling stay completed within their recorded
scope. Negative experiments are not silently put back into this queue.

## Database capabilities and dependencies

Generic database functionality owns commit identity, catalog sessions, temporal
history, FTS and hybrid retrieval. Temporal history is **logical retained history**,
not accidental access to MVCC versions. One transaction writes one physical store;
physical edges do not cross stores. Embedding generation stays outside the engine.

| Milestone | Status | Scope, prerequisites and contract |
| --- | --- | --- |
| GX-CAP-0 | Implemented checkpoint | Database-first boundaries, six ADRs, capability manifest, isolated package/import checks. [Spec](docs/specs/SPEC-GX-CAP-0.md). |
| GX-CAP-1 | Partial | Qualified CommitId, immutable admitted metadata, monotonic logical commit time, native journal/recovery foundation. Finish the public/durable contract before dependents assume it exists. [Spec](docs/specs/SPEC-GX-CAP-1.md). |
| GX-CAP-2 | Planned | Attached CatalogSession, explicit resolution/permissions, workspace scopes, one-store writes, copy/promotion receipts; provenance depends on CAP-1. [Spec](docs/specs/SPEC-GX-CAP-2.md). |
| GX-CAP-3 | Planned | Opt-in system-time history, historical schema/edges, delete/recreate semantics, retention, indexes and typed time-travel API; CAP-1 first. [Spec](docs/specs/SPEC-GX-CAP-3.md). |
| GX-CAP-4 | Planned | Valid time, bitemporal semantics and graph/version diff; builds on CAP-3. [Spec](docs/specs/SPEC-GX-CAP-4.md). |
| GX-CAP-5 | Planned | Native FTS with explicit analyzer/version, term/prefix/phrase semantics, score/freshness/budgets and transactional recovery. [Spec](docs/specs/SPEC-GX-CAP-5.md). |
| GX-CAP-6 | Planned | Explainable text/vector/graph retrieval, fusion, bounded expansion and explicit degraded results; requires CAP-5 and vector foundation. [Spec](docs/specs/SPEC-GX-CAP-6.md). |
| GX-CAP-7 | Planned | Trusted in-process extension SPI, scalar/aggregate UDFs and procedures, manifests, quotas and typed failures; no access to mutable WAL/pages. [Spec](docs/specs/SPEC-GX-CAP-7.md). |
| GX-CAP-8 | Planned | Arrow/batch interoperability, external scans and graph exchange; explicit schemas/ownership/snapshot boundaries, no implicit distributed transaction. [Spec](docs/specs/SPEC-GX-CAP-8.md). |
| GX-CAP-9 | Planned | Bounded graph projections and optional algorithm package: reachability, components, shortest paths and ranking with declared semantics. [Spec](docs/specs/SPEC-GX-CAP-9.md). |
| GX-CAP-10 | Planned | Application schema migration ledger, staged evolution, logical views and explicit derived/materialized graphs; distinct from engine format migration. [Spec](docs/specs/SPEC-GX-CAP-10.md). |
| GX-CAP-11 | Planned | Cross-capability hardening, compatibility/upgrade/recovery matrices, packaging, operational docs and release acceptance. [Spec](docs/specs/SPEC-GX-CAP-11.md). |

Implementation precedence: close release boundary → complete CAP-1 → catalog/
system-time/FTS foundations → bitemporal/hybrid → extension/interoperability/
algorithms/schema evolution → combined hardening. Independent design can overlap;
shared WAL/format changes must be integrated in a controlled sequence, not competing
branches that independently redefine the protocol.

## Optional agent product

These are optional consumer packages, not required dependencies of `okto-grafx`.
No mandatory MCP daemon, prompt storage or automatic “winning” contradiction is
introduced. Identities and capabilities are injected by the host; `project`,
`user/global` and `shared` are conventions over explicit stores/catalogs.

| Source milestone | Status / scope |
| --- | --- |
| AGENT-0 | Planned: contracts, ADRs, package boundaries and ports |
| AGENT-1 | Planned: deterministic workspace resolver, scoped stores and promotion rules; generic catalog foundation reused |
| AGENT-2 | Planned: AgentProfile/AgentSession/AgentOperation, provenance and idempotency; generic commit identity reused |
| AGENT-3 | Planned: MemoryItem, Claim, Evidence, semantic relationships and knowledge API |
| AGENT-4 | Planned: optional local MCP stdio preview, tools/resources, errors and host configuration |
| AGENT-5 | Planned: governed namespaces/domain mutations, policies, schema-poisoning/scope-leakage protection |
| AGENT-6 | Planned: hybrid ContextPack with evidence, lineage and bounded graph expansion; CAP-5/6 reused |
| AGENT-7 | Planned: temporal memory lifecycle, contradictions, forgetting/compaction without lost lineage; temporal database foundations reused |
| AGENT-8 | Planned: hardening, conformance, observability, DX and release |

[GX-AGENT-0](docs/specs/SPEC-GX-AGENT-0.md) groups AGENT-0–4 plus required
AGENT-5 governance for preview; [GX-AGENT-1](docs/specs/SPEC-GX-AGENT-1.md)
groups integrated knowledge/hybrid/temporal hardening. Neither summary deletes the
full original requirements, security rules or exit gates preserved in the register.

## Completed foundations

Do not treat the initial audit's P0/P1 descriptions as current unresolved defects.
The execution record includes recovery/publication and bootstrap corrections,
read-only refusal, concurrent HNSW publication, index redo/freshness, checkpoint/WAL
policies, query/transaction quotas, OpenMetrics loopback policy, typed facade,
custom-adapter conformance, identity range leasing, two-slot control publication,
catalog-v2 identity indexes, explicit rehash/rebuild, vacuum v1, WAL compression,
streaming/bulk API and substantial 0.0.2–0.0.4 performance work.

These statements close only the documented implementation slice. They do not claim
universal RSS bounds, lock-free writes, hot backup, full Cypher or completion of
GX-CAP-1. Release/compatibility certification remains version/platform-specific.

The DELETE quota correction is retained: a DELETE intent has an empty tuple and
must not be re-encoded as a row just to compute quota; charge its fixed intent
entry. Keep its regression when evolving write accounting.

## Legacy requirement register

All former plans are consolidated **in full**, not replaced by lossy summaries:
[source archive](docs/archive/ROADMAP_SOURCES.md), with original paths, hashes,
line counts and explicit boundaries in the [manifest](docs/archive/ROADMAP_SOURCES_MANIFEST.json).
Use the archive for exact requirements/evidence, this roadmap for current status.

| Source / original identifiers | Current destination |
| --- | --- |
| Main evolution P0.1–P0.5 | Completed recovery/read-only/bootstrap/HNSW foundations; OPS-12 for current safety boundaries |
| P1.1–P1.4, P2.1–P2.2, P2.10 | Index/recovery work and historical evidence; OPS-3/12/13 and vector validation obligations |
| P1.5–P1.13, P2.3 | Remaining performance queue, OPS-4/5/6; completed budgets, indexes, WAL compression and vacuum slices |
| P1.14–P1.16, P2.4–P2.9 | Configuration/DX/adapter work; OPS-7/9/10; existing closed slices stay closed |
| Main §8.1–8.9 | OPS-1/2/3/8, CAP-1/7/8/9/10 and changefeed direction below |
| Main §9 / M-PULSE milestones | Consumer compatibility contracts, query/path specs and dated Pulse reports; Core stays agnostic |
| Main phases 0–3 | Completed foundations, operational backlog, finite performance residuals and database capability track |
| Complementary capabilities A–K / GX-CAP-0–11 | Database table above, six ADRs and individual specs; full scope/gates preserved |
| Agent-first AGENT-0–8 | Optional product table; full models, policies, tools/resources, namespace rules and acceptance preserved |
| Fable D-01–D-37 / NT-1 and final round batches | Historical disposition/evidence, not 37 new tasks; residuals/rejections above/below |
| Performance 0.0.2/0.0.3/0.0.4, Wave 0–3, OIX-0–3 | Completed slices and finite residual queue; detailed measurements remain in archive/reports |
| Round 7 / PUNCHLIST / RESUME | Frozen historical implementation/critic record; surviving unverified debts mapped to OPS-13 |

Future product directions retained from the initial plan: generic logical
changefeed with stable cursor/retention and acknowledged commit semantics; optional
synchronization/replication with explicit distributed-conflict policy; encryption
at rest only after a threat model and key lifecycle. These are **planned/deferred**,
not existing engine doors or dependencies added to 0.0.4.

## Deferred and rejected directions

- Group commit was rejected for a measured approximately 1.002× ceiling; do not
  reopen merely because it is a common database technique.
- Indexed-only DETACH DELETE was rejected because it stopped detecting pinned
  non-incident corruption cases. Faster success cases do not authorize that loss.
- Block-delta/physiological WAL and cross-shard commit protocols are not covered
  by current full-page zlib compression. They require a separate format/recovery
  decision and proof; no multi-read/write relaxation is authorized.
- Raising every buffer pool to 256 MiB, changing vector `auto` to NumPy,
  quantization, Bloom filters, wider codec rewrites and native/process-pool work
  are not automatic follow-ups. Follow measured workload, memory and semantics.
- Cluster/consensus/sharding/distributed transactions are outside the selected
  database-first scope. The untracked local `FABLE_SHARDING_GRAFX.md` was not
  modified, promoted to an accepted plan or used to silently expand this roadmap.
- Historical numeric gates, D5 ratios and endless marginal-gain rounds are
  superseded. Correctness failures and unexplained timeouts are not waived.

## Acceptance and maintenance

For each selected item, record scope, implementation commit, focused tests,
adversarial/corruption cases where relevant, grouped regressions, measured workload
and residual limitations. Never label a microbenchmark as UI speedup or an
observation as causal A/B evidence. Preserve errors, multiplicity, snapshots,
ordering, budgets, WAL barriers and cross-process behavior.

Documentation maintenance: update consumer reference + this roadmap status +
changelog for public changes; attach a dated report only when it adds evidence.
Do not create another `NEXT_STEPS`, `EVOLUTION_PLAN` or version-specific active
roadmap. Run `python tools/check_documentation.py` and the documentation consumer
tests. The archive is immutable provenance; new progress belongs here.
