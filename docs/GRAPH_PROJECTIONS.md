# Snapshot graph projections

Available in **0.0.5 development** through `okto_grafx.projections`. This is the
bounded GX-CAP-9 API, not a persistent projection catalog or algorithm server.
The default algorithms require no optional dependency and do not write WAL, properties
or schema. PageRank's explicit NumPy backend requires `okto-grafx[accel]`.

```python
from okto_grafx.projections import project_graph, ProjectionLimits

with db.begin("read") as reader:
    graph = project_graph(db, reader, node_tables=("Person",),
                          relationship_tables=("KNOWS",),
                          limits=ProjectionLimits(max_nodes=10000, max_edges=50000))
counts = graph.degrees(direction="total")
components = graph.weakly_connected_components()
for node, degree, component in zip(graph.nodes, counts, components):
    print(node.table, node.record_id, degree, component)
```

## Selection, identities and lifecycle

`project_graph(database, reader=None, *, node_tables, relationship_tables=(),
limits=ProjectionLimits(), cancellation=None, timeout_seconds=None,
weight_columns=None, default_weight=None)` captures selected committed rows
using the public physical scan cursor. Without `reader`, the function owns a read
transaction and closes it on success or failure. An explicit reader must be active,
read-only and from that exact handle; it remains caller-owned and usable after a
budget/cancellation refusal. Writers from other handles retain their normal rights.

Each selection is a tuple of at most 256 distinct names, each at most 256 characters;
at least one node table is required. Both endpoint tables of every selected
relationship must be selected. Unselected relationship tables are intentionally
absent, not followed implicitly. Missing visible endpoints fail closed with
`GrafxCorruptionDetected`. Schema definitions are captured at invocation; this is
not historical schema time travel. Rows come from the reader's fixed snapshot.

`GraphProjection` is an immutable detached value: `database_uuid` and `snapshot_lsn`
identify the source view, not a qualified commit-history ID. `nodes` contains
`ProjectionNode(table, record_id)` values; record IDs are physical, not application
primary keys. `edges` contains every physical relationship occurrence with table,
record ID and integer `source`/`target` offsets into `nodes`. Node order follows the
selected table order and physical scan order; it is not primary-key order.

Parallel edges, self-loops and isolates are preserved. Only explicitly selected
numeric weights may accompany topology; no other properties, vectors or mutable
engine handles are retained. A picture may outlive its reader
and database. There is no resource-bearing `close()`; release Python references
normally. Construct pictures with `project_graph`, not by forging DTOs. Caller-held
tuples/results are ordinary caller-owned memory and are not an engine cache.

## Algorithms and controls

`degrees(direction="in"|"out"|"total", cancellation=None)` returns integers aligned
with `nodes`. Each physical edge counts once in each selected direction. A loop
contributes one incoming, one outgoing and two total; parallel edges each count.
Work is O(V+E), or O(V) with retained adjacency.

`weakly_connected_components(cancellation=None)` ignores direction and returns each
node's component label, the smallest `(table, record_id)` identity in that component.
Isolates label themselves. Parallel edges/loops do not change membership. Union by
size/path compression uses O((V+E) alpha(V)) work and O(V) workspace.

### Reusable adjacency and directed components

`graph.with_adjacency(cancellation=None)` returns an immutable copy sharing its
nodes/edges, with two compressed sparse row (CSR) structures. Retain this returned
picture to reuse topology across calls; the original stays unchanged. Calling it
again returns the same picture. `adjacency` contains `out_offsets`, `out_edges`,
`in_offsets`, `in_edges`, and `logical_bytes`; entries are **edge indices**, preserving
physical parallel edges and loops, not deduplicated neighbors. Construction is
O(V+E), reserved before allocation, charged 4,096 + 256(V+E) logical bytes.
There is no persisted cache, implicit invalidation or background storage access.

`with_lookup(cancellation=None)` retains an immutable `ProjectionLookup.positions`
mapping from node identity to offset, charged 4,096 + 256V additional logical bytes.
Construction is O(V), repeated lookup expected O(1). `with_adjacency()` includes it;
retain the returned picture, since the original is unchanged. Repeating either
preparation on an already prepared picture reuses that state. This is derived
detached data, never storage authority or a global cache.

`strongly_connected_components(cancellation=None)` returns minimum-identity labels
aligned with `nodes`, like WCC but respecting direction. Iterative Kosaraju uses
O(V+E) work, O(V) algorithm workspace and no recursive Python traversal. SCC, paths
and PageRank create a temporary adjacency if absent; retaining `with_adjacency()`
avoids rebuilding it for repeated algorithms.

### Reachability and shortest paths

`reachable(source, *, direction="out", max_depth=None, max_results=100000,
cancellation=None)` returns node identities in BFS order, **including source**.
`shortest_path(source, target, *, direction="out", max_depth=None,
max_results=100000, cancellation=None)` returns `ProjectionPath(found, nodes, edges)`.
Use `ProjectionNode` identities from this picture, not application keys or offsets;
missing identities raise `GrafxConfigurationError`. A node reached from itself has
a zero-edge path; unreachable/out-of-depth target returns `(False, (), ())`.

Direction is `out`, `in`, or `both` (outgoing neighbors before incoming). Equal-hop
ties follow physical adjacency order, not primary keys. Returned edges keep their
original directed identity even for reverse traversal. `max_depth` is None or an
exact integer 0..2^31; depth zero admits only source. `max_results` is an exact integer
1..2^31 and caps **discovered** nodes, including queued nodes, not just path length.
Exceeding it refuses the call; depth is a selection bound, not an error. Work is
O(V+E) on first preparation. With retained lookup and CSR, work follows visited
nodes/incident edges; source/target lookup no longer scans V. BFS workspace is
charged by discovered nodes (1,024 each + 4,096 fixed), not total V. No storage query.

### PageRank and k-core

`pagerank(*, damping=0.85, tolerance=1e-8, max_iterations=100, cancellation=None,
backend="python", weighted=False, personalization=None)` by default
uses uniform initial ranks, uniform teleportation and uniform redistribution of
dangling mass. Every physical outgoing edge gets equal share; parallel occurrences
therefore carry multiplicity, and loops participate. Damping must be finite in
(0,1), tolerance finite in (0,1], and iterations an exact integer 1..1,000,000.
The `PageRankResult` contains node-aligned `scores`, `iterations`, `converged`, and
`residual` (L1 difference between successive iterates). Reaching the iteration cap
returns `converged=False`, never pretends to meet tolerance. An empty picture returns
empty scores, zero iterations/residual and `converged=True`. Work is
O(iterations × (V+E)), workspace O(V) for the default path, excluding CSR.

`backend="numpy"` explicitly selects bounded float64 numeric buffers and weighted
destination reduction; absent NumPy raises `GrafxUnsupportedOperation`, with no
silent fallback. Python stays the default/reference. No `auto` selector is offered.
Tests compare scores at absolute tolerance 1e-11 on converged fixtures; reduction
order can differ, so bitwise scores, identical iteration counts and identical
convergence decisions at a tolerance boundary are not promised. Cancellation/work
checks bracket each kernel/iteration; a running native kernel is not preempted.
Prefer NumPy for larger/repeated analyses, Python for dependency-free or small jobs;
neither backend accelerates Pulse queries that do not call this API.

`k_core(cancellation=None)` returns integer core numbers aligned with nodes. It
explicitly interprets the picture as **simple undirected**: parallel edges collapse,
direction is ignored, self-loops are excluded and isolates have core zero. Bucket
peeling uses O(V+E) work after simple-neighbor construction (expected constant-time
sets), and O(V+E) workspace; no stale heap entries or unused CSR are built.
Persistent catalogs and algorithm write-back remain outside this API.

```python
picture = graph.with_adjacency()
strong = picture.strongly_connected_components()
ranks = picture.pagerank(max_iterations=200)
cores = picture.k_core()
if picture.nodes:
    reachable = picture.reachable(picture.nodes[0], max_depth=3)
    path = picture.shortest_path(picture.nodes[0], picture.nodes[-1])
```

### Optional weights, Dijkstra and personalized ranking

`weight_columns={"KNOWS": "cost"}` must name exactly every selected relationship
table and one existing INT64/DOUBLE property for each (not `_from`/`_to`). Values
must be finite non-negative numbers; bool, NaN, infinity and negatives are refused.
Weights are converted to float64 semantics (large INT64 may round), captured in
the same reader snapshot and retained as `graph.weights`, aligned with physical
`edges`. No mapping means `weights=None` and unchanged unweighted capture. Missing
columns/wrong types refuse. NULL refuses unless explicit finite non-negative
`default_weight` is supplied; it applies only to NULL, not a missing column.
Supplying a default without weight_columns is invalid. Zero weight is valid.

`weighted_shortest_path(source, target, *, direction="out", max_results=100000,
max_distance=None, cancellation=None)` returns `WeightedProjectionPath(found,
distance, nodes, edges)` using Dijkstra. Weights must be captured first. Source
equals target yields distance 0.0; unreachable/out-of-distance returns
`(False, None, (), ())`. Directions and physical edge identity match BFS. Equal-cost
ties keep the first discovered predecessor, with a stable queue insertion order.
There is no hop/depth constraint, negative-weight support or implicit unit fallback.
`max_distance` is None or finite non-negative; overflowing a path sum refuses with
`GrafxConfigurationError`. `max_results` caps discovered nodes, not returned hops.
After preparation, worst-case work is O((V+E) log(V+E)); local reachable regions
avoid a full V census. Workspace charges discovered maps plus every pending heap
entry, including stale entries, before allocation; exceeding a bound returns no path.

`pagerank(weighted=True)` normalizes outgoing weights per node. Parallel edges
retain separate shares; all-zero outgoing total makes that node dangling. Captured
weights are ignored unless `weighted=True`. `personalization` is None (uniform)
or an exact nonempty dictionary of this picture's ProjectionNode keys and finite
non-negative numbers, with positive total mass. Unknown keys, all-zero mass and
negative/NaN/infinite values refuse. Omitted nodes receive zero seed mass. Max-scaled
normalization handles very large finite values without summing them to infinity.
Teleportation AND dangling redistribution use this normalized seed; initial ranks
remain uniform. Weighted and personalization options are independently selectable
and supported by both backends. No seed/weight mutation or storage access occurs.

```python
from okto_grafx.projections import project_graph

weighted = project_graph(db, node_tables=("Person",), relationship_tables=("KNOWS",),
                         weight_columns={"KNOWS": "cost"}, default_weight=1.0).with_adjacency()
if weighted.nodes:
    route = weighted.weighted_shortest_path(weighted.nodes[0], weighted.nodes[-1])
    personalized = weighted.pagerank(weighted=True,
                                     personalization={weighted.nodes[0]: 1.0})
```

### Capture and resource limits

| `ProjectionLimits` field | Default | Meaning |
| --- | ---: | --- |
| `max_nodes` | 100,000 | Visible nodes admitted |
| `max_edges` | 1,000,000 | Physical relationship occurrences admitted |
| `max_memory_bytes` | 256 MiB | Logical picture and one algorithm workspace |
| `max_work` | 10,000,000 | Cooperative observations, independently per capture/algorithm |
| `batch_rows` | 256 | Rows per projected scan call, at most 65,536 |
| `max_batch_bytes` | 16 MiB | Separate conservative scan batch workspace cap |

All limits are exact positive integers up to 2^31 (bool is refused). Admission is
checked before appending each row: 4,096 fixed bytes, 512 + four times the name
length per table, 1,024 per node, 512 per edge (544 with retained weight). This includes capture lookup/tuple assembly
and one algorithm's arrays/output; it is **not RSS**, simultaneous algorithm calls,
caller-retained earlier results, or the separately bounded scan batch workspace.
Additional algorithms reserve 4,096 + 1,024V workspace; k-core also reserves 512E.
Weighted or NumPy PageRank also reserves 512E; Python unweighted does not. Prepared
BFS reserves 4,096 + 1,024 times discovered nodes; Dijkstra adds 96 per pending heap
entry. Retained identity lookup and CSR are included in `graph.logical_bytes`.
CSR construction reserves its retained charge and concurrent workspace before allocation.
The conservative tariffs can refuse a graph that would fit in measured RSS.
Capture requests `columns=()` for nodes and `_from`, `_to` plus any explicitly
selected weight for relationships:
omitted properties/vectors are structurally validated but not materialized. The
scan batch charge is 512 + 64 × column count + 64 × complete encoded payload bytes
per row, including omitted payloads. A single over-budget row refuses instead of
looping or silently truncating. Total logical working bounds include both picture
and batch caps. `diagnostics` exposes `scan_calls`, admitted `rows`, `max_batch_rows`,
and `capture_work`; these are not latency, disk-I/O or RSS measurements.
Physical scan/revalidation costs remain those of the underlying cursor;
`max_work` counts cooperative observations, not hidden physical I/O or CPU time.
Capture is an explicit selected-table census, not an indexed O(1) operation.

Budgets raise `GrafxQueryBudgetExceeded` with a `projection_*` resource and requested/
limit fields. Invalid selections/directions/limits raise `GrafxConfigurationError`;
invalid reader ownership/state raises `GrafxTransactionStateError`. Cancellation
uses the existing one-way `CancellationToken`, checked within scan page/slot walks
and algorithm steps, and raises `GrafxQueryCancelled`. Capture's optional finite,
positive `timeout_seconds` is a cooperative deadline propagated across batches;
it raises `GrafxQueryDeadlineExceeded`. Neither preempts blocking OS I/O, participant
admission or a single payload decode. Owned-reader admission precedes the capture
deadline. No implicit retries or partial pictures/results are returned, except the
explicit non-converged PageRank result on its iteration cap. Read controls never
interrupt a durable commit. Algorithms on detached pictures do not hold read leases.

[API signatures and DTOs](API_REFERENCE.md) · [Roadmap](../ROADMAP.md)
