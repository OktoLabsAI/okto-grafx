"""Weighted graph contracts, independent oracles and failure controls."""

from dataclasses import replace
import random
import pytest

from okto_grafx import CancellationToken
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded, GrafxQueryCancelled
from okto_grafx.projections import ProjectionLimits, ProjectionNode
from tests.api.test_projection_algorithms import picture


def test_dijkstra_matches_bellman_ford_and_preserves_ties():
    randomizer = random.Random(4)
    for _ in range(12):
        pairs = [(randomizer.randrange(8), randomizer.randrange(8)) for _ in range(30)]
        costs = tuple(float(randomizer.randrange(5)) for _ in pairs)
        graph = replace(picture(8, pairs), weights=costs).with_adjacency()
        for direction in ("out", "in", "both"):
            links = list(zip(pairs, costs))
            if direction == "in":
                links = [((b, a), w) for (a, b), w in links]
            elif direction == "both":
                links += [((b, a), w) for (a, b), w in links]
            expected = [float("inf")] * 8
            expected[0] = 0.0
            for _ in range(7):
                for (a, b), weight in links:
                    expected[b] = min(expected[b], expected[a] + weight)
            for node in range(8):
                result = graph.weighted_shortest_path(graph.nodes[0], graph.nodes[node], direction=direction)
                assert result.distance == (None if expected[node] == float("inf") else expected[node])
                if result.found:
                    assert result.nodes[0] == graph.nodes[0] and result.nodes[-1] == graph.nodes[node]
                    assert sum(costs[edge.record_id] for edge in result.edges) == result.distance
    graph = replace(picture(3, [(0, 1), (0, 1), (1, 2), (0, 2)]), weights=(0., 0., 1., 1.))
    assert graph.weighted_shortest_path(graph.nodes[0], graph.nodes[1]).edges == (graph.edges[0],)
    assert not graph.weighted_shortest_path(graph.nodes[0], graph.nodes[2], max_distance=0).found


def weighted_rank_oracle(n, pairs, weights, seed, damping=.85):
    matrix = [[float(i == j) for j in range(n)] + [(1 - damping) * seed[i]] for i in range(n)]
    for source in range(n):
        total = sum(w for (a, _), w in zip(pairs, weights) if a == source)
        for target in range(n):
            probability = sum(w for (a, b), w in zip(pairs, weights) if a == source and b == target) / total if total else seed[target]
            matrix[target][source] -= damping * probability
    for col in range(n):
        pivot = matrix[col][col]
        matrix[col] = [value / pivot for value in matrix[col]]
        for row in range(n):
            if row != col:
                ratio = matrix[row][col]
                matrix[row] = [a - ratio * b for a, b in zip(matrix[row], matrix[col])]
    return tuple(row[-1] for row in matrix)


@pytest.mark.parametrize("backend", ["python", pytest.param("numpy", marks=pytest.mark.optional_dependency("numpy"))])
def test_weighted_personalized_rank_against_linear_system(backend):
    if backend == "numpy":
        pytest.importorskip("numpy")
    pairs, weights = [(0, 1), (0, 1), (0, 2), (1, 1), (2, 3)], (1., 2., 3., 0., 2.)
    graph = replace(picture(5, pairs), weights=weights)
    result = graph.pagerank(weighted=True, personalization={graph.nodes[0]: 3, graph.nodes[4]: 1},
                            tolerance=1e-12, max_iterations=1000, backend=backend)
    assert result.converged and sum(result.scores) == pytest.approx(1.0)
    assert result.scores == pytest.approx(weighted_rank_oracle(5, pairs, weights, [.75, 0, 0, 0, .25]), abs=1e-11)
    huge = replace(picture(3, [(0, 1), (0, 2)]), weights=(1e308, 1e308))
    assert sum(huge.pagerank(weighted=True, personalization={huge.nodes[0]: 1e308, huge.nodes[1]: 1e308}, backend=backend).scores) == pytest.approx(1.0)


def test_weighted_algorithm_refusals_and_cancellation():
    graph = replace(picture(4, [(0, 1), (1, 2)]), weights=(1., 1.))
    for seed in ({}, {graph.nodes[0]: 0}, {graph.nodes[0]: -1}, {ProjectionNode("other", 1): 1}, {graph.nodes[0]: float("nan")}):
        with pytest.raises(GrafxConfigurationError):
            graph.pagerank(personalization=seed)
    with pytest.raises(GrafxConfigurationError):
        replace(graph, weights=None).pagerank(weighted=True)
    with pytest.raises(GrafxQueryBudgetExceeded):
        graph.weighted_shortest_path(graph.nodes[0], graph.nodes[2], max_results=1)
    bounded = replace(graph.with_adjacency(), limits=ProjectionLimits(max_work=1))
    with pytest.raises(GrafxQueryBudgetExceeded):
        bounded.weighted_shortest_path(graph.nodes[0], graph.nodes[2])
    token = CancellationToken()
    token.cancel()
    with pytest.raises(GrafxQueryCancelled):
        graph.weighted_shortest_path(graph.nodes[0], graph.nodes[1], cancellation=token)
