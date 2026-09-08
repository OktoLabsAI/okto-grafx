# Okto Grafx roadmap

**Single active product backlog — reconciled September 8, 2026.**
Baseline: published `0.0.4`, tag `v0.0.4`, main merge
`425362a44c4b8224cb43b33f0f7f65458c4ee193`; latest recorded Pulse measurement is a different build,
`0.0.4@fa8f188`. See [performance](docs/PERFORMANCE.md).

This roadmap includes **new capabilities, corrective work, known limitations,
operational hardening and developer experience**. It replaces the execution
authority of the former evolution/agent/performance/round plans. It does not
reopen completed work, authorize production data changes or imply release approval.

## Index

- [Rules and status vocabulary](#rules-and-status-vocabulary)
- [Current delivery boundary](#current-delivery-boundary)
- [Approved four-item follow-up](#approved-four-item-follow-up)
- [Proposed next round after the 0.0.5 checkpoint](#proposed-next-round-after-the-005-checkpoint)
- [Next proposed round after N1–N4 closure](#next-proposed-round-after-n1n4-closure)
- [Known limitations and corrective work](#known-limitations-and-corrective-work)
- [Remaining performance work](#remaining-performance-work)
- [Next iteration assessment: feature/v0.0.5](#next-iteration-assessment-featurev005)
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

## Current delivery boundary

| ID | Status | Deliverable / exit condition |
| --- | --- | --- |
| REL-004 | Published September 8, 2026 | [PR #2](https://github.com/OktoLabsAI/okto-grafx/pull/2) merged; tag `v0.0.4` points to `425362a`. Wheel/sdist built from the tag, Twine validation, 149 package-file parity checks, isolated consumer smoke and public PyPI install smoke passed; remote SHA-256 values matched. [PyPI 0.0.4](https://pypi.org/project/okto-grafx/0.0.4/). GitHub Actions could not start because of account billing; local evidence does not certify the remote/platform matrix. |
| DOC-1 | Implemented in this documentation refactor | One README entry point, public API/configuration/query/operations references, measured-performance table, one roadmap and a preserved source archive. Links, examples, API/config field coverage and preservation hashes are checked; [validation receipt](docs/reports/DOCUMENTATION_REFACTOR_2026_09_08.md). |
| CAP-1B | Implemented checkpoint | `6b5163e`: native journal preflight is connected to page application/checkpoint; UUID, activation/COMMIT coverage, target/resident LSN and file extents validated. 2,433 tests / 77.27 s recorded; no new typing diagnostics in the isolated comparison. This is recovery correctness, not a latency benchmark. |
| GX-CAP-1 remainder | Implemented and locally validated in 0.0.5 development | Metadata-at-begin/retry, qualified lookup/paging, full journal verification, metrics and coordinated transfer/restore/fork identity semantics. [Consumer contract](docs/COMMIT_HISTORY.md), [acceptance evidence and limits](docs/reports/V005_N3_N4_ACCEPTANCE.md). Opt-in activation is not a production rollout or release. |
| PULSE-BENCH | Reserved workload | Latest recorded authorized run consumed one spec; 19 remain reserved. Do not consolidate more, redrive, rebuild or reset data to improve this document. New live runs need a deliberate workload decision. |

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

Proposed September 8, 2026 when committing the completed checkpoint. These four
items are **not yet selected for implementation** and do not reopen N1–N4 or the
earlier six-plus-four deliveries. No new branch/version, production run or release
is implied. Gains below are hypotheses, not measured speedup promises.

| Order / existing IDs | Bounded next delivery | Effort / expected benefit | Precedence and stop condition |
| --- | --- | --- | --- |
| R1 / PERF-COLD, PERF-WRITE | Reduce residual cold participant admission and first-reader checkpoint work in the existing complete synthetic Pulse consolidation. Profile the remaining repeated index/catalog/page work once, then implement the dominant safe reduction. | Medium to large / highest evidenced remaining cold-load and reconciliation latency opportunity; percentage unknown | N1 removed duplicate existing-index admission, not the checkpoint. Preserve independent readers, fresh identity proofs, all checkpoint phases and end-to-end measurement. If residual work is necessary or savings marginal, report that and stop; no authority bundle or warmup relocation |
| R2 / OPS-7 | Make checksum-provider selection isolated per database/composition rather than process-global, preserving the current pure/native/auto choices and identical checksum bytes. | Medium / stronger embedding isolation and predictable accelerator use; no guaranteed latency gain | Independent localized hardening. Two handles with different settings must not change each other's provider; cover custom ports, explicit provider absence, concurrent use and wheel consumption. Do not change the disk format or weaken checksums |
| R3 / OPS-3, PERF-MEM | Reuse the overflow pages already retired as FREE by N3 so eligible space can serve subsequent overflow allocations. Start with the existing quiescent maintenance contract and an explicit persisted reuse protocol. | Large / potentially high reduction in growth under update/delete churn; not a file-shrinking or immediate UI-speed claim | Depends on N3, now delivered. Require ownership, reclaimed-snapshot floor, stale-reference/ABA, WAL/crash/reopen and allocation-quota proofs. No online vacuum, truncation or immutable-index orphan cleanup bundled in this slice |
| R4 / OPS-1 | Deliver a consistent physical backup and restore into a new directory, with a checked manifest, identity/provenance preservation, interruption handling and verify/reopen before success. | Large / high operational recoverability value; no throughput claim | Uses completed CAP-1 identity semantics; does not require R3. Prove the snapshot/WAL retention boundary with concurrent writers and refuse unsafe/incomplete promotion. No logical export engine, automatic repair of authoritative corruption or independently writable same-UUID fork |

Recommendation: approve R1–R2 first and take one checkpoint before starting the
persisted lifecycle changes in R3–R4. Use existing isolated fixtures, focused
semantic tests and grouped regressions; do not consume the 19 reserved specs.
FTS (GX-CAP-5), general logical export/import (OPS-2), larger hash directories and
immutable-index orphan reclamation remain visible below but are not additional
requirements for this proposed four-item round.

## Known limitations and corrective work

| ID / legacy mapping | Status and impact | Required work / acceptance |
| --- | --- | --- |
| OPS-1 / §8.1 | Planned: no generic public consistent hot-backup/restore API | Snapshot/file inventory, checksums, reader-horizon retention, interrupt-safe copy, restore into a new directory, identity/provenance handling, verify/reopen and crash tests. Never copy/replace live files as an alleged hot backup. |
| OPS-2 / §8.2 | Partial: physical scan primitives and consumer-owned logical transfer exist; no general versioned export/import product | Versioned schema/types/nodes/parallel edges/vectors, resumability, compatibility and identity remapping; reject unsupported formats before partial promotion. |
| OPS-3 / P1.4, §8.3 | Partial: manual foreground vacuum with overflow retirement | Eligible exclusively owned overflow pages can now be retired through WAL. Current vacuum requires real operator-asserted quiescence; automatic reuse, truncation/file-space reduction and online operation remain unimplemented. Any online extension needs reader/lifetime proofs and crash/reopen tests. |
| OPS-4 / P1.8 | Open limitation: buffer and query budgets are not process RSS caps | Account/measure all retained engine state, vector memory, concurrent handles and temporary encodings. Preserve deterministic refusal; publish a realistic peak-memory envelope, not an RSS promise derived from nominal page bytes. |
| OPS-5 / P1.12 | Partial: explicit growth/rebuild exists | Hash directories cap at 4,096 buckets; skew/overflow and retained immutable orphan generations remain. Define bounded reclamation and larger-scale indexing without in-place generation replacement. |
| OPS-6 / P1.11 | Open limitation: physical conflicts and exclusive publication | Remove only proven redundant publication/page work. Disjoint logical rows may still conflict; do not promise linear writer scaling or replace multiwriter with an application-wide single-writer premise. |
| OPS-7 / P2.6, P2.9 | Partial DX improvement in 0.0.5 | `connect(**options)` declares validated `Unpack[ConnectOptions]` keyword/static contracts; evidence above. Checksum selection is still process-global. No provider or byte semantics changed. |
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
| 3 / PERF-SCALE, OPS-5 | Avoid whole-graph work for selective lookups and bounded pages; inspect skew, relationship fan-out and index growth. Ordered cursor work is already implemented; hash directories still cap at 4,096 buckets and completeness fallbacks can remain O(N). | Medium to large / high at larger N if an affected access path dominates | Fixed selective queries at three declared sizes, with operation/page counts and identical semantics. Identify the actual scan/overflow path before choosing an index change. Full graph enumeration and verification remain proportional to data; no universal O(1) promise or removal of corruption detection. |
| 4 / PERF-MEM, OPS-3/4/5 | Reduce retained state and growth under updates/deletes: multiple handles, immutable index generations, overflow history. N3 retires eligible overflow pages, but automatic page reuse and file shrinking remain absent. [Current maintenance limits](docs/OPERATIONS.md#maintenance-backup-and-upgrades). | Medium diagnosis; large reclamation work / high long-lived-store value | A bounded churn workload with peak RSS, disk growth and a pinned reader. Separate cache/accounting fixes from physical reclamation. Reclamation requires snapshot/lifetime and crash proofs; it is not a quick online-vacuum toggle. |
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
| 4 / Retained memory | Closed cache slice and lifetime experiment: text/byte admission and owner release; 12 update/delete/overflow cycles with 1/2/4 handles plus pinned reader, measured RSS, exact old/current answers and clean reopen | Historical experiment predates N3 overflow retirement. Heap growth remains real because retired pages are not automatically reused or truncated. Physical allocation/index history remains OPS-3/4/5 |
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
| Operational evolution / OPS-1, OPS-2 | Consistent backup/restore into a new directory and versioned logical export/import. | Large / high recoverability and integration value. No generic hot backup API exists today. Identity/retention and provenance must align with CAP-1; copying live files is not a shortcut. Finish one usable contract before combining both products. |
| Next search capability / GX-CAP-5 | Native FTS with transactional postings, explicit analyzers/versioning, BM25 and typed bounded search. | Large / high new retrieval value, not an optimization of every graph query. Its direct prerequisites are existing index lifecycle/budgets/planner and CAP-0; it is not technically blocked on all temporal milestones. Shared format/WAL integration still follows controlled sequencing. [FTS spec](docs/specs/SPEC-GX-CAP-5.md). |
| After commit provenance / GX-CAP-2, GX-CAP-3 | Attached catalog sessions and opt-in system-time history. | Large each / strategic multi-store/temporal value. CAP-1 first; one-store writes remain the rule. Neither is required to fix current KG loading. |
| Later dependent capabilities / GX-CAP-4/6/7/8/9/10, AGENT | Bitemporal/hybrid retrieval, extension SPI, Arrow/exchange, graph algorithms, schema evolution and optional agent packages. | Medium to large individual slices / valuable but not all immediate. Respect declared dependencies: hybrid needs FTS, Arrow/external scans currently depend on CAP-7 and existing streaming/bulk. Do not advertise them as small already-enabled follow-ups. |

The selected performance/DX checkpoint and CAP-1 N4 scope are now delivered. Backup
and FTS remain the strongest subsequent product candidates, not automatic scope
for this branch. Existing multi-read/write, durability, consistency and fail-closed
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
