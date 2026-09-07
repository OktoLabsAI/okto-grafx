# Pulse cognitive consolidation — live write audit, 2026-09-07

## Outcome and scope

Board: `15877207-c147-4805-96d7-d53a625571df` (Okto Pulse).
Pulse 0.3.3 runs from the Community/Core worktrees with Grafx 0.0.4 sources;
this is not a measurement of the published Grafx 0.0.3 wheel. Pulse PID 44652.
No configuration, durability, reader/writer contract or graph generation was changed.

The requested consolidation is **partially completed, not closed**. The baseline
cognitive ledger contained 22 pending and 18 consolidated items. It now contains
21 pending and 19 consolidated. No pending item was skipped to hide a technical
failure. One successful cognitive session added 4 Alternative nodes and 8 edges
(4 explicit Decision-to-Alternative judgement links plus automatic provenance).

The batch was held after its first successful board commit generated another
terminal Global Discovery delivery failure. Continuing would accumulate known
broken downstream deliveries. The board commit itself was acknowledged and its
data was read back; it must not be repeated as though it had failed.

## Protocol and receipts

Read Pulse's MCP preflight, KG workflow and consolidation tool reference, the
board guidelines and the complete source context for the attempted specs.
Queried before proposing new Decisions; used similarity and reconciliation;
never authored deterministic Criterion/Constraint/Entity/belongs_to candidates.

1. `cbd4eedd-a16b-51ae-93b6-5f850467fb4d`, Community persistence/API/whole-Spec export:
   session `kgses_9791c21072d2407b`. Ten proposed nodes and five judgement edges.
   Commit refused with `kg_node_connectivity_violation` / `unresolved_source_ref`
   **before graph mutation**. Explicitly aborted; no compensating delete.
2. `5daee82f-5758-5f66-b3c9-e75144cb88ff`, Analytics 6/6 Board KG health/effectiveness:
   session `kgses_0e4a43a5dfc540a1`, committed at
   `2026-09-07T11:29:31.845174Z`. Existing canonical Decisions were preserved.
   Their recorded alternatives were consolidated with explicit rejected-option
   context and source references, rather than duplicated as new Decisions.
   Ledger item `cogn_a69ec28b2ec5a2544bf2f830afaf30aa` updated using
   `outcome_type=relation_created` and four concrete KG evidence references.

Canonical node readback returned all four new nodes. Physical relationship-table
readback returned all four Decision-to-Alternative pairs. Natural-language
retrieval also returned the new rejected debt-KPI alternative. A logical-edge
query exposed an independent integration defect, described below.

## Measurements — wall time is not physical commit time

| Operation | Measured result |
|---|---:|
| Initial KG health MCP call | 7.336 s; bounded probes initially unavailable |
| Cognitive inventory MCP call | 2.059 s |
| First commit, rejected before mutation | 40.373 s |
| Successful commit, 4 new nodes / 8 edges | 26.960 s |
| Canonical new-node query, executor-reported duration | 20.7 ms |
| Physical judgement-edge query, executor-reported duration | 290.0 ms |
| Same edge query, MCP wall time | 2.607 s |
| Post-commit health MCP call | 2.481 s; board healthy, metrics available |

Commit wall times include MCP, admission/health, provenance checks, graph work,
relational audit/outbox and finalization. They exclude this agent's reasoning and
candidate preparation. **26.960 s / 4 is not a Grafx insert benchmark.** The sample
is one successful live transaction and one rejected attempt, not a p95, sustained
throughput estimate, or controlled before/after comparison. No monetary API/token
cost was exposed by Pulse; no dollar cost is inferred.

Two 60-second py-spy captures sampled active threads at 50 Hz, without locals:

- `../.grafx-tmp/cognitive-first-commit-20260907.json`: 3,313 samples, zero errors.
- `../.grafx-tmp/cognitive-eligible-commit-20260907.json`: 2,317 samples, zero errors.
- `../.grafx-tmp/cognitive-run-evidence-20260907.json`: receipts and call timings.

These local evidence files are intentionally ignored, not release artifacts.
The successful capture contained 1,115 samples in health-probe worker stacks,
616 in artifact-health snapshot construction, 521 in cognitive-source
fingerprinting, 162 in Grafx checkpoint stacks and 121 in `_do_graph_commit`.
Counts are inclusive/overlapping, span the whole capture and multiple threads,
and exclude idle waits. They **cannot** be added or converted into a wall-time
breakdown of the request. Profiling itself also adds some overhead.

## Blocking and correctness findings

### 1. Seven pending specs lack a graph provenance root

A complete all-layer inventory returned 898 Entity and 151 Decision rows without
truncation. Seven of the 22 pending specs had neither their `spec:<id>` Entity nor
their spec-scoped Decision rows. The other 15 had canonical roots and Decisions.

Missing-root specs:

- `cbd4eedd-a16b-51ae-93b6-5f850467fb4d`
- `c2e4996a-dcfd-5e92-9025-313d9afb27cb`
- `9944bc62-8e0c-5f16-97ad-cba2f7ae2081`
- `b4df3d30-0f4b-5ff7-a307-0995fb15e7f4`
- `eed9e13f-563a-5dac-bca1-7d86da0db074`
- `d9381a62-0912-5a40-bcc2-5bd798561447`
- `715791a8-9c76-5fb5-a5e4-a547b202736e`

`core/kg/primitives.py:2055` resolves provenance against Entity/Bug roots;
`core/kg/connectivity_guard.py` requires Decision provenance plus judgement.
The refusal is correct for the observed missing materialization. It is not
evidence of corrupt Grafx bytes, and must not be bypassed with fabricated roots,
an unrelated parent, or a `final_report` identity. Restore the deterministic
projection through its governed owner before these cognitive closeouts.

### 2. Global Discovery delivery is still broken

Board-scoped Global outbox DLQ count increased from 224 to 225 after the successful
session. New row `c9fe69fd-45d6-4976-816d-7b5964e1ac6d`, event
`evt_4b636627858d434a`, created `2026-09-07T11:29:32.267213`, reports:

`graph_capability_unavailable:global_statement_write failed in Okto Grafx (configuration_error)`.

The same bounded error exists in older delivery rows. This is confirmed delivery
failure, not an unproven suspicion about speed. The available envelope does not
identify the precise invalid native configuration. Inspect the preserved cause
at `community/adapters/grafx_global_discovery.py:934` (summary transaction commit)
and its error mapping before selecting a fix. Do not relax fences/validation.
No manual redrive was performed in this audit.

### 3. Logical relationship query and scoring compatibility gaps

`MATCH (d:Decision)-[r:relates_to]->(a:Alternative) ...` fails with `plan_error`.
The identical query using `relates_to__Decision__Alternative` returns the four
new links. This distinguishes a logical/physical schema gap from missing data.
`community/adapters/grafx_cypher_executor.py:118` normalizes/bounds the query but
does not translate those logical names. Physical names are a diagnostic only,
not an acceptable product-facing requirement or a reason to specialize Core.

Application output also contains repeated `kg.scoring.fetch_failed` with
`parse_error`, including existing Decision nodes. The query originates at
`core/kg/scoring.py:477`: chained OPTIONAL MATCH/WITH and aggregation, including
the logical `contradicts` relation. `_fetch_node_inputs` catches the failure and
returns None. Thus successful consolidation alone does not certify refreshed
relevance scores. The exact unsupported grammar still requires a targeted
reproduction; the source query is the evidence, not a guessed parser cause.

## Performance opportunities, ordered by precedence

1. **Correct the delivery/query/scoring gaps first.** Avoid repeated known failures
   and reprocessing overhead; verify board commit plus Global delivery before
   resuming the remaining 21 closeouts (14 currently have roots, 7 do not).
2. **Separate write admission from expensive diagnostic enumeration.** Every
   consolidation resolves health before graph IO (`core/kg/primitives.py:4575`).
   The capture shows repeated source enumeration/fingerprinting around health
   (`community/adapters/sqlalchemy_kg_cognitive_source.py:573`, `_revision_record`
   at 115; `core/ports/kg_cognitive_source.py`). Investigate bounded single-flight
   diagnostic snapshots and incremental source fingerprints, preserving fresh
   quarantine/recovery/authority checks. Do not cache a stale permissive decision.
3. **Batch provenance and endpoint reads inside one snapshot.** Root resolution
   loops over references/types (`primitives.py:2055`); existing-node and endpoint
   lookups (`4793`, `4826`) recur across candidates and score recomputation.
   Deduplicate by source root/node within the operation and use bounded batch
   ports, not cross-transaction authority caches. Measure actual query counts
   before promising a gain.
4. **Inspect checkpoint/index work on the real write path.** Samples reach
   Grafx `_checkpoint_in_section`, `_redo_onto_device`, index confirmation and
   version validation. Determine whether the relevant work is incremental with
   touched data or proportional to historical state. This capture identifies
   candidates, not proof that checkpoint dominates the 26.96 seconds.
5. **Expose an attribution chain for the next run.** Measure admission, source
   resolution, embedding/reconciliation, graph staging, native OCC/materialize/
   WAL/index/publish, checkpoint, relational finalization and outbox separately.
   Grafx already defines `oktografx_commit_phase_duration_seconds` in
   `engine/txn_manager.py:213`; compose per-handle telemetry in Community with
   bounded correlation, rather than adding Grafx dependencies to Core.

No optimization or weakening of multi-reader/multi-writer, WAL, OCC, snapshot or
durability semantics was implemented as part of this diagnostic run.

## Follow-up: blocker corrections and preserved benchmark corpus

The operator explicitly stopped further cognitive consolidation. The **21 pending
specs are reserved for subsequent controlled benchmarks**, not a backlog to drain
as part of this repair. No new cognitive session was opened in this follow-up.

### Corrections implemented

- Native Grafx preserves interleaved OPTIONAL MATCH/WITH reading order and uses
  its existing aggregate operator for WITH. Spill retains private node bindings
  so a carried node can anchor the next hop. Scope, budgets, snapshot and writer
  overlay tests cover the complete 11-field scoring projection.
- Community shares logical-to-physical relationship translation between its
  transaction and read-only executor. Translation is restricted to provable
  endpoint pairs; ambiguous scope remains refused. A singleton logical relation
  with incompatible endpoints correctly null-extends in native OPTIONAL MATCH.
- Physical-name path regression was caught during validation: the baseline's
  three original path tests passed, but the physical schema exposed a literal
  native `supersedes` restriction. The same one-hop path shape now resolves
  caller-defined labels/types through the catalog, preserving endpoint and
  reserved-metadata checks. This is native generic support, not Core hardcoding.
- Global visibility reconciliation now sends at most 512 source IDs per batch.
  Source IDs above Grafx's 1,024-element query-list limit reproduce the original
  `configuration_error` at `parameters.ids`; a consistent isolated copy of the
  Global database reproduced it without modifying the original database.
  An additional predicate avoids rewriting already-correct visibility values;
  NULL is still explicitly materialized. Batch failure propagates and prevents
  delivery acknowledgement. A repeated successful assignment returns zero
  mutations in the native integration test.
- A bounded, authorized deterministic projection repair endpoint was added to
  Pulse. It schedules exact source IDs through the normal worker and atomically
  audits admission; it does not alter the cognitive ledger or fabricate roots.
  Active claims, paused/rebuild work and deletion tombstones are preserved.
  See Community `docs/DETERMINISTIC_PROJECTION_REPAIR.md`.

### Live validation and remaining closure checks

The restarted Pulse returned all four logical Decision→Alternative links and the
complete scoring row for an existing Alternative (degrees 1/1, contradictory
confidence 0). Reported executor durations were 1,203.7 ms and 471 ms respectively;
MCP wall times 5,575 ms and 2,698 ms. These cold/read samples are not a before/after
performance comparison or a new write benchmark.

Only delivery `c9fe69fd-45d6-4976-816d-7b5964e1ac6d` was redriven. After clearing
the visibility step it exposed the same oversized-list limitation in
`delete_invalid_board_digest_links(expected_digest_ids)`. This must be corrected
without independently chunking NOT IN predicates, which would delete valid
links. Global delivery is **not yet certified complete** at this checkpoint.
The seven deterministic root repairs have not yet been admitted live.

The second fix now compares the **complete** expected-ID set against an
aggregated link inventory in one native write snapshot, validates that inventory
before staging any deletion, and deletes only proven-invalid positive IDs in
512-item batches with a single fenced commit. A failure in the second deletion
batch rolls back the first. Native inventory result/memory budgets remain
fail-closed rather than silently truncating the authority set.

Focused validation: 115 Community relationship/executor/transaction tests; 15
Core visibility/worker tests; native visibility integration (2,414 source IDs
and no-op retry); three native invalid-link cases (large expected set, rollback
across 513 links and preflight budget refusal); 13 deterministic repair/queue
tests. Native query runs additionally covered OPTIONAL/WITH aggregation,
spill/analysis/planner/parser and the generic one-hop path contract. Overlapping
test runs are not added together as a unique test total.

### Identity index advanced to unblock the real delivery validation

The corrected delivery triggers convergence of previously missing Global cache
digests. A read-only py-spy stack sample found
`upsert_grafx_decision_digest_vector`'s `by_source` check (`grafx_global_discovery.py`)
using a native node scan for `(board_id, original_node_id)` before each upsert.
Repeated full scans across a growing inventory are a candidate quadratic cost;
the stack alone is not a throughput measurement. Preserve the identity-collision
check and investigate a secondary/composite index or a bounded transactional
bulk identity-resolution capability. Do not replace it with unchecked stable-ID
derivation. Initially reserved for the next round, this bounded index installation
was advanced when real cache convergence remained in these scans after more than
ten minutes. No collision validation was removed.

Source review confirms the Global manifest has a primary key and vector space,
but no composite identity index. Grafx's existing exact composite index supports
the two STRING columns and its planner can select IndexSeek for full-key equality.
A low-effort candidate is an idempotently installed/fenced adapter-owned index,
keeping both by-source and by-primary-key collision checks in the same transaction.
Do not silently add LIMIT 1. The hash index has bounded bucket capacity; do not
promise unlimited constant-time lookup. The ordered index currently supports a
TIMESTAMP/STRING pair, not this STRING/STRING pair.

One paired measurement used the private 853-digest audit copy, five reads before
and five after the native composite index, with identical returned IDs. Before:
median **3,411.89 ms**, native `rows_scanned=853`. After: median **6.97 ms**, native
`rows_seeked=1`, about **489.8× faster for this specific identity lookup**. The
first indexed sample was 73.79 ms, followed by 9.43/6.92/6.97/6.94 ms. Initial
index creation/backfill took **57,483.75 ms**. This is not an end-to-end write
speedup, p95, production throughput estimate or a promise at arbitrary scale.
Script: local ignored `.grafx-tmp/benchmark_digest_identity.py`.

Community now installs `pulse_global_digest_source` idempotently after durable
schema creation, including on existing databases. Existing incompatible indexes
are refused, not overwritten. Pre-DDL and pre-commit fences remain mandatory;
the caller must renew its writer lease during the one-time build.

The additional DDL also exposed a false certification requirement: HNSW and
generic vector watermarks can legitimately precede a global publication caused
by unrelated index DDL. Certification still requires clean native `verify(all)`,
matching vector/generic views and watermarks, non-stale state, watermark bounds
and the same published LSN before/after the entire probe. Fault tests reject
ahead/mismatched/stale watermarks, coverage findings and concurrent publication.
The 19-test index/global suite passed in 53 s. See Community
`docs/GRAFX_GLOBAL_DIGEST_SOURCE_INDEX.md`.

Proposed later synthetic benchmark sizes: 1k/5k/10k digests, insert/update/idempotent replay,
cold and warm; record examined rows/pages, native commit/WAL work and p50/p95.
Require actual IndexSeek selection plus duplicate identity, PK collision, fence,
rollback, reopen and multiwriter tests. Only then select a small real spec cohort
from the preserved corpus, leaving the remainder unchanged for later comparisons.

The seven done specs have no current queue, canonical-debt, consolidation-audit
or board-DLQ rows. The missing materialization is proven; the available history
does not prove its original cause. No graph reset or broad rebuild is justified.

### Exact digest-link probes avoid the Board hub

After installing the identity index, live stack sampling found the worker in
`link_board_digest` traversing Board's outgoing `CONTAINS_DECISION` adjacency and
resolving each digest before applying its exact ID filter. The equivalent
preflight now begins at the digest PK and traverses incoming links. The matching
normalization count/delete queries use the same direction; duplicate detection,
ownership guards and existing write fences remain unchanged.

A focused native fixture with 32 digests, a duplicate and a foreign-board link
returned the same count of two: old traversal examined 33 edges, new traversal
examined three incoming edges and sought one digest. Normalization removed three
target links, recreated the correct one, preserved another digest and refused
foreign ownership. This test plus the existing 19-method runtime test passed
(two tests, 18.54 s). This is an examined-work reduction, not an end-to-end timing.

At the subsequent checkpoint the cognitive ledger still had 21 pending specs;
SHA256 remained `4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.
The single redriven Global delivery remained unacknowledged, with no new recorded
error, while its normal worker converged missing cache digests. Health probes
timed out under this work; no deterministic repair admission was bypassed.

### Live targeted repair admitted, cognitive benchmarks preserved

After the query-direction change, Pulse PID 9448 returned board and discovery
`healthy`, metrics `available`; overall `at_risk` remained due to preexisting
policy-projection DLQ. The authenticated REST request admitted exactly the seven
listed specs in 1,352.8 ms, HTTP 202, correlation
`665ff221-7d3f-4a76-8d76-a3401daa7db2`, `queued_count=7`,
`cognitive_consolidation_started=false`. One row became claimed and six pending.
This is a scheduling receipt, not seven completed graph writes. The cognitive
ledger hash was unchanged after admission. No new cognitive session was created.

Operational caveat: graceful Ctrl+C stops of PIDs 22788 and 46652 exceeded the
worker's ten-second native drain budget. The app explicitly skipped graph/SQLite
close rather than closing handles underneath native work. No force-kill or
data-file removal was used. Subsequent native reopen and healthy live read probes
succeeded; this is not a claim of a clean prior shutdown or a full corruption audit.

Live repair exposed a queue-selection blocker, not a reason to bypass provenance.
The seven selected specs contain the internal prerequisite chain
`d9381a62 -> eed9e13f -> b4df3d30` (full UUIDs listed above); the other four have
no prerequisites. Identical admission timestamps plus UUID ordering put the
dependent first. `_select_board_aware_entries` reserved its board before testing
backoff, so its typed `relational_projection_endpoint_pending` prevented the
prerequisites from ever being claimed, even though the deferral contract promises
to yield. Read-only queue observations confirmed repeated deferrals of the same
dependent and no claims of its prerequisites. Ordinary failure/rebuild barriers
must remain protected when fixing this specific scheduling defect.

The selector correction passed 15 focused tests (5.42 s), including retries
already overdue by two minutes, prerequisite-chain progress and ordinary/rebuild
barriers. It only reorders typed prerequisite waits; missing/cyclic dependencies
are not fabricated or automatically expanded into additional work.

### Cold-header certification refusal isolated and corrected

The live delivery subsequently reached post-flush native verification but was
refused (`retry_count=1`, no ACK). After Pulse stopped, a quiescent private copy
of Global (`heap.dat` 102,473,728 bytes, publication LSN 140805) reproduced
`index_status_mismatch` for `board_summary_idx` in 77.46 s. Native `verify(all)`
had no findings. The actual mismatch was `built_through_lsn=None` in both cheap
public views: their contract deliberately omits a cold, nonresident header.
This was an adapter certification defect, not proof of corrupt vector data.

Grafx now exposes `read_index_status(name)` as an explicit durable-header read,
reusing the catalog/physical-generation validation protocol and returning an
immutable DTO. It neither rebuilds nor changes watermarks. The adapter requires
that header's identity, non-stale state and valid integer watermark; any resident
observations must agree. Full coverage verification and publication fencing
remain required. Focused runs passed 91 native facade/output tests and 24
Community index/certification tests. Live terminal verification remains pending
until the corrected process finishes its normal delivery.

The corrected certifier passed on the same private copy in 80.23 s, returning
valid watermarks for all four spaces at unchanged publication 140805. This proves
the cold-header refusal was removed, not that verification became faster.

Additional bounded follow-ups for the next performance round (not new gates for
this repair): reject known missing prerequisites before expensive candidate
embedding/similarity work; measure repeated per-candidate exact vector scans;
measure whole-board inventory convergence and full post-flush verification
separately from native commit/WAL latency. Any future reduction in verification
cost needs equivalent coverage/freshness evidence, not omission of checks.
Concurrent deterministic repair legitimately changed the source inventory of a
Global attempt (`added=157, removed=5`), which was refused rather than ACKed from
an obsolete snapshot. Measure bounded retry/coalescing behavior under sustained
writers before choosing a change to that protocol.

### Seven deterministic repairs completed; all cognitive benchmarks retained

All seven exact queue rows drained with commit receipts, and authenticated Pulse
REST readback returned exactly seven roots, all `graph_layer=canonical`, without
truncation (852.3 ms executor-reported time). This is deterministic source repair,
not cognitive closeout. Cognitive REST returned pending=21, in_progress=0,
consolidated=19, skipped=0, failed=0, total=40. The ledger SHA256 still exactly
matches the baseline recorded above.

| Spec prefix | Deterministic session receipt | Added nodes | Updated nodes | Added edges | Session-to-commit seconds |
|---|---|---:|---:|---:|---:|
| cbd4eedd | kgses_44f2d5f29e4c4f3c | 110 | 8 | 149 | 120.295 |
| 715791a8 | kgses_303c65728d5b4dbd | 42 | 2 | 78 | 61.765 |
| d9381a62 | kgses_296bfca03bee480b | 61 | 7 | 77 | 59.967 |
| 9944bc62 | kgses_0334dc6c00d84ce0 | 90 | 7 | 124 | 76.349 |
| eed9e13f | kgses_daf59797537c4b8d | 65 | 6 | 104 | 64.360 |
| c2e4996a | kgses_8f96f6ec208145b0 | 78 | 6 | 104 | 81.438 |
| b4df3d30 | kgses_946623d06b214c7d | 87 | 6 | 114 | 111.624 |

Times come from persisted `consolidation_audit.started_at/committed_at`; they
include session work, similarity/provenance/scoring and commit coordination, not
just native writes. They are heterogeneous workloads with concurrent Global
convergence, not a controlled speedup comparison or p95. No dollar/token cost
is inferred. Receipts cover 533 node creations, 42 update operations and 750 edge
additions; repeated updates are not a count of distinct nodes.

At this checkpoint the original Global delivery still awaits terminal ACK after
the source-inventory change. Existing unrelated DLQ/debt was not redriven or
removed. No further cognitive specs are authorized for consumption as part of
this repair.

The next delivery failure was also traced to concurrent source evolution, not
an absent heap row: `constraint_e90f96516f68e60bec35b074` still existed with an
embedding, but REST readback showed `revocation_reason='superseded by consolidation
session'` and `superseded_by='constraint_ae4a75ad6384156b8794ad47'`. Both source
inventory and payload materialization correctly exclude revoked/superseded rows;
the long-running attempt had enumerated it before the repair superseded it.
Keep the retry/consistency guard; do not republish the revoked source to force ACK.

### Short live delivery profile after deterministic repair (11:09 local)

Work continues without Claude. No further cognitive admission was made. The
ledger SHA256 remains exactly the baseline above. Read-only SQLite inspection
found seven nonterminal Global deliveries for the same board; the original
delivery is still at retry 4 and awaits the batch's final durable verification.
This is not yet an ACK or a completed recovery claim.

A 10-second, 20 Hz stack-only `py-spy` sample of PID 13884 collected 205 samples
with zero sampling errors while the worker advanced through digest upsert/link
work. Artifact: `.grafx-tmp/global-delivery-live-20260907.speedscope.json` (local
diagnostic, not a reproducible benchmark fixture). Inclusive stack counts were
121/205 in `link_board_digest`, 71/205 in `upsert_decision_digest`, 113/205 in
`_stable_view`, and 48/205 in `commit`; these overlap and must not be added.
The most frequent leaf was `_decode_vector_mode` (25/205). This short mixed
sample does not measure isolated WAL/fsync latency or establish an end-to-end
speedup. It indicates that identity/traversal/decoding work remains material
even after the exact lookup improvements, rather than proving that physical
commit alone accounts for perceived write slowness.

The processor also applies each event before one final per-board batch
verification (`global_outbox.py::_process_once_under_writer` and
`_verify_processed_batch`). Measure repeated same-board reconciliation as part
of the already recorded convergence-cost follow-up; do not skip per-event
semantics or freshness checks merely to ACK this batch sooner.

### Terminal checkpoint — 2026-09-07 14:09:24 UTC (11:09:24 local)

The original delivery `evt_4b636627858d434a` received durable `processed_at`
`2026-09-07 14:09:24.331384`, retaining historical retry_count=4 and clearing
`last_error`. All seven remaining Global events received ACK in the same batch;
read-only SQLite now reports zero nonterminal Global deliveries. The worker's
mandatory post-flush verification precedes these ACKs; no direct ledger update,
retry reset, verification bypass or additional redrive was used.

Authenticated REST again confirms pending=21, in_progress=0, consolidated=19,
failed=0, skipped=0, total=40; the cognitive ledger SHA256 is unchanged. The seven
deterministic receipts and roots remain the only source repairs in this scope.

KG Health returned HTTP 200 in 920 ms with board/discovery `healthy`, board graph
`queryable`, zero active queue depth, seven safe writes `applied`, and no graph
or discovery recovery required. Overall `at_risk` is **not** claimed resolved:
the pre-existing board backlog still contains 224 Global DLQ rows, 439 policy
projection DLQ rows and one canonical debt. Health also reports stale-canonical
parity; some auxiliary snapshots were stale and refreshing on this request.
These unrelated diagnostics were not silently purged or redriven. This checkpoint
closes the selected deterministic repair and its downstream delivery, not every
historical board issue or the entire 0.0.4 performance plan.
