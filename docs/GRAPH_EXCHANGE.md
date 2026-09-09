# Detached graph exchange

0.0.5 development, continuation after `a4dd85a`. These are copied, optional consumer
representations of a captured `GraphProjection`, not graph import, backup, persistent
catalogs or database mutation. Capture provenance is the source UUID and snapshot LSN;
it is not a new COMMIT proof. Generic algorithm contracts are in [projections](GRAPH_PROJECTIONS.md).

## NetworkX

Install `okto-grafx[networkx]`; the core never imports NetworkX automatically.
`to_networkx(graph, max_memory_bytes=67108864, cancellation=None)` returns a mutable,
independent `networkx.MultiDiGraph`. Node keys are `(uuid_hex, table, record_id)`;
edge keys are `(relationship_table, record_id)` within their directed endpoint pair.
All physical parallel edges and self-loops survive. Captured weights become `weight`
attributes; absent weights do not invent them. Graph attributes carry `database_uuid`
and `snapshot_lsn`. Mutating the exported graph does not change the projection/store.

Admission charges 4,096 + 4,096V + 8,192E against the explicit output limit (positive
integer <=2^31). This is additional logical export memory, not RSS or a bound on
subsequent NetworkX operations. Work/cancellation reuse the projection's work limit
and check each admitted node/edge. Missing optional dependency raises
`GrafxUnsupportedOperation`; invalid configuration and memory/work/cancellation use
the existing typed errors. No partial graph is returned on refusal.

```python
from okto_grafx.graph_interop import to_networkx

external = to_networkx(graph)
assert external.number_of_nodes() == len(graph.nodes)
assert external.number_of_edges() == len(graph.edges)
```

NetworkX conformance compares SCC, degrees, unweighted paths and simple-graph k-core
on seeded small fixtures. Converting a multigraph to a simple graph for an oracle is
explicit; it is not the export contract. No NetworkX execution is claimed for the
new label-propagation update policy or all possible weighted algorithms.
See the upstream [MultiDiGraph contract](https://networkx.org/documentation/stable/reference/classes/multidigraph.html).

## Arrow batches

`projection_arrow_batches(graph, kind="nodes", results=None, result_type="DOUBLE",
batch_rows=256, max_batch_bytes=16777216, cancellation=None)` requires `[arrow]`.
`kind` is exactly `nodes` or `edges`; it preserves projection order. Empty selections
produce no batches, consistent with native Arrow query export.

| Selection | Columns and types |
| --- | --- |
| Nodes | `table`, `record_id`: STRING |
| Edges | `table`, `record_id`, `source_table`, `source_record_id`, `target_table`, `target_record_id`: STRING; `weight`: DOUBLE only if captured |
| Optional results | `result`: explicit BOOL/INT64/DOUBLE/STRING, nullable |

Physical record IDs use canonical decimal strings to preserve the complete identity
without signed-INT64 or floating conversion. Schema metadata declares
`grafx.record_id.encoding=decimal-string-v1`, `grafx.database_uuid` (hex),
`grafx.snapshot_lsn` (decimal) and `grafx.projection.kind`. Field metadata uses the
existing native Arrow types. Results must be an exact tuple with one value for every
selected node/edge. The caller asserts that alignment/provenance; the exporter can
check length/types, not whether the supplied values were actually computed here.
For component labels, explicitly map identities to strings or use node-aligned IDs;
arbitrary objects and implicit casts are refused.

```python
from okto_grafx.graph_interop import projection_arrow_batches

batches = list(projection_arrow_batches(graph, results=graph.k_core(),
                                        result_type="INT64", batch_rows=256))
assert sum(batch.num_rows for batch in batches) == len(graph.nodes)
```

`batch_rows` is 1..65,536; batch bytes 1..2^31. Before building each output row batch,
the extra logical charge is 4,096 + 512 per cell + 16 times string lengths; native
Arrow export applies its own conversion admission too. Work/cancellation checks run
per member and before yielding. A late invalid value can refuse after earlier batches
were delivered; no whole-stream rollback or file publication is claimed. Earlier
batches belong to the caller. This bounds additional export batches, not retained
input/output tuples, native allocator RSS or algorithm computation memory. No cursor
or store lease is owned. Do not materialize `list(...)` for large workloads unless you
intend to retain all batches.

[API reference](API_REFERENCE.md) · [Tabular interop](TABULAR_AND_PARQUET.md) · [Roadmap](../ROADMAP.md)
