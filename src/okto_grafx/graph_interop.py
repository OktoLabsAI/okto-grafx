"""Optional detached graph exchange; neither import nor database authority."""

from __future__ import annotations

from typing import TYPE_CHECKING
from collections.abc import Iterator

from okto_grafx.projections import GraphProjection, ProjectionNode, _Work, _bound
from okto_grafx.domain.query.control import CancellationToken
from okto_grafx.errors import GrafxConfigurationError, GrafxUnsupportedOperation

if TYPE_CHECKING:
    from networkx import MultiDiGraph
    from pyarrow import RecordBatch

__all__ = ["to_networkx", "projection_arrow_batches"]


def to_networkx(
    graph: GraphProjection,
    *,
    max_memory_bytes: int = 64 * 1024 * 1024,
    cancellation: CancellationToken | None = None,
) -> MultiDiGraph:
    """Copy a detached multigraph with scoped identities and physical edge keys."""
    if type(graph) is not GraphProjection:
        raise GrafxConfigurationError("Expected a GraphProjection.", field="graph")
    if type(max_memory_bytes) is not int or not 1 <= max_memory_bytes <= 2**31:
        raise GrafxConfigurationError(
            "Invalid export memory bound.", field="max_memory_bytes"
        )
    work = _Work(graph.limits.max_work, cancellation)
    charge = 4096 + 4096 * len(graph.nodes) + 8192 * len(graph.edges)
    _bound("networkx_export", charge, max_memory_bytes)
    work.step(0)
    try:
        import networkx as nx
    except ImportError as failure:
        raise GrafxUnsupportedOperation(
            "Install okto-grafx[networkx] for graph export.", field="networkx"
        ) from failure
    result = nx.MultiDiGraph(
        database_uuid=graph.database_uuid.hex(), snapshot_lsn=graph.snapshot_lsn
    )

    def identity(node: ProjectionNode) -> tuple[str, str, int]:
        """Include store, table and physical record scope in every node identity."""
        return (graph.database_uuid.hex(), node.table, node.record_id)

    for node in graph.nodes:
        work.step()
        result.add_node(identity(node))
    for i, edge in enumerate(graph.edges):
        work.step()
        attributes = {} if graph.weights is None else {"weight": graph.weights[i]}
        result.add_edge(
            identity(graph.nodes[edge.source]),
            identity(graph.nodes[edge.target]),
            key=(edge.table, edge.record_id),
            **attributes,
        )
    return result


def projection_arrow_batches(
    graph: GraphProjection,
    *,
    kind: str = "nodes",
    results: tuple | None = None,
    result_type: str = "DOUBLE",
    batch_rows: int = 256,
    max_batch_bytes: int = 16 * 1024 * 1024,
    cancellation: CancellationToken | None = None,
) -> Iterator[RecordBatch]:
    """Export scoped identities/endpoints and optional aligned scalar results in bounded batches."""
    from okto_grafx.arrow import to_arrow_batches
    from okto_grafx.engine.query_engine import QueryResult
    from okto_grafx.tabular import _limit

    if (
        type(graph) is not GraphProjection
        or type(kind) is not str
        or kind not in ("nodes", "edges")
    ):
        raise GrafxConfigurationError(
            "Expected graph and nodes/edges kind.", field="kind"
        )
    _limit("batch_rows", batch_rows, 65536)
    _limit("max_batch_bytes", max_batch_bytes)
    members = graph.nodes if kind == "nodes" else graph.edges
    if results is not None and (
        type(results) is not tuple or len(results) != len(members)
    ):
        raise GrafxConfigurationError(
            "Results must be an exactly aligned tuple.", field="results"
        )
    if type(result_type) is not str or result_type not in (
        "BOOL",
        "INT64",
        "DOUBLE",
        "STRING",
    ):
        raise GrafxConfigurationError(
            "Result type must be BOOL/INT64/DOUBLE/STRING.", field="result_type"
        )
    columns, types = ("table", "record_id"), ("STRING", "STRING")
    if kind == "edges":
        columns += (
            "source_table",
            "source_record_id",
            "target_table",
            "target_record_id",
        )
        types += ("STRING",) * 4
        if graph.weights is not None:
            columns += ("weight",)
            types += ("DOUBLE",)
    if results is not None:
        columns += ("result",)
        types += (result_type,)
    work = _Work(graph.limits.max_work, cancellation)
    work.step(0)
    provenance = {
        b"grafx.database_uuid": graph.database_uuid.hex().encode("ascii"),
        b"grafx.snapshot_lsn": str(graph.snapshot_lsn).encode("ascii"),
        b"grafx.projection.kind": kind.encode("ascii"),
        b"grafx.record_id.encoding": b"decimal-string-v1",
    }
    for start in range(0, max(1, len(members)), batch_rows):
        rows, charge = [], 4096
        for index in range(start, min(start + batch_rows, len(members))):
            work.step()
            member = members[index]
            row = (member.table, str(member.record_id))
            if kind == "edges":
                source, target = graph.nodes[member.source], graph.nodes[member.target]
                row += (
                    source.table,
                    str(source.record_id),
                    target.table,
                    str(target.record_id),
                )
                if graph.weights is not None:
                    row += (graph.weights[index],)
            if results is not None:
                row += (results[index],)
            charge += 512 * len(row) + sum(16 * len(v) for v in row if type(v) is str)
            _bound("projection_export", charge, max_batch_bytes)
            rows.append(row)
        for batch in to_arrow_batches(
            QueryResult(columns, tuple(rows)),
            types=types,
            batch_rows=batch_rows,
            max_batch_bytes=max_batch_bytes,
        ):
            work.step(0)
            yield batch.replace_schema_metadata(provenance)
