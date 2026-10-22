"""Connected IN anchor selection preserves the canonical traversal contract."""

import pytest
import okto_grafx

from okto_grafx.domain.query.plan import NodeMultiKeySeek
from okto_grafx.engine.index_manager import IndexManager
from tests.query import test_node_in_seek as fixture

database = fixture.database
QUERY = "MATCH (a:A)-[r:R]->(b:B) WHERE a.id IN $ids RETURN a.id,b.id"


def seeks(db, query):
    return [node for node in db.explain(query).walk() if type(node) is NodeMultiKeySeek]


def test_connected_anchor_uses_existing_multi_key_door(database):
    assert len(seeks(database, QUERY)) == 1
    result = database.execute(QUERY, {"ids": ["a1", "a1", "missing"]})
    assert result.rows == (("a1", "b1"),)
    # Two anchor keys, one endpoint lookup, and one destination identity lookup.
    assert result.statistics.get("index_probe_keys") == 4
    assert result.statistics.get("edge_lookups") == 1
    assert result.statistics.get("edge_scans", 0) == 0


@pytest.mark.parametrize(
    "ids", [[], None, [None], ["a1", "a1"], [1, True, "a1"], 1, "a1"]
)
def test_connected_in_matches_canonical_scan(database, ids, monkeypatch):
    expected = fixture._outcome(database, QUERY, {"ids": ids})
    monkeypatch.setattr(IndexManager, "validated_versions_many", None)
    assert fixture._outcome(database, QUERY, {"ids": ids}) == expected


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a:A)-[:R]->(b:B) WHERE a.v/0 > 1 AND a.id IN $ids RETURN a.id",
        "MATCH (a:A)-[:R]->(b:B) WHERE a.id IN $ids AND a.v/0 > 1 RETURN a.id",
        "MATCH (a:A)-[:R]->(b:B) WHERE a.id IN $ids OR a.v=2 RETURN a.id",
        "MATCH (a:A)-[:R]->(b:B) WHERE a.id IN ['a1'] RETURN a.id",
        "UNWIND [1] AS k MATCH (a:A)-[:R]->(b:B) WHERE a.id IN $ids RETURN a.id",
        "MATCH (a:A)-[:R {confidence:1/0}]->(b:B) WHERE a.id IN $ids RETURN a.id",
    ],
)
def test_unproven_shapes_keep_scan(database, query):
    assert not seeks(database, query)


def test_total_local_residual_stays_above_traversal(database):
    query = QUERY.replace("RETURN", "AND a.v=1 RETURN")
    assert len(seeks(database, query)) == 1
    assert database.execute(query, {"ids": ["a1", "a2"]}).rows == (("a1", "b1"),)


def test_owner_overlay_uses_existing_fallback(database):
    with database.begin("write") as tx:
        tx.execute("MATCH (a:A {id:'a1'}) SET a.v=9")
        result = tx.execute(QUERY, {"ids": ["a1"]})
        assert result.rows == (("a1", "b1"),)
        assert result.statistics.get("fallback_reason_owner_overlay") == 1


def test_old_reader_preserves_snapshot_and_parallel_edge_multiplicity(database):
    with database.begin("read") as old:
        assert old.execute(QUERY, {"ids": ["a1"]}).rows == (("a1", "b1"),)
        with database.begin("write") as writer:
            writer.execute(
                "MATCH (a:A {id:'a1'}),(b:B {id:'b1'}) CREATE (a)-[:R {confidence:0.7}]->(b)"
            )
        assert old.execute(QUERY, {"ids": ["a1", "a1"]}).rows == (("a1", "b1"),)
        assert database.execute(QUERY, {"ids": ["a1", "a1"]}).rows == (
            ("a1", "b1"),
            ("a1", "b1"),
        )


@pytest.mark.parametrize("authority", ["stale", "missing_many"])
def test_connected_anchor_authority_falls_back_to_canonical_scan(
    database, monkeypatch, authority
):
    """Exercise both refusals on the connected query, including edge work."""
    ids = {"ids": ["a1", "missing"]}
    with database.begin("read") as reader:
        with monkeypatch.context() as control:
            control.setattr(IndexManager, "validated_versions_many", None)
            expected = fixture._outcome(reader, QUERY, ids)
        if authority == "missing_many":
            monkeypatch.setattr(IndexManager, "validated_versions_many", None)
        else:
            from okto_grafx.engine import query_engine

            original = query_engine._IndexAuthorityProjection.named

            class Stale:
                stale = True

                def __init__(self, store):
                    self.store = store

                def __getattr__(self, name):
                    return getattr(self.store, name)

            def named(projection, name):
                store = original(projection, name)
                return None if store is None else Stale(store)

            monkeypatch.setattr(query_engine._IndexAuthorityProjection, "named", named)
        assert fixture._outcome(reader, QUERY, ids) == expected
        result = reader.execute(QUERY, ids)
        assert result.rows == (("a1", "b1"),)
        assert result.statistics.get("rows_scanned", 0) >= 5
        assert result.statistics.get("edge_scans", 0) + result.statistics.get("edge_lookups", 0) > 0


def test_optional_retains_canonical_null_extension(database):
    query = "OPTIONAL " + QUERY
    assert not seeks(database, query)
    assert database.execute(query, {"ids": ["missing"]}).rows == ((None, None),)


@pytest.mark.parametrize("ids", [[], None, ["missing"], ["a1"]])
@pytest.mark.parametrize(
    "predicate", ["a.v/0>1 AND a.id IN $ids", "a.id IN $ids AND a.v/0>1"]
)
def test_refusing_predicates_keep_order_and_phase(
    database, monkeypatch, ids, predicate
):
    query = "MATCH (a:A)-[:R]->(b:B) WHERE " + predicate + " RETURN a.id"
    expected = fixture._outcome(database, query, {"ids": ids})
    monkeypatch.setattr(IndexManager, "validated_versions_many", None)
    assert fixture._outcome(database, query, {"ids": ids}) == expected


def test_empty_destination_and_malformed_list_do_not_advance_refusal(
    database, monkeypatch
):
    # Schema checks still run even when the supplied IN list is empty.
    query = "MATCH (a:A)-[:R]->(b:D) WHERE a.id IN $ids RETURN a.id"
    assert fixture._outcome(database, query, {"ids": []})[0] == "erro"
    assert fixture._outcome(database, query, {"ids": 42})[0] == "erro"
    with database.begin("write") as tx:
        tx.execute("MATCH (a:A)-[r:R]->(b:B) DELETE r")
    # Membership argument typing is an eager execution check even without edges.
    expected = fixture._outcome(database, QUERY, {"ids": 42})
    assert expected[0] == "erro"
    monkeypatch.setattr(IndexManager, "validated_versions_many", None)
    assert fixture._outcome(database, QUERY, {"ids": 42}) == expected


def test_wide_probe_set_retains_existing_scan_policy(database):
    result = database.execute(
        QUERY, {"ids": ["a1"] + [f"missing{i}" for i in range(20)]}
    )
    assert result.rows == (("a1", "b1"),)
    assert result.statistics.get("fallback_reason_nonselective_probe_set") == 1


def test_exact_connected_fixture_reduces_anchor_scan_only(tmp_path, monkeypatch):
    with okto_grafx.connect(tmp_path / "large") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM A TO B)")
        with db.begin("write") as tx:
            tx.execute(
                "UNWIND $ids AS i CREATE (:A {id:i})",
                {"ids": [f"a{i}" for i in range(1024)]},
            )
            tx.execute(
                "UNWIND $ids AS i CREATE (:B {id:i})",
                {"ids": [f"b{i}" for i in range(128)]},
            )
            links = [{"a": f"a{i}", "b": f"b{i % 128}"} for i in range(1024)]
            links += [{"a": "a0", "b": f"b{i}"} for i in range(1, 128)]
            for chunk in (links[:1024], links[1024:]):
                tx.execute(
                    "UNWIND $links AS x MATCH (a:A {id:x.a}),(b:B {id:x.b}) CREATE (a)-[:R]->(b)",
                    {"links": chunk},
                )
        ids = [f"a{i}" for i in range(8)]
        fast = db.execute(QUERY, {"ids": ids})
        expected = [("a0", f"b{i}") for i in range(128)] + [
            (f"a{i}", f"b{i}") for i in range(1, 8)
        ]
        assert sorted(fast.rows) == sorted(expected)
        assert fast.statistics.get("rows_seeked") == 8
        assert fast.statistics.get("edge_lookups") == 8
        assert fast.statistics.get("edge_scans", 0) == 0
        assert fast.statistics.get("physical_edge_versions_examined", 1152) < 1151
        monkeypatch.setattr(IndexManager, "validated_versions_many", None)
        scan = db.execute(QUERY, {"ids": ids})
        assert fast.rows == scan.rows
        assert (
            fast.statistics["physical_node_versions_examined"]
            < scan.statistics["physical_node_versions_examined"]
        )
