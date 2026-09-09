"""Pure, bounded algorithms over detached pictures; never storage authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections import deque
import heapq
import math
from typing import TYPE_CHECKING

from okto_grafx.domain.query.control import CancellationToken
from okto_grafx.errors import GrafxConfigurationError

if TYPE_CHECKING:
    from okto_grafx.projections import GraphProjection, ProjectionNode, ProjectionEdge

__all__ = ["ProjectionAdjacency", "ProjectionPath", "PageRankResult"]


@dataclass(frozen=True, slots=True)
class ProjectionPath:
    """One shortest path within the requested direction/depth, preserving edge identity."""

    found: bool
    nodes: tuple[ProjectionNode, ...]
    edges: tuple[ProjectionEdge, ...]


@dataclass(frozen=True, slots=True)
class PageRankResult:
    """Scores aligned with projection nodes and explicit convergence status."""

    scores: tuple[float, ...]
    iterations: int
    converged: bool
    residual: float


@dataclass(frozen=True, slots=True)
class ProjectionAdjacency:
    """Immutable CSR offsets and physical edge positions, in both directions."""

    out_offsets: tuple[int, ...]
    out_edges: tuple[int, ...]
    in_offsets: tuple[int, ...]
    in_edges: tuple[int, ...]
    logical_bytes: int


def _control(graph, cancellation, *, edge_workspace=0):
    from okto_grafx.projections import _Work, _bound
    work = _Work(graph.limits.max_work, cancellation)
    _bound("projection_memory", graph.logical_bytes + 4096 + 1024 * len(graph.nodes)
           + edge_workspace * len(graph.edges), graph.limits.max_memory_bytes)
    return work


def _adjacent(graph, work):
    from okto_grafx.projections import _bound
    if graph.adjacency is not None:
        return graph
    n, m = len(graph.nodes), len(graph.edges)
    charge = 4096 + 256 * (n + m)
    _bound("projection_memory", graph.logical_bytes + charge + 4096 + 1024 * n,
           graph.limits.max_memory_bytes)
    work.step(n)
    outgoing, incoming = [0] * (n + 1), [0] * (n + 1)
    for edge in graph.edges:
        work.step()
        outgoing[edge.source + 1] += 1
        incoming[edge.target + 1] += 1
    for i in range(n):
        work.step()
        outgoing[i + 1] += outgoing[i]
        incoming[i + 1] += incoming[i]
    out_cursor, in_cursor = outgoing.copy(), incoming.copy()
    out_edges, in_edges = [0] * m, [0] * m
    for index, edge in enumerate(graph.edges):
        work.step()
        out_edges[out_cursor[edge.source]] = index
        in_edges[in_cursor[edge.target]] = index
        out_cursor[edge.source] += 1
        in_cursor[edge.target] += 1
    work.step(n + m)
    adjacency = ProjectionAdjacency(tuple(outgoing), tuple(out_edges), tuple(incoming), tuple(in_edges), charge)
    return replace(graph, adjacency=adjacency, logical_bytes=graph.logical_bytes + charge)


def _neighbors(graph, node, direction, work):
    adjacency = graph.adjacency
    for outgoing in ((True, False) if direction == "both" else (direction == "out",)):
        offsets = adjacency.out_offsets if outgoing else adjacency.in_offsets
        entries = adjacency.out_edges if outgoing else adjacency.in_edges
        for position in range(offsets[node], offsets[node + 1]):
            work.step()
            index = entries[position]
            edge = graph.edges[index]
            yield (edge.target if outgoing else edge.source), index


def _direction(value):
    if type(value) is not str or value not in ("out", "in", "both"):
        raise GrafxConfigurationError("direction must be out, in or both.", field="direction")


def _with_adjacency(graph: GraphProjection, cancellation: CancellationToken | None) -> GraphProjection:
    return _adjacent(graph, _control(graph, cancellation))


def _strong_components(graph: GraphProjection, cancellation: CancellationToken | None) -> tuple[ProjectionNode, ...]:
    work = _control(graph, cancellation)
    graph = _adjacent(graph, work)
    work.step(len(graph.nodes))
    seen, order = set(), []
    for start in range(len(graph.nodes)):
        work.step()
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, iter(_neighbors(graph, start, "out", work)))]
        while stack:
            work.step()
            node, neighbors = stack[-1]
            following = next(neighbors, None)
            if following is None:
                order.append(node)
                stack.pop()
            elif following[0] not in seen:
                seen.add(following[0])
                stack.append((following[0], iter(_neighbors(graph, following[0], "out", work))))
    seen.clear()
    labels = list(graph.nodes)
    for start in reversed(order):
        work.step()
        if start in seen:
            continue
        component, stack = [], [start]
        seen.add(start)
        while stack:
            work.step()
            node = stack.pop()
            component.append(node)
            for neighbor, _edge in _neighbors(graph, node, "in", work):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        label = min(graph.nodes[i] for i in component)
        for node in component:
            work.step()
            labels[node] = label
    return tuple(labels)


def _positive(name, value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**31:
        raise GrafxConfigurationError("Invalid bounded algorithm option.", field=name)


def _bfs(graph, source, target, direction, max_depth, max_results, cancellation):
    from okto_grafx.projections import ProjectionNode, _bound
    _direction(direction)
    _positive("max_results", max_results)
    if max_depth is not None:
        _positive("max_depth", max_depth, zero=True)
    work = _control(graph, cancellation)
    work.step(len(graph.nodes))
    if type(source) is not ProjectionNode or (target is not None and type(target) is not ProjectionNode):
        raise GrafxConfigurationError("Source and target must be projection node identities.", field="node")
    try:
        start = graph.nodes.index(source)
        end = None if target is None else graph.nodes.index(target)
    except ValueError as failure:
        raise GrafxConfigurationError("Node does not belong to this projection.", field="node") from failure
    graph = _adjacent(graph, work)
    parents = {start: (start, -1)}
    pending = deque([(start, 0)])
    order = []
    while pending:
        work.step()
        node, depth = pending.popleft()
        order.append(node)
        if node == end:
            break
        if max_depth is not None and depth == max_depth:
            continue
        for neighbor, edge in _neighbors(graph, node, direction, work):
            if neighbor not in parents:
                _bound("projection_results", len(parents) + 1, max_results)
                parents[neighbor] = node, edge
                pending.append((neighbor, depth + 1))
    if target is None:
        work.step(len(order))
        return tuple(graph.nodes[i] for i in order)
    if end not in parents:
        return ProjectionPath(False, (), ())
    nodes, edges, current = [], [], end
    while True:
        work.step()
        nodes.append(graph.nodes[current])
        previous, edge = parents[current]
        if current == start:
            break
        edges.append(graph.edges[edge])
        current = previous
    return ProjectionPath(True, tuple(reversed(nodes)), tuple(reversed(edges)))


def _pagerank(graph, damping, tolerance, max_iterations, cancellation):
    _positive("max_iterations", max_iterations)
    if max_iterations > 1_000_000:
        raise GrafxConfigurationError("max_iterations must be <=1000000.", field="max_iterations")
    if type(damping) not in (float, int) or not 0 < damping < 1:
        raise GrafxConfigurationError("damping must be finite and between zero and one.", field="damping")
    if type(tolerance) not in (float, int) or not 0 < tolerance <= 1 or not math.isfinite(tolerance):
        raise GrafxConfigurationError("tolerance must be finite in (0,1].", field="tolerance")
    work = _control(graph, cancellation)
    graph = _adjacent(graph, work)
    n = len(graph.nodes)
    if not n:
        return PageRankResult((), 0, True, 0.0)
    work.step(n)
    ranks = [1.0 / n] * n
    offsets = graph.adjacency.out_offsets
    for iteration in range(1, max_iterations + 1):
        work.step(n)
        dangling = sum(ranks[i] for i in range(n) if offsets[i] == offsets[i + 1])
        updated = [(1.0 - damping + damping * dangling) / n] * n
        for node in range(n):
            work.step()
            degree = offsets[node + 1] - offsets[node]
            if degree:
                share = damping * ranks[node] / degree
                for target, _edge in _neighbors(graph, node, "out", work):
                    updated[target] += share
        work.step(n)
        residual = sum(abs(a - b) for a, b in zip(updated, ranks))
        ranks = updated
        if residual <= tolerance:
            return PageRankResult(tuple(ranks), iteration, True, residual)
    return PageRankResult(tuple(ranks), max_iterations, False, residual)


def _k_core(graph, cancellation):
    work = _control(graph, cancellation, edge_workspace=512)
    # Reserve simultaneous simple-neighbor sets, stale heap pairs and output.
    from okto_grafx.projections import _bound
    _bound("projection_memory", graph.logical_bytes + 4096 + 1024 * len(graph.nodes)
           + 512 * len(graph.edges), graph.limits.max_memory_bytes)
    work.step(len(graph.nodes))
    neighbors = [set() for _ in graph.nodes]
    for edge in graph.edges:
        work.step()
        if edge.source != edge.target:
            neighbors[edge.source].add(edge.target)
            neighbors[edge.target].add(edge.source)
    degrees = [len(row) for row in neighbors]
    queue = [(degree, i) for i, degree in enumerate(degrees)]
    heapq.heapify(queue)
    removed = set()
    result = [0] * len(degrees)
    while queue:
        work.step()
        degree, node = heapq.heappop(queue)
        if node in removed or degree != degrees[node]:
            continue
        removed.add(node)
        result[node] = degree
        for neighbor in neighbors[node]:
            work.step()
            if neighbor not in removed and degrees[neighbor] > degree:
                degrees[neighbor] -= 1
                heapq.heappush(queue, (degrees[neighbor], neighbor))
    return tuple(result)
