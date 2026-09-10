"""Pure, bounded algorithms over detached pictures; never storage authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections import deque
from collections.abc import Mapping
from types import MappingProxyType
import heapq
import math
from typing import TYPE_CHECKING

from okto_grafx.domain.query.control import CancellationToken
from okto_grafx.errors import GrafxConfigurationError

if TYPE_CHECKING:
    from okto_grafx.projections import GraphProjection, ProjectionNode, ProjectionEdge

__all__ = ["ProjectionLookup", "ProjectionAdjacency", "ProjectionPath", "WeightedProjectionPath", "PageRankResult",
           "PageRankPreparation", "SimpleTopology", "LabelPropagationResult", "TopologicalOrderResult"]


@dataclass(frozen=True, slots=True)
class TopologicalOrderResult:
    """Typed DAG/cycle result. Blocked includes cycle descendants, not just cycle members."""
    order: tuple[ProjectionNode, ...]
    acyclic: bool
    blocked: tuple[ProjectionNode, ...]


def _topological_order(graph, cancellation):
    work = _control(graph, cancellation, edge_workspace=128)
    n = len(graph.nodes)
    work.step(n)
    incoming = [0] * n
    outgoing = [[] for _ in range(n)]
    for edge in graph.edges:
        work.step()
        if not (type(edge.source) is int and type(edge.target) is int
                and 0 <= edge.source < n and 0 <= edge.target < n):
            raise GrafxConfigurationError("Invalid projection endpoint.", field="edges")
        incoming[edge.target] += 1
        outgoing[edge.source].append(edge.target)
    # FIFO ready queue: deterministic capture order without an O(log N) priority queue.
    ready = deque(i for i, count in enumerate(incoming) if count == 0)
    order = []
    while ready:
        work.step()
        node = ready.popleft()
        order.append(graph.nodes[node])
        for target in outgoing[node]:
            work.step()
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    work.step(n)
    blocked = tuple(graph.nodes[i] for i, count in enumerate(incoming) if count)
    return TopologicalOrderResult(tuple(order), not blocked, blocked)


@dataclass(frozen=True, slots=True)
class PageRankPreparation:
    """Immutable transition data for one projection, backend and weight mode; no rank state."""

    backend: str
    weighted: bool
    sources: tuple[int, ...]
    targets: tuple[int, ...]
    shares: tuple[float, ...]
    dangling: tuple[int, ...]
    numeric_buffers: tuple[bytes, ...] | None
    logical_bytes: int


@dataclass(frozen=True, slots=True)
class SimpleTopology:
    """Retained loop-free undirected neighbors; physical projection edges remain unchanged."""

    neighbors: tuple[tuple[int, ...], ...]
    logical_bytes: int


@dataclass(frozen=True, slots=True)
class LabelPropagationResult:
    """Labels aligned with nodes; convergence means one whole sweep without changes."""

    labels: tuple[ProjectionNode, ...]
    iterations: int
    converged: bool


def _simple(graph, work):
    from okto_grafx.projections import _bound
    if graph.simple_topology is not None:
        return graph
    n, m = len(graph.nodes), len(graph.edges)
    charge = 4096 + 256 * n + 256 * m
    _bound("projection_memory", graph.logical_bytes + charge + 4096 + 1024 * n + 512 * m,
           graph.limits.max_memory_bytes)
    work.step(n)
    neighbors = [dict() for _ in range(n)]
    for edge in graph.edges:
        work.step()
        if edge.source != edge.target:
            neighbors[edge.source][edge.target] = None
            neighbors[edge.target][edge.source] = None
    # Preserve physical encounter order without a sorting/logarithmic construction cost.
    for row in neighbors:
        work.step(1 + len(row))
    topology = SimpleTopology(tuple(tuple(row) for row in neighbors), charge)
    return replace(graph, simple_topology=topology, logical_bytes=graph.logical_bytes + charge)


def _with_simple(graph, cancellation):
    return _simple(graph, _control(graph, cancellation))


def _label_propagation(graph, max_iterations, cancellation):
    _positive("max_iterations", max_iterations)
    if max_iterations > 1_000_000:
        raise GrafxConfigurationError("max_iterations must be <=1000000.", field="max_iterations")
    work = _control(graph, cancellation)
    graph = _simple(graph, work)
    from okto_grafx.projections import _bound
    _bound("projection_memory", graph.logical_bytes + 4096 + 1024 * len(graph.nodes), graph.limits.max_memory_bytes)
    work.step(len(graph.nodes))
    labels = list(graph.nodes)
    if not labels:
        return LabelPropagationResult((), 0, True)
    for iteration in range(1, max_iterations + 1):
        changed = False
        for node, neighbors in enumerate(graph.simple_topology.neighbors):
            work.step()
            votes = {}
            for neighbor in neighbors:
                work.step()
                label = labels[neighbor]
                votes[label] = votes.get(label, 0) + 1
            if votes:
                work.step(len(votes))
                winner = min(votes, key=lambda label: (-votes[label], label))
                changed |= labels[node] != winner
                labels[node] = winner
        if not changed:
            work.step(len(labels))
            return LabelPropagationResult(tuple(labels), iteration, True)
    work.step(len(labels))
    return LabelPropagationResult(tuple(labels), max_iterations, False)


@dataclass(frozen=True, slots=True)
class WeightedProjectionPath:
    """Minimum non-negative cost path, or an explicit unreachable result."""

    found: bool
    distance: float | None
    nodes: tuple[ProjectionNode, ...]
    edges: tuple[ProjectionEdge, ...]


@dataclass(frozen=True, slots=True)
class ProjectionLookup:
    """Read-only node identity lookup retained by one detached picture."""

    positions: Mapping[ProjectionNode, int]
    logical_bytes: int


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


def _control(graph, cancellation, *, edge_workspace=0, node_workspace=None):
    from okto_grafx.projections import _Work, _bound
    work = _Work(graph.limits.max_work, cancellation)
    count = len(graph.nodes) if node_workspace is None else node_workspace
    _bound("projection_memory", graph.logical_bytes + 4096 + 1024 * count
           + edge_workspace * len(graph.edges), graph.limits.max_memory_bytes)
    return work


def _indexed(graph, work):
    from okto_grafx.projections import _bound
    if graph.lookup is not None:
        return graph
    charge = 4096 + 256 * len(graph.nodes)
    _bound("projection_memory", graph.logical_bytes + charge, graph.limits.max_memory_bytes)
    positions = {}
    for index, node in enumerate(graph.nodes):
        work.step()
        if node in positions:
            raise GrafxConfigurationError("Projection node identities must be unique.", field="nodes")
        positions[node] = index
    return replace(graph, lookup=ProjectionLookup(MappingProxyType(positions), charge),
                   logical_bytes=graph.logical_bytes + charge)


def _with_lookup(graph, cancellation):
    return _indexed(graph, _control(graph, cancellation, node_workspace=0))


def _adjacent(graph, work):
    from okto_grafx.projections import _bound
    graph = _indexed(graph, work)
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


def _weight(value, field="weight"):
    try:
        result = float(value) if type(value) in (int, float) else float("nan")
    except OverflowError as failure:
        raise GrafxConfigurationError("Weight exceeds finite numeric range.", field=field) from failure
    if not math.isfinite(result) or result < 0:
        raise GrafxConfigurationError("Weight must be finite and non-negative.", field=field)
    return result


def _bfs(graph, source, target, direction, max_depth, max_results, cancellation):
    from okto_grafx.projections import ProjectionNode, _bound
    _direction(direction)
    _positive("max_results", max_results)
    if max_depth is not None:
        _positive("max_depth", max_depth, zero=True)
    work = _control(graph, cancellation, node_workspace=0)
    if type(source) is not ProjectionNode or (target is not None and type(target) is not ProjectionNode):
        raise GrafxConfigurationError("Source and target must be projection node identities.", field="node")
    graph = _indexed(graph, work)
    try:
        start = graph.lookup.positions[source]
        end = None if target is None else graph.lookup.positions[target]
    except KeyError as failure:
        raise GrafxConfigurationError("Node does not belong to this projection.", field="node") from failure
    graph = _adjacent(graph, work)
    _bound("projection_memory", graph.logical_bytes + 4096 + 1024, graph.limits.max_memory_bytes)
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
                _bound("projection_memory", graph.logical_bytes + 4096 + 1024 * (len(parents) + 1),
                       graph.limits.max_memory_bytes)
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


def _weighted_path(graph, source, target, direction, max_results, max_distance, cancellation):
    from okto_grafx.projections import ProjectionNode, _bound
    _direction(direction)
    _positive("max_results", max_results)
    if max_distance is not None:
        max_distance = _weight(max_distance, "max_distance")
    if graph.weights is None:
        raise GrafxConfigurationError("Weighted paths require captured relationship weights.", field="weights")
    if type(source) is not ProjectionNode or type(target) is not ProjectionNode:
        raise GrafxConfigurationError("Source and target must be projection node identities.", field="node")
    work = _control(graph, cancellation, node_workspace=0)
    graph = _adjacent(graph, work)
    try:
        start, end = graph.lookup.positions[source], graph.lookup.positions[target]
    except KeyError as failure:
        raise GrafxConfigurationError("Node does not belong to this projection.", field="node") from failure
    _bound("projection_memory", graph.logical_bytes + 4096 + 1024 + 96, graph.limits.max_memory_bytes)
    best, parents, settled = {start: 0.0}, {start: (start, -1)}, set()
    queue, serial = [(0.0, 0, start)], 0
    while queue:
        work.step()
        distance, _serial, node = heapq.heappop(queue)
        if node in settled or distance != best[node]:
            continue
        settled.add(node)
        if node == end:
            nodes, edges = [], []
            current = node
            while True:
                work.step()
                nodes.append(graph.nodes[current])
                previous, edge = parents[current]
                if current == start:
                    break
                edges.append(graph.edges[edge])
                current = previous
            return WeightedProjectionPath(True, distance, tuple(reversed(nodes)), tuple(reversed(edges)))
        for neighbor, edge in _neighbors(graph, node, direction, work):
            if neighbor in settled:
                continue
            candidate = distance + graph.weights[edge]
            if not math.isfinite(candidate):
                raise GrafxConfigurationError("Path distance exceeds finite numeric range.", field="distance")
            if max_distance is not None and candidate > max_distance:
                continue
            if candidate < best.get(neighbor, float("inf")):
                count = len(best) + (neighbor not in best)
                _bound("projection_results", count, max_results)
                _bound("projection_memory", graph.logical_bytes + 4096 + 1024 * count + 96 * (len(queue) + 1),
                       graph.limits.max_memory_bytes)
                best[neighbor], parents[neighbor] = candidate, (node, edge)
                serial += 1
                heapq.heappush(queue, (candidate, serial, neighbor))
    return WeightedProjectionPath(False, None, (), ())


def _prepare_rank(graph, work, backend, weighted):
    from okto_grafx.projections import _bound
    if type(backend) is not str or backend not in ("python", "numpy"):
        raise GrafxConfigurationError("backend must be python or numpy.", field="backend")
    if type(weighted) is not bool or (weighted and graph.weights is None):
        raise GrafxConfigurationError("weighted requires captured weights and must be boolean.", field="weighted")
    old = graph.pagerank_preparation
    if old is not None and (old.backend, old.weighted) == (backend, weighted):
        return graph
    graph = _adjacent(graph, work)
    n, m = len(graph.nodes), len(graph.edges)
    charge = 4096 + 256 * n + 512 * m
    _bound("projection_memory", graph.logical_bytes + charge + 4096 + 1024 * n + 512 * m,
           graph.limits.max_memory_bytes)
    offsets = graph.adjacency.out_offsets
    work.step(n + m)
    shares = [0.0] * m
    dangling = []
    for node in range(n):
        work.step()
        start, end = offsets[node:node + 2]
        if weighted:
            entries = graph.adjacency.out_edges[start:end]
            work.step(3 * len(entries))
            maximum = max((graph.weights[i] for i in entries), default=0.0)
            if maximum:
                total = math.fsum(graph.weights[i] / maximum for i in entries)
                for i in entries:
                    shares[i] = graph.weights[i] / maximum / total
            else:
                dangling.append(node)
        elif start == end:
            dangling.append(node)
        else:
            for pos in range(start, end):
                work.step()
                shares[graph.adjacency.out_edges[pos]] = 1.0 / (end - start)
    work.step(m + n)
    sources = tuple(e.source for e in graph.edges)
    targets = tuple(e.target for e in graph.edges)
    shares, dangling = tuple(shares), tuple(dangling)
    buffers = None
    if backend == "numpy":
        from okto_grafx.adapters.numpy_projection import prepare_numpy
        buffers = prepare_numpy(sources, targets, shares, dangling)
        work.step(0)
    prepared = PageRankPreparation(backend, weighted, sources, targets, shares, dangling, buffers, charge)
    return replace(graph, pagerank_preparation=prepared,
                   logical_bytes=graph.logical_bytes + charge - (old.logical_bytes if old else 0))


def _with_pagerank(graph, backend, weighted, cancellation):
    return _prepare_rank(graph, _control(graph, cancellation), backend, weighted)


def _pagerank(graph, damping, tolerance, max_iterations, cancellation, backend="python",
              weighted=False, personalization=None):
    _positive("max_iterations", max_iterations)
    if max_iterations > 1_000_000:
        raise GrafxConfigurationError("max_iterations must be <=1000000.", field="max_iterations")
    if type(damping) not in (float, int) or not 0 < damping < 1:
        raise GrafxConfigurationError("damping must be finite and between zero and one.", field="damping")
    if type(tolerance) not in (float, int) or not 0 < tolerance <= 1 or not math.isfinite(tolerance):
        raise GrafxConfigurationError("tolerance must be finite in (0,1].", field="tolerance")
    if type(backend) is not str or backend not in ("python", "numpy"):
        raise GrafxConfigurationError("backend must be python or numpy.", field="backend")
    if type(weighted) is not bool or (weighted and graph.weights is None):
        raise GrafxConfigurationError("weighted requires captured weights and must be boolean.", field="weighted")
    work = _control(graph, cancellation, edge_workspace=512 if backend == "numpy" or weighted else 0)
    graph = _adjacent(graph, work)
    n = len(graph.nodes)
    from okto_grafx.projections import ProjectionNode, _bound
    _bound("projection_memory", graph.logical_bytes + 4096 + 1024 * n
           + (512 * len(graph.edges) if backend == "numpy" or weighted else 0), graph.limits.max_memory_bytes)
    work.step(n)
    seed = [1.0 / n] * n if n else []
    if personalization is not None:
        if type(personalization) is not dict or not personalization or len(personalization) > n:
            raise GrafxConfigurationError("personalization must be a nonempty bounded node-weight dictionary.", field="personalization")
        seed = [0.0] * n
        for node, value in personalization.items():
            work.step()
            if type(node) is not ProjectionNode or node not in graph.lookup.positions:
                raise GrafxConfigurationError("Personalization node is not in this projection.", field="personalization")
            seed[graph.lookup.positions[node]] = _weight(value, "personalization")
        maximum = max(seed)
        if maximum == 0:
            raise GrafxConfigurationError("Personalization needs positive total mass.", field="personalization")
        total = math.fsum(value / maximum for value in seed)
        seed = [value / maximum / total for value in seed]
    offsets = graph.adjacency.out_offsets
    prepared = graph.pagerank_preparation
    if prepared is not None and (prepared.backend, prepared.weighted) == (backend, weighted):
        shares = prepared.shares if weighted else None
        dangling = prepared.dangling
    elif weighted:
        work.step(len(graph.edges))
        shares = [0.0] * len(graph.edges)
        for node in range(n):
            work.step()
            entries = graph.adjacency.out_edges[offsets[node]:offsets[node + 1]]
            maximum = max((graph.weights[i] for i in entries), default=0.0)
            if maximum:
                total = math.fsum(graph.weights[i] / maximum for i in entries)
                for i in entries:
                    work.step()
                    shares[i] = graph.weights[i] / maximum / total
        outgoing = [False] * n
        for index, edge in enumerate(graph.edges):
            work.step()
            if shares[index] > 0:
                outgoing[edge.source] = True
        dangling = tuple(i for i, value in enumerate(outgoing) if not value)
    else:
        shares = None
        dangling = tuple(i for i in range(n) if offsets[i] == offsets[i + 1])
    if backend == "numpy":
        from okto_grafx.adapters.numpy_projection import pagerank_numpy
        _bound("projection_memory", graph.logical_bytes + 4096 + 1024 * n + 512 * len(graph.edges),
               graph.limits.max_memory_bytes)
        offsets = graph.adjacency.out_offsets
        work.step(n + len(graph.edges))
        if prepared is not None and (prepared.backend, prepared.weighted) == (backend, weighted):
            result = pagerank_numpy(n, prepared.sources, prepared.targets, prepared.shares,
                tuple(seed), dangling, damping, tolerance, max_iterations, work.step,
                prepared=prepared.numeric_buffers)
            return PageRankResult(*result)
        result = pagerank_numpy(n, tuple(e.source for e in graph.edges), tuple(e.target for e in graph.edges),
            tuple(shares) if shares is not None else tuple(1.0 / (offsets[e.source + 1] - offsets[e.source]) for e in graph.edges),
            tuple(seed), dangling,
            damping, tolerance, max_iterations, work.step)
        return PageRankResult(*result)
    if not n:
        return PageRankResult((), 0, True, 0.0)
    work.step(n)
    ranks = [1.0 / n] * n
    offsets = graph.adjacency.out_offsets
    for iteration in range(1, max_iterations + 1):
        work.step(n)
        mass = sum(ranks[i] for i in dangling)
        updated = [(1.0 - damping + damping * mass) * value for value in seed]
        for node in range(n):
            work.step()
            degree = offsets[node + 1] - offsets[node]
            if degree:
                share = damping * ranks[node] / degree
                for target, edge in _neighbors(graph, node, "out", work):
                    updated[target] += share if shares is None else damping * ranks[node] * shares[edge]
        work.step(n)
        residual = sum(abs(a - b) for a, b in zip(updated, ranks))
        ranks = updated
        if residual <= tolerance:
            return PageRankResult(tuple(ranks), iteration, True, residual)
    return PageRankResult(tuple(ranks), max_iterations, False, residual)


def _k_core(graph, cancellation):
    work = _control(graph, cancellation, edge_workspace=512)
    # Reserve simultaneous simple-neighbor sets, bucket arrays and output.
    from okto_grafx.projections import _bound
    _bound("projection_memory", graph.logical_bytes + 4096 + 1024 * len(graph.nodes)
           + 512 * len(graph.edges), graph.limits.max_memory_bytes)
    work.step(len(graph.nodes))
    if graph.simple_topology is not None:
        neighbors = graph.simple_topology.neighbors
    else:
        neighbors = [set() for _ in graph.nodes]
        for edge in graph.edges:
            work.step()
            if edge.source != edge.target:
                neighbors[edge.source].add(edge.target)
                neighbors[edge.target].add(edge.source)
    degrees = [len(row) for row in neighbors]
    bins = [0] * (max(degrees, default=0) + 1)
    for degree in degrees:
        work.step()
        bins[degree] += 1
    start = 0
    for degree in range(len(bins)):
        work.step()
        size = bins[degree]
        bins[degree] = start
        start += size
    positions, vertices = [0] * len(degrees), [0] * len(degrees)
    for node, degree in enumerate(degrees):
        work.step()
        positions[node] = bins[degree]
        vertices[positions[node]] = node
        bins[degree] += 1
    for degree in range(len(bins) - 1, 0, -1):
        bins[degree] = bins[degree - 1]
    bins[0] = 0
    for offset in range(len(vertices)):
        work.step()
        node = vertices[offset]
        for neighbor in neighbors[node]:
            work.step()
            if degrees[neighbor] > degrees[node]:
                degree = degrees[neighbor]
                position = positions[neighbor]
                boundary = bins[degree]
                other = vertices[boundary]
                if neighbor != other:
                    positions[neighbor], positions[other] = boundary, position
                    vertices[position], vertices[boundary] = other, neighbor
                bins[degree] += 1
                degrees[neighbor] -= 1
    return tuple(degrees)
