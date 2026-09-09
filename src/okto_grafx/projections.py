"""Bounded, detached graph pictures captured through the public scan API."""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.engine.database import Database, Transaction
from okto_grafx.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxQueryBudgetExceeded,
    GrafxQueryCancelled, GrafxTransactionStateError,
)
from okto_grafx.domain.query.control import CancellationToken
from okto_grafx.projection_algorithms import (
    ProjectionAdjacency, ProjectionPath, PageRankResult,
    _with_adjacency, _strong_components, _bfs, _pagerank, _k_core,
)

__all__ = ["ProjectionLimits", "ProjectionDiagnostics", "ProjectionNode", "ProjectionEdge", "GraphProjection", "project_graph"]


@dataclass(frozen=True, slots=True)
class ProjectionLimits:
    """Logical picture/work limits; not process RSS or underlying scan I/O limits."""

    max_nodes: int = 100_000
    max_edges: int = 1_000_000
    max_memory_bytes: int = 256 * 1024 * 1024
    max_work: int = 10_000_000
    batch_rows: int = 256
    max_batch_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        for field in ("max_nodes", "max_edges", "max_memory_bytes", "max_work", "batch_rows", "max_batch_bytes"):
            value = getattr(self, field)
            if type(value) is not int or not 0 < value <= 2**31:
                raise GrafxConfigurationError("Projection limits must be positive bounded integers.", field=field)
        if self.batch_rows > 65536:
            raise GrafxConfigurationError("batch_rows must be <=65536.", field="batch_rows")


@dataclass(frozen=True, slots=True)
class ProjectionDiagnostics:
    """Capture observations, not physical I/O counts or a freshness certificate."""

    scan_calls: int
    rows: int
    max_batch_rows: int
    capture_work: int


@dataclass(frozen=True, slots=True, order=True)
class ProjectionNode:
    """A table-qualified physical record identity, not an application primary key."""

    table: str
    record_id: int


@dataclass(frozen=True, slots=True)
class ProjectionEdge:
    """One physical relationship; endpoints are offsets in GraphProjection.nodes."""

    table: str
    record_id: int
    source: int
    target: int


class _Work:
    def __init__(self, maximum: int, token: CancellationToken | None) -> None:
        if token is not None and type(token) is not CancellationToken:
            raise GrafxConfigurationError("Expected a CancellationToken.", field="cancellation")
        self.maximum = maximum
        self.token = token
        self.used = 0
        self.step(0)

    def step(self, amount: int = 1) -> None:
        """Observe cancellation and charge bounded capture or algorithm work."""
        if self.token is not None and self.token.cancelled:
            raise GrafxQueryCancelled("Graph projection operation cancelled.")
        self.used += amount
        _bound("projection_work", self.used, self.maximum)


def _bound(resource: str, requested: int, limit: int) -> None:
    if requested > limit:
        raise GrafxQueryBudgetExceeded("Graph projection budget exceeded.", resource=resource,
                                      requested=requested, limit=limit)


@dataclass(frozen=True, slots=True)
class GraphProjection:
    """Immutable directed multigraph with no handles, pins or durable effects.

    Capture owns no transaction after returning. Pictures may outlive their source
    database; release ordinary Python references to release memory. Algorithms
    return tuples aligned with nodes; they never update the source graph.
    """

    database_uuid: bytes
    snapshot_lsn: int
    nodes: tuple[ProjectionNode, ...]
    edges: tuple[ProjectionEdge, ...]
    limits: ProjectionLimits
    logical_bytes: int
    diagnostics: ProjectionDiagnostics | None = None
    adjacency: ProjectionAdjacency | None = None

    def with_adjacency(self, *, cancellation: CancellationToken | None = None) -> GraphProjection:
        """Return a new picture with reusable immutable adjacency, charging its memory."""
        return _with_adjacency(self, cancellation)

    def strongly_connected_components(self, *, cancellation: CancellationToken | None = None) -> tuple[ProjectionNode, ...]:
        """Return deterministic directed SCC labels without recursion or storage reads."""
        return _strong_components(self, cancellation)

    def reachable(self, source: ProjectionNode, *, direction: str = "out", max_depth: int | None = None,
                  max_results: int = 100_000, cancellation: CancellationToken | None = None) -> tuple[ProjectionNode, ...]:
        """Return discovered nodes in BFS order, including source; no silent truncation."""
        return _bfs(self, source, None, direction, max_depth, max_results, cancellation)

    def shortest_path(self, source: ProjectionNode, target: ProjectionNode, *, direction: str = "out",
                      max_depth: int | None = None, max_results: int = 100_000,
                      cancellation: CancellationToken | None = None) -> ProjectionPath:
        """Return one unweighted shortest path; equal choices follow physical edge order."""
        return _bfs(self, source, target, direction, max_depth, max_results, cancellation)

    def pagerank(self, *, damping: float = 0.85, tolerance: float = 1e-8, max_iterations: int = 100,
                 cancellation: CancellationToken | None = None) -> PageRankResult:
        """Compute bounded unweighted PageRank with explicit convergence and L1 residual."""
        return _pagerank(self, damping, tolerance, max_iterations, cancellation)

    def k_core(self, *, cancellation: CancellationToken | None = None) -> tuple[int, ...]:
        """Return simple-undirected core numbers: parallel edges collapse and loops are ignored."""
        return _k_core(self, cancellation)

    def degrees(self, *, direction: str = "total",
                cancellation: CancellationToken | None = None) -> tuple[int, ...]:
        """Count physical incoming/outgoing occurrences; a self-loop has total degree two."""
        if type(direction) is not str or direction not in ("in", "out", "total"):
            raise GrafxConfigurationError("direction must be in, out or total.", field="direction")
        work = _Work(self.limits.max_work, cancellation)
        work.step(len(self.nodes))
        counts = [0] * len(self.nodes)
        if self.adjacency is not None:
            for i in range(len(counts)):
                work.step()
                if direction != "in":
                    counts[i] += self.adjacency.out_offsets[i + 1] - self.adjacency.out_offsets[i]
                if direction != "out":
                    counts[i] += self.adjacency.in_offsets[i + 1] - self.adjacency.in_offsets[i]
            return tuple(counts)
        for edge in self.edges:
            work.step()
            if direction != "in":
                counts[edge.source] += 1
            if direction != "out":
                counts[edge.target] += 1
        work.step(len(counts))
        return tuple(counts)

    def weakly_connected_components(self, *, cancellation: CancellationToken | None = None
                                    ) -> tuple[ProjectionNode, ...]:
        """Return each node's component label (minimum table/record identity), including isolates.

        Direction is ignored; parallel edges and loops do not change membership.
        Union by size plus path compression bounds work by O((V+E) alpha(V)).
        """
        work = _Work(self.limits.max_work, cancellation)
        work.step(len(self.nodes))
        parent = list(range(len(self.nodes)))
        sizes = [1] * len(parent)
        labels = list(self.nodes)

        def root(index: int) -> int:
            """Find a representative with bounded, charged path compression."""
            while parent[index] != index:
                work.step()
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        for edge in self.edges:
            work.step()
            left, right = root(edge.source), root(edge.target)
            if left != right:
                if sizes[left] < sizes[right]:
                    left, right = right, left
                parent[right] = left
                sizes[left] += sizes[right]
                labels[left] = min(labels[left], labels[right])
        result = []
        for index in range(len(parent)):
            work.step()
            result.append(labels[root(index)])
        return tuple(result)


def project_graph(database: Database, reader: Transaction | None = None, *,
                  node_tables: tuple[str, ...], relationship_tables: tuple[str, ...] = (),
                  limits: ProjectionLimits = ProjectionLimits(),
                  cancellation: CancellationToken | None = None,
                  timeout_seconds: float | None = None) -> GraphProjection:
    """Capture selected tables in one read snapshot, preserving parallel edges and loops.

    Explicit readers must belong to database and remain caller-owned. Without one,
    a short-lived read transaction is owned here. Properties/weights are not retained.
    The logical budget includes algorithm workspace, but excludes caller-retained
    results and the separately bounded scan batch workspace. No hidden full-table query.
    """
    if type(database) is not Database or type(limits) is not ProjectionLimits:
        raise GrafxConfigurationError("Expected a Database and ProjectionLimits.", field="projection")
    work = _Work(limits.max_work, cancellation)
    from okto_grafx.domain.query.control import _read_control
    control = _read_control(database._clock, timeout_seconds, cancellation)
    for field, names in (("node_tables", node_tables), ("relationship_tables", relationship_tables)):
        if (type(names) is not tuple or len(names) > 256
                or any(type(n) is not str or not n or len(n) > 256 for n in names)
                or len(set(names)) != len(names)):
            raise GrafxConfigurationError("Expected up to 256 distinct table names.", field=field)
    if not node_tables:
        raise GrafxConfigurationError("At least one node table is required.", field="node_tables")
    if reader is None:
        with database.begin("read") as owned:
            return project_graph(database, owned, node_tables=node_tables,
                                 relationship_tables=relationship_tables, limits=limits,
                                 cancellation=cancellation, timeout_seconds=timeout_seconds)
    if (type(reader) is not Transaction or reader._database is not database
            or not reader.active or reader.mode != "read"):
        raise GrafxTransactionStateError("Projection requires an active read transaction from this handle.")
    catalog = database.catalog.catalog
    definitions = tuple(catalog.table(n) for n in node_tables + relationship_tables)
    for table in definitions:
        work.step()
        if table.name in node_tables:
            if table.kind != "node":
                raise GrafxConfigurationError("Expected a node table.", table=table.name)
        elif (table.kind != "rel" or table.from_table not in node_tables
              or table.to_table not in node_tables):
            raise GrafxConfigurationError("Relationship endpoints must both be selected node tables.", table=table.name)
    nodes: list[ProjectionNode] = []
    edges: list[ProjectionEdge] = []
    offsets: dict[ProjectionNode, int] = {}
    # Includes name storage, temporary lookup, tuple assembly and one algorithm's
    # counters/union-find/output. Not an estimate of Python process RSS.
    logical_bytes = 4096 + sum(512 + 4 * len(t.name) for t in definitions)
    _bound("projection_memory", logical_bytes, limits.max_memory_bytes)
    scan_calls = maximum_batch = 0
    for table in definitions:
        cursor = None
        while True:
            work.step()
            if control is not None:
                control.check()
            remaining = (None if control is None or control._deadline is None else
                         max(1e-12, control._deadline - database._clock.monotonic()))
            page = reader.scan_rows_v1(table.name, limit=limits.batch_rows, cursor=cursor,
                                      columns=() if table.kind == "node" else ("_from", "_to"),
                                      max_batch_bytes=limits.max_batch_bytes, cancellation=cancellation,
                                      timeout_seconds=remaining)
            scan_calls += 1
            maximum_batch = max(maximum_batch, len(page.rows))
            for row in page.rows:
                work.step()
                if table.kind == "node":
                    _bound("projection_nodes", len(nodes) + 1, limits.max_nodes)
                    addition = 1024
                else:
                    _bound("projection_edges", len(edges) + 1, limits.max_edges)
                    addition = 512
                _bound("projection_memory", logical_bytes + addition, limits.max_memory_bytes)
                logical_bytes += addition
                if table.kind == "node":
                    node = ProjectionNode(table.name, row.record_id)
                    if node in offsets:
                        raise GrafxCorruptionDetected("Duplicate visible projection node.")
                    offsets[node] = len(nodes)
                    nodes.append(node)
                else:
                    source = offsets.get(ProjectionNode(table.from_table, row.values[0]))
                    target = offsets.get(ProjectionNode(table.to_table, row.values[1]))
                    if source is None or target is None:
                        raise GrafxCorruptionDetected("Projection edge has no visible endpoint.")
                    edges.append(ProjectionEdge(table.name, row.record_id, source, target))
            cursor = page.next_cursor
            if cursor is None:
                break
    work.step(len(nodes) + len(edges))
    if control is not None:
        control.check()
    return GraphProjection(database.identity.database_uuid, reader.snapshot.read_lsn,
                           tuple(nodes), tuple(edges), limits, logical_bytes,
                           ProjectionDiagnostics(scan_calls, len(nodes) + len(edges), maximum_batch, work.used))
