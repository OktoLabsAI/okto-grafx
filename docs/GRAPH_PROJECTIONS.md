# Snapshot graph projections

Available in **0.0.5 development** through `okto_grafx.projections`. This is the
first bounded GX-CAP-9 slice, not a persistent projection catalog or algorithm server.
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
limits=ProjectionLimits(), cancellation=None)` captures selected committed rows
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
Work is O(V+E).

`weakly_connected_components(cancellation=None)` ignores direction and returns each
node's component label, the smallest `(table, record_id)` identity in that component.
Isolates label themselves. Parallel edges/loops do not change membership. Union by
size/path compression uses O((V+E) alpha(V)) work and O(V) workspace. It is not SCC,
weighted connectivity, PageRank, k-core or an algorithm write-back API.

| `ProjectionLimits` field | Default | Meaning |
| --- | ---: | --- |
| `max_nodes` | 100,000 | Visible nodes admitted |
| `max_edges` | 1,000,000 | Physical relationship occurrences admitted |
| `max_memory_bytes` | 256 MiB | Logical picture and one algorithm workspace |
| `max_work` | 10,000,000 | Cooperative observations, independently per capture/algorithm |

All limits are exact positive integers up to 2^31 (bool is refused). Admission is
checked before appending each row: 4,096 fixed bytes, 512 + four times the name
length per table, 1,024 per node, 512 per edge. This includes lookup/tuple assembly
and one algorithm's arrays/output; it is **not RSS**, simultaneous algorithm calls,
caller-retained earlier results, or the scan engine's decode buffers. Capture asks
for one row at a time, so large property payloads are decoded but not retained in
the projection. Physical scan/revalidation costs remain those of the underlying
cursor; `max_work` counts observed rows/pages, not hidden physical I/O or CPU time.
Capture is an explicit selected-table census, not an indexed O(1) operation.

Budgets raise `GrafxQueryBudgetExceeded` with a `projection_*` resource and requested/
limit fields. Invalid selections/directions/limits raise `GrafxConfigurationError`;
invalid reader ownership/state raises `GrafxTransactionStateError`. Cancellation
uses the existing one-way `CancellationToken`, is checked between scan calls and
algorithm steps, and raises `GrafxQueryCancelled`; it cannot preempt a scan already
in progress. No implicit retries or partial pictures/results are returned.

[API signatures and DTOs](API_REFERENCE.md) · [Roadmap](../ROADMAP.md)
