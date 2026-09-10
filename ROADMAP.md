# Okto Grafx roadmap

**Single active product backlog — reconciled September 10, 2026.**
Published/main baseline: `0.0.5`, tag `v0.0.5`, main merge
`83cc3137bb7e95ad2a1ed9271b1a1134063097a8`. The `feature/v0.0.6` MP-1–MP-8
checkpoint is implemented and locally validated, not released. Latest recorded Pulse measurement is a different build,
`0.0.4@fa8f188`. See [performance](docs/PERFORMANCE.md).

This roadmap includes **new capabilities, corrective work, known limitations,
operational hardening and developer experience**. It replaces the execution
authority of the former evolution/agent/performance/round plans. It does not
reopen completed work, authorize production data changes or imply release approval.

## Index

September 10, 2026 — `okto-grafx==0.0.5` published on PyPI using the same wheel
validated and installed in Pulse. See the [publication receipt](docs/reports/PYPI_0_0_5_PUBLICATION.md).
Publication and merge/tag are separate events; the 0.0.5 main/tag baseline above
was independently verified in Git when preparing the 0.0.6 checkpoint commit.

September 10, 2026 — approved 0.0.5 default-acceleration update: NumPy and
google-crc32c move to base dependencies; `[accel]` remains a compatibility alias.
New connection codec/vector-math defaults become `numpy`, with explicit pure
overrides preserved. No persistent format or concurrency/durability change.
Pulse Community Settings derives these defaults from Grafx; saved overrides
are preserved. Targeted Windows validation: 574 regression tests, 50 bootstrap
tests (including mixed pure/NumPy snapshots and durable reopen), and 370 initial
configuration/codec/vector/packaging checks passed; these overlapping batches are
not a full release-wide regression. See [configuration](docs/CONFIGURATION.md).

- [Rules and status vocabulary](#rules-and-status-vocabulary)
- [Current delivery boundary](#current-delivery-boundary)
- [Approved capability continuation after 4ee4d2e](#approved-capability-continuation-after-4ee4d2e)
- [Approved continuation after a4dd85a](#approved-continuation-after-a4dd85a)
- [Approved continuation after 970aa1e](#approved-continuation-after-970aa1e)
- [Approved continuation after 7dde256](#approved-continuation-after-7dde256)
- [Approved continuation after 69ed311](#approved-continuation-after-69ed311)
- [Approved continuation after 3f3819f](#approved-continuation-after-3f3819f)
- [Search and resumable-transfer follow-up](#search-and-resumable-transfer-follow-up)
- [Approved four-item follow-up](#approved-four-item-follow-up)
- [Proposed next round after the 0.0.5 checkpoint](#proposed-next-round-after-the-005-checkpoint)
- [Next proposed round after N1–N4 closure](#next-proposed-round-after-n1n4-closure)
- [Known limitations and corrective work](#known-limitations-and-corrective-work)
- [Comparative feature gaps and minimum parity](#comparative-feature-gaps-and-minimum-parity)
- [Operational checkpoint after R1–R4](#operational-checkpoint-after-r1r4)
- [Remaining performance work](#remaining-performance-work)
- [Next iteration assessment: feature/v0.0.5](#next-iteration-assessment-featurev005)
- [Database capabilities and dependencies](#database-capabilities-and-dependencies)
- [CAP-2 through CAP-4 opportunity assessment](#cap-2-through-cap-4-opportunity-assessment)
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
5. No release date or complete version scope is promised by a row below. Version
   0.0.4 was published with the operator; later release approval remains explicit.

| Status | Meaning |
| --- | --- |
| Implemented checkpoint | Named implementation/evidence exists; scope is bounded, not universal certification |
| Partial | Some foundation exists; remaining work is mandatory before consumer exposure |
| Planned | Accepted product direction, not currently callable |
| Open limitation | Present constraint or unclosed acceptance requirement |
| Historical / revalidate | Older finding retained; do not assert it still reproduces without checking current source |
| Deferred / rejected | Not an active implementation task; reopening needs evidence/decision |

## Approved capability continuation after 4ee4d2e

Approved September 10, 2026, on `feature/v0.0.6`; these are **not** the already
completed minimum-parity MP-1–MP-8. Fixed order and DoD: feature tests, grouped
affected regression, roadmap plus capability/configuration/API docs. No Pulse
installation, PyPI release or production store mutation is implied.

| Order | Existing initiative | Bounded delivery | Current status |
| --- | --- | --- | --- |
| 1 | GX-CAP-2 | CatalogSession: aliases, ownership/permissions and single-store pinned transactions | Complete in development; grouped affected regression passed |
| 2 | GX-CAP-2 | Optional explicit-root/allowlist/bounded-marker workspace resolver | Complete in development; grouped affected regression passed |
| 3 | GX-CAP-2 | Bounded copy/promotion into an existing target, atomic data plus durable idempotency receipt | Implemented and tested: bounded PK-table/fail-policy slice; broader copy policies remain outside this slice |
| 4 | GX-CAP-5 | Positional full-text phrase search | Implemented and tested: exact analyzed phrase verification; durable positional postings/position-return APIs remain open |
| 5 | GX-CAP-10 | Read-only logical views | Implemented and tested: bounded typed base-table views, snapshots and atomic definitions. [Evidence](docs/reports/V006_VIEWS_SCHEMA_CHECKPOINT.md) |
| 6 | GX-CAP-10 | Nullable column addition with old-row/schema compatibility | Implemented and tested: typed append-only column API, required bit 13 and exact prior layouts. [Evidence](docs/reports/V006_VIEWS_SCHEMA_CHECKPOINT.md) |
| 7 | OPS-5 / PERF-SCALE | Repeated-key posting layout, based on confirmed hotspot evidence | Implemented and tested: opt-in `posting_hash`, bounded same-key INSERT preparation, native snapshots/recovery/maintenance. [Usage](docs/POSTING_HASH.md), [acceptance](docs/reports/V006_POSTING_TEMPORAL_CHECKPOINT.md) |
| 8 | GX-CAP-3 | Opt-in durable system-time history, first persistence checkpoint | Partial internal foundation: typed event codec, bounded immutable append-image plans and complete append-transition validation tested. **Not connected to native commit/recovery, not a consumer history API.** [Exact remaining boundary](docs/specs/SYSTEM_HISTORY_APPEND_DRAFT.md) |

Items 1–2 share **437 passing affected regression tests** (51.06 s, Windows).
[Acceptance evidence and exclusions](docs/reports/V006_CATALOG_WORKSPACE_ACCEPTANCE.md).
The subsequent 1–4 checkpoint passed **916 grouped tests** (241.58 s), including
an inherited scalar import-contract correction. [Evidence and explicit boundaries](docs/reports/V006_COPY_PHRASE_ACCEPTANCE.md).
The 1–6 expanded checkpoint passed **7,535 tests** (994.36 s), followed by
**155 supplemental tests** (144.57 s) after the virtual-NULL cache-admission fix.
Counts overlap and are not added. [Commands, crash cuts and remaining work](docs/reports/V006_VIEWS_SCHEMA_CHECKPOINT.md).
The item-7 checkpoint passed **6,830 affected regression tests**, followed by
**1,314 final supplemental tests** after reference-preflight and documentation
corrections. Item 8 has a tested internal prototype, not native history recording.
[Exact build/test boundaries and remaining delivery](docs/reports/V006_POSTING_TEMPORAL_CHECKPOINT.md).
The entire eight-item delivery remains **in progress**. Items 1–2 introduce no
persistent format changes or new connection flags. Public consumption
and limits: [catalogs/workspaces](docs/CATALOGS_AND_WORKSPACES.md). Their completion
does not close the full CAP-2 spec: arbitrary subgraph selection and CLI inventory remain
separately tracked. [Copy consumption and limitations](docs/CATALOG_COPY.md),
[phrase semantics](docs/FULL_TEXT_SEARCH.md#exact-analyzed-phrases-006-development).
A snapshot or commit journal is **not** retained graph history.
Existing logical transfer into a fresh destination does **not** satisfy item 3's
atomic update and replay contract for an existing destination.

Persistence work must freeze exact capabilities, bytes, recovery/refusal and
backup/transfer behavior before claiming completion. This is the existing spec's
prerequisite, not a new performance gate. No marginal timing target is added.

## Current delivery boundary

| ID | Status | Deliverable / exit condition |
| --- | --- | --- |
| REL-004 | Published September 8, 2026 | [PR #2](https://github.com/OktoLabsAI/okto-grafx/pull/2) merged; tag `v0.0.4` points to `425362a`. Wheel/sdist built from the tag, Twine validation, 149 package-file parity checks, isolated consumer smoke and public PyPI install smoke passed; remote SHA-256 values matched. [PyPI 0.0.4](https://pypi.org/project/okto-grafx/0.0.4/). GitHub Actions could not start because of account billing; local evidence does not certify the remote/platform matrix. |
| DOC-1 | Implemented in this documentation refactor | One README entry point, public API/configuration/query/operations references, measured-performance table, one roadmap and a preserved source archive. Links, examples, API/config field coverage and preservation hashes are checked; [validation receipt](docs/reports/DOCUMENTATION_REFACTOR_2026_09_08.md). |
| CAP-1B | Implemented checkpoint | `6b5163e`: native journal preflight is connected to page application/checkpoint; UUID, activation/COMMIT coverage, target/resident LSN and file extents validated. 2,433 tests / 77.27 s recorded; no new typing diagnostics in the isolated comparison. This is recovery correctness, not a latency benchmark. |
| GX-CAP-1 remainder | Implemented and locally validated in 0.0.5 development | Metadata-at-begin/retry, qualified lookup/paging, full journal verification, metrics and coordinated transfer/restore/fork identity semantics. [Consumer contract](docs/COMMIT_HISTORY.md), [acceptance evidence and limits](docs/reports/V005_N3_N4_ACCEPTANCE.md). Opt-in activation is not a production rollout or release. |
| PULSE-005 candidate | Local validation passed; product evolution paused by user | `8c3f6f2`: one complete Windows/Python 3.13 regression passed (16,228 passed, 18 POSIX skips, zero failures), plus clean wheel/sdist builds and isolated installed consumption. [Receipt and D: storage-latency caveat](docs/reports/V005_PRE_PULSE_VALIDATION.md). Next step is controlled Pulse installation/testing with the exact validated wheel and `[accel]`, not more Grafx capability work. No live installation, data operation or publication has occurred in this validation. |
| PULSE-005 adoption | First Community batch installed; broader adoption pending | Exact candidate installed with `[accel]` in both local Pulse environments; bounded Board readers, scoped vector reads and the complete 0.0.5 Settings catalog. 350 backend + 20 UI tests passed, frontend rebuilt and isolated installed-consumer smoke passed. Core contracts remain neutral; no real-data backfill/consolidation, no publication. [Checkpoint and remaining scope](docs/reports/PULSE_V005_ADOPTION_CHECKPOINT.md). |
| PULSE-BENCH | Reserved workload | Latest recorded authorized run consumed one spec; 19 remain reserved. Do not consolidate more, redrive, rebuild or reset data to improve this document. New live runs need a deliberate workload decision. |

## Search and resumable-transfer follow-up

### Approved continuation after a4dd85a

The completed eight-item delivery is committed and pushed as `a4dd85a` on
`feature/v0.0.5`: 16,121 distinct tests passed, 18 attributed skips, no unresolved
failures. **All eight subsequent items are implemented and locally validated:
16,228 distinct tests passed, 18 attributed skips, no unresolved failures.**
Acceptance combines grouped regression and the documented corrective reruns; it
is not a claim that every initial invocation was green. The table preserves the approved pre-implementation evidence,
not current defects or a reopening of that delivery. It reuses PERF-SCALE/MEM and GX-CAP-8/9 requirements; it does not
authorize release, production Pulse workloads or changes to concurrency/durability.

Current surface: `with_pagerank`, `with_simple_topology`, `to_networkx`,
`label_propagation`, `projection_arrow_batches`, `PolarsFrame`/`to_polars`/`import_polars`,
`TextImportLimits` and CSV/JSONL batch readers plus atomic import facades.
Checkpoint 1–4: 8 passed; combined new feature/isolation checks: 43 passed;
eight executable documentation recipes passed. Final algorithm coverage: 30 passed;
text ingestion: 48 passed; final interop/examples/isolation: 12 passed. The final
wheel matched all 185 source Python files and passed an isolated native consumer
commit/reopen smoke. No source storage/WAL/OCC change.
[Implementation evidence and final acceptance status](docs/reports/V005_AFTER_A4DD85A.md).

Implementation and evidence committed and pushed as `65ab659` on `feature/v0.0.5`.

| Order / IDs | Bounded candidate and inspected evidence | Effort | Expected value / dependency |
| --- | --- | --- | --- |
| 1 / PERF-SCALE, GX-CAP-9 | Explicit reusable PageRank transition preparation owned by the immutable projection. `_pagerank` currently recomputes normalized edge shares/dangling nodes and recreates source/target tuples and NumPy arrays per call, even with retained CSR. Retain only opt-in derived data with complete logical-memory charges, immutable ownership and backend/weight identity; do not retain personalization-specific ranks or storage authority. | Medium | Avoid repeated O(V+E) preparation when ranking one snapshot with multiple seeds. Iteration complexity is unchanged. Measure preparation separately once; stop if the real saving is marginal rather than introduce a cache merely to complete this row. |
| 2 / PERF-SCALE, GX-CAP-9 | Optional reusable simple-undirected topology. `_k_core` rebuilds neighbor sets and collapses physical parallel edges on every invocation. Retain a bounded immutable representation that algorithms can reuse, while keeping the original directed multigraph untouched. | Medium | Amortize deduplication and allocation for repeated analytics; prerequisite for item 4. Building the picture still scans V+E and k-core still performs peeling. |
| 3 / GX-CAP-8/9 | Optional NetworkX export and conformance fixtures. The spec explicitly retains graph exchange and NetworkX acceptance as outstanding; current oracles are independent local implementations, not NetworkX runs. Preserve store/table/record identity, directed physical edge keys, parallel edges, loops and captured weights in a bounded MultiDiGraph export; no arbitrary object ingestion or storage writes. | Small–medium | Developer integration and an additional independent oracle. No runtime performance promise; keep dependency lazy/optional. Compare algorithms only under equivalent graph semantics. |
| 4 / GX-CAP-9 | Bounded read-only label propagation over item 2, one algorithm from the existing second package. Specify simple-undirected/unweighted interpretation, deterministic node order and label ties, isolates, iteration/work/memory caps and explicit convergence status. Do not silently equate this with Louvain/modularity optimization. | Medium | New community discovery capability, not faster database reads/writes. Depends on 2; uses small independently checked fixtures from 3 where update policies agree. No write-back or clustering-quality guarantee. |
| 5 / GX-CAP-8/9 | Bounded Arrow batch export for detached projection nodes/edges and explicitly supplied aligned algorithm results. Current `to_arrow_batches` accepts native query results/cursors, not GraphProjection. Publish identity/endpoints/weight/result schemas and snapshot provenance; validate alignment and preserve multiplicity without first building a whole DataFrame. | Medium | Bridge analytics results into the existing tabular/Parquet ecosystem with bounded additional output batches. Does not make the projection or already-computed algorithm output streaming/O(1)-memory. |
| 6 / GX-CAP-8 | Optional explicitly typed Polars bridge over the existing Arrow contract. Current `tabular.py` exposes Pandas only. Preserve NULL versus NaN, vector metadata and exact types through an explicit metadata-bearing contract; reject metadata loss rather than infer vector identity. Whole-call import staging uses the existing native savepoint and caller commit. | Medium | Completes the other named DataFrame integration; no zero-copy or throughput promise without evidence. No LazyFrame query-engine integration or mandatory Polars dependency. |
| 7 / GX-CAP-8 | Typed bounded local CSV batch reader and atomic native import facade, outside Cypher. Reuse the trusted local-directory policy of Parquet. Declare UTF-8, header/delimiter/quote/NULL rules, exact scalar codecs and file/record/field/row/batch limits before exposing functions. Report malformed row/column; late errors roll back this call only. | Medium–large | Practical local ingestion without user-written parsers; common foundation for 8. No schema inference, globs, URLs, COPY syntax, graph backup or vector/nested data in this initial slice. |
| 8 / GX-CAP-8 | Typed bounded local JSON Lines reader and atomic native import using 7's file/codec foundation. Require one object per bounded line, explicit columns, duplicate/unknown-key and missing-versus-NULL policy, exact numeric admission and localized errors; reject arbitrary nesting and nonstandard NaN/Infinity tokens. | Medium | Another existing local JSON requirement in a finite format. JSON arrays, remote sources and query external scans remain deferred. Preserve whole-call staging rollback and caller-owned commit. |

Evidence inspected: [PageRank/k-core preparation](src/okto_grafx/projection_algorithms.py),
[NumPy adapter](src/okto_grafx/adapters/numpy_projection.py),
[projection surface](src/okto_grafx/projections.py), [Arrow boundary](src/okto_grafx/arrow.py),
[Pandas bridge](src/okto_grafx/tabular.py), [local-file policy](src/okto_grafx/parquet.py),
[external-data scope](docs/specs/SPEC-GX-CAP-8.md) and
[algorithm/NetworkX scope](docs/specs/SPEC-GX-CAP-9.md).

Recommended sequence: 1–8, checkpoint after 1–4. Each implemented slice needs
feature/failure tests, optional-dependency isolation where relevant, resource and
ownership contracts, executable usage examples and API/configuration/roadmap updates;
one grouped final regression covers the batch. These are primarily analytics and
interoperability improvements, **not evidence of faster Pulse KG loading or writes**.
No new percentage gate, persisted format, authority cache, mandatory dependency,
single-writer premise or live-data consumption is included.

### Approved continuation after 970aa1e

Candidate queue recovered after the eight-item delivery was committed and pushed
as `970aa1e` on `feature/v0.0.5`. **All eight delivered; final grouped regression
passed.** The candidate table below preserves the pre-implementation
evidence, not present-tense defects. [Current receipt](docs/reports/V005_AFTER_970AA1E.md).
The 16,046-pass preceding delivery is closed; these are not new acceptance gates for it. First
three items target demonstrated code-level costs; the others extend existing
GX-CAP-8/9 scope. Benefits are hypotheses until measured, not promised speedups.

Current delivery: immutable identity lookup and discovery-sized BFS (1), bucket
k-core (2), explicit NumPy PageRank (3), snapshot weights (4), bounded Dijkstra (5),
weighted/personalized PageRank (6), typed Arrow-backed Pandas (7), local bounded
Parquet with atomic no-overwrite publication and native import savepoint (8).
Checkpoint 1–4: 15 passed; weighted oracles: 4 passed; interop checkpoint: 10 passed.
Final consolidated acceptance: **16,121 distinct passed, 18 attributed skips,
no unresolved failures/errors**. The final documentation/public-surface rerun
after the host crash passed all 2,450 tests; completed regression reports survived
and were not needlessly rerun. Counts include overlapping focused coverage only once.
Consumer guides, operation defaults, API signatures/DTOs, capability specs and
executable examples are part of this delivery. No Pulse, release or commit/push
action is implied by this implementation approval.

| Order / IDs | Bounded candidate and current evidence | Effort | Benefit / dependency |
| --- | --- | --- | --- |
| 1 / PERF-SCALE, GX-CAP-9 | Optional immutable node-identity lookup retained with projection topology. `_bfs` in `projection_algorithms.py` uses `nodes.index` for source/target even when CSR is already retained; capture's `offsets` map is discarded. Reuse a bounded derived lookup and charge path workspace by admitted/discovered state where safe. No durable cache authority. | Small–medium | Remove repeated O(V) identity lookup for local path calls on a prepared picture; one-time O(V) construction remains. Preserve BFS order, quotas and exact paths. |
| 2 / PERF-SCALE, GX-CAP-9 | Linear bucket-based k-core peeling. `_k_core` uses heap push/pop and retained stale pairs today. Keep simple-undirected deduplication, ignored self-loops and identical core numbers; retain the current implementation as an independent comparison during validation. | Medium | Target O(V+E) peeling after simple-neighbor construction, eliminating heap's logarithmic factor and stale entries; no claim that reading E edges can be avoided. Independent of 1. |
| 3 / CONC-CPU-1, PERF-MEM, GX-CAP-9 | Explicit opt-in NumPy PageRank execution path through the optional acceleration boundary. `_pagerank` currently distributes ranks in Python for each physical edge/iteration. Keep Python as the default/reference, bound temporary numeric buffers, document finite numeric tolerance and check cancellation between kernels/iterations. Missing selected dependency must refuse, not silently fall back. | Medium | Reduce Python edge-loop overhead on larger projections; benchmark a fixed fixture, not a marginal ratio gate. Preserve multiplicity, dangling behavior and explicit non-convergence. No change to database read/write concurrency. |
| 4 / GX-CAP-9 | Optional scalar relationship weights captured in the same snapshot via projected scans. `project_graph` retains only endpoints today. Start with explicitly selected numeric columns, finite non-negative weights and an explicit NULL/missing policy; default topology stays unweighted. Charge retained values and preserve physical edge identities. | Medium | Foundation for weighted analysis without subsequent storage reads; a capability, not an automatic performance gain. |
| 5 / GX-CAP-9 | Bounded non-negative weighted shortest paths (Dijkstra), with distance/path result, direction, deterministic equal-cost ties and clear work/output/memory limits. Existing shortest_path is unweighted BFS. Do not reuse BFS depth semantics without defining hop-constrained behavior; negative weights and Bellman–Ford remain outside this slice. | Medium | Cost-aware routing/dependency analysis; depends on 4 and reuses 1. Default unweighted API remains unchanged. |
| 6 / GX-CAP-9 | Weighted and personalized PageRank: explicit seed distribution, normalization and dangling-mass policy; node-aligned results, residual/convergence and bounded inputs. `_pagerank` currently hardcodes uniform teleportation and equal outgoing-edge shares. All-zero/unknown-node/invalid weights must have explicit refusal or documented semantics. | Medium | Domain-specific relevance over a captured graph; depends on 4. Python and any selected accelerated path from 3 must agree within the published numeric tolerance. |
| 7 / GX-CAP-8 | Optional Pandas bridge through the existing typed Arrow boundary. Start with bounded DataFrame-to-batch import and explicitly materialized result-to-DataFrame export; preserve nullable scalar/vector schema and metadata without dtype guessing or silently converting NaN/NULL. Do not claim full-DataFrame export is streaming or zero-copy. | Small–medium | Easier Python analytics integration and less caller conversion code, not an engine throughput promise. Reuse whole-call native import atomicity. Polars remains a separate later slice. |
| 8 / GX-CAP-8 | Local Parquet batch read/write on the existing Arrow contract: explicit schema/vector metadata, row/byte/batch limits, caller-owned resources and localized failures. Restrict to explicitly permitted local files, no URL resolution; no overwrite by default and no visible partial final file on export failure. Feed import through the existing staging savepoint. | Medium–large | Interop with larger local datasets using bounded batches; no COPY/query external scans, portable graph backup or unbounded transaction promise. Independent of 4–7, reuses current Arrow. |

Evidence: [current algorithms](src/okto_grafx/projection_algorithms.py),
[capture](src/okto_grafx/projections.py), [Arrow implementation](src/okto_grafx/arrow.py),
[remaining graph scope](docs/specs/SPEC-GX-CAP-9.md) and
[remaining interop scope](docs/specs/SPEC-GX-CAP-8.md).
Recommended order is 1–8, with a checkpoint after 1–4. Every selected item keeps
feature/failure tests, independent reference checks when applicable, grouped final
regression, and roadmap/feature/configuration/API documentation in its DoD.
Algorithms run on detached pictures; none of this promises faster Pulse KG pages
unless that consumer actually uses the new path. No Pulse installation/data use,
reserved spec consolidation, release, storage-format change, authority bundle,
multiwriter weakening or persistent algorithm write-back is part of this proposal.

### Approved continuation after 7dde256

Approved by the user after commit `7dde256`; **all eight items are implemented and
locally validated**. Consolidated final regression: **16,046 distinct passed,
18 attributed skips, no unresolved failures**, after the corrective runs below.
Feature/failure tests, executable examples and consumer/API/configuration docs passed.
Checkpoint 1–4 passed: 30 focused scan/projection/algorithm tests in 9.00 s.
The [implementation and acceptance receipt](docs/reports/V005_AFTER_7DDE256.md)
records the final test boundary and the native vector-admission correction.
These are new bounded items, not missing acceptance from the
15,994-pass delivery below and not additional mandatory gates for its release.
Order favors reusable performance foundations before dependent capabilities.

| Order / existing IDs | Approved scope and original source evidence | Effort | Expected benefit / dependency |
| --- | --- | --- | --- |
| 1 / OPS-8, PERF-MEM, GX-CAP-9 | Public snapshot-bound physical scan with explicit column selection and bounded pages. Reuse the internal projected-read foundation in `engine/heap_store.py` (`scan_projected`/`_read_if_projected`) instead of exposing private heap access. Existing `Transaction.scan_rows_v1` retains its default full-row contract. Preserve validation of skipped payloads, record identity, cursor ownership and snapshot. | Medium | Potentially high allocation/decode reduction for wide rows/vectors when only identities/endpoints are needed; no promise to avoid physical integrity I/O. |
| 2 / PERF-SCALE, GX-CAP-9 | Use item 1 in `project_graph` with row/byte-bounded batches, cancellation/deadline boundaries and work diagnostics. The baseline scanned `limit=1` and decoded unretained properties. Preserve identical nodes, physical edges and snapshot with fewer scan/admission calls. | Small–medium | Direct reduction of per-row API/coordination overhead; depends on 1. Capture remains a selected-table census, not O(1). |
| 3 / PERF-MEM, GX-CAP-9 | Optional immutable compact adjacency owned by a detached projection; bounded construction and explicit accounting. The baseline retained an edge tuple but no reusable adjacency. Keep physical parallel-edge/self-loop identity and no persistent catalog/cache authority. | Medium | Reuse topology for repeated traversal/algorithms without rebuilding Python adjacency each time; foundation for 4–6, not a storage-format change. |
| 4 / GX-CAP-9 | Iterative strongly connected components with work/memory/cancellation bounds and deterministic labels. The baseline exposed degrees and weak components only. Independent oracle checks cycles, isolated nodes, parallel edges and loops. | Medium | New directed-dependency/cycle analysis; depends on 3. No recursion-depth or write-back dependency. |
| 5 / GX-CAP-9 | Read-only reachability and unweighted shortest paths on the detached projection. Explicit source/target, direction, depth/output/work limits and edge-identity tie semantics; reuse BFS rather than rescan storage per frontier. | Small–medium | Repeated path/dependency analysis over one captured snapshot; depends on 3. Weighted paths remain outside this slice. |
| 6 / GX-CAP-9 | Bounded unweighted PageRank: explicit damping, tolerance, iteration cap, dangling-node and parallel/self-loop semantics; distinguish declared non-convergence from resource refusal. No graph mutation or embedding service. | Medium | New graph-importance ranking, not faster database writes; depends on 3. |
| 7 / GX-CAP-9 | Bounded k-core decomposition over an explicitly defined undirected interpretation; document parallel-edge/self-loop treatment and test against an independent oracle. Stored/projected physical edges are not removed or rewritten. | Medium | New cohesion/dense-subgraph analysis; shares projection controls but builds simple neighbors directly, avoiding unused CSR. |
| 8 / GX-CAP-8 | Extend optional Arrow import/export to declared vector columns with fixed-size lists, explicit space/dimension/precision metadata, NULL and shape/range validation. The baseline admitted only seven scalar types. Preserve whole-call import staging atomicity and caller-owned commit. | Medium | Typed analytics/ML interop without manual list conversion; no zero-copy promise, external scans or new persisted format. |

Implemented surface: `scan_rows_v1(columns=, max_batch_bytes=, timeout_seconds=,
cancellation=)`; batched `project_graph` and `ProjectionDiagnostics`;
`with_adjacency`, `strongly_connected_components`, `reachable`, `shortest_path`,
`pagerank`, `k_core`; `ArrowVectorType` in both Arrow functions. Native writes now
validate encapsulated vector parameters against their declared target space rather
than letting them bypass dimension/identity/precision admission. No new format bit,
WAL effect or connection setting. [Usage and algorithm contracts](docs/GRAPH_PROJECTIONS.md),
[scan contract](docs/INTEGRATION.md#bounded-physical-scans),
[Arrow contract](docs/EXTENSIONS_AND_ARROW.md#explicit-native-vectors).

Approved checkpoints: **1–4**, then **5–8**. The
existing DoD applies: focused positive/failure tests, grouped final regression,
executable examples and roadmap/feature/configuration/API documentation. Publish
work counts or timings only after measurement, without marginal speed thresholds.
These candidates do not authorize a Pulse deployment or consuming reserved specs.
Multi-reader/writer, both OCC checks, WAL/durability and fail-closed validation stay
unchanged. Sharding, online reclamation, persisted algorithm write-back and temporal
storage are intentionally outside this bounded round.

### Approved continuation after 69ed311

Approved September 9, 2026 on `feature/v0.0.5`, in the order below. This finite
round preserves multi-reader/writer access, both OCC checks and WAL durability.
Checkpoint after items 1–4; all eight are authorized. No release, push or Pulse
data mutation is included. Each item requires feature/failure tests and updated
consumer/API/configuration contracts; final grouped regression closes the round.

| Order | Approved bounded deliverable | Status |
| --- | --- | --- |
| 1 / OPS-5 | Page-wise sparse-directory traversal for diagnostics/maintenance | Implemented checkpoint: 8,192 empty buckets / 72 directory visits |
| 2 / OPS-4/5 | Configurable repeated-key memo and immutable diagnostics | Implemented checkpoint: page/byte caps, zero disables, exact-image authority preserved |
| 3 / OPS-5/6 | Batch absent sparse-head materialization inside one existing publication | Implemented checkpoint: up to 64 heads per barrier; native crash/replay tests |
| 4 / OPS-4 | Aggregate per-handle HNSW logical-memory budget across retained pictures | Implemented checkpoint: live/retired shared reservations; not RSS |
| 5 / GX-CAP-8 | Typed bounded Arrow batch import with explicit atomicity | Implemented checkpoint: whole-call staging savepoint, prior writes preserved on failure |
| 6 / GX-CAP-5 | Bounded prefix full-text search | Implemented checkpoint: bit 11, exact expanded-term scoring; no phrase/position search |
| 7 / GX-CAP-5 | Full-text indexes over relationship text properties | Implemented checkpoint: bit 12, physical edge identities, replay/backup/transfer |
| 8 / GX-CAP-9 | Read-only snapshot-bound graph projections, degrees and connected components | Implemented checkpoint: detached directed multigraph, degree/WCC, bounds/cancellation; no persisted catalog or writes |

All eight bounded items are implemented and locally validated. Consolidated
regression: **15,994 distinct passes, 18 platform skips, no unresolved failures**;
two missing-helper-docstring failures were corrected and revalidated. Ruff and
documentation coverage passed. This is grouped Windows/Python 3.13 evidence, not
a clean uninterrupted run, remote cross-platform certification or a release.
[Round evidence and limits](docs/reports/V005_AFTER_69ED311.md).

These are extensions of the completed checkpoint below, not a reopening of its
DoD. Performance figures must be measured; there is no percentage gate.

### Approved continuation after 3f3819f

Approved September 9, 2026, in this order on `feature/v0.0.5`. This is a finite
implementation round, not deployment, publication or a relaxation of concurrency,
WAL, OCC, snapshots or fail-closed validation. Internal checkpoints follow 1–2 and
3–5; all eight are authorized. Feature/failure tests, grouped regression and
roadmap/capability/configuration/API documentation are required for completion.

| Order | Bounded scope | Status |
| --- | --- | --- |
| 1 / OPS-4, PERF-MEM | Explicit HNSW derived-picture construction/cache logical-memory budget and diagnostics, including safe warm-cache retirement after durable writes | Implemented and locally validated; cold admission, warm retirement and grouped regression |
| 2 / GX-CAP-5 | Bounded retained durable corpus summaries for eligible historical FTS snapshots; exact census when retention cannot prove coverage | Implemented and locally validated; snapshots, retention, interval verification, crash/replay and grouped regression |
| 3 / OPS-3 | Indexed discovery of already retired overflow FREE pages; revalidate ownership/horizon at allocation, preserve quiescent retirement | Implemented and locally validated; native crash cuts, valid-CRC corruption, backup, capability refusal and verifier corrective regression |
| 4 / OPS-5 | Opt-in sparse hash-directory allocation, with explicit format/capability, recovery, rebuild and old-reader contracts | Implemented and locally validated; first-head crash cuts, changed directory images, backup/transfer and grouped regression |
| 5 / OPS-5, PERF-SCALE | Repeated-key overflow access; preserve all visible versions and completeness, not just wider bucket sizing | Implemented and locally validated: bounded exact-image decoded-page memo. Still O(chain pages + candidates), no posting-tree or constant-time claim |
| 6 / GX-CAP-7 | Trusted explicit extension registry and typed scalar functions first; no mutable storage/WAL access or sandbox claim | Implemented and locally validated: per-handle immutable allowlist and typed scalar query/direct calls |
| 7 / GX-CAP-8 | Optional Arrow result-batch export, with exact type/ownership/snapshot/budget contracts; after the item 6 foundation | Implemented and locally validated: optional copied scalar batches over native results/cursors |
| 8 / OPS-10, GX-CAP-11 | Reproducible 0.0.5 compatibility/upgrade/accelerator/platform matrix; report unavailable platform rows, never imply they executed | Implemented workflow and isolated real-wheel upgrade tool; 3 local selector tests and 4 real 0.0.4 upgrade cases passed; unexecuted OS/Python rows remain unmeasured |

Performance value is workload-dependent; no unmeasured percentage is promised.
Persisted items require their concrete format/recovery contracts before code.
This round does not include online vacuum, sharding, full temporal history,
arbitrary UDF procedures, external Arrow scans or production Pulse workloads.
**Local DoD closed:** 15,904 distinct tests passed and 18 platform skips across full
grouped coverage plus corrective reruns; no unresolved failures. This is not one
uninterrupted clean invocation or a remote-platform certification. Documentation
checks cover links, 36 connect fields, public APIs/DTOs and preserved plans.
Initial defects, their dispositions and exact acceptance boundaries are
recorded in the [continuation receipt](docs/reports/V005_NEXT_EIGHT_PROGRESS.md).

### Approved eight-item continuation after e34a9e7

The operator approved items 1–8 in order. This is a finite continuation on
`feature/v0.0.5`, not release/deployment approval. Each slice requires feature and
failure-path tests, grouped regression and updated API/configuration/usage contracts.
No percentage performance gate or weaker multi-reader/writer/WAL/OCC guarantee is
authorized. The first four form an internal checkpoint, not a request to reauthorize
the remaining four. Durable changes require explicit format/recovery/upgrade contracts
before implementation; unimplemented parts must not be advertised as available.

| Order | Scope | Status |
| --- | --- | --- |
| 1 | Single-pass query-term frequencies for BM25; identical scores/order, bounded temporary memory | Delivered; locally validated |
| 2 | Index-driven hybrid incident expansion with certified completeness or safe scan fallback | Delivered; locally validated |
| 3 | Cooperative cancellation/deadline inside native vector loops, never interrupting commits | Delivered; locally validated |
| 4 | Integrated hybrid logical-memory accounting and consumption diagnostics, not an RSS cap | Delivered; locally validated |
| 5 | Opt-in durable snapshot-qualified FTS statistics, complete-COMMIT replay and verification; bit 6 | Delivered; locally validated |
| 6 | Explicit 65,536-bucket ceiling, bit 7, bounded physical distribution/skew and optional assisted-growth suppression | Delivered; locally validated |
| 7 | Default disk-spooled chunked physical capture/readback; optional memory mode, unchanged consistent-cut writer pause | Delivered; locally validated |
| 8 | Application checksum ledger/dry-run; additive NODE/REL TABLE and VECTOR SPACE only, atomic per version | Delivered; locally validated |

Acceptance: broad functional coverage produced **15,274 passes**, with 18 POSIX-only
skips on Windows and 11 facade/planner/docstring expectation failures. All were
corrected; final affected-surface/feature regression passed **3,653 tests with
zero failures**, in addition to verifier and installed-wheel validation. Counts
overlap and are not additive. No unresolved test failure remains. Full commands,
failure dispositions, final wheel hash and limits: [delivery receipt](docs/reports/V005_EIGHT_ITEM_CHECKPOINT.md).

Consumer contracts: [FTS](docs/FULL_TEXT_SEARCH.md), [hybrid](docs/HYBRID_SEARCH.md),
[index sizing](docs/INDEXES_AND_VECTORS.md), [backup](docs/BACKUP_RESTORE.md),
[application migrations](docs/SCHEMA_MIGRATIONS.md), [configuration](docs/CONFIGURATION.md)
and generated [API signatures/DTOs](docs/API_REFERENCE.md). Remaining scope is
explicit at that checkpoint: historical durable FTS summaries, sparse/sharded hash directories,
no-pause/resumable physical backups, arbitrary ALTER/views/derived graphs are not
part of that delivery. Historical summaries and sparse allocation were subsequently
implemented in the [continuation after 3f3819f](#approved-continuation-after-3f3819f).
No production installation or release is implied.

The operator approved all four candidates after `b16bf1f`, on `feature/v0.0.5`.
This finite delivery changes no release/version, production data or Pulse installation.
All four items are implemented and locally validated. Final broad regression:
**12,643 passed, 15 POSIX-only skips, zero failures**; final feature/consumer group:
**83 passed**. Package parity and executable examples also passed. Detailed scope,
initial-failure disposition and commands are in
[the delivery receipt](docs/reports/V005_SEARCH_RESUME_CHECKPOINT.md).

| Item | Implementation and acceptance boundary |
| --- | --- |
| 1. Reuse pure FTS analysis | Bounded operation/transaction memo reuses exact text/analyzer derivations during quota counting, staging, search and verification. No row/page/visibility authority cached; commit/abort release; saturation computes uncached. [Contract](docs/FULL_TEXT_SEARCH.md). |
| 2. Incremental snapshot BM25 statistics | Advance a validated same-generation summary through a complete bounded committed native WAL interval. Snapshot scores agree with independent census. Missing/recycled/oversized/unsupported proof declines to census; new handles still start cold. No new durable aggregate or WAL format. [Limits](docs/FULL_TEXT_SEARCH.md#budgets-and-performance-boundaries). |
| 3. GX-CAP-6 v1 | Typed weighted RRF, bounded union/intersection, source ranks/scores/coverage, exact/ANN regimes, opt-in missing-source partial disposition and bounded graph boost/filter on one reader. Single target table and single vector binding; graph relationship scans are bounded, not incident-only. [Usage](docs/HYBRID_SEARCH.md). |
| 4. OPS-2 resume extension | Explicit private workspace, normal WAL recovery, checked durable row prefixes and lease-safe identity remapping. Resume after batch/WAL/publication cuts; no partial target, overwrite or reinsertion of proved batches. Full artifact/prefix validation remains linear. [Usage](docs/LOGICAL_TRANSFER.md#opt-in-resumable-import). |

These close the four approved slices, not every future search/operations capability.
At that four-item checkpoint the remaining limits included cold corpus census,
graph scanning and separate source/fusion envelopes. The eight-item continuation
above adds opt-in durable summaries, indexed incident expansion and aggregate
logical accounting; bounded source-window recall, ineligible scan/census fallbacks
and no existing-target merge remain.
No marginal timing threshold is added; semantic/recovery/regression failures block closure.
The quality pass also closes root-export fixture/order drift and missing
cancellation/deadline labels in the query metrics catalog. Allocation attribution
checks remain strict but run in isolated processes; this is regression repair,
not a fifth product initiative.

## Approved four-item follow-up

Approved after the six bounded 0.0.5 actions below. This is a finite follow-up,
not a reopening of their acceptance criteria. Work remains on `feature/v0.0.5`.

| Item | Status | Delivery / acceptance |
| --- | --- | --- |
| 1. Ordered-page projection | Implemented checkpoint | Materializes only demanded columns; validates omitted payloads, index keys, both certificates and original snapshots. Fixed foreign-root/stale-heap reference failure. Latest 128-row wide projected page: 20.247 ms. |
| 2. Open/reopen admission | Implemented checkpoint | Removed duplicate index admission inside the startup artifact section; retained gap recovery, catalog refresh, existing-only recovery baseline, identity checks and fenced creation. Latest full-schema + 128-node writable open: 2,521.420 ms. |
| 3. Full synthetic Pulse consolidation | Implemented checkpoint; production scope remains separate | Real Community/SQLite/Grafx/primitives/outbox/ACK passed: four nodes, four edges, four refs/digests, one ACK, empty second tick. Commit 1,404.860 ms; worker through ACK 2,509.933 ms. Dominant residual cost is first independent-reader joins/checkpoint, not SQLite; no reserved specs consumed or guarantees removed. |
| 4. Typed connection configuration | Implemented checkpoint | All 35 config keywords and selector literals exported through `ConnectOptions`; positive/negative static consumers, runtime errors, docs, custom registry signature and isolated installed-wheel export validated. |

[Full evidence, failure disposition, boundaries and reproduction](docs/reports/V005_FOUR_ITEM_FOLLOWUP.md).
The grouped run had 4,198 passes, five investigated failures and one skip; the
corrected affected/adjacent set passed all 138 tests. Four failures were invalid
physical-page fixtures reproduced on untouched HEAD; their corruption refusal is
now retained as a separate negative test. No performance gate or release action
was added. Further cold-reader/open work must preserve independent participants
and include checkpoint cost, not move it outside the timing boundary.

## Proposed next round after the 0.0.5 checkpoint

Selected September 8, 2026: **N1 and N2 approved; checkpoint required before N3
and N4.** The committed 0.0.5 build was installed in both local global Pulse 0.3.3
environments before starting this source work. The previous six-plus-four bounded actions remain
closed; these are residual product slices, not additional gates on that delivery.
No version bump, new branch, production workload or release is implied.

| Order / existing IDs | Bounded proposed delivery | Effort / expected value | Precedence and acceptance boundary |
| --- | --- | --- | --- |
| N1 / PERF-COLD, PERF-WRITE | Reduce demonstrated repeated work in independent-reader admission and first-join checkpoint. The full consolidation profile identifies four reader opens and one checkpoint, not SQLite commit, as the dominant reconciliation cost. | Medium to large / highest measured latency opportunity in the small cold workload; percentage unknown | Reuse the existing full synthetic consolidation. Separate native admission/checkpoint changes from Community participant lifetime. Preserve independent lanes and fresh identity/recovery checks; moving checkpoint into warmup is not a speedup. Verify cold/warm and concurrent-reader/writer behavior. |
| N2 / BATCH-REL-1, PERF-COLD | Reduce remaining per-layout preparation/dispatch in a KG page. Existing filtering already reduces 69 layouts to 13; do not repeat that implementation. Share only bounded derived work that still preserves each layout's result and error. | Medium / conditional warm-page benefit, smaller than N1 for the cold fixture | After N1, use the fixed first/next-page workload. Exact nodes/edges, multiplicity, totals, snapshot boundaries, corruption refusal and independent error diagnostics must remain. No authority bundle; no public batch API unless measured residual dispatch justifies it. Stop if that overhead is marginal. |
| N3 / OPS-3, PERF-MEM | Extend foreground vacuum to reclaim horizon-eligible overflow history safely. The existing churn fixture demonstrates retained disk growth; it does not justify an online vacuum or a file-shrinking claim. | Large / high long-lived-store and disk-growth value, not an immediate UI speedup | Reuse pinned-reader/churn fixtures. Deliver only overflow lifetime/reclamation with WAL/crash/reopen proofs and refusal when quiescence is absent. File truncation, online reclamation and immutable-index orphan cleanup remain separate scopes. |
| N4 / GX-CAP-1 remainder | Complete native durable commit provenance: writer publication, public qualified lookup/metadata, verification/metrics and the transfer semantics required by the existing spec. | Large / high integration/recovery foundation value; may add write overhead, not a performance optimization | Separate capability checkpoint after the performance slices. Reuse the implemented journal/recovery foundation. Require atomic metadata/COMMIT behavior, replay idempotency, bounded lookup and concurrency/crash coverage; measure added commit cost. No temporal-history, FTS or attached-catalog scope bundled into this item. |

Execution boundary: deliver the N1–N2 performance checkpoint; leave N3 and N4
unstarted until that checkpoint is reviewed. Keep snapshot/WAL/durability tests as blockers,
group regressions after cohesive changes and impose no timing percentage floor.
Evidence: [four-item follow-up](docs/reports/V005_FOUR_ITEM_FOLLOWUP.md),
[lifetime and page workload](docs/reports/V005_NATIVE_PERFORMANCE_CHECKPOINT.md),
[CAP-1 contract](docs/specs/SPEC-GX-CAP-1.md).

**N1–N2 implemented checkpoint:** native existing-only index admission now
combines its initial structural observations; fresh certificates and the complete
first-join checkpoint remain. Scalar parameter preparation avoids redundant
dynamic container checks in Grafx and the Pulse Community adapter, with no Core
change or new batch API. 1,009 grouped tests and 31 Community executor tests
passed; exact 1,100-node pagination/concurrency and full synthetic ACK evidence
are recorded in the [checkpoint report](docs/reports/V005_N1_N2_CHECKPOINT.md).
Cold-open costs remain, and no overall percentage improvement is claimed.
The operator reviewed that checkpoint and subsequently authorized N3 and N4.

**N3 implemented bounded retirement:** foreground vacuum now removes eligible
overflow-backed versions and WAL-logs their exclusively owned pages as empty FREE
pages. Whole-store ownership/coverage checks precede any retirement effects;
quiescence remains an explicit operator assertion, not inferred from reader TTL.
No automatic allocator reuse or truncation: disk-growth control is still partial.

**N4 implemented and locally validated:** native publication includes
physical OCC interests, first-file initialization, final-LSN/segment-roll rebinding,
internal identity-floor commits and recovery. Development public APIs now provide
explicit activation, metadata-at-begin/retry, qualified snapshot lookup/paging,
full journal verification, privacy-safe metrics and coordinated logical-import
mapping hooks. Offline physical restore preserves identity; logical import/fork
uses a new store and atomically records the qualified source reference. This is
not a general graph backup engine or automatic directory cloning tool. The broad
regression and corrective reruns, native crash/concurrency tests, isolated wheel
and synthetic Pulse validations are recorded in the
[acceptance report](docs/reports/V005_N3_N4_ACCEPTANCE.md).
No new live spec was consumed; deployment and release remain separate.
[Consumer contract](docs/COMMIT_HISTORY.md),
[prior checkpoint evidence](docs/reports/V005_N3_N4_PROGRESS.md).

## Next proposed round after N1–N4 closure

Proposed September 8, 2026 when committing the completed checkpoint. The operator
subsequently approved **R1–R2 only before a checkpoint**. That checkpoint was committed
and pushed as `e2acb25`; the operator then approved R3–R4. Their bounded implementation
and local acceptance are complete. These items do not reopen
N1–N4 or the earlier six-plus-four deliveries. No new branch/version, production run
or release is implied. Gains below are hypotheses, not measured speedup promises.

| Order / existing IDs | Bounded next delivery | Effort / expected benefit | Precedence and stop condition |
| --- | --- | --- | --- |
| R1 / PERF-COLD, PERF-WRITE | Reduce residual cold participant admission and first-reader checkpoint work in the existing complete synthetic Pulse consolidation. Profile the remaining repeated index/catalog/page work once, then implement the dominant safe reduction. | Medium to large / highest evidenced remaining cold-load and reconciliation latency opportunity; percentage unknown | N1 removed duplicate existing-index admission, not the checkpoint. Preserve independent readers, fresh identity proofs, all checkpoint phases and end-to-end measurement. If residual work is necessary or savings marginal, report that and stop; no authority bundle or warmup relocation |
| R2 / OPS-7 | Make checksum-provider selection isolated per database/composition rather than process-global, preserving the current pure/native/auto choices and identical checksum bytes. | Medium / stronger embedding isolation and predictable accelerator use; no guaranteed latency gain | Independent localized hardening. Two handles with different settings must not change each other's provider; cover custom ports, explicit provider absence, concurrent use and wheel consumption. Do not change the disk format or weaken checksums |
| R3 / OPS-3, PERF-MEM | Reuse the overflow pages already retired as FREE by N3 so eligible space can serve subsequent overflow allocations. Start with the existing quiescent maintenance contract and an explicit persisted reuse protocol. | Large / potentially high reduction in growth under update/delete churn; not a file-shrinking or immediate UI-speed claim | Depends on N3, now delivered. Require ownership, reclaimed-snapshot floor, stale-reference/ABA, WAL/crash/reopen and allocation-quota proofs. No online vacuum, truncation or immutable-index orphan cleanup bundled in this slice |
| R4 / OPS-1 | Deliver a consistent physical backup and restore into a new directory, with a checked manifest, identity/provenance preservation, interruption handling and verify/reopen before success. | Large / high operational recoverability value; no throughput claim | Uses completed CAP-1 identity semantics; does not require R3. Prove the snapshot/WAL retention boundary with concurrent writers and refuse unsafe/incomplete promotion. No logical export engine, automatic repair of authoritative corruption or independently writable same-UUID fork |

**R1–R2 checkpoint:** exact-string ASCII identifier validation now removes repeated
Python per-character dispatch in cold catalog/index admission. Required file
identity checks and checkpoint phases remain; cold IO is not claimed solved.
Connections now retain a validated, execution-local checksum selection independent
of other handles, including custom registries and nested/threaded operations.
Grouped regression: 5,953 passes and one platform skip; complementary public-surface
and documentation group: 1,402 passes; Pulse adapter group: 105 passes. Full synthetic
consolidation and isolated wheel smoke passed. [Evidence and limits](docs/reports/V005_R1_R2_CHECKPOINT.md).

R3 consumes existing persisted FREE overflow images under the ordinary commit fence,
with floor/current-page/LSN validation and O(1)-memory incremental discovery. No new
mutable free-list format was necessary. R4 provides bounded checkpoint-fenced
capture plus verified artifact/restore publication; source commit publication waits
during capture, not during destination IO or verification. This is explicitly not
no-pause streaming backup. [Backup contract](docs/BACKUP_RESTORE.md).

Acceptance: 5,986 grouped passes / one platform skip; 562 coordination passes /
three platform skips; 114 Pulse adapter passes; isolated installed-wheel smoke
passed. Final focused guards and consumer-surface checks also passed; overlapping
counts and remaining platform/operational limits are in the
[R3–R4 checkpoint](docs/reports/V005_R3_R4_CHECKPOINT.md).

R3–R4 use existing isolated fixtures, focused
semantic tests and grouped regressions; do not consume the 19 reserved specs.
FTS (GX-CAP-5), general logical export/import (OPS-2), larger hash directories and
immutable-index orphan reclamation remain visible below but are not additional
requirements for this proposed four-item round.

## Operational checkpoint after R1–R4

Items **1–2 were completed, committed and pushed** at `692cc26`. The operator
subsequently approved **3–4**, including tests and documentation as part of their
delivery. Work stays on `feature/v0.0.5`; this continuation does not request a
release, global Pulse installation or production data changes.

| Order / ID | Scope / status |
| --- | --- |
| 1 / OPS-5 | Implemented checkpoint, locally tested: explicit quiescent orphan-index census, dry run and removal; preserve every catalog state and retained-WAL dependency. No online GC or catalog-generation retirement. |
| 2 / OPS-8 | Implemented checkpoint, locally tested: cooperative cancellation/deadlines on materialized reads and cursors, typed errors and resource cleanup. No write/commit interruption or preemptive I/O deadline. |
| 3 / OPS-2 | Implemented v1; locally validated: streaming versioned schema/data/vector export/import, identity/endpoint remapping and verified fresh-store promotion. [Usage and limitations](docs/LOGICAL_TRANSFER.md). |
| 4 / GX-CAP-5 | Implemented v1; locally validated: persisted inverted postings, versioned analyzers, weighted BM25, filters, bounded typed/procedure search, update/tombstones, recovery and generation rebuild. [Usage and limitations](docs/FULL_TEXT_SEARCH.md). |

[API contracts and limits](docs/READ_CONTROL_AND_INDEX_CLEANUP.md). Existing multi-reader/
multi-writer, OCC, WAL/durability and recovery guarantees remain required. This
round adds disk-hygiene and responsiveness controls, not a measured throughput claim.
Grouped regression, final API/query coverage, explicit closure of every observed
failure, 114 Pulse adapter passes and final installed-wheel acceptance are recorded
in the [checkpoint report](docs/reports/V005_OPS5_OPS8_CHECKPOINT.md). That report
certifies neither implementation nor acceptance of items 3–4.

Items 3–4 now have their own [implementation, regression, package and documentation
audit receipt](docs/reports/V005_OPS2_FTS_CHECKPOINT.md). It records the initial failures
and their corrective runs, including actual historical recovery floors and native
journal/index-seal interoperability. Final affected transaction/feature regression:
991 passed. The earlier 5,300-pass and corrective 2,709-pass groups overlap; their
counts are not an all-green single run or additive unique total. FTS cold/warm and
transfer observations are in the [performance reference](docs/PERFORMANCE.md).
No global Pulse deployment or publication is part of this checkpoint.

### Documentation included in the items 3–4 delivery

Documentation is part of the approved implementation, not a separate follow-up
or an expansion of the feature scope. Update it alongside each implemented
contract; do not advertise proposed interfaces as callable features.

| Area | Required documentation alongside tested implementation |
| --- | --- |
| OPS-2 logical transfer | Public entry points and result types; executable export/import examples; versioned artifact schema and supported values, schema and vectors; source snapshot and identity/endpoint remapping; budgets, resumability or its explicit limitations; validation, failure/retry and fresh-directory promotion. Distinguish logical transfer from same-identity physical restore and state what history/index state is preserved, rebuilt or excluded. |
| GX-CAP-5 full-text search | Index creation/search/rebuild APIs and supported procedure/query forms; analyzers and their versioned behavior; BM25, field weights, filters and result fields; transactional visibility and freshness; work/memory/time/cancellation limits; persistence, recovery, compatibility/refusal and backup/transfer behavior. Document supported behavior only, keeping deferred capabilities explicit. |
| Shared public references | Update the [API reference](docs/API_REFERENCE.md), generated signatures/DTOs and [configuration reference](docs/CONFIGURATION.md) for actual additions. Explain defaults, accepted values, tradeoffs and when to use or avoid each new option; distinguish connection settings from per-operation parameters. Update query, index, integration and operations guides where their contracts change. |
| Discovery and traceability | Link consumer guides from [README](README.md) and the [documentation index](docs/README.md); update this roadmap's status and known limitations with code/test evidence. Keep one active roadmap, not another competing plan. |

Acceptance includes focused feature and failure-path tests, a grouped relevant
regression, validation of executable examples, and the existing documentation
link/configuration/API-drift checks. Record commands, results and remaining
limitations in the delivery evidence; documentation checks do not substitute for
runtime tests. Publish performance figures only when measured, with their workload
and conditions, using the latest measurement rather than an unmeasured gain claim.
No additional marginal performance threshold is introduced by this requirement.

## Known limitations and corrective work

| ID / legacy mapping | Status and impact | Required work / acceptance |
| --- | --- | --- |
| OPS-1 / §8.1 | R4 plus item 7: bounded physical backup/restore with chunked capture | Checked manifest, checkpoint-fenced disk spool (default) or memory capture, verified no-replace publication and offline same-UUID replacement. No no-pause/resumable hot backup, generic custom-storage backup or independently writable fork. [Contract](docs/BACKUP_RESTORE.md). |
| OPS-2 / §8.2 | Implemented bounded v1 plus explicit resume extension | Streaming checksummed logical schema/data/vectors, parallel edges, current-ID mappings, FTS declarations, private-workspace crash resumption and verified no-replace promotion. Current state only; no existing-target merge or historical journal copy. [Contract](docs/LOGICAL_TRANSFER.md), [latest acceptance](docs/reports/V005_SEARCH_RESUME_CHECKPOINT.md). |
| OPS-3 / P1.4, §8.3 | Partial: quiescent vacuum, validated overflow reuse and opt-in indexed candidate discovery | Retirement and immutable candidate directories are WAL-published. Default discovery remains amortized O(heap pages); indexed discovery visits candidates/stale entries/directory pages and is not constant time. Quiescence remains an operator assertion; truncation and online vacuum remain unimplemented. [Indexed discovery](docs/OPERATIONS.md#indexed-retired-overflow-discovery-005-development). |
| OPS-4 / P1.8 | Partial: per-picture and aggregate per-handle HNSW admission, configurable key memo and diagnostics; still not RSS caps | Cross-handle/provider/temporary retention and measured process envelopes remain. Preserve deterministic refusal; do not derive RSS promises from nominal page bytes. [Memory contract](docs/INDEXES_AND_VECTORS.md#continuation-after-69ed311-bounded-maintenance-and-memory). |
| OPS-5 / P1.12 | Partial: growth/rebuild, quiescent cleanup, 65,536-bucket sizing, sparse page-wise maintenance/batched heads, configurable HASH decoding and opt-in repeated-key posting layout | Cleanup preserves catalog-owned STALE/BUILDING and retained-WAL dependencies. Online/catalog-generation retirement and sharding remain. [Posting layout](docs/POSTING_HASH.md), [index contract](docs/INDEXES_AND_VECTORS.md), [cleanup](docs/READ_CONTROL_AND_INDEX_CLEANUP.md). |
| OPS-6 / P1.11 | Partial: sparse head-initialization barriers shared inside one committed publication; physical conflicts and exclusive publication remain | Remove only proven redundant publication/page work. Disjoint logical rows may still conflict; do not promise linear writer scaling or replace multiwriter with an application-wide single-writer premise. |
| OPS-7 / P2.6, P2.9 | Implemented bounded DX/isolation checkpoint in 0.0.5 | Typed `Unpack[ConnectOptions]` plus per-connection checksum selection, including custom registries. Standalone low-level installers retain their legacy default outside database operations. No checksum algorithm or persisted byte semantics changed. [R2 evidence](docs/reports/V005_R1_R2_CHECKPOINT.md). |
| OPS-8 / §8.5 | Partial: reusable Query/cursor and cooperative read cancellation/deadlines exist | `Query` is not a durable prepared plan or HTTP token. Broader streaming/prepared APIs remain; controls do not preempt blocking calls, run watchdog cleanup or interrupt commits. [Contract](docs/READ_CONTROL_AND_INDEX_CLEANUP.md). |
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

## Next iteration assessment: feature/v0.0.5

Assessment opened September 8, 2026 at the operator's request. Local branch
`feature/v0.0.5` starts at the released main merge `425362a`. This section orders
existing backlog items; it is not another independent plan. The operator subsequently
selected performance items 1–6 for delivery 0.0.5; the bounded implementation and
follow-up validation are now complete as recorded below,
and package/source versions are bumped to 0.0.5. Product-capability rows below remain
unselected. No live Pulse mutation is implied. Effort is relative: small
means a localized change, medium crosses a few components, large changes persisted
or concurrent protocols. Potential impact is a hypothesis, not a measured speedup.

### Performance: recommended execution order

| Order / existing IDs | Concrete opportunity and evidence | Effort / potential impact | Bounded first action and stop condition |
| --- | --- | --- | --- |
| 1 / PERF-COLD, BATCH-REL-1 | KG first page, next 500 nodes and relationship statement fan-out. Latest-source UI latency is unknown; existing 64-step destination batches already remove much per-identity certification overhead, so repeating that implementation is not new work. [Batch evidence](docs/reports/GLOBAL_DESTINATION_BATCHING_0_0_4.md). | Small assessment; medium implementation / potentially high visible latency benefit | One fixed read-only workload, first/next page, cold/warm and one independent writer; separate open, query/layout, transport and render costs. Select the dominant repeated work only. Preserve exact nodes/edges, multiplicity, totals and independent errors. No new public batch API unless remaining overhead justifies it. |
| 2 / PERF-WRITE | Attribute native commit versus admission, SQLite/outbox, verification and scheduling. Latest full calls are 10.726 s commit and 54.952 s Global ACK, while a different private native workload measured 51–57 ms; these are not comparable A/B samples. [Measurement boundaries](docs/PERFORMANCE.md). | Small attribution; implementation depends on result / potentially high end-to-end benefit | Validate phase-observation capture first. Use a fixed synthetic representative write before any newly authorized real spec. Remove repeated generic engine work only where measured; consumer scheduling/relational costs stay in Pulse, not Grafx Core. Do not consume all reserved specs or keep repeating runs without a discriminating question. |
| 3 / PERF-SCALE, OPS-5 | Avoid whole-graph work for selective lookups and bounded pages; inspect skew, relationship fan-out and growth. Ordered cursors, indexed hybrid BFS, explicit 65,536-bucket directories and skew diagnostics exist; completeness fallbacks can remain O(N). | Medium to large / high at larger N if an affected access path dominates | Fixed selective queries at three declared sizes, with operation/page counts and identical semantics. Identify actual scan/overflow pressure before another index change. Full graph enumeration and verification remain proportional to data; no universal O(1) promise or removal of corruption detection. |
| 4 / PERF-MEM, OPS-3/4/5 | Reduce retained state and growth under updates/deletes: multiple handles, immutable index generations, overflow history. N3 retirement and R3 reuse address eligible overflow growth; file shrinking and indexed free-page discovery remain absent. [Current maintenance limits](docs/OPERATIONS.md#maintenance-backup-and-upgrades). | Medium diagnosis; large reclamation work / high long-lived-store value | A bounded churn workload with peak RSS, disk growth and a pinned reader. Separate cache/accounting fixes from physical reclamation. Reclamation requires snapshot/lifetime and crash proofs; it is not a quick online-vacuum toggle. |
| 5 / CONC-CPU-1, OPS-6 | Reduce demonstrated CPU/decode/expression or publication contention after legitimate I/O parallelism. Independent readers already exist; more handles do not remove the GIL or exclusive durable publication. | Medium; large for native/process changes / conditional | Reuse the same fixtures with 1/2/4 participants and identify wait versus CPU. Prefer bounded local hot-path work. Native kernels, processes or publication redesign are selected only if that cost dominates; retain both OCC checks and multiwriter semantics. |
| 6 / OPS-8, BATCH-REL-1 | Reusable prepared execution and bounded shared statement preparation, beyond existing expression reuse. A reusable public Query is not a persisted prepared plan. | Medium / conditional, lower priority until preparation is material | Count parse/plan/catalog preparation in the chosen KG workload. Cache only derived immutable preparation with explicit schema/snapshot/config invalidation and budgets. Do not cache authority or silently combine independent statement failures. |

Orders 1–2 form the first checkpoint: one bounded attribution pass per workload,
then the demonstrated high-impact localized fixes and one grouped regression.
No percentage floor or repeated marginal timing gate. If the dominant cost is
outside Grafx, record that fact and route the integration change appropriately;
do not invent an engine rewrite to keep the queue busy. Orders 3–6 were handled
after that checkpoint; the outcomes below are not six new mandatory rewrites.

### 0.0.5 implementation checkpoint — September 8, 2026

The six selected **bounded 0.0.5 actions are complete**, including their follow-up
measurements and dispositions below. This does not close the broader PERF/OPS
backlog. Four bounded engine changes are implemented and tested: layout-sized plan retention with admission
limits, numeric IN membership, unstaged-writer scalar PK reuse and standard-library
top-k heaps. [Evidence and latest synthetic measurements](docs/reports/V005_NATIVE_PERFORMANCE_CHECKPOINT.md).
Version/package metadata is 0.0.5; no on-disk format, public configuration, WAL/OCC
protocol or automatic commit-history capability was changed.

| Selected item | 0.0.5 bounded outcome | Explicit remaining product/consumer work, not silently included |
| --- | --- | --- |
| 1 / KG loading | Closed: exact route/service/adapter fixture at 128/512/1,100 nodes; 13 of 69 layouts selected; real Pulse API client + GraphCanvas loaded 500/1,000/1,100 nodes and terminated pagination | Full authenticated production KnowledgeGraphPage/SQLite overhead and latest-source Ladybug parity are not measured by fixture controls. No redundant layout implementation |
| 2 / Write/Global delivery | Closed for the selected fixed synthetic write: fresh-certified preflight reuse; native phases, actual Global vector writes, Core dispatch and integrity inventories measured; phase/fence/cancellation capture tests pass | Historical full production commit/54.952 s ACK still lacks graph-versus-SQLite/scheduling attribution. Not claimed solved; requires a separate consumer receipt after deployment, not another native timing floor |
| 3 / Scale | Closed bounded experiment: numeric IN no longer walks each list for every numeric match; three native sizes plus Pulse sizes and 128-edge hub; ordered page examines 500 candidates at both 512 and 1,100 nodes | Whole enumeration remains O(N); 4,096-bucket limit not reached, so no speculative format/sharding rewrite. Wider skew/index growth remains PERF-SCALE/OPS-5 |
| 4 / Retained memory | Closed cache slice and lifetime experiment: text/byte admission and owner release; 12 update/delete/overflow cycles with 1/2/4 handles plus pinned reader, measured RSS, exact old/current answers and clean reopen | Historical experiment predates N3 retirement and R3 reuse; it is not a measurement of the new allocator. No truncation or immutable-index orphan cleanup. Physical allocation/index history remains OPS-3/4/5 |
| 5 / CPU/concurrency | Closed attribution: actual page shape at 1/2/4 independent handles, including two writers; correct reads/durable updates; wall/thread CPU/non-CPU recorded | More threads did not yield linear scaling. Non-CPU includes GIL/OS/I/O, not proven lock-only wait. Native kernels/processes/publication redesign remain OPS-6 |
| 6 / Preparation | Closed: 160-shape cyclic cache regression fixed within limits; real warm KG cycle built zero parses/plans, with 16 retained plans | Preparation is not the dominant warm KG cost. Durable public prepared-plan APIs remain OPS-8; no authority cache added |

Validation: 2,841 grouped query/API/import/documentation tests passed in 306.28 s;
113 consumer integration tests in 28.45 s; final focused 137 tests in 5.04 s overlap
that coverage and include the final writer OCC/read-partition cases. Follow-up:
92 additional/overlapping Community tests in 61.82 s, 39 Core phase/fence/inventory
tests in 4.75 s, browser fixture and isolated 0.0.5 wheel smoke all passed. The final
focused/documentation slice passed 149 tests in 9.43 s (overlapping prior coverage).
Strict mypy
retains the identical 254 baseline diagnostics in query_engine.py after normalized
line offsets; no new diagnostics, not a type-clean-engine claim. No live Pulse
restart, production deployment, spec consolidation, backfill, commit/push or release
occurred. Isolated UI listeners were stopped. The linked report preserves exact
measurement boundaries and every remaining limitation; closure is of the six
bounded actions, not a promise to finish every referenced OPS milestone in 0.0.5.

### Product evolution: proximity versus value

| Recommended position / existing IDs | Next usable capability | Effort / value / dependency |
| --- | --- | --- |
| Small independent DX improvement / OPS-7 | Typed keyword discoverability for connect options, matching the documented configuration and runtime validation; preserve custom-adapter consumption. | Small to medium / immediate integration usability, no expected throughput gain. Keep checksum-provider isolation as a separate deeper item; autocomplete does not solve process-global selection. |
| First substantial capability / GX-CAP-1 remainder | Delivered as N4: durable writer publication, qualified lookup, metadata/history, verification/metrics and coordinated transfer/restore/fork semantics. | Local development acceptance closed; release/deployment remain separate. Adds measured write/retention cost, not a speedup or full temporal history. [Evidence and limits](docs/reports/V005_N3_N4_ACCEPTANCE.md). |
| Operational evolution / OPS-1, OPS-2 | R4 bounded physical backup/restore and OPS-2 logical transfer with opt-in crash resumption. | Implemented development APIs. No no-pause physical hot backup, generic custom-storage restore or journal-history transfer. [Logical contract](docs/LOGICAL_TRANSFER.md). |
| Delivered search foundation / GX-CAP-5/6 | Native FTS, single-pass query frequencies, operation-local analysis reuse, WAL or opt-in durable statistics, explainable hybrid retrieval, indexed incident evidence and aggregate memory diagnostics. | Ineligible/legacy cold census and scan fallback remain linear; pure reuse is capped; hybrid ranks bounded source windows. [FTS](docs/FULL_TEXT_SEARCH.md), [hybrid](docs/HYBRID_SEARCH.md). |
| After commit provenance / GX-CAP-2, GX-CAP-3 | Attached catalog sessions and opt-in system-time history. | Large each / strategic multi-store/temporal value. CAP-1 first; one-store writes remain the rule. Neither is required to fix current KG loading. |
| Later dependent capabilities / GX-CAP-4/7/8/9/10, AGENT | Bitemporal retrieval, extension SPI, Arrow/exchange, general graph algorithms, schema evolution and optional agent packages. | Medium to large individual slices / valuable but not all immediate. Arrow/external scans currently depend on CAP-7 and existing streaming/bulk. Hybrid v1 does not implement agent ContextPacks or general cross-table traversal. |

The selected performance/DX checkpoint and CAP-1 N4 scope are delivered. Physical
backup was subsequently implemented; logical transfer and FTS were explicitly
approved and are in the items 3–4 acceptance checkpoint above. Existing
multi-read/write, durability, consistency and fail-closed
constraints apply throughout. Group commit, indexed-only DETACH DELETE, authority
bundles and sharding remain in their existing rejected/deferred dispositions.

## Database capabilities and dependencies

Generic database functionality owns commit identity, catalog sessions, temporal
history, FTS and hybrid retrieval. Temporal history is **logical retained history**,
not accidental access to MVCC versions. One transaction writes one physical store;
physical edges do not cross stores. Embedding generation stays outside the engine.

| Milestone | Status | Scope, prerequisites and contract |
| --- | --- | --- |
| GX-CAP-0 | Implemented checkpoint | Database-first boundaries, six ADRs, capability manifest, isolated package/import checks. [Spec](docs/specs/SPEC-GX-CAP-0.md). |
| GX-CAP-1 | Implemented bounded N4 checkpoint | Qualified CommitId, opt-in durable metadata/history, monotonic logical commit time, lookup/pagination, verification and coordinated transfer/restore semantics. Not full temporal row history; logical graph v1 transfer excludes historical journal entries. [Evidence](docs/reports/V005_N3_N4_ACCEPTANCE.md), [usage](docs/COMMIT_HISTORY.md), [spec](docs/specs/SPEC-GX-CAP-1.md). |
| GX-CAP-2 | Partial: session/workspace and bounded copy implemented | Explicit resolution/permissions, ownership, pinned one-store transactions, bounded workspace discovery and atomic PK-table copy/fail policy with indexed durable receipts. Arbitrary selection, skip/merge, anonymous-node copy, CLI inventory and federation remain open. [Spec](docs/specs/SPEC-GX-CAP-2.md), [evidence](docs/reports/V006_COPY_PHRASE_ACCEPTANCE.md). |
| GX-CAP-3 | Partial internal prototype; native persistence not implemented | Typed event/image planning and append-transition validation tested, without runtime admission. Opt-in atomic history, historical schema/edge semantics, retention, indexes and typed time-travel API remain. [Spec](docs/specs/SPEC-GX-CAP-3.md), [exact prototype boundary](docs/specs/SYSTEM_HISTORY_APPEND_DRAFT.md). |
| GX-CAP-4 | Planned | Valid time, bitemporal semantics and graph/version diff; builds on CAP-3. [Spec](docs/specs/SPEC-GX-CAP-4.md). |
| GX-CAP-5 | Implemented v1 plus prefixes, relationship indexes, durable/history statistics and exact phrase verification | Native FTS, frozen analyzers, weighted BM25, snapshot scores and full lifecycle. Phrase positions are verified in same-snapshot heap candidates; durable positional postings, position-return/proximity APIs and same-name analyzer replacement remain open. Existing ineligible-statistics census remains linear. [Usage](docs/FULL_TEXT_SEARCH.md), [spec](docs/specs/SPEC-GX-CAP-5.md), [latest evidence](docs/reports/V006_COPY_PHRASE_ACCEPTANCE.md). |
| GX-CAP-6 | Implemented bounded v1 plus certified incident expansion and shared controls/memory | Weighted RRF, union/intersection, source explanations, graph boost/filter, explicit missing-source partial policy, indexed BFS and per-phase logical peaks. No embedding provider, cross-table fusion, graph-only candidate expansion or universal recall promise. [Usage](docs/HYBRID_SEARCH.md), [spec](docs/specs/SPEC-GX-CAP-6.md), [latest evidence](docs/reports/V005_EIGHT_ITEM_CHECKPOINT.md). |
| GX-CAP-7 | Partial: trusted scalar SPI implemented | Explicit immutable per-handle registry, typed scalar UDFs/direct calls, NULL/value budgets and typed failures. Aggregate/table/procedure extensions, durable manifests and sandboxing are not implemented. [Usage](docs/EXTENSIONS_AND_ARROW.md), [remaining spec](docs/specs/SPEC-GX-CAP-7.md). |
| GX-CAP-8 | Partial: scalar/vector Arrow, Pandas/Polars, local Parquet/CSV/JSONL and detached graph exchange implemented | Explicit native vector identity, nullable typed batches, atomic import staging and caller-owned commit; bounded local files and NetworkX/projection Arrow exports. External query scans/COPY, arbitrary nested/entity ingestion and graph import remain. [Usage](docs/TABULAR_AND_PARQUET.md), [text](docs/LOCAL_TEXT_IMPORT.md), [exchange](docs/GRAPH_EXCHANGE.md), [remaining spec](docs/specs/SPEC-GX-CAP-8.md). |
| GX-CAP-9 | Partial: weighted projections, retained lookup/CSR/transitions/simple topology, degree/WCC/SCC, BFS/Dijkstra, PageRank, k-core and label propagation implemented | Bounded read-only algorithms, cancellation, explicit NumPy and independent/NetworkX oracles for declared algorithms. Persistent catalog, mutation/write modes, Louvain and broader algorithm packages remain. [Usage](docs/GRAPH_PROJECTIONS.md), [remaining spec](docs/specs/SPEC-GX-CAP-9.md). |
| GX-CAP-10 | Partial: additive migrations, bounded logical views and nullable-column evolution | Atomic per-version CREATE DDL; typed persisted base-table read views; typed append-only nullable columns with exact old-row layouts. Arbitrary ALTER, other S1/S2/S3 migrations, nested views and derived/materialized graphs remain. [Migrations](docs/SCHEMA_MIGRATIONS.md), [views](docs/LOGICAL_VIEWS.md), [columns](docs/NULLABLE_COLUMNS.md), [remaining spec](docs/specs/SPEC-GX-CAP-10.md). |
| GX-CAP-11 | Partial: 0.0.5 compatibility automation and local upgrade evidence implemented | Real-wheel upgrade/refusal, cross-selector reopen and Windows/Ubuntu × Python 3.11–3.13 workflow. Remote matrix runs and release acceptance are not implied. [Current evidence](docs/V005_COMPATIBILITY.md), [spec](docs/specs/SPEC-GX-CAP-11.md). |

Implementation precedence: close release boundary → complete CAP-1 → catalog/
system-time/FTS foundations → bitemporal/hybrid → extension/interoperability/
algorithms/schema evolution → combined hardening. Independent design can overlap;
shared WAL/format changes must be integrated in a controlled sequence, not competing
branches that independently redefine the protocol.

## CAP-2 through CAP-4 opportunity assessment

0.0.6 consumption additions also extend the bounded CAP-8/9 surfaces above:
[SQLite ingestion](docs/LOCAL_SQLITE_IMPORT.md), [offline HTML inspection](docs/HTML_SNAPSHOTS.md)
and [topological ordering](docs/GRAPH_PROJECTIONS.md#topological-ordering-006).
These do not implement CatalogSession, temporal history or the other remaining
CAP-8/9 capabilities.

Assessed September 9, 2026 against `65ab659`, after the preceding eight-item
implementation was committed and pushed. **Assessment only: CAP-2/3/4 remain
Planned.** No new version assignment, implementation approval, release or Pulse
data operation is implied. This section refines the existing backlog; the linked
specs, ADRs and preserved source requirements remain authoritative, including
requirements not repeated here. It does not reopen the preceding delivery's DoD.

### Existing foundations versus missing capability

| Capability | Reusable, implemented foundation | Actual remaining work | Effort / value |
| --- | --- | --- | --- |
| GX-CAP-2 | Explicit database handles, read-only admission, transaction lifecycle, store UUIDs, CAP-1 provenance, checksummed logical export and resumable import into a fresh store. | CatalogSession, alias/permission/path policy, pinned catalog selection, deterministic workspace resolver, ownership-safe detach/close, CLI inventory, selected copy into an existing target and durable idempotency receipts. | Large overall; session-only slice medium. High integration value for project/user/reference stores, not an automatic engine speedup. |
| GX-CAP-3 | Qualified CommitId, monotonic ordered commit time, journal lookup/paging, stable record identities, WAL/OCC/recovery, snapshot reads and bounded maintenance. | Durable opt-in node/edge versions and historical schema, atomic current/history effects, lineage, indexed typed historical reads, timestamp resolution, retention pins/horizons, verification and temporal transfer semantics. | Very large; highest storage/protocol risk of these items. Enables reproducible historical graph queries and auditing; introduces write/storage amplification. |
| GX-CAP-4 | CAP-1 time/identity values and ordinary typed timestamps. CAP-3 is still missing. | Application validity periods and overlap policy, concurrent enforcement, two-coordinate queries, graph/version/property diff with lineage, full retention policies and later query syntax/CLI. | Large to very large after CAP-3. Enables retroactive corrections and “what was known then about what was valid then”; not a shortcut to faster current-state reads. |

Concrete evidence:

- [Database.begin](src/okto_grafx/engine/database.py) currently accepts mode and
  commit metadata, not a catalog or temporal query context. There is no public
  CatalogSession/workspace resolver or temporal query facade in this source tree.
- [TableDef](src/okto_grafx/domain/model/schema.py) carries schema version and
  relationship endpoint declarations, but no system/valid-time policy. Endpoint
  RecordIds are already stable across updates; that helps lineage, but does not
  retain deleted rows or historical schema by itself.
- [Commit identities](src/okto_grafx/domain/txn/commit_identity.py) are store-qualified;
  [journal lookup/history](src/okto_grafx/engine/commit_catalog_store.py) address
  commits. [CAP-1's consumer contract](docs/COMMIT_HISTORY.md) explicitly excludes
  graph time travel and does not make correlation metadata an idempotency key.
- [Logical transfer](docs/LOGICAL_TRANSFER.md) and its
  [implementation](src/okto_grafx/transfer.py) export current state only; import
  commits batches in a private fresh-UUID store and publishes the directory.
  Resumable import can recover a lost publication acknowledgement, but this is
  **not** a selected, one-transaction merge into an existing target catalog.
- [Heap reclamation](src/okto_grafx/engine/heap_store.py) has a physical snapshot
  horizon. Keeping its old MVCC versions or keeping WAL forever would not satisfy
  the separate durable temporal-history contract.

### Recommended finite implementation order

The order below is a proposal, not an additional authorization. CAP-2 and CAP-3
are siblings over CAP-1; CAP-3 does **not** depend on completing CAP-2. CAP-4 does
depend on CAP-3. Existing CAP-1 implementation satisfies the basic provenance
prerequisite, but copy receipts and temporal storage still need their own design.

| Order | Bounded checkpoint | Required exit evidence |
| --- | --- | --- |
| 1 / CAP-2 session | CatalogSession attach/detach/list/use/begin; unique ASCII aliases, reserved main, read-only default, immutable identity binding, explicit handle ownership and one catalog pinned at begin. | Two stores and concurrent participants; default changes cannot reroute active transactions; writes through a read-only attachment refuse before mutation; in-use detach refuses; attach failures release resources; close attempts all owned handles and reports cleanup failures. |
| 2 / CAP-2 workspace | Optional resolver outside the engine: explicit config, supplied root, bounded allowed-marker search, policy-permitted cwd; user/global opt-in. Canonical-path/root policy, explicit configuration and CLI status/list. | Independent processes resolve the same workspace; denied roots, alias/path/UUID ambiguity, Windows junction/symlink policy and resolution failures refuse. No directory creation on denied resolution; no hidden global store or paths derived from retrieved text. |
| 3 / CAP-2 copy | Bounded snapshot selection and checksummed package, node/edge identity mapping, explicit conflict policy, one target write transaction, durable receipt bound to source UUID/commit, target, selection hash, policy and idempotency key. | Endpoint closure, concurrent same-key attempts, input-mismatch refusal and crash before/after target COMMIT including lost acknowledgement. Data and receipt share the commit. Oversized atomic copies refuse at declared limits; do not silently batch visible target mutations. |
| 4 / CAP-3 durable history | Specify physical format/capability/upgrade/replay first; table opt-in, activation boundary, retained schema and lineage; publish node/edge current and history effects atomically. | Create/update/delete/recreate and DETACH DELETE; concurrent writers; cold reopen and repeated recovery at publication/crash cuts; old readers refuse unsupported required capabilities. No invented pre-activation history and no temporal overhead silently enabled for ordinary tables. |
| 5 / CAP-3 read and maintenance | Typed system-as-of by qualified commit or ordered timestamp, between/versions and historical traversal; entity/commit-range access paths, explicit scan diagnostics, temporal verify, manual bounded resumable retention, reader pins and backup/transfer horizon semantics. | Edge plus both matching-lineage endpoints visible at the selected time; clock ties/regression; wrong-store/future/unavailable coordinates; pinned pruning and vacuum independence; corruption distinct from expired history; reopen/export/import preserves or explicitly maps supported identities/horizons. |
| 6 / CAP-4 valid time | Explicit valid-time intervals independent of system time, interval typing, configurable overlap rule and indexed concurrent enforcement. | Half-open/unbounded interval boundaries, invalid ranges, retroactive corrections and competing overlapping writers. A backdated business correction must not rewrite what the database previously knew. |
| 7 / CAP-4 bitemporal and diff | Combine system and valid coordinates; bounded node/relationship/property diff with before/after, endpoints, commit provenance and explicit update versus delete/recreate; complete retention policies. | Two-coordinate fixtures, lineage/PK reuse, empty versus unavailable history, bounded pagination/work, indexed range plans and retention interaction. Specify diff meaning for both coordinates; never compare only current PK values. |
| 8 / CAP-4 consumption closure | Historical query syntax and CLI after typed contracts stabilize; finish configuration, API/error contracts, examples and operational guidance. | Typed API/CLI/query equivalence for supported operations, documented limitations, executable examples and one grouped regression of integrated capabilities. Earlier checkpoints also require their own docs and focused tests. |

### Decisions to settle within those checkpoints

These are finite contract decisions already implied by the specs, not permission
to expand into distributed transactions, a new agent product or arbitrary DDL:

- **Session authority:** permissions constrain operations through that session;
  a Python library is not an OS sandbox for unrelated code/handles. Define
  canonical-path and store-identity revalidation and ownership explicitly. One
  store per transaction does not mean one writer per application or a global lock.
- **Promotion:** specify fail/skip/explicit-merge behavior for PKs, schemas,
  relationship multiplicity and endpoint mappings. Distinguish partial selection
  from partial success. Require or explicitly activate CAP-1 before promising
  commit-backed provenance; never manufacture a commit from an arbitrary LSN.
  Receipt lookup must be indexed, not a scan of all commit metadata. Define
  receipt retention/idempotency horizon so pruning cannot turn a retry into a
  duplicate. Do not confuse artifact checksums with source authentication.
- **History activation/schema:** define the initial current-state baseline for
  pre-existing rows and history availability for non-temporal tables/endpoints.
  Either retain all required endpoint history or reject unsupported mixed-history
  traversals explicitly; never silently substitute today's endpoints. Retain the
  schema required to decode historical payloads without requiring arbitrary ALTER
  implementation as a prerequisite. Define disable/re-enable behavior before
  exposing any such operation.
- **Time/access paths:** store-local CommitId is ordering authority; raw wall clock
  is not. Timestamp resolution selects by monotonic ordered_at. Plan efficient
  entity history, time containment and commit-range diff paths rather than
  repeatedly scanning all versions. Full unfiltered output still costs at least
  the size of the requested result; do not promise universal O(1) queries.
- **Retention and transport:** keep manual CAP-3 pruning distinct from CAP-4
  none/keep-last/keep-for/keep-since policies. Publish available horizons and typed
  expiry errors. Version the logical-transfer contract before claiming history
  preservation; current-state-only v1 must remain explicit, not silently lose
  requested history. Writable copies get remapped identities, not cloned authority.
- **History search:** existing current-state FTS/vector indexes are not certified
  historical indexes. Declare which temporal operations are supported and refuse
  unsupported combinations; extending every search/analytics feature is not an
  implicit extra milestone in this assessment.

Quality closure preserves multi-reader/multiwriter access, both OCC checks,
fencing, durable WAL order and fail-closed recovery. Run focused feature/failure
checks per checkpoint and grouped regression per cohesive delivery. Measure
opt-out overhead, temporal write amplification, retained bytes and query p50/p99
on fixed bounded synthetic workloads; report the observed cost without a moving
performance percentage gate or using reserved Pulse specs.

Scope references: [CAP-2 spec](docs/specs/SPEC-GX-CAP-2.md),
[catalog ADR](docs/architecture/ADR-GX-004-ATTACHED-CATALOGS.md),
[CAP-3 spec](docs/specs/SPEC-GX-CAP-3.md),
[CAP-4 spec](docs/specs/SPEC-GX-CAP-4.md),
[temporal ADR](docs/architecture/ADR-GX-002-TEMPORAL.md) and
[complete preserved requirements](docs/archive/ROADMAP_SOURCES.md).
Federated search with per-store independent snapshot tokens and explicit source
failures remains a later catalog milestone; cross-catalog query grammar, physical
cross-store edges and distributed commit are not silently included here.

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

## Comparative feature gaps and minimum parity

Registered September 10, 2026 at the user's request, from
[Where Grafx is less complete](docs/FEATURE_COMPARISON.md#where-grafx-is-less-complete).
This records the complete gap set without replacing existing capability specs.
The broad CMP register remains **assessment/backlog**. Its selected MP-1–MP-8
minimum-parity slices were subsequently approved and implemented for 0.0.6,
as recorded below. This does not authorize Pulse changes, release or relaxation
of multi-read/write, WAL, consistency or isolation guarantees.

### Complete comparative gap register

| ID | Gap retained from the comparison | Existing owner / bounded direction | Effort and disposition |
| --- | --- | --- | --- |
| CMP-Q | Query-language breadth | Extend the closed query surface in explicit slices; broader joins/subqueries/procedures are not supplied by parser acceptance. Reuse CAP-7 for extension contracts. | Small-to-large by slice; no full Cypher compatibility claim. |
| CMP-T | Stored value/type breadth | Evaluate DATE, DECIMAL and nested-column needs against current typed values; coordinate schema evolution with CAP-10 and upgrade/refusal with CAP-11. | Medium-to-large; persistent types require format/recovery/interop decisions, not just Python wrappers. |
| CMP-D | Language drivers | First consider documented structured CLI consumption from another language. A real network protocol/client or native binding is a separate supported surface. | Small for recipes; medium-to-large for genuine drivers. |
| CMP-U | Graphical tooling | Start with detached read-only graph/schema inspection using bounded exports; later consider an optional local explorer. Pulse UI is not a bundled Grafx GUI. | Small-to-medium for a static viewer; larger for interactive administration. |
| CMP-F | Remote federation and attached catalogs | CAP-2 owns CatalogSession, resolution/permissions/workspaces and one-store writes. CAP-8 owns external scans/interoperability; bounded import is not federation. | Large overall; selected ingestion adapters can be smaller. |
| CMP-HA | Distributed availability | Retain replication/synchronization direction with explicit conflict policy; cluster/consensus/sharding still requires a separate architecture/scope decision. | Very large; deferred, not promoted into the current local-first delivery by this register. |
| CMP-S | Built-in authentication/authorization | Assess optional service-boundary auth and scoped read/write policy before any server exposure. Distinguish API permissions from direct filesystem access; never claim a wrapper secures untrusted local file owners. | Medium-to-large plus threat modeling. At-rest encryption retains its separate key-lifecycle prerequisite. |
| CMP-H | Full temporal history and bitemporal queries | CAP-3 owns retained historical schema/nodes/edges and time travel; CAP-4 adds valid time/bitemporal semantics and graph/version diff. | Large/very large; depends on CAP-1 and then CAP-3, not satisfied by commit metadata or current MVCC snapshots. |
| CMP-E | Ecosystem | Broader examples, language recipes and optional integrations; CAP-7/8/9 and AGENT remain the owners of real extension/interop/analytics/agent capabilities. | Small for bounded recipes; medium-to-large for maintained integrations. |
| CMP-O | Operational evidence and maturity | CAP-11 plus existing operational validation: supported-version/platform results, reproducible crash/reopen cases, mixed-workload measurements and explicitly bounded SLO evidence. Publication/test counts alone do not prove production maturity. | Small for an evidence map; sustained work for certification/SLOs. No endless percentage-based performance gate. |

These ten entries decompose all four bullets in the comparison; they do not add
ten competing execution programs. Missing catalog/temporal/security/HA features
remain missing until their own acceptance contracts are met.

### Relatively inexpensive minimum-parity candidates

Execution approved for the 0.0.6 development line (`feature/v0.0.6`).
MP-1–MP-8 are **delivered and validated locally**: 3419 passing grouped regression
tests, 4 Node tests (including the real CLI), strict TypeScript compilation and
Chrome viewer acceptance. See the [acceptance report](docs/reports/V006_MINIMUM_PARITY_ACCEPTANCE.md)
for the exact test scope and remaining platform/release boundaries. Their minimum
contracts are documented below; larger CMP gaps remain open. No persistent-format,
multi-reader/writer, WAL/durability or default connection changes were required.

| Delivered slice | Public consumption and focused evidence |
| --- | --- |
| MP-1 | [CLI schema/spaces/indexes/build capabilities](docs/CLI.md); `tests/cli/test_schema_inventory.py`, `tests/cli/test_discovery_search.py` |
| MP-2 | [CLI text/vector/hybrid search](docs/CLI.md); native search/filter/refusal acceptance in `tests/cli/test_discovery_search.py` |
| MP-3 | [JS/TS subprocess recipe](examples/cli-consumer/README.md); Node tests including real CLI, timeout/output/JSON/unsafe-integer failures; strict TypeScript compile |
| MP-4 | [Native scalar contracts](docs/QUERY_LANGUAGE.md#native-scalar-additions-006); `tests/query/test_native_scalars.py` |
| MP-5 | [Offline HTML picture](docs/HTML_SNAPSHOTS.md); `tests/api/test_html_snapshot.py` plus headless Chrome acceptance |
| MP-6 | [Bounded SQLite ingestion](docs/LOCAL_SQLITE_IMPORT.md); `tests/api/test_sqlite_import.py`, including whole-call rollback/source release |
| MP-7 | [Two-branch UNION ALL](docs/QUERY_LANGUAGE.md); `tests/query/test_union_all.py`, including shared budgets and old/new snapshots |
| MP-8 | [Topological order/cycle result](docs/GRAPH_PROJECTIONS.md#topological-ordering-006); `tests/api/test_topological_order.py` against an independent stdlib oracle |

Effort is a comparative engineering estimate, **including focused tests and docs**,
not a measured schedule. Low means a bounded wrapper/recipe over existing contracts;
low–medium adds UI or adapter behavior; medium changes a closed query/algorithm
surface. None provides complete parity with Ladybug or Neo4j.

| Order / ID | Minimum useful delivery | Comparison target and reuse | Effort / expected benefit | Explicit boundary and acceptance |
| --- | --- | --- | --- | --- |
| 1 / MP-1 | CLI schema/index/capability inventory as stable JSON | Minimum schema-inspection convenience of Ladybug Explorer/Neo4j tooling; reuse public catalog/index views and existing CLI envelopes. CMP-U/D/E. | Low / better discovery for developers and agents. | Read-only, deterministic schema, no index creation; cover empty stores, stale handles, budgets and CLI exit codes. Inventory APIs already exist; this is a new consumption path, not a new engine capability. |
| 2 / MP-2 | CLI text/vector/hybrid search subcommands | Expose existing search APIs without requiring a Python script. CAP-5/6, CMP-D/E. | Low / immediate search accessibility. | Typed vector/space/filter input, one owned read snapshot, bounded output and faithful error/partial-result status; no new ranking engine or embedding provider. |
| 3 / MP-3 | Tested JavaScript/TypeScript CLI-consumer recipe | Minimal non-Python consumption alongside competitors' drivers. Reuse existing `--json`, subprocess lifetime and error envelopes. CMP-D/E. | Low / onboarding with no server deployment. | No shell interpolation, bounded stdout/timeouts and process cleanup. Explicitly not a native driver, connection pool, remote service or long-lived transaction API. |
| 4 / MP-4 | Small native scalar-function batch: lower/upper/trim and abs | Reduce routine Cypher adaptation; reuse current scalar planning/evaluation and trusted-UDF experience. CMP-Q/CAP-7. | Low–medium / common query compatibility. | Freeze arity, NULL/Unicode/numeric/overflow semantics; test planner and execution agreement. No general casts, date arithmetic or language-complete claim. |
| 5 / MP-5 | Standalone read-only HTML graph/schema snapshot viewer | Minimum visualization comparable in purpose to Explorer/Browser; reuse bounded detached projections/exports. CMP-U. | Low–medium / useful inspection without Pulse. | Node/edge cap, visible truncation metadata, escaped labels, no executable property content or external CDN dependence. Static snapshot only: not live administration, authentication or editing. |
| 6 / MP-6 | Bounded SQLite-to-Grafx ingestion recipe/adapter | Minimum interoperability with a common local relational source; reuse SQLite reads and typed atomic import staging. CMP-F/E, CAP-8. | Low–medium / simpler local adoption. | Read-only source, explicit query/type/root/NULL policy, row/byte limits, rollback tests. Not SQL pushdown/federation, CDC or a cross-database transaction; do not hold unrelated source locks across long commits. |
| 7 / MP-7 | Two-branch read-only `UNION ALL` | Narrow query-language parity beyond current duplicate-eliminating UNION. CMP-Q. | Medium / fewer client-side merges. | Preserve duplicates, compatible types/arity, NULLs, budgets and documented branch/order/window rules. No chains, nesting, write branches or general set-operator expansion. |
| 8 / MP-8 | Bounded topological ordering with typed cycle detection | Small graph-analytics addition using detached projections, adjacency and existing work/cancellation budgets. CMP-E, CAP-9. | Medium / useful dependency-graph analysis without a full analytics subsystem. | Directed graph only; deterministic order, disconnected graphs, self-loops and cycles tested against an independent oracle. No persisted algorithm catalog, mutation or claim of GDS parity. |

Checkpoint order: MP-1–3 before MP-4–6; MP-7–8 remain
separate bounded query/algorithm slices. Prioritize actual consumer demand; a wrapper that
duplicates an already sufficient API need not be implemented just to gain a checkbox.

Not cheap substitutes: nested persistent types, phrase-position FTS, general
federation, native multi-language bindings, full temporal storage, arbitrary
serializability, production RBAC and HA/consensus. A demo or wrapper does not close
those contracts. All selected code changes require feature/adversarial tests,
grouped regression and updated public API/configuration/usage documentation.

### ACID is a clarification, not a missing engine feature

The first comparison used “ACID” for competitors but listed Grafx's mechanisms,
creating a misleading asymmetry. Corrected: Grafx implements single-store ACID
properties with snapshot/OCC isolation and documented host/filesystem boundaries.
[ACID scope and evidence](docs/OPERATIONS.md#acid-scope). This does not promise
general serializability or independent certification.

`CMP-O` includes a bounded public guarantee-to-test evidence map and any genuinely
uncovered anomaly/recovery cases discovered by review. **Do not create an
“implement ACID” backlog item or count ACID as a new performance feature.** A
future stronger isolation mode would need its own supported workload, anomaly
tests and concurrency/throughput analysis, not an automatic serialization of writers.

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
