"""A named node-only pattern is a native path, not an entity or metadata map."""

import pytest

import okto_grafx
from okto_grafx.errors import GrafxPlanError, GrafxQueryBudgetExceeded, GrafxQueryCancelled


@pytest.fixture(params=[None, 8192], ids=["memory", "spill"])
def graph(tmp_path, request):
    with okto_grafx.connect(tmp_path / "nodes", query_memory_budget_bytes=request.param) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, mark INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")
            for label, identity in [("P", 1), ("P", 2), ("Q", 1)]:
                tx.execute(f"CREATE (:{label} {{id:$id}})", {"id": identity})
        yield db


def test_node_only_capture_has_exact_components_and_provenance(graph):
    rows = graph.execute("MATCH p=(n:P) RETURN p,n,length(p),nodes(p),relationships(p) ORDER BY n.id").rows
    assert len(rows) == 2
    for path, node, length, nodes, edges in rows:
        assert type(path) is okto_grafx.PathValue
        assert path.nodes == nodes == (node,)
        assert path.relationships == edges == ()
        assert len(path) == length == 0
        assert path.nodes[0].provenance == node.provenance
    assert graph.transactions.open_transactions == 0


@pytest.mark.parametrize("pattern", ["p=(n)", "p=()"])
def test_unlabelled_and_anonymous_node_paths_include_all_tables(graph, pattern):
    rows = graph.execute(f"MATCH {pattern} RETURN p").rows
    assert sorted((p.nodes[0].label, p.nodes[0].properties["id"]) for (p,) in rows) == [
        ("P", 1), ("P", 2), ("Q", 1)]


@pytest.mark.parametrize("query", [
    "MATCH p=(n:P {id:1}) WITH p AS q RETURN q",
    "CALL () { MATCH p=(n:P {id:1}) RETURN p } RETURN p",
    "MATCH (n:P {id:1}) CALL (n) { MATCH p=(n) RETURN p } RETURN p",
    "MATCH p=(n:P {id:1}) WITH * RETURN p",
])
def test_node_paths_cross_alias_and_subquery_boundaries(graph, query):
    rows = graph.execute(query).rows
    assert len(rows) == 1
    assert type(rows[0][0]) is okto_grafx.PathValue
    assert rows[0][0].nodes[0].properties["id"] == 1


@pytest.mark.parametrize("query", [
    "OPTIONAL MATCH p=(n:P {id:99}) RETURN p",
    "WITH null AS n OPTIONAL MATCH p=(n) RETURN p",
    "MATCH (a:P {id:1}) OPTIONAL MATCH p=(n:P {id:99}) RETURN p",
    "WITH null AS n CALL (n) { OPTIONAL MATCH p=(n) RETURN p } RETURN p",
])
def test_missing_or_null_anchor_is_null_not_a_fabricated_path(graph, query):
    assert graph.execute(query).rows == ((None,),)


def test_union_and_distinct_retain_qualified_node_identity(graph):
    rows = graph.execute("MATCH p=(n:P {id:1}) RETURN p UNION MATCH p=(n:Q {id:1}) RETURN p").rows
    assert len(rows) == 2
    assert len({p for (p,) in rows}) == 2
    assert len(graph.execute("MATCH p=(n:P {id:1}) RETURN p UNION MATCH p=(n:P {id:1}) RETURN p").rows) == 1
    assert len(graph.execute("MATCH p=(n:P {id:1}) RETURN p UNION ALL MATCH p=(n:P {id:1}) RETURN p").rows) == 2
    rows = graph.execute("UNWIND [1,1,2] AS id MATCH p=(n:P {id:id}) RETURN DISTINCT p").rows
    assert len(rows) == 2


def test_nested_aggregate_keeps_paths_native(graph):
    rows = graph.execute("MATCH p=(n:P) RETURN collect({path:p, node:n})").rows
    assert sorted(item["path"].nodes[0].properties["id"] for item in rows[0][0]) == [1, 2]
    assert all(item["path"].nodes == (item["node"],) for item in rows[0][0])


def test_pending_creation_is_owned_and_rollback_does_not_leak(graph):
    tx = graph.begin("write")
    try:
        tx.execute("CREATE (:P {id:99})")
        path = tx.execute("MATCH p=(n:P {id:99}) RETURN p").rows[0][0]
        assert path.nodes[0].provenance.pending
        assert graph.execute("MATCH p=(n:P {id:99}) RETURN p").rows == ()
    finally:
        tx.rollback()
    assert path.nodes[0].properties["id"] == 99
    assert graph.execute("MATCH p=(n:P {id:99}) RETURN p").rows == ()


@pytest.mark.parametrize("capture", ["p=(n:P {id:1})", "p=(n:P {id:1})-[r:E]->(m:P)"])
def test_path_components_observe_a_subsequent_same_statement_set(graph, capture):
    with graph.begin("write") as tx:
        tx.execute("CREATE REL TABLE E(FROM P TO P)")
        tx.execute("MATCH (n:P {id:1}), (m:P {id:2}) CREATE (n)-[:E]->(m)")
    with graph.begin("write") as tx:
        row = tx.execute(f"MATCH {capture} SET n.mark=7 "
                         "RETURN p,n,nodes(p)[0].mark").rows[0]
        path, node, direct = row
        assert path.nodes[0] == node
        assert node.properties["mark"] == direct == 7


def test_cursor_retains_snapshot_and_cancels_without_readers(graph):
    token = okto_grafx.CancellationToken()
    query = "MATCH p=(n:P) RETURN p ORDER BY n.id"
    with graph.query(query).cursor(batch_size=1, cancellation=token) as cursor:
        first = cursor.fetchone()[0]
        with graph.begin("write") as tx:
            tx.execute("MATCH (n:P {id:2}) DELETE n")
        second = cursor.fetchone()[0]
        assert [first.nodes[0].properties["id"], second.nodes[0].properties["id"]] == [1, 2]
        token.cancel()
        with pytest.raises(GrafxQueryCancelled):
            cursor.fetchone()
    assert graph.transactions.open_transactions == 0


def test_capture_consumes_path_budget_without_edge_expansion(tmp_path):
    with okto_grafx.connect(tmp_path / "budget", max_traversal_paths=1) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:P {id:1}), (:P {id:2})")
        result = db.execute("MATCH p=(n:P) RETURN p LIMIT 1")
        assert len(result.rows) == 1
        assert result.statistics.get("traversal_expansions", 0) == 0
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("MATCH p=(n:P) RETURN p")
        assert db.transactions.open_transactions == 0


def test_node_paths_survive_instrumented_disk_spill(tmp_path, monkeypatch):
    from okto_grafx.adapters.query_spill_local import _Sorter

    written = []
    original = _Sorter._write_pair

    def observed(stream, record):
        original(stream, record)
        written.append(len(record[1]))

    monkeypatch.setattr(_Sorter, "_write_pair", staticmethod(observed))
    with okto_grafx.connect(tmp_path / "spill", query_memory_budget_bytes=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:P {id:1})")
        rows = db.execute("UNWIND range(1,200) AS i MATCH p=(n:P {id:1}) "
                          "RETURN p,i ORDER BY i DESC").rows
        assert written and sum(written) > 8192
        assert [i for _, i in rows] == list(range(200, 0, -1))
        assert all(type(p) is okto_grafx.PathValue and p.nodes[0].properties["id"] == 1
                   and not p.relationships for p, _ in rows)
        assert len({p for p, _ in rows}) == 1
        assert db.transactions.open_transactions == 0


def test_public_plan_is_detached_and_capture_preserves_index_seek(graph):
    query = "MATCH p=(n:P {id:1}) RETURN p"
    first, second = graph.execute(query), graph.execute(query)
    first_capture = next(node for node in first.plan.walk() if node.label == "CaptureNodePath")
    second_capture = next(node for node in second.plan.walk() if node.label == "CaptureNodePath")
    assert first_capture is not second_capture
    assert any(node.label == "IndexSeek" for node in first.plan.walk())
    object.__setattr__(first_capture, "path", "mutated")
    assert second_capture.path == "p"
    assert graph.execute(query).rows == second.rows


def test_late_failure_after_capture_rolls_back_observed_writes(graph, monkeypatch):
    import okto_grafx.engine.query_engine as engine
    from okto_grafx.errors import GrafxError

    original = engine._write_assignments
    applied = []

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        applied.append(True)
        return result

    monkeypatch.setattr(engine, "_write_assignments", observed)
    with graph.begin("write") as tx:
        tx.execute("CREATE (:P {id:99})")
        with pytest.raises(GrafxError):
            tx.execute("MATCH p=(n:P) SET n.mark=length(p) RETURN 10/(n.id-2)")
        assert applied
        assert tx.execute("MATCH (n:P) RETURN n.mark").rows == ((None,), (None,), (None,))
        assert tx.execute("MATCH (n:P {id:99}) RETURN n.id").rows == ((99,),)


@pytest.mark.parametrize("query", ["MATCH n=(n:P) RETURN n", "WITH 1 AS p MATCH p=(n:P) RETURN p"])
def test_capture_cannot_replace_an_existing_name(graph, query):
    with pytest.raises(GrafxPlanError):
        graph.execute(query)
