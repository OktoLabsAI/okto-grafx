"""Conditional IN seeks use the existing bounded endpoint hybrid safely."""

import pytest

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
        "MATCH (a:A) WHERE a.id IN $ids WITH a "
        "MATCH (a)-[r:R]->(b:B) RETURN a.id,b.id"
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
