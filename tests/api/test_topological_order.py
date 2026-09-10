"""Independent stdlib DAG oracle and bounded multigraph/cycle behavior."""

from dataclasses import replace
import graphlib
import random
import pytest
from tests.api.test_projection_algorithms import picture
from okto_grafx import CancellationToken
from okto_grafx.projections import ProjectionLimits
from okto_grafx.errors import GrafxQueryBudgetExceeded, GrafxQueryCancelled


def test_random_oracle():
    rng = random.Random(907)
    for _ in range(50):
        edges = [(rng.randrange(8), rng.randrange(8)) for _ in range(rng.randrange(20))]
        graph = picture(8, edges)
        predecessors = {i: [] for i in range(8)}
        for source, target in edges:
            predecessors[target].append(source)
        try:
            tuple(graphlib.TopologicalSorter(predecessors).static_order())
            acyclic = True
        except graphlib.CycleError:
            acyclic = False
        result = graph.topological_order()
        assert result.acyclic == acyclic
        assert result == graph.topological_order()
        assert set(result.order) | set(result.blocked) == set(graph.nodes)
        if acyclic:
            rank = {node: i for i, node in enumerate(result.order)}
            assert all(rank[graph.nodes[a]] < rank[graph.nodes[b]] for a, b in edges)


def test_cycles_descendants_multiedges_and_empty():
    assert picture(0, []).topological_order().acyclic
    dag = picture(3, [(0, 1), (0, 1), (1, 2)])
    assert dag.topological_order().order == dag.nodes
    graph = picture(3, [(0, 0), (0, 1)])
    result = graph.topological_order()
    assert not result.acyclic and result.blocked == graph.nodes[:2]
    assert result.order == graph.nodes[2:]


def test_deep_and_bounded():
    graph = picture(2000, [(i, i + 1) for i in range(1999)])
    assert graph.topological_order().order == graph.nodes
    for limits in [ProjectionLimits(max_work=1), ProjectionLimits(max_memory_bytes=1)]:
        with pytest.raises(GrafxQueryBudgetExceeded):
            replace(graph, limits=limits).topological_order()
    token = CancellationToken()
    token.cancel()
    with pytest.raises(GrafxQueryCancelled):
        graph.topological_order(cancellation=token)
