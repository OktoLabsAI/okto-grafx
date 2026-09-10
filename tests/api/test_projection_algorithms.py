"""Independent small-graph oracles and hostile bounded algorithm inputs."""

from dataclasses import replace
import pytest

from okto_grafx import CancellationToken
from okto_grafx.projections import GraphProjection, ProjectionNode, ProjectionEdge, ProjectionLimits
from okto_grafx.errors import GrafxQueryBudgetExceeded, GrafxQueryCancelled


def picture(n, pairs):
    return GraphProjection(b"x" * 16, 1, tuple(ProjectionNode("N", i) for i in range(n)),
                           tuple(ProjectionEdge("R", i, a, b) for i, (a, b) in enumerate(pairs)),
                           ProjectionLimits(), 4096 + 1024 * n + 512 * len(pairs))


def test_adjacency_parallel_loop_and_scc_independent_oracle():
    pairs = [(0, 1), (0, 1), (1, 0), (1, 2), (2, 2), (2, 3), (3, 2), (4, 3)]
    original = picture(6, pairs)
    graph = original.with_adjacency()
    assert original.adjacency is None
    assert graph.with_adjacency() is graph
    assert graph.nodes is original.nodes and graph.edges is original.edges
    assert sorted(graph.adjacency.out_edges) == list(range(len(pairs)))
    for direction in ("in", "out", "total"):
        assert graph.degrees(direction=direction) == original.degrees(direction=direction)
    reachable = [[i == j or (i, j) in pairs for j in range(6)] for i in range(6)]
    for k in range(6):
        for i in range(6):
            for j in range(6):
                reachable[i][j] |= reachable[i][k] and reachable[k][j]
    expected = tuple(min(graph.nodes[j] for j in range(6) if reachable[i][j] and reachable[j][i]) for i in range(6))
    assert graph.strongly_connected_components() == expected
    assert original.strongly_connected_components() == expected


def test_scc_iterative_deep_chain_and_budgets():
    graph = picture(1500, [(i, i + 1) for i in range(1499)])
    assert graph.strongly_connected_components() == graph.nodes
    for limits in (ProjectionLimits(max_work=1), ProjectionLimits(max_memory_bytes=graph.logical_bytes)):
        bounded = replace(graph, limits=limits)
        for operation in (bounded.with_adjacency, bounded.strongly_connected_components):
            with pytest.raises(GrafxQueryBudgetExceeded):
                operation()
    token = CancellationToken()
    token.cancel()
    with pytest.raises(GrafxQueryCancelled):
        graph.strongly_connected_components(cancellation=token)
    assert picture(0, []).strongly_connected_components() == ()


def test_bfs_paths_depth_direction_and_parallel_ties():
    graph = picture(5, [(0, 1), (0, 1), (1, 2), (0, 3), (3, 2)])
    a, b, c, d, isolated = graph.nodes
    assert graph.reachable(a) == (a, b, d, c)
    assert graph.reachable(a, max_depth=0) == (a,)
    assert graph.reachable(c, direction="in") == (c, b, d, a)
    path = graph.shortest_path(a, c)
    assert path.nodes == (a, b, c) and path.edges == (graph.edges[0], graph.edges[2])
    assert graph.shortest_path(a, a).found
    assert not graph.shortest_path(a, isolated).found
    assert not graph.shortest_path(a, c, max_depth=1).found
    assert graph.shortest_path(c, a, direction="both").found
    with pytest.raises(GrafxQueryBudgetExceeded):
        graph.reachable(a, max_results=2)


def rank_oracle(n, pairs, damping):
    # Solve the stationary linear system, independently of power iteration.
    matrix = [[float(i == j) for j in range(n)] + [(1 - damping) / n] for i in range(n)]
    for source in range(n):
        targets = [b for a, b in pairs if a == source]
        for target in (targets or list(range(n))):
            matrix[target][source] -= damping / (len(targets) or n)
    for column in range(n):
        pivot = matrix[column][column]
        matrix[column] = [v / pivot for v in matrix[column]]
        for row in range(n):
            if row != column:
                ratio = matrix[row][column]
                matrix[row] = [a - ratio * b for a, b in zip(matrix[row], matrix[column])]
    return tuple(row[-1] for row in matrix)


def test_pagerank_independent_linear_system_and_nonconvergence():
    pairs = [(0, 1), (0, 1), (0, 2), (1, 1)]
    graph = picture(4, pairs)
    result = graph.pagerank(tolerance=1e-12, max_iterations=1000)
    assert result.converged and result.residual <= 1e-12
    assert result.scores == pytest.approx(rank_oracle(4, pairs, .85), abs=1e-10)
    assert sum(result.scores) == pytest.approx(1.0)
    limited = graph.pagerank(max_iterations=1, tolerance=1e-15)
    assert limited.iterations == 1 and not limited.converged
    assert picture(0, []).pagerank().scores == ()


def test_kcore_independent_peeling_oracle():
    import random
    randomizer = random.Random(37)
    for _ in range(12):
        pairs = [(randomizer.randrange(7), randomizer.randrange(7)) for _ in range(22)]
        graph = picture(7, pairs)
        expected = [0] * 7
        for k in range(1, 7):
            retained = set(range(7))
            while True:
                rejected = {i for i in retained if len({b if a == i else a for a, b in pairs
                    if a != b and (a == i or b == i) and a in retained and b in retained}) < k}
                if not rejected:
                    break
                retained -= rejected
            for node in retained:
                expected[node] = k
        assert graph.k_core() == tuple(expected)
    assert picture(1, [(0, 0), (0, 0)]).k_core() == (0,)


def test_new_algorithm_limits_cancellation_and_invalid_arguments():
    from okto_grafx.errors import GrafxConfigurationError
    graph = picture(3, [(0, 1), (1, 2)])
    token = CancellationToken()
    token.cancel()
    for bounded in (replace(graph, limits=ProjectionLimits(max_work=1)),
                    replace(graph, limits=ProjectionLimits(max_memory_bytes=graph.logical_bytes))):
        for operation in (bounded.pagerank, bounded.k_core, lambda: bounded.reachable(graph.nodes[0])):
            with pytest.raises(GrafxQueryBudgetExceeded):
                operation()
    for operation in (graph.pagerank, graph.k_core, lambda **kw: graph.reachable(graph.nodes[0], **kw)):
        with pytest.raises(GrafxQueryCancelled):
            operation(cancellation=token)
    for kw in ({"damping": True}, {"damping": float("nan")}, {"tolerance": 0}, {"max_iterations": 0}):
        with pytest.raises(GrafxConfigurationError):
            graph.pagerank(**kw)
    for kw in ({"direction": "invalid"}, {"max_depth": -1}, {"max_results": False}):
        with pytest.raises(GrafxConfigurationError):
            graph.reachable(graph.nodes[0], **kw)
