# Snapshot graph projections

Available in **0.0.5 development** through `okto_grafx.projections`. This is the
bounded GX-CAP-9 API, not a persistent projection catalog or algorithm server.
It requires no optional dependency and does not write WAL, properties or schema.

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
limits=ProjectionLimits(), cancellation=None, timeout_seconds=None)` captures selected committed rows
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

Parallel edges, self-loops and isolates are preserved. No properties, vectors,
weights or mutable engine handles are retained. A picture may outlive its reader
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
O(V+E) including source lookup/adjacency, workspace O(V); no storage/frontier query.

### PageRank and k-core

`pagerank(*, damping=0.85, tolerance=1e-8, max_iterations=100, cancellation=None)`
uses uniform initial ranks, uniform teleportation and uniform redistribution of
dangling mass. Every physical outgoing edge gets equal share; parallel occurrences
therefore carry multiplicity, and loops participate. Damping must be finite in
(0,1), tolerance finite in (0,1], and iterations an exact integer 1..1,000,000.
The `PageRankResult` contains node-aligned `scores`, `iterations`, `converged`, and
`residual` (L1 difference between successive iterates). Reaching the iteration cap
returns `converged=False`, never pretends to meet tolerance. An empty picture returns
empty scores, zero iterations/residual and `converged=True`. Work is
O(iterations × (V+E)), workspace O(V), excluding CSR. This is unweighted PageRank.

`k_core(cancellation=None)` returns integer core numbers aligned with nodes. It
explicitly interprets the picture as **simple undirected**: parallel edges collapse,
direction is ignored, self-loops are excluded and isolates have core zero. Heap-based
peeling uses O((V+E) log(V+E)) work and O(V+E) workspace; it does not build unused CSR.
Weighted algorithms, personalization, persistent catalogs and algorithm write-back
remain outside this API.

```python
picture = graph.with_adjacency()
strong = picture.strongly_connected_components()
ranks = picture.pagerank(max_iterations=200)
cores = picture.k_core()
if picture.nodes:
    reachable = picture.reachable(picture.nodes[0], max_depth=3)
    path = picture.shortest_path(picture.nodes[0], picture.nodes[-1])
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
length per table, 1,024 per node, 512 per edge. This includes lookup/tuple assembly
and one algorithm's arrays/output; it is **not RSS**, simultaneous algorithm calls,
caller-retained earlier results, or the separately bounded scan batch workspace.
Additional algorithms reserve 4,096 + 1,024V workspace; k-core also reserves 512E.
CSR construction reserves its retained charge and concurrent workspace before allocation.
The conservative tariffs can refuse a graph that would fit in measured RSS.
Capture requests `columns=()` for nodes and only `_from`, `_to` for relationships:
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
