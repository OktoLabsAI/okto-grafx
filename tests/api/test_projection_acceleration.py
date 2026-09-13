"""Retained lookup, linear peeling, optional kernels and snapshot weights."""

from dataclasses import replace
import heapq
import math
import random

import pytest

from okto_grafx import connect, CancellationToken
from okto_grafx.projections import project_graph, ProjectionLimits
from okto_grafx.errors import GrafxConfigurationError, GrafxUnsupportedOperation, GrafxQueryBudgetExceeded, GrafxQueryCancelled
from tests.api.test_projection_algorithms import picture


def test_retained_lookup_removes_full_node_work_and_is_immutable():
    graph = picture(10000, [(0, 1)]).with_adjacency()
    assert graph.with_lookup() is graph
    with pytest.raises(TypeError):
        graph.lookup.positions[graph.nodes[0]] = 99
    bounded = replace(graph, limits=ProjectionLimits(max_work=10,
                      max_memory_bytes=graph.logical_bytes + 4096 + 2048))
    assert bounded.shortest_path(graph.nodes[0], graph.nodes[1]).nodes == graph.nodes[:2]
    assert bounded.reachable(graph.nodes[0]) == graph.nodes[:2]


def heap_oracle(n, pairs):
    neighbors = [set() for _ in range(n)]
    for a, b in pairs:
        if a != b:
            neighbors[a].add(b)
            neighbors[b].add(a)
    degree = list(map(len, neighbors))
    queue = [(d, i) for i, d in enumerate(degree)]
    heapq.heapify(queue)
    removed, result = set(), [0] * n
    while queue:
        d, i = heapq.heappop(queue)
        if i in removed or d != degree[i]:
            continue
        removed.add(i)
        result[i] = d
        for j in neighbors[i]:
            if j not in removed and degree[j] > d:
                degree[j] -= 1
                heapq.heappush(queue, (degree[j], j))
    return tuple(result)


def test_bucket_kcore_matches_previous_heap_and_has_linear_work(monkeypatch):
    from okto_grafx.projections import _Work
    randomizer = random.Random(97)
    for n in (0, 1, 5, 50, 300):
        pairs = [(randomizer.randrange(n), randomizer.randrange(n)) for _ in range(5 * n)] if n else []
        graph = picture(n, pairs)
        expected = heap_oracle(n, pairs)
        steps = [0]
        original = _Work.step
        def observed(self, amount=1):
            steps[0] += amount
            return original(self, amount)
        with monkeypatch.context() as scoped:
            scoped.setattr(_Work, "step", observed)
            assert graph.k_core() == expected
        assert steps[0] <= 10 * (n + len(pairs) + 1)


@pytest.mark.optional_dependency("numpy")
def test_numpy_pagerank_matches_python():
    pytest.importorskip("numpy")
    for graph in (picture(0, []), picture(5, []), picture(6, [(0, 1), (0, 1), (1, 1), (1, 2), (3, 0)])):
        reference = graph.pagerank(tolerance=1e-12, max_iterations=1000)
        actual = graph.pagerank(tolerance=1e-12, max_iterations=1000, backend="numpy")
        assert actual.converged
        assert actual.scores == pytest.approx(reference.scores, abs=1e-12)
    with pytest.raises(GrafxConfigurationError):
        graph.pagerank(backend="auto")


def test_capture_weights_same_snapshot_null_policy_and_refusals(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N,cost DOUBLE)")
            tx.execute("CREATE (:N {id:1})")
            tx.execute("MATCH (n:N) CREATE (n)-[:R {cost:2.5}]->(n)")
            tx.execute("MATCH (n:N) CREATE (n)-[:R]->(n)")
        with db.begin("read") as reader:
            with pytest.raises(GrafxConfigurationError):
                project_graph(db, reader, node_tables=("N",), relationship_tables=("R",), weight_columns={"R": "cost"})
            with connect(tmp_path / "db") as other, other.begin() as tx:
                tx.execute("MATCH (:N)-[r:R]->(:N) SET r.cost=99.0")
            graph = project_graph(db, reader, node_tables=("N",), relationship_tables=("R",), weight_columns={"R": "cost"}, default_weight=1)
            assert graph.weights == (2.5, 1.0)
        plain = project_graph(db, node_tables=("N",), relationship_tables=("R",))
        assert plain.weights is None and len(plain.edges) == 2
        with pytest.raises(GrafxConfigurationError):
            project_graph(db, node_tables=("N",), relationship_tables=("R",), weight_columns={})


def test_selected_numpy_missing_and_algorithm_limits(monkeypatch):
    import builtins
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("deliberately absent")
        return original(name, *args, **kwargs)
    graph = picture(4, [(0, 1), (1, 2)]).with_adjacency()
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", missing)
        with pytest.raises(GrafxUnsupportedOperation):
            graph.pagerank(backend="numpy")
        assert graph.pagerank().scores
    limited = replace(graph, limits=ProjectionLimits(max_memory_bytes=graph.logical_bytes + 4096 + 4096))
    with pytest.raises(GrafxQueryBudgetExceeded):
        limited.pagerank(backend="numpy")
    token = CancellationToken()
    token.cancel()
    for operation in (graph.with_lookup, graph.k_core, lambda **kw: graph.pagerank(backend="numpy", **kw)):
        with pytest.raises(GrafxQueryCancelled):
            operation(cancellation=token)


@pytest.mark.parametrize("cost", [-1., float("nan"), float("inf")])
def test_capture_rejects_invalid_weights_without_losing_reader(cost, monkeypatch):
    from okto_grafx.domain.model.errors import SchemaMismatchError
    from okto_grafx.engine.database import Transaction

    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N,cost DOUBLE,label STRING)")
            tx.execute("CREATE (:N {id:1})")
            if not math.isfinite(cost):
                with pytest.raises(SchemaMismatchError):
                    tx.execute("MATCH (n:N) CREATE (n)-[:R {cost:$cost}]->(n)", {"cost": cost})
                assert tx.execute("MATCH(:N)-[e:R]->(:N) RETURN count(e)").rows == ((0,),)
            tx.execute("MATCH (n:N) CREATE (n)-[:R {cost:$cost}]->(n)",
                       {"cost": cost if math.isfinite(cost) else 1.0})
        original_scan = Transaction.scan_rows_v1

        def faulty_scan(tx, table, **kwargs):
            page = original_scan(tx, table, **kwargs)
            if table == "R" and kwargs.get("columns") == ("_from", "_to", "cost"):
                # Defensive consumer validation remains tested even though native
                # storage now correctly rejects NaN/Infinity before commit.
                return replace(page, rows=tuple(replace(row, values=row.values[:2] + (cost,))
                                                for row in page.rows))
            return page

        monkeypatch.setattr(Transaction, "scan_rows_v1", faulty_scan)
        with db.begin("read") as reader:
            for column in ("cost", "label", "absent"):
                with pytest.raises(GrafxConfigurationError):
                    project_graph(db, reader, node_tables=("N",), relationship_tables=("R",), weight_columns={"R": column})
            assert reader.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)


def test_oversized_weight_selection_refuses_before_copying_keys(monkeypatch):
    import okto_grafx.projections as module
    weights = {str(i): "cost" for i in range(257)}
    def bounded_set(value):
        assert value is not weights, "oversized input keys must not be copied"
        return set(value)
    with connect(":memory:") as db:
        monkeypatch.setattr(module, "set", bounded_set, raising=False)
        with pytest.raises(GrafxConfigurationError):
            project_graph(db, node_tables=("N",), relationship_tables=("R",), weight_columns=weights)
