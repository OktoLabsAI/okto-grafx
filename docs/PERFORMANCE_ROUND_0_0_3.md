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

Status: **diagnosed; implementation selection awaits the joint Claude/Codex review**.

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

## Milestone log

| Milestone | State | Evidence |
|---|---|---|
| 0.0.2 frozen and pushed | complete | `feature/v0.0.2@acf63c8` |
| 0.0.3 branch and version bump | complete | `feature/v0.0.3@1f2172d` |
| Real Pulse KG load-path baseline | complete | measurements and source mapping above |
| Claude/Codex final selection | pending | Nexus notification and adversarial consensus |
| First implementation batch | pending | focused correctness tests before grouped regression |
