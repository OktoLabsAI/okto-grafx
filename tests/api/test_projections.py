"""Snapshot projections preserve multigraph semantics and bound detached work."""

from dataclasses import FrozenInstanceError
import pytest

from okto_grafx import connect, CancellationToken
from okto_grafx.projections import project_graph, ProjectionLimits
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded, GrafxQueryCancelled, GrafxTransactionStateError


def seed(db):
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
        tx.execute("CREATE NODE TABLE M(id INT64,PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM N TO N)")
        tx.execute("CREATE REL TABLE S(FROM N TO M)")
        for i in range(4):
            tx.execute("CREATE (:N {id:$id})", {"id": i})
        tx.execute("CREATE (:M {id:0})")
        for _ in range(2):
            tx.execute("MATCH (a:N {id:0}),(b:N {id:1}) CREATE (a)-[:R]->(b)")
        tx.execute("MATCH (a:N {id:0}) CREATE (a)-[:R]->(a)")
        tx.execute("MATCH (a:N {id:1}),(b:M {id:0}) CREATE (a)-[:S]->(b)")


def test_snapshot_multigraph_oracle_and_detached_lifetime(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        seed(db)
        with db.begin("read") as old:
            with connect(path) as other, other.begin() as tx:
                tx.execute("MATCH (a:N {id:2}),(b:N {id:3}) CREATE (a)-[:R]->(b)")
            graph = project_graph(db, old, node_tables=("N", "M"), relationship_tables=("R", "S"))
            assert graph.snapshot_lsn == old.snapshot.read_lsn
            assert old.active
            assert len(graph.edges) == 4
        fresh = project_graph(db, node_tables=("N", "M"), relationship_tables=("R", "S"))
        assert len(fresh.edges) == 5
        assert graph.database_uuid == db.identity.database_uuid
        assert not db.verify().findings
    assert len(graph.nodes) == 5
    assert sorted(graph.degrees()) == [0, 0, 1, 3, 4]
    assert sum(graph.degrees(direction="in")) == sum(graph.degrees(direction="out")) == 4
    # Independent reachability oracle: no union-find or dependency on capture order.
    adjacency = {n: set() for n in graph.nodes}
    for edge in graph.edges:
        a, b = graph.nodes[edge.source], graph.nodes[edge.target]
        adjacency[a].add(b)
        adjacency[b].add(a)
    expected = []
    for start in graph.nodes:
        seen, pending = {start}, [start]
        while pending:
            for n in adjacency[pending.pop()] - seen:
                seen.add(n)
                pending.append(n)
        expected.append(min(seen))
    assert graph.weakly_connected_components() == tuple(expected)
    assert len(set(expected)) == 3
    with pytest.raises(FrozenInstanceError):
        graph.nodes = ()


@pytest.mark.parametrize("limits", [ProjectionLimits(max_nodes=1), ProjectionLimits(max_edges=1),
                                   ProjectionLimits(max_memory_bytes=4096), ProjectionLimits(max_work=1)])
def test_budget_refusal_preserves_reader(tmp_path, limits):
    with connect(tmp_path / "db") as db:
        seed(db)
        with db.begin("read") as reader:
            with pytest.raises(GrafxQueryBudgetExceeded):
                project_graph(db, reader, node_tables=("N", "M"), relationship_tables=("R", "S"), limits=limits)
            assert reader.execute("MATCH (n:N) RETURN count(n)").rows == ((4,),)


def test_validation_cancellation_and_empty_projection(tmp_path):
    with connect(tmp_path / "db") as db:
        seed(db)
        for names, rels in (((), ()), (("N", "N"), ()), (("N",), ("S",)), (("R",), ())):
            with pytest.raises(GrafxConfigurationError):
                project_graph(db, node_tables=names, relationship_tables=rels)
        with db.begin() as writer:
            with pytest.raises(GrafxTransactionStateError):
                project_graph(db, writer, node_tables=("N",))
        with connect(tmp_path / "other") as other, other.begin("read") as foreign:
            with pytest.raises(GrafxTransactionStateError):
                project_graph(db, foreign, node_tables=("N",))
        token = CancellationToken()
        token.cancel()
        with pytest.raises(GrafxQueryCancelled):
            project_graph(db, node_tables=("N",), cancellation=token)
        graph = project_graph(db, node_tables=("N",))
        for operation in (graph.degrees, graph.weakly_connected_components):
            with pytest.raises(GrafxQueryCancelled):
                operation(cancellation=token)
        with pytest.raises(GrafxConfigurationError):
            graph.degrees(direction="sideways")
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE Empty(id INT64,PRIMARY KEY(id))")
        empty = project_graph(db, node_tables=("Empty",))
        assert empty.degrees() == empty.weakly_connected_components() == ()


@pytest.mark.parametrize("value", [True, 0, -1, 2**32])
def test_invalid_limits(value):
    with pytest.raises(GrafxConfigurationError):
        ProjectionLimits(max_nodes=value)


def test_mid_capture_cancellation_and_algorithm_budget(tmp_path, monkeypatch):
    from dataclasses import replace
    from okto_grafx.engine.database import Transaction
    with connect(tmp_path / "db") as db:
        seed(db)
        token = CancellationToken()
        original = Transaction.scan_rows_v1
        def cancel_after_scan(self, *args, **kwargs):
            page = original(self, *args, **kwargs)
            token.cancel()
            return page
        with db.begin("read") as reader:
            with monkeypatch.context() as scoped:
                scoped.setattr(Transaction, "scan_rows_v1", cancel_after_scan)
                with pytest.raises(GrafxQueryCancelled):
                    project_graph(db, reader, node_tables=("N",), cancellation=token)
            assert reader.execute("MATCH (n:N) RETURN count(n)").rows == ((4,),)
        graph = project_graph(db, node_tables=("N",), relationship_tables=("R",))
        restricted = replace(graph, limits=ProjectionLimits(max_work=1))
        for operation in (restricted.degrees, restricted.weakly_connected_components):
            with pytest.raises(GrafxQueryBudgetExceeded):
                operation()
