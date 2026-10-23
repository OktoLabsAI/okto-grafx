"""Conditional IN seeks use the existing bounded endpoint hybrid safely."""

import pytest

from okto_grafx import connect
from okto_grafx.engine import query_engine
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.index_manager import edge_from_index_name, edge_to_index_name
from tests.query import test_node_in_seek as fixture

database = fixture.database
QUERY = "MATCH (a:A)-[r:R]->(b:B) WHERE a.id IN $ids RETURN a.id,b.id"


def test_selective_conditional_frontier_uses_endpoint_lookups(database):
    result = database.execute(QUERY, {"ids": ["a1", "a1", "missing"]})
    assert result.rows == (("a1", "b1"),)
    assert result.statistics.get("edge_lookups") == 1
    assert result.statistics.get("edge_scans", 0) == 0


def test_sixteen_in_starts_cross_with_without_edge_census(database):
    with database.begin("write") as tx:
        tx.execute(
            "UNWIND $ids AS id CREATE (:A {id:id,v:1})",
            {"ids": [f"a{i}" for i in range(6, 17)]},
        )
    query = (
        "MATCH (a:A) WHERE a.id IN $ids WITH a MATCH (a)-[r:R]->(b:B) RETURN a.id,b.id"
    )
    result = database.execute(query, {"ids": [f"a{i}" for i in range(1, 17)]})
    assert result.rows == (("a1", "b1"),)
    assert result.statistics.get("edge_lookups") == 16
    assert result.statistics.get("edge_scans", 0) == 0


def test_conditional_frontier_matches_grouped_scan_on_fallback(database, monkeypatch):
    fast = database.execute(QUERY, {"ids": ["a1", "a2"]})
    with monkeypatch.context() as patch:
        patch.setattr(IndexManager, "validated_versions_many", None)
        slow = database.execute(QUERY, {"ids": ["a1", "a2"]})
    assert fast.rows == slow.rows
    assert fast.statistics.get("edge_scans", 0) == 0
    assert slow.statistics.get("fallback_reason_index_unavailable") == 1


def test_stale_endpoint_authority_falls_back_without_changing_rows(database):
    expected = database.execute(QUERY, {"ids": ["a1", "a2"]}).rows
    database._indexes.index(edge_from_index_name("R")).mark_stale("test fallback")
    database._indexes.index(edge_to_index_name("R")).mark_stale("test fallback")
    actual = database.execute(QUERY, {"ids": ["a1", "a2"]})
    assert actual.rows == expected
    assert actual.statistics.get("edge_lookups", 0) == 0
    assert actual.statistics.get("edge_scans") == 1


def test_wide_conditional_fallback_switches_to_one_grouped_edge_scan(database):
    with database.begin("write") as tx:
        tx.execute(
            "UNWIND $ids AS id CREATE (:A {id:id,v:1})",
            {"ids": [f"a{i}" for i in range(6, 76)]},
        )
    result = database.execute(QUERY, {"ids": [f"a{i}" for i in range(1, 76)]})
    assert result.rows == (("a1", "b1"),)
    assert result.statistics.get("edge_lookups") == 64
    assert result.statistics.get("edge_scans") == 1


@pytest.mark.parametrize("ids", [[], ["missing"], ["a1"], ["a1", "a2"]])
def test_conditional_frontier_keeps_exact_rows_for_empty_and_small_lists(database, ids):
    expected = fixture._outcome(database, QUERY, {"ids": ids})
    actual = database.execute(QUERY, {"ids": ids})
    assert expected == ("linhas", actual.rows)


@pytest.fixture
def loop_graph(tmp_path):
    """Real persisted parallel, reverse and self-loop edges, plus an unrelated edge."""
    with connect(tmp_path / "loops", page_size=512) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM N TO N, weight INT64)")
        with db.begin("write") as tx:
            tx.execute("UNWIND $ids AS id CREATE (:N {id:id})", {"ids": [1, 2, 3, 4]})
        with db.begin("write") as tx:
            for source, target, weight in (
                (1, 1, 1),
                (1, 2, 2),
                (1, 2, 3),
                (2, 1, 4),
                (3, 4, 5),
            ):
                tx.execute(
                    "MATCH (a:N {id:$source}),(b:N {id:$target}) "
                    "CREATE (a)-[:E {weight:$weight}]->(b)",
                    {"source": source, "target": target, "weight": weight},
                )
        db.ensure_identity_indexes()
        yield db


_DIRECTIONS = {
    "outgoing": "(a)-[r:E]->(b:N)",
    "incoming": "(a)<-[r:E]-(b:N)",
    "undirected": "(a)-[r:E]-(b:N)",
}


def _direction_query(direction):
    # Explicit WITH keeps the conditional producer identical across direction variants.
    return (
        "MATCH (a:N) WHERE a.id IN $ids WITH a MATCH "
        + _DIRECTIONS[direction]
        + " RETURN a.id,b.id,r.weight ORDER BY r.weight,a.id,b.id"
    )


def _compare_scalar_fallback(handle, direction, monkeypatch):
    query = _direction_query(direction)
    parameters = {"ids": [1, 1, 999]}
    candidate = handle.execute(query, parameters)
    with monkeypatch.context() as patch:
        patch.setattr(IndexManager, "validated_versions_many", None)
        # Missing node-many alone still admits the endpoint hybrid for this plan.
        # Explicitly select its retained grouped scan to obtain an independent oracle.
        patch.setattr(query_engine, "_frontier_is_bounded", lambda *_: False)
        oracle = handle.execute(query, parameters)
    assert candidate.columns == oracle.columns
    assert candidate.rows == oracle.rows
    # Logical work/result multiplicity is checked separately from strategy counters.
    assert candidate.statistics.get("edge_scans", 0) <= 1
    assert oracle.statistics.get("edge_scans", 0) == 1
    return candidate


@pytest.mark.parametrize("direction", _DIRECTIONS)
def test_direction_self_loop_and_parallel_edges_match_scan(
    loop_graph, monkeypatch, direction
):
    result = _compare_scalar_fallback(loop_graph, direction, monkeypatch)
    expected = {
        "outgoing": ((1, 1, 1), (1, 2, 2), (1, 2, 3)),
        "incoming": ((1, 1, 1), (1, 2, 4)),
        "undirected": ((1, 1, 1), (1, 2, 2), (1, 2, 3), (1, 2, 4)),
    }
    assert result.rows == expected[direction]
    assert result.statistics.get("edge_lookups", 0) > 0
    assert result.statistics.get("edge_scans", 0) == 0


@pytest.mark.parametrize("direction", _DIRECTIONS)
@pytest.mark.parametrize("operation", ["create", "delete", "update"])
def test_owned_overlay_matches_scan_without_losing_multiplicity(
    loop_graph, monkeypatch, direction, operation
):
    before = loop_graph.execute(_direction_query(direction), {"ids": [1]}).rows
    tx = loop_graph.begin("write")
    try:
        if operation == "create":
            tx.execute(
                "MATCH (a:N {id:$source}),(b:N {id:$target}) CREATE (a)-[:E {weight:6}]->(b)",
                {
                    "source": 3 if direction == "incoming" else 1,
                    "target": 1 if direction == "incoming" else 3,
                },
            )
        elif operation == "delete":
            tx.execute(
                "MATCH (:N)-[r:E]->(:N) WHERE r.weight=$weight DELETE r",
                {"weight": 4 if direction == "incoming" else 2},
            )
        else:
            tx.execute(
                "MATCH (:N)-[r:E]->(:N) WHERE r.weight=$weight SET r.weight=30",
                {"weight": 4 if direction == "incoming" else 3},
            )
        result = _compare_scalar_fallback(tx, direction, monkeypatch)
        expected = list(before)
        if operation == "create":
            expected.append((1, 3, 6))
        elif operation == "delete":
            expected.remove((1, 2, 4 if direction == "incoming" else 2))
        else:
            expected.remove((1, 2, 4 if direction == "incoming" else 3))
            expected.append((1, 2, 30))
        assert result.rows == tuple(
            sorted(expected, key=lambda row: (row[2], row[0], row[1]))
        )
    finally:
        if tx.active:
            tx.rollback()
    assert loop_graph.execute(_direction_query(direction), {"ids": [1]}).rows == before
