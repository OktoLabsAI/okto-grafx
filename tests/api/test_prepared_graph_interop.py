"""Prepared immutable analytics, independent graph exchange and bounded communities."""

from dataclasses import replace
import builtins
import random

import pytest

from okto_grafx import CancellationToken
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxQueryBudgetExceeded,
    GrafxQueryCancelled,
    GrafxUnsupportedOperation,
)
from okto_grafx.graph_interop import to_networkx
from okto_grafx.projections import ProjectionLimits
from tests.api.test_projection_algorithms import picture


@pytest.mark.parametrize(
    "backend",
    ["python", pytest.param("numpy", marks=pytest.mark.optional_dependency("numpy"))],
)
@pytest.mark.parametrize("weighted", [False, True])
def test_prepared_rank_parity_identity_and_no_renormalization(
    backend, weighted, monkeypatch
):
    if backend == "numpy":
        pytest.importorskip("numpy")
    g = replace(
        picture(6, [(0, 1), (0, 1), (1, 2), (2, 0), (3, 4), (4, 4)]),
        weights=(0.0, 2.0, 1.0, 4.0, 0.0, 3.0),
    )
    p = g.with_pagerank(backend=backend, weighted=weighted)
    assert p.with_pagerank(backend=backend, weighted=weighted) is p
    assert g.pagerank_preparation is None
    assert p.logical_bytes > g.logical_bytes
    assert p.pagerank_preparation.numeric_buffers is None or all(
        type(x) is bytes for x in p.pagerank_preparation.numeric_buffers
    )
    for seed in (None, {g.nodes[0]: 1.0, g.nodes[3]: 4.0}):
        expected = g.pagerank(backend=backend, weighted=weighted, personalization=seed)
        actual = p.pagerank(backend=backend, weighted=weighted, personalization=seed)
        assert actual.scores == pytest.approx(expected.scores, abs=1e-12)
        assert actual.iterations == expected.iterations
    # Mismatched modes must use the requested semantics, never the retained mode.
    assert p.pagerank(weighted=not weighted).scores == pytest.approx(
        g.pagerank(weighted=not weighted).scores
    )
    from okto_grafx import projection_algorithms as algorithms

    def forbidden(*args):
        raise AssertionError("normalization should not recur")

    monkeypatch.setattr(algorithms.math, "fsum", forbidden)
    p.pagerank(backend=backend, weighted=weighted)


def test_prepared_budgets_and_controls():
    g = picture(10, [(0, 1)])
    token = CancellationToken()
    token.cancel()
    for operation in (
        g.with_pagerank,
        g.with_simple_topology,
        g.label_propagation,
        lambda **kw: to_networkx(g, **kw),
    ):
        with pytest.raises(GrafxQueryCancelled):
            operation(cancellation=token)
    small = replace(g, limits=ProjectionLimits(max_memory_bytes=4096))
    for operation in (
        small.with_pagerank,
        small.with_simple_topology,
        small.label_propagation,
    ):
        with pytest.raises(GrafxQueryBudgetExceeded):
            operation()
    with pytest.raises(GrafxConfigurationError):
        g.with_pagerank(weighted=True)
    with pytest.raises(GrafxConfigurationError):
        g.with_pagerank(backend="auto")
    with pytest.raises(GrafxConfigurationError):
        g.label_propagation(max_iterations=True)
    with pytest.raises(GrafxQueryBudgetExceeded):
        to_networkx(g, max_memory_bytes=1)


def test_simple_topology_reuse_and_community_semantics():
    for n, edges in (
        (0, []),
        (4, []),
        (6, [(0, 1), (1, 2), (2, 0), (3, 4), (3, 4), (4, 4)]),
    ):
        g = picture(n, edges)
        p = g.with_simple_topology()
        assert p.with_simple_topology() is p
        assert p.edges == g.edges
        assert p.k_core() == g.k_core()
        assert p.label_propagation() == g.label_propagation()
        result = p.label_propagation()
        assert result.converged
        if n == 6:
            assert result.labels[0] == result.labels[1] == result.labels[2]
            assert result.labels[3] == result.labels[4]
            assert result.labels[5] == g.nodes[5]
            assert not p.label_propagation(max_iterations=1).converged


def test_retained_community_initial_and_result_copy_work():
    graph = picture(100, []).with_simple_topology()
    for max_work in (99, 299):
        bounded = replace(graph, limits=replace(graph.limits, max_work=max_work))
        with pytest.raises(GrafxQueryBudgetExceeded):
            bounded.label_propagation()
    assert replace(graph, limits=replace(graph.limits, max_work=300)).label_propagation().converged


@pytest.mark.optional_dependency("networkx")
def test_networkx_exchange_oracles_and_independent_ownership():
    nx = pytest.importorskip("networkx")
    r = random.Random(41)
    for n in (1, 5, 20):
        edges = [(r.randrange(n), r.randrange(n)) for _ in range(5 * n)]
        g = picture(n, edges)
        net = to_networkx(g)
        keys = list(net)
        assert net.number_of_edges() == len(edges)
        assert tuple(dict(net.degree()).values()) == g.degrees()
        simple = nx.Graph(net)
        simple.remove_edges_from(nx.selfloop_edges(simple))
        assert tuple(nx.core_number(simple)[node] for node in keys) == g.k_core()
        expected = {frozenset(part) for part in nx.strongly_connected_components(net)}
        labels = g.strongly_connected_components()
        actual = {
            frozenset(keys[i] for i, value in enumerate(labels) if value == label)
            for label in labels
        }
        assert actual == expected
        for target in range(n):
            path = g.shortest_path(g.nodes[0], g.nodes[target])
            assert path.found == nx.has_path(net, keys[0], keys[target])
            if path.found:
                assert len(path.edges) == nx.shortest_path_length(
                    net, keys[0], keys[target]
                )
        net.clear()
        assert len(g.edges) == len(edges)


def test_networkx_and_numpy_missing_typed(monkeypatch):
    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name in ("networkx", "numpy"):
            raise ImportError(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(GrafxUnsupportedOperation):
        to_networkx(picture(1, []))
    with pytest.raises(GrafxUnsupportedOperation):
        picture(1, []).with_pagerank(backend="numpy")


def test_label_propagation_matches_independent_sweep_and_reuse_work(monkeypatch):
    from okto_grafx.projections import _Work

    rng = random.Random(617)
    for n in (0, 2, 10, 50):
        edges = (
            [(rng.randrange(n), rng.randrange(n)) for _ in range(5 * n)] if n else []
        )
        graph = picture(n, edges)
        labels = list(graph.nodes)
        adjacency = {
            i: {b if a == i else a for a, b in edges if a != b and i in (a, b)}
            for i in range(n)
        }
        converged, iteration = not n, 0
        for iteration in range(1, 31) if n else ():
            before = list(labels)
            for i in range(n):
                if adjacency[i]:
                    votes = [labels[j] for j in adjacency[i]]
                    labels[i] = min(
                        set(votes), key=lambda label: (-votes.count(label), label)
                    )
            if labels == before:
                converged = True
                break
        result = graph.label_propagation(max_iterations=30)
        assert (result.labels, result.iterations, result.converged) == (
            tuple(labels),
            iteration,
            converged,
        )
    graph = picture(100, [(i % 99, i % 99 + 1) for i in range(5000)])
    prepared = graph.with_simple_topology()
    original = _Work.step
    observed = []
    for candidate in (graph, prepared):
        count = [0]

        def step(self, amount=1):
            count[0] += amount
            return original(self, amount)

        with monkeypatch.context() as scoped:
            scoped.setattr(_Work, "step", step)
            candidate.k_core()
        observed.append(count[0])
    assert observed[0] - observed[1] == len(graph.edges)


@pytest.mark.optional_dependency("networkx")
def test_weighted_multigraph_export_and_source_snapshot(tmp_path):
    nx = pytest.importorskip("networkx")
    from okto_grafx import connect
    from okto_grafx.projections import project_graph

    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N,cost DOUBLE)")
            tx.execute("CREATE (:N {id:1}),(:N {id:2})")
            tx.execute(
                "MATCH (a:N),(b:N) WHERE a.id=1 AND b.id=2 CREATE (a)-[:R {cost:2.0}]->(b)"
            )
        graph = project_graph(
            db,
            node_tables=("N",),
            relationship_tables=("R",),
            weight_columns={"R": "cost"},
        ).with_pagerank(weighted=True)
        before = graph.pagerank(weighted=True)
        with connect(tmp_path / "db") as other, other.begin() as tx:
            tx.execute("MATCH (:N)-[r:R]->(:N) SET r.cost=999.0")
    assert graph.pagerank(weighted=True) == before
    net = to_networkx(graph)
    keys = list(net)
    assert nx.shortest_path_length(net, keys[0], keys[1], weight="weight") == 2.0
    assert net.graph["database_uuid"] == graph.database_uuid.hex()
