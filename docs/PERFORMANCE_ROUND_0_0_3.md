# Performance round 0.0.3 — Knowledge Graph load path

This is the execution record for the 0.0.3 performance round. Its first bounded target is the
user-visible loading of the Okto Pulse 0.3.3 Knowledge Graph. It records measured facts before
selecting implementation work; it is not a collection of speculative engine targets.

## Non-negotiable invariants

The round may change internal algorithms and the Pulse/Grafx integration substantially, but it
must preserve all of the following:

- concurrent independent readers and writers;
- snapshot isolation and both OCC validations;
- WAL ordering, recovery and acknowledged durability;
- catalog, heap and index consistency, including fail-closed corruption checks;
- the public query result and pagination semantics consumed by Pulse.

No performance number by itself authorizes weakening one of these properties.

## KG-LOAD-0 — measured Pulse baseline

Status: **diagnosed; first implementation batch integrated**.

The active runtime was Okto Pulse Community 0.3.3 with the board graph bound to Grafx generation
1 at:

`C:\Users\jpamb\.okto-pulse\boards\15877207-c147-4805-96d7-d53a625571df\grafx\generation-1`

The browser and the installed Pulse sources establish the actual initial-load path:

1. the frontend starts the graph, health, historical-progress and statistics requests in one
   `Promise.all` and does not publish the graph until all of them finish;
2. the graph endpoint obtains up to 500 nodes, then executes one read-only relationship query for
   each of 70 physical relationship-table variants;
3. each relationship query is a separate Grafx autocommit read transaction, returns up to 5,000
   rows and is filtered against the current page's node IDs in Python;
4. the statistics endpoint separately obtains nodes and issues counts by node and relationship
   type;
5. permission-state changes during the observed mount caused the frontend effect to run twice,
   producing two graph requests and two statistics requests for one navigation.

### Reproducible observations

| Operation | Observed result |
|---|---:|
| Graph endpoint, 4 sequential runs, limit 500 | 5.894–6.947 s |
| Graph endpoint, limit 100 | 6.144 s |
| Graph endpoint, limit 500 in the same comparison | 6.166 s |
| Statistics endpoint | 12.010 s |
| Graph payload at limit 500 | 500 nodes, 777 edges, 70/70 tables successful |
| Browser initial navigation | still loading at 31.8 s; complete before 59.6 s |

The near-identical 100-node and 500-node times falsify WebGL rendering and page cardinality as the
primary cause of the endpoint delay. The fixed fan-out over relationship tables dominates this
sample. The browser delay is then amplified by the statistics request and by the duplicated mount
round.

### Direct engine isolation

A second read-only Grafx handle opened the live generation under the supported multi-reader
protocol with `descriptor_revalidation="generation"`. The same 70 statements were then measured
without HTTP, authorization or Pulse value conversion:

| Shape | Time | Interpretation |
|---|---:|---|
| 70 ordinary autocommit traversals | 4.478 s | current Pulse shape |
| 70 ordinary traversals in one fixed snapshot | 3.872 s | transaction setup is only part of the cost |
| 70 physical `scan_rows_v1` calls in one snapshot | 0.166 s | storage scan itself is not the dominant cost |
| 70 edge-first `RelationshipScan` plans, autocommit | 1.864 s | same endpoint-visible logical rows |
| 70 edge-first plans in one snapshot | 1.335 s | candidate composition of the two bounded gains |

All four logical variants returned the same 3,004 relationship rows before Pulse's page filter.
The physical scan is diagnostic only: it deliberately does not validate that endpoints remain
visible and therefore is not a semantic replacement for the query.

`cProfile` on the ordinary one-snapshot form attributed 9.42 of 11.62 instrumented seconds to
`TraverseRelationship`, 4.94 s to heap scans and 4.84 s to repeated logical node scans. It
constructed 15,528 source-node rows while only 3,004 relationship rows existed. The current
planner selects the already-correct edge-first `RelationshipScan` only when an unseekable typed
one-hop has a relationship-only predicate. A no-predicate hop and a hop whose predicate refers to
an endpoint fall back to `NodeScan + TraverseRelationship`, even though `RelationshipScan` already
resolves and validates both visible endpoints before emitting a row. Generalizing that selection,
while leaving endpoint predicates as a filter above the endpoint-validating scan, is therefore the
first engine candidate backed by a direct Amdahl decomposition.

### Candidate work, not yet promoted

The implementation order will be finalized with Claude's new performance review and direct
profiles of the Grafx execution path. The bounded candidates are:

1. eliminate the duplicate initial request round and let Pulse publish graph data independently
   of health/statistics, reducing perceived latency without changing database semantics;
2. replace 70 independent autocommit snapshots with a bounded same-snapshot graph-projection
   operation, so relationship reads pay transaction/catalog/plan setup once while becoming more
   internally consistent;
3. push the current-page endpoint restriction into the engine or a structured scan so unrelated
   relationship rows are not materialized only to be discarded by Pulse;
4. make statistics use one catalog/snapshot pass or cached revision-scoped aggregates rather than
   repeated type counts;
5. optimize any remaining engine-local scan, planning, decode or publication hotspot only after
   the direct profile shows material contribution.

The candidates above do not relax multiwriter/multireader admission, WAL, OCC, recovery or data
validation. Any implementation that cannot retain those properties is rejected rather than
treated as an acceptable latency tradeoff.

## Joint Claude/Codex selection — 2026-09-06

Status: **closed and finite**. Claude's 15-agent survey was checked against the current code and
the direct KG-load profile above. The survey's Amdahl denominator is a Pulse logical transfer of
86.81 s at `acf63c8`; the main remaining fractions are DDL execution (20.8%), relationship
execution (18.1%), checkpoint (12.7%), node commit (11.1%) and verification (6.7%). These numbers
are prioritisation evidence, not new pass/fail gates.

The implementation order is:

1. **KG load, first visible result:** select the existing endpoint-validating
   `RelationshipScan` for Pulse's directed typed single hop with an explicit relationship
   variable and without a predicate only when the consumer cannot benefit from the small-frontier
   path: aggregation, no `LIMIT`, or a literal `LIMIT` greater than the named frontier threshold
   of 64. Literal limits 64 and 65 are boundary tests; anonymous relationships retain the
   canonical traversal order, so adding a small limit remains a prefix of the same query.
   Endpoint predicates remain on the existing path in this change. Pulse will separately omit
   its tautological endpoint predicate, publish `/graph` independently of health/statistics and
   stop the duplicate mount request.
2. **Small storage/index removals:** E-1 removes the redundant `exists()` from
   `BufferPool.allocate` with a narrow `missing_file` match; E-2 deduplicates checkpoint barrier
   file names before proving presence; E-4 asks `page_count` before the missing-file creation
   path in `grow_to`; R-2 removes the value-keyed definition-match LRU whose hash is slower than
   the immutable comparison it caches. The exact-name proof is narrowed from per-page to
   per-allocation/replay loop, while descriptor identity and all corruption refusals remain.
3. **Medium engine batch:** E-3 admits HNSW stores to logical replay only through an explicit
   capability and a once-at-end invalidation/certification hook, with a cross-process generation
   replacement test; R-4 replaces serialize/deserialize DDL working-catalog clones with a
   structural clone of immutable definitions. This is owned by Claude in a separate worktree and
   was implemented by Claude in a separate worktree and accepted only after adversarial review,
   focused parity/replacement tests and local integration.
4. **Pulse projection:** execute the 70 relationship reads in one fixed read snapshot, push the
   current-page endpoint restriction down instead of discarding unrelated rows in Python, group
   node statistics in one scan, and batch endpoint-map reads. A native incident-edge access path
   remains the scale fix if an `IN` predicate still scans all relationship rows.
5. **Second engine batch after focused remeasurement:** statement-identity authority memo (R-1),
   bulk page allocation with complete buffer-pool accounting (E-1 complete), HNSW/string decode
   residue (R-11), and the other small validated items R-6/R-10. P-1 sizing is measured only
   after E-1 because its reported DDL time overlaps that same redundant presence work.

Explicitly not selected in this round are D-1 (removing endpoint identity/canonical-reference
proofs), D-2/D-5 (weakening physical generation or exact-name revalidation), P-5 (reducing Pulse
verification scope), fewer commits, `executemany`, or any other proposal that changes durability,
snapshot/OCC, multiwriter admission or corruption detection. D-3/D-6/D-7 and large format changes
remain separate product/format decisions, not implicit consequences of the performance mandate.

### First-batch focused result

With the guarded planner change applied to the local 0.0.3 source, all 69 relationship tables
present in the active generation planned as `RelationshipScan` for the Pulse-sized literal
`LIMIT 5000`. Three direct runs returned the same 3,004 logical relationship rows; the 69
autocommit statements took 1.10–1.23 s total, versus 4.48 s for the prior node-first shape in the
baseline. This is a 3.6–4.1× reduction in that engine-local fanout, before the Pulse snapshot,
endpoint and frontend changes. Literal `LIMIT 64` remains node-first and `LIMIT 65` becomes
edge-first, while aggregate input remains edge-first even with `LIMIT 1`.

The companion Pulse branch `perf/v0.3.3-kg-load-grafx` now omits the tautological Code
Traceability endpoint predicates when the caller is authorized, executes the physical
relationship fanout in one immutable Grafx read snapshot, and retains table-by-table fallback
only when the batch fails so partial-failure diagnostics stay exact. A direct run of the actual
Pulse edge-projection helper against the active generation returned 810 page-incident edges from
70 relationship layouts with zero failures in 1.74–2.08 s. The historical `/graph` endpoint took
5.89–6.95 s, of which 4.48 s was the prior engine-local relationship fanout.

The frontend graph request is also independent from health, historical-progress and statistics:
nodes and edges become renderable as soon as `/graph` completes, permission settlement no longer
causes a second `/graph` request, explicit refresh still refreshes both projections, and stale
responses are fenced by per-request generations. This removes the 12.01 s statistics endpoint
from the critical rendering path without hiding or weakening its diagnostics.

The same Pulse branch now removes the 81-query statistics fan-out without making statistics a
cached or eventually consistent answer. The 11 node-type counts are grouped by `label(n)` in one
filtered scan, while the 70 physical relationship counts run in one immutable read snapshot.
On the active generation the two direct helpers took 0.662 s and 2.178 s respectively, returned
2,001 nodes and 3,004 relationships, and reported zero failed relationship tables. Providers
without the optional batch capability retain the scalar path; a rejected batch is retried per
table so the existing partial-failure diagnostics remain exact. The Pulse commits are `e2b6053`
for the critical rendering path and `880db68` for statistics batching.

The accepted Claude batch adds two further internal improvements:

- HNSW logical replay can compose WAL records only when the exact store explicitly declares the
  capability and has no published picture. Publication rechecks the page-0 generation identity,
  unknown stores and active pictures stay scalar, a mid-batch replacement is refused and the
  touched store is marked stale through the existing fail-closed path.
- DDL statements clone the working catalog structurally, sharing only frozen definitions and a
  previously validated serialized image. Mutation dictionaries remain independent in both
  directions; subclasses retain the defensive serialize/deserialize round trip. This removes
  the former O(tables x columns) encode/decode from every DDL statement.
- Statement-scoped index authority is retained by exact parsed-statement identity only while the
  catalog object, concrete manager and registry revision remain identical. A transaction with
  speculative DDL never uses the memo; parse-cache eviction removes the corresponding entry and
  the independent memo bound is 256. The component dropped from 48.2 to 5.4 microseconds per
  node statement and from 100.4 to 5.6 microseconds per relationship statement in Claude's
  alternating harness. This is an 8.9–18× local gain but only about 0.5% of the measured Pulse
  transfer, so it is recorded as a safe small removal rather than a headline end-to-end gain.

## KG-LOAD-1 — typed multi-key incident seek

Status: **implemented, adversarially reviewed and selected by measured cost on the Grafx and
Pulse working branches**.

The scale defect left by the first batch was precise: `RelationshipScan` made the current graph
page tolerable, but it still read every relationship row of every applicable physical layout.
Adding `IN` as a filter above that scan would remain `O(E)`. Issuing one scalar equality statement
per page node was also rejected after a direct reproduction: 5,276 statements took 8.459 s. A
private exact-index reproduction found the same 810 incident candidates with 5,556 scalar probes
in 1.407 s, identifying durable per-probe certification rather than edge materialization as the
next removable cost.

Claude implemented `IndexManager.validated_versions_many` in `1926fcd`. It canonicalizes and
validates every requested key before opening the view, probes repeated keys once, validates every
candidate against the heap and transaction snapshot, and returns only after one page-0
post-certificate for the whole index batch. A generation transition repeats the entire batch or
refuses; it never publishes a prefix. On disk, 300 primary-key lookups fell from 87.9 ms and 300
certificates to 27.3 ms and one certificate, a 3.2× component gain.

Grafx `bcfa395` adds the closed `RelationshipIncidentSeek` only for a directed typed one-hop whose
predicate is exactly `from.pk IN keys OR to.pk IN keys`, with a named relationship and a large/full
frontier. It resolves page keys through the node PK indexes, unions and deduplicates relationship
rows from the two automatic endpoint indexes, then resolves the opposite landings in one identity
index batch per endpoint table when that catalog-v2 capability is active. Missing/stale
capabilities and owner-dirty tables use the retained canonical plan before any accelerated read;
after all four endpoint stores are adopted, generation, read or corruption failures propagate
fail-closed. Parallel edges are preserved because deduplication is by physical `RecordRef`, not by
endpoint pair.

Pulse `523e759` is the consumer experiment: it supplies separate endpoint-type ID batches from the
node page it has already read and does not issue a physical-layout statement when neither endpoint
type occurs on that page. Untyped provider rows are conservatively included in both arms,
preserving the old result. The Python membership check remains a final provider boundary defence.

The naive first integration, which probed all 500 page IDs against every endpoint table, regressed
a diagnostic unordered page to 5.783 s cold and 4.188–4.744 s warm and was not retained. A second
measurement then used the exact Pulse node query, including its stable ordering and filters. That
node phase took 0.960 s and produced the same 500-node/777-edge page as the HTTP baseline. For that
actual page, the typed incident consumer queried 66 of 70 layouts, made 248 multi-key calls and
17,116 key probes, and measured 4.392 s on the first edge run and 2.677 s warm. The prior bounded
scan helper measured 1.74–2.08 s. Direct activation of `523e759` is therefore a measured **NO-GO at
the current cardinality**: the warm experiment is about 1.29× slower than the top of the prior
band, even though its relationship work no longer grows with unrelated edges.

The independent review `hof_aecbb8257cb443c198a50634d2d1af2d` compared 540 accelerated and
fallback executions containing 54,102 rows. It covered catalog v2, v1 without identity indexes,
self-loops, parallel edges, incoming syntax, types absent from the page, empty/null/duplicate and
wrong-typed keys, a foreign writer committing between snapshots, read-your-own-writes and stale
endpoint indexes. Every result multiset agreed. Mutation testing found two missing focused cases
(wrong-typed INT64 probes and v1 opposite landings outside the key frontier); both are now in the
repository tests.

`a726744` closes the current-cardinality regression with a deterministic cost choice before the
first durable index certificate. The relationship table's
`next_record_id - FIRST_RECORD_ID` is an O(1) durable upper bound on allocated edges. When that
upper bound is at most `0.5 * (distinct from keys + distinct to keys)`, the operator executes
`FilterRows(RelationshipScan)` edge-first; otherwise it executes the incident seek. The comparison
uses integer arithmetic. `page_count` is deliberately not consulted because it is a repairable
chain hint that may lag an interrupted append. Deletes and identity-reservation gaps can only make
the allocation bound too high and therefore conservatively select the seek; they cannot make a
large table look small. Focused tests prove the measured crossover (`12 edges / 84 keys` scans,
`60 / 84` seeks), no multi-key certificate on the scan branch and independence from a drifted
`page_count`.

The exact ordered Pulse page was then repeated against the live generation. It still returned
500 nodes and exactly 777 edges with zero failed layouts. The hybrid reduced the typed fanout from
248 multi-key calls / 17,116 probes to 54 / 4,355 (`-78.2%` calls, `-74.6%` probes). Node-PK work
fell from 7,844 to 2,082 probes. In two paired runs, the first hybrid pass measured 1.827–2.056 s
against 2.377–2.736 s for forced edge-first scan; subsequent passes measured 0.763–0.967 s against
2.273–2.724 s. This is direct provider/engine time rather than an HTTP claim, but it changes the
Pulse consumer from the earlier measured NO-GO to **GO on the candidate branches**. The remaining
1,582 repeated node-PK probes are the bounded next optimization (transaction-lexical memo B), not
a condition for recognising this completed gain.

Focused validation includes the multi-key primitive, incident operator, relationship-scan and
path suites plus 10 Pulse consumer tests and Ruff/diff checks. The Grafx set covers batch/scalar
snapshot parity, page-0 transition retry/refusal, hostile and wrong-typed keys, stale and proximity
refusal, primary/endpoint index near misses, v1 landings, incoming syntax, parallel relationships,
identity landing batching, owner read-your-writes, both cost branches and differential comparison
with the canonical fallback. No format, WAL, commit, OCC, lease, recovery, writer admission or
reader-snapshot code changed.

Two apparent follow-ups are deliberately not being smuggled into this wave. R-3 cannot skip the
commit-time tuple encoding solely because `intent.values` retained object identity: the direct
`TransactionContext.stage_row_insert/update` port does not schema-encode unless byte quota
accounting happens to be enabled. It needs an authenticated, rollback-bounded validation witness
and a fresh gain measurement before it is safe; the simple identity shortcut is rejected. For
KG endpoint pushdown, adding `IN $ids` above `RelationshipScan` would still scan O(E), while one
equality query per page node multiplies statement/index work. The scalable follow-up is a real
multi-key endpoint seek/incident-edge operator (or an equally explicit structured port), with
the same endpoint visibility and canonical-reference validation as ordinary traversal.

## KG-LOAD-2 — refreshed hot path and second implementation wave

Status: **finite joint selection; KG hot-path batch closed and transfer/storage batch in progress**.

Claude repeated the Amdahl transfer on `a726744` after the earlier optimizations, rather than
reusing the obsolete baseline. The clean median fell from 86.81 s to 58.49 s (`1.48x`). A
separate profile of the exact Pulse graph-page shapes found the remaining scan-branch time in
three bounded mechanisms: landing certification per edge (44%), re-encoding an already decoded
wide endpoint only to charge its optional memo (18%), and linear `IN $list` evaluation with two
`_freeze` operations per comparison and no early exit (17%). The node page separately spent 34%
of its profiled time decoding all 44 columns, including the vector, before returning its selected
properties. These fractions come from the profiled synthetic board; the live board remains the
end-user validation target and the percentages are not release gates.

The joint order was deliberately closed: (1) remove re-encoding from landing accounting using an
authenticated payload-length witness; (2) add bounded statement-local hashing only for detached
`str`/`bytes`/`None` parameter lists, while every mixed/numeric case retains the canonical walk
and its `1 = 1.0` semantics; (3) add exact same-type scalar equality and universal membership
early exit; and (4) reduce wide-row decode overhead without changing the stored format or
corruption oracle. Physical endpoint batching was investigated and then explicitly deferred by
the joint adversarial decision below. Row-independent expression folding and unbounded
relationship materialization are not part of this wave because their error-order and memory
semantics are not proved.

`8d806b0` completed the first item. `HeapVersion` now carries the `RecordHeader.payload_len` that
the heap already authenticated while decoding. Optional landing accounting consumes that exact
witness and falls back to canonical encoding only for synthetic or modified versions that do not
have it. Tests prove zero re-encodes for a disk row and prove that `dataclasses.replace` drops the
witness and therefore cannot undercharge changed values.

`a610b55` completed the first safe part of wide-row decode: a planned matching `STRING` body is
decoded directly in the tuple loop, avoiding one Python dispatch per ordinary graph string. The
length bounds, UTF-8 validation and `GrafxCorruptionDetected` details are the same as the canonical
body decoder; mismatched tags, nulls and compound values still use the existing oracle. The full
schema/value codec selection passed 197 tests.

After those changes, the exact live Pulse page still returned 500 nodes and 777 edges across 66
queried layouts with zero failures. The node query measured 0.858 s in that run. Alternating arms
measured the hybrid relationship phase at 1.036–1.103 s warm, versus 2.334–2.547 s for forced
edge-first scan. This is a compatibility/non-regression observation, not an isolated attribution
of the wall-time delta to the two new commits.

Claude's evaluator batch was accepted after adversarial rework and integrated as `216319e` plus
`d12602f`. The universal walk stops on its first match. Hashing is narrower: it runs only when the
RHS is a named parameter whose detached value is an exact tuple containing exact `str`, exact
`bytes` or `None`, and the LHS is exact `str` or `bytes`. Bindings, lists, maps, numbers, booleans,
`bytearray` and every mixed RHS retain the canonical walk. This preserves cross-type numeric
equality, including `1 = 1.0`, and three-valued null results. The statement-local total is capped
at 4,096 elements, records a declined build once per parameter name, and proves object identity
before reuse. It cannot outlive the query context or become shared authority.

The focused file contains 114 cases and killed 11 non-equivalent mutations. The complete query
suite passed 2,107 tests on the source branch; a post-cherry-pick composition of evaluator,
incident seek/scan, landing memo and schema codec passed 240 tests. Claude's alternating synthetic
A/B measured `1.083x` for hybrid fan-out, `1.144x` for forced scan and `1.045x` for the node page.
On the live board, the next exact-page observation preserved 500/777 with zero failures, measured
the node query at 0.866 s, the warm hybrid at 0.762–0.848 s and forced scan at 0.811–0.818 s. The
large scan change relative to the immediately preceding 2.334–2.547 s observation is useful
directional evidence but is not presented as a statistically isolated promise.

### KG-2 decision — deferred after live remeasurement

Claude's bounded endpoint-landing design was reviewed against the public pull-driven cursor, not
only against a fully consumed statement. A generic chunk inside `RelationshipScan` would resolve
later rows before yielding the first row of that chunk. That can surface relationship or endpoint
corruption which the current `LIMIT`/early-close consumer would never reach and can reorder the
first `from`/`to` refusal across rows. A row count of 256 is also not a memory bound for wide
relationship payloads, and a saturated landing cache would have to consume the exact per-chunk
map rather than accidentally resolving the same key again through the scalar path. Finally, the
change would affect every `RelationshipScan`, not only the closed Pulse projection.

The live measurement after KG-1/KG-4 put warm hybrid fan-out at 0.762–0.848 s and forced scan at
0.811–0.818 s. The plausible remaining KG-2 benefit is therefore about 0.08–0.16 s on this page,
not the 0.3–0.4 s derived from the earlier synthetic board. Claude accepted that the live
denominator supersedes the model, and the joint decision is **NO-GO in this wave** for both generic
KG-2 batching and the cross-statement `memo B`. This is a finite decision, not a new performance
gate. A future attempt must use a narrow/materialized API, bound both rows and bytes, leave cursor
error order unchanged, consume its returned local map exactly after cache saturation, and retain
the canonical v1, stale, RYOW and pending-row routes.

### Transfer/storage continuation

The first transfer/storage pair is complete. `9d17ed8` adds `BufferPool.allocate_run`: an eager
structural directory can grow its physical file with one `StorageDevice.allocate(file, count)`
and one initial `page_count`, while admitting ordinary unpinned dirty frames one at a time under
the same buffer budget. Budget refusal still occurs before growth; every page remains in the
existing `_grown`, load-revocation, dirty-candidate and write-back accounting. `fcd4e21` wires the
primitive into `IndexStore._grow_buckets`: a new index now makes one scalar allocation for page 0
and one allocation for the whole fixed bucket directory. A partially recovery-grown file keeps
all existing pages and allocates only the missing suffix. Tests exercise both that repair and a
complete directory under a one-page buffer budget.

An alternating two-round Pulse-shaped DDL measurement used the same Core `ea11b76`, Community
`523e759`, 92 DDL statements and minimal data cardinality in both arms. The scalar directory arm
measured `7.85–8.04 s` for `begin_candidate` (median `7.95 s`) and `17.19–17.70 s` for the complete
short transfer (median `17.45 s`). The run-allocation arm measured `6.25–6.74 s` (median `6.49 s`,
`-18.4%`) and `15.48–15.96 s` (median `15.72 s`, `-9.9%`) respectively. This deliberately short
A/B isolates the fixed DDL-heavy shape; it is not promoted as the expected percentage for a large
data transfer. Raw reports are `e1c_scalar_baseline_short.json` and
`e1c_batch_candidate_short.json` under the ignored `.grafx-tmp/amdahl` evidence directory.

Claude's LB-3 arrived as `d8851a6` and was integrated as `81615af`. The existing bounded replay
directory was already correct but applied only to buckets with at least eight effects; Pulse
places roughly two effects in most buckets, leaving them on a chain scan per effect. Every touched
bucket now uses one prepared directory, while the unchanged ceilings still bound bucket
identities, retained targets and retained pages and fall back to scalar semantics when exceeded.
The identity census is a set because effect counts are no longer consumed. Differential tests
cover exactly one through seven inserts per bucket, all page bytes, header, missing targets and
tombstone backlog against the scalar oracle; failure, interruption, page-0 replacement and active
vector-picture injections cover the newly selected door. The author branch passed 1,913 index,
recovery, WAL and transaction tests. Its alternating crash-replay A/B measured checkpoint
`3.31/3.55/3.35 s -> 3.15/3.16/3.06 s` (about `-6%`, with roughly `+/-5%` dispersion), identical
index-file hashes and clean verification. This is about `0.35 s`, or `0.6%`, of the refreshed
transfer and is recorded as a small bounded gain rather than a headline result.

After integrating both changes, one combined 281-test slice covering common/vector replay,
index creation and repair, buffer allocation, process-global state rules and real devices passed;
Ruff and diff checks were clean. Neither item changes format, WAL, either OCC validation,
durability, recovery ordering or multi-process admission.

## KG-LOAD-3 — fourth profile and finite implementation order

The user-visible priority remains the time from opening the Pulse Knowledge Graph page until its
graph can be rendered; a transfer benchmark is supporting evidence, not a substitute for this
operation. Claude's fourth profile is recorded in `.grafx-tmp/levantamento2/LEVANTAMENTO_4.md`
against `645afa4`. Its fresh Pulse-shaped transfer median was `60.18 s`, with Grafx accounting for
`86.8%`. The corresponding synthetic KG page measured `2.75 s` on the hybrid route and `2.52 s`
on forced scan; the real-board warm observations above (`0.762–0.848 s`) remain the relevant
end-user denominator.

The joint adversarial selection is closed and ordered by precedence:

1. exact-type dispatch in query evaluation (`KG-6a`) and value encoding (`R-15`), in Claude's
   isolated branch;
2. passage-local replay decode/catalog reuse (`R-5`), heap allocation without a redundant eager
   size probe (`E-1d`), and immutable decoded scan-row publication (`R-13`) in Codex's branch;
3. one combined focused regression and the already-defined KG-page harness after integration;
4. the independent Pulse generation-open setting (`D-4`) and active-filter collapse (`KG-5`);
5. only then, a materialized/replayable design for per-index read certification (`D-2`) and exact
   path/descriptor reproof (`D-8`), followed by measurements of `R-12` and open high-water work.

Three attractive shortcuts are finite NO-GOs. `R-14` cannot memoize a slotted-page directory as
proposed because the current buffer frame already stores the decoded `Page`; observed decode
counts belong to reload/eviction and require proof of duplicate work on the same frame. Replacing
open-time committed high-water traversal with `next_record_id` is invalid because an allocation
floor is not a committed-LSN watermark. `D-1` is rejected because it removes canonical-reference
and two-visible-version fences. None will be silently reintroduced as a moving target.

`dfefe00` implements `R-5`. Partitioned replay now decodes each record once and resolves repeated
exact built-in index names once inside the same protected replay passage. The canonical
`CommitRedo` preflight remains independent and complete. Custom managers/stores use the old path,
and no prepared authority is stored on the manager or accepted through the normal common-batch
API. Seventy-seven replay tests passed.

`073d042` implements `E-1d`. Buffer admission needs one available frame, not the future physical
page number, so successful scalar allocation now lets the atomic storage `allocate` call perform
the sole descriptor-size and index proof. The prospective index remains a lazy callback and is
resolved before any budget/re-entrancy refusal, preserving exact diagnostics. There is no local
high-water hint for a foreign writer to stale. All 174 buffer-pool tests passed.

`2cf6820` implements `R-13`. `scan_rows_v1` retains the heap decoder's exact tuple when every leaf
is an exact immutable stored scalar (`None`, built-in scalar, timestamp, UUID or vector). Compound,
subclassed or over-limit values take the canonical recursive copier, including the same field and
limit details. Focused scan/publication tests passed, and an instrumented vector row performs zero
recursive value-copy calls. These three changes do not alter stored format, WAL, either OCC
validation, snapshot visibility, durability or multi-reader/multi-writer admission.

`6165bce` + `6e29c7e` integrate `R-15`. Exact built-in value classes use a sealed
`MappingProxyType` dispatch to the same `ValueType` results, while subclasses, hostile values and
unsupported values retain the original ordered `isinstance` chain and failures. The focused micro
measured `value_type_of` around `2.2x` faster; encoded bytes are unchanged. The transfer-level
effect is below the run-to-run noise and is deliberately not promoted as a wall-clock claim.

`4de7be9` + `1940466` + `b3e70e5` integrate `KG-6a`. Exact `Literal` uses a direct identity fast
path and the other eleven exact expression classes use a sealed table that invokes the same
per-kind evaluators; computed values remain first, and subclasses/unknown expressions retain the
old chain and typed failures. The alternating KG-path sample improved the node page from `713 ms`
to `667 ms` (`1.069x`) and hybrid fan-out from `2.783 s` to `2.543 s` (`1.094x`). One vector sample
was noisy and slightly negative, without a code-specific mechanism and within the observed
single-run dispersion; following the instruction not to create a marginal performance gate, no
extra long A/B was required. A combined 381-test slice across value dispatch, expression dispatch,
query memoization, scan publication, replay and buffer allocation passed; Ruff and diff checks
were clean.

The companion Pulse work is also concrete. Community commit `52dbd22` makes `generation`
descriptor revalidation the default only for Pulse-managed generation directories and documents
when `strict` remains required; handles are still closed before restore or generation replacement.
Core commit `56b4764` collapses seven equivalent active-tombstone inequalities into one `NOT IN`
predicate with identical null semantics on Grafx and Ladybug. Those changes preserve storage
format, WAL, both OCC validations, recovery, durability and multi-reader/multi-writer operation.

The installed-browser validation exposed one functional cursor defect outside Grafx: Pulse kept
the opaque cursor timestamp as `STRING`. Ladybug implicitly cast it against a `TIMESTAMP` column,
while Grafx kept the two value domains distinct and correctly returned no ordered matches.
Core `7ebc849` now converts the decoded ISO boundary to a UTC `datetime` only for execution while
retaining the original string in the cache identity; Community `6ae14ed` normalizes typed read
parameters to Grafx's public immutable value domain. The focused result was 5 Core and 34
Community tests plus a Ladybug typed-parameter probe. On the installed real board, page one was
500 nodes/777 edges and page two was a disjoint 500 nodes/775 edges; the browser advanced from
`Knowledge graph with 500 nodes` / `Load more (500+)` to 1000 nodes / `Load more (1000+)`.

## Milestone log

| Milestone | State | Evidence |
|---|---|---|
| 0.0.2 frozen and pushed | complete | `feature/v0.0.2@acf63c8` |
| 0.0.3 branch and version bump | complete | `feature/v0.0.3@1f2172d` |
| Real Pulse KG load-path baseline | complete | measurements and source mapping above |
| Claude/Codex final selection | complete | finite selection and rejected guarantee changes above |
| First Grafx implementation batch | complete | `4638204`, 3.6–4.1× relationship-fanout reduction |
| HNSW replay and structural catalog batch | complete | `477fd45`, `36cea9b`; 230 focused tests and Ruff pass |
| Statement authority memo | complete | `4786496`; identity/catalog/revision/DDL fences, 39 integrated focused tests and Ruff pass |
| Exact multi-key index validation | complete | `1926fcd`; one durable certificate per index batch, 3.2× for 300 on-disk PK keys |
| Typed incident-edge operator and cost selection | complete and GO on candidate branches | Grafx `bcfa395` + `a726744`, Pulse `523e759`; adversarial review `hof_aecbb8257cb443c198a50634d2d1af2d` PASS; ordered page preserved 500/777, calls/probes `248/17,116 -> 54/4,355`, paired warm hybrid `0.763–0.967 s` versus forced scan `2.273–2.724 s` |
| Landing accounting without row re-encode | complete | `8d806b0`; authenticated durable payload length on disk rows, canonical fallback for synthetic/modified rows; 50 focused tests and Ruff pass |
| Planned STRING body decode | complete | `a610b55`; same byte validation/error taxonomy, 197 schema/value codec tests and Ruff pass; live ordered page preserved 500/777 |
| Bounded `IN` memo and scalar equality | complete | Claude `1c1a57e` + `2672fce`, integrated as `216319e` + `d12602f`; 2,107 complete query tests, 240 post-integration focused tests, 11 killed mutations; live 500/777 preserved |
| Bounded batch endpoint landing / memo B | deferred after adversarial review and live remeasurement | generic batching changes pull-cursor error order and has no byte cap; residual is about 0.08–0.16 s on the live page; only a future narrow/materialized API may reopen it |
| Structural allocation run (E-1c) | complete and measured | `9d17ed8` + `fcd4e21`; one physical directory grow, partial-repair and one-page-budget tests; Pulse-shaped DDL median `7.95 -> 6.49 s` (`-18.4%`) and short transfer `17.45 -> 15.72 s` (`-9.9%`) |
| Checkpoint bucket replay (LB-3) | complete and integrated | Claude `d8851a6`, integrated as `81615af`; exact 1–7-effect differential, 1,913 broad author tests, byte-identical index files and clean verify; checkpoint about `-6%`, approximately `0.6%` of refreshed transfer |
| Replay resolution reuse (R-5) | complete and integrated | `dfefe00`; independent full recovery preflight retained, passage-local only, custom fallbacks retained; 77 focused tests |
| Heap allocation sizing (E-1d) | complete and integrated | `073d042`; no eager page-count probe on successful scalar allocation, atomic device allocation remains authoritative; 174 buffer-pool tests |
| Immutable scan publication (R-13) | complete and integrated | `2cf6820`; exact decoded scalar/vector tuples bypass recursive copy, compound/hostile/over-limit fallback retained; focused scan/publication tests |
| Exact expression/value dispatch (KG-6a/R-15) | complete and integrated | `6165bce`, `4de7be9`, `1940466`, `6e29c7e`, `b3e70e5`; sealed exact-type tables with original fallback; KG node page `713 -> 667 ms`, fan-out `2.783 -> 2.543 s`; 381 combined focused tests |
| Pulse generation revalidation default (D-4) | complete on companion Community branch | `perf/v0.3.3-kg-load-grafx@52dbd22`; 60 focused tests with paired Core; strict mode remains available and documented |
| Active tombstone filter collapse (KG-5) | complete on companion Core branch | `perf/v0.3.3-kg-active-filter@56b4764`; seven focused tests plus Grafx/Ladybug semantic probes |
| Pulse cursor pagination parity | complete, installed and browser-validated | Core `7ebc849` + Community `6ae14ed`; typed UTC cursor boundary and Grafx public-value normalization; disjoint real pages `500/777 + 500/775`, UI advanced to 1000 nodes |
| Pulse critical rendering path | complete on companion branch | `e2b6053`; one-snapshot fanout 1.74–2.08 s; backend/frontend focused tests and production build |
| Pulse statistics fan-out | complete on companion branch | `880db68`; grouped nodes 0.662 s plus batched relationships 2.178 s; 90 backend tests and Ruff pass |
