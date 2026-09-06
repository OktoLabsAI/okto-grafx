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

Status: **implemented on the Grafx and Pulse working branches; independent adversarial review in
progress**.

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

The operator and primitive remain valid scale building blocks, but they are not presented as the
current screen-speed win. Before Pulse promotion, the next implementation must remove the repeated
node-PK probes across relationship statements (or add a proved cost selection that retains the
scan for small layouts), then repeat the exact ordered-page comparison. This is a fixed follow-up,
not a new exploratory gate: the consumer must beat the already measured scan path while retaining
its `O(page keys + incident edges)` scaling advantage.

Focused validation is 68 Grafx tests plus 10 Pulse consumer tests and Ruff/diff checks. The Grafx
set includes batch/scalar snapshot parity, page-0 transition retry/refusal, hostile keys, stale and
proximity refusal, primary/endpoint index near misses, incoming syntax, parallel relationships,
identity landing batching, owner read-your-writes and differential comparison with the canonical
fallback. No format, WAL, commit, OCC, lease, recovery, writer admission or reader-snapshot code
changed.

Two apparent follow-ups are deliberately not being smuggled into this wave. R-3 cannot skip the
commit-time tuple encoding solely because `intent.values` retained object identity: the direct
`TransactionContext.stage_row_insert/update` port does not schema-encode unless byte quota
accounting happens to be enabled. It needs an authenticated, rollback-bounded validation witness
and a fresh gain measurement before it is safe; the simple identity shortcut is rejected. For
KG endpoint pushdown, adding `IN $ids` above `RelationshipScan` would still scan O(E), while one
equality query per page node multiplies statement/index work. The scalable follow-up is a real
multi-key endpoint seek/incident-edge operator (or an equally explicit structured port), with
the same endpoint visibility and canonical-reference validation as ordinary traversal.

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
| Typed incident-edge operator | engine building block complete; direct Pulse activation NO-GO pending repeated-PK removal | Grafx `bcfa395`, Pulse experiment `523e759`; actual ordered page preserved 500/777, but edge phase measured 2.677 s warm versus prior 1.74–2.08 s |
| Pulse critical rendering path | complete on companion branch | `e2b6053`; one-snapshot fanout 1.74–2.08 s; backend/frontend focused tests and production build |
| Pulse statistics fan-out | complete on companion branch | `880db68`; grouped nodes 0.662 s plus batched relationships 2.178 s; 90 backend tests and Ruff pass |
