"""ST-1: a single hop starts at its seekable side, and an r-only hop scans the edges once."""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.ast import Direction
from okto_grafx.domain.query.plan import IndexSeek, NodeScan, TraverseRelationship
from okto_grafx.domain.query.planner import RELATIONSHIP_LOOKUP_FRONTIER_LIMIT


@pytest.fixture()
def database(tmp_path: Path):
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    try:
        yield handle
    finally:
        handle.close()


def _graph(db) -> None:
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")
    with db.begin("write") as txn:
        for identity in range(1, 6):
            txn.execute("CREATE (:A {id: $i})", {"i": identity})
        for identity in range(1, 4):
            txn.execute("CREATE (:B {id: $i})", {"i": identity})
    with db.begin("write") as txn:
        for source, target, weight in (
            (1, 1, 1),
            (1, 2, 2),
            (2, 1, 1),
            (3, 3, 2),
            (4, 1, 1),
            (5, 2, 2),
        ):
            txn.execute(
                "MATCH (a:A {id: $a}), (b:B {id: $b}) CREATE (a)-[:E {w: $w}]->(b)",
                {"a": source, "b": target, "w": weight},
            )


def _operators(handle, statement: str) -> set[str]:
    seen: set[str] = set()
    stack = [handle.explain(statement)]
    while stack:
        node = stack.pop()
        seen.add(type(node).__name__)
        stack.extend(node.children())
    return seen


def _find(handle, statement: str, kind: type) -> object:
    stack = [handle.explain(statement)]
    while stack:
        node = stack.pop()
        if isinstance(node, kind):
            return node
        stack.extend(node.children())
    raise AssertionError(f"{kind.__name__} not in the plan of {statement!r}")


# --- (a) the seekable side drives the hop -------------------------------------------------------


def test_a_hop_whose_target_is_seekable_starts_at_the_target(database) -> None:
    # ST-1 (a): today this plans NodeScan(a) + Traverse(outgoing) and scans all of A per row.
    # The seekable end is b, so the plan must start there: IndexSeek(b) + the SAME hop walked
    # with the mirrored direction, landing on a.
    _graph(database)
    statement = "MATCH (a:A)-[:E]->(b:B) WHERE b.id = 1 RETURN a.id"
    operators = _operators(database, statement)
    assert IndexSeek.__name__ in operators
    assert NodeScan.__name__ not in operators
    hop = _find(database, statement, TraverseRelationship)
    assert hop.source == "b"
    assert hop.target == "a"
    assert hop.direction is Direction.INCOMING
    assert sorted(database.execute(statement).rows) == [(1,), (2,), (4,)]


def test_the_inline_map_form_of_a_seekable_target_mirrors_too(database) -> None:
    _graph(database)
    statement = "MATCH (a:A)-[:E]->(b:B {id: 1}) RETURN a.id"
    operators = _operators(database, statement)
    assert IndexSeek.__name__ in operators
    assert NodeScan.__name__ not in operators
    assert sorted(database.execute(statement).rows) == [(1,), (2,), (4,)]


def test_a_hop_that_already_starts_at_its_seekable_side_keeps_its_shape(
    database,
) -> None:
    # The reverse-arrow spelling already drives from b; nothing may change for it.
    _graph(database)
    statement = "MATCH (b:B {id: 1})<-[:E]-(a:A) RETURN a.id"
    operators = _operators(database, statement)
    assert IndexSeek.__name__ in operators
    assert NodeScan.__name__ not in operators
    assert sorted(database.execute(statement).rows) == [(1,), (2,), (4,)]


def test_a_bound_target_is_never_mirrored(database) -> None:
    # `b` arrives bound from an earlier pattern; the hop is a filter on the landing, and the
    # source keeps its scan exactly as today.
    _graph(database)
    statement = "MATCH (b:B) MATCH (a:A)-[:E]->(b) WHERE b.id = 1 RETURN a.id"
    hop = _find(database, statement, TraverseRelationship)
    assert hop.source == "a"
    assert hop.target_bound is True
    assert sorted(database.execute(statement).rows) == [(1,), (2,), (4,)]


def test_both_labels_are_validated_against_the_relationship_endpoints(database) -> None:
    # ST-1 (a): today only the SOURCE label is validated; a wrong TARGET label plans fine and
    # silently matches nothing. Both ends of a typed hop are declared by the relationship, so
    # both are refused at planning time.
    _graph(database)
    with pytest.raises(GrafxPlanError) as refused:
        database.explain("MATCH (a:A)-[:E]->(b:A) RETURN a.id")
    assert "E" in str(refused.value)
    with pytest.raises(GrafxPlanError):
        database.execute("MATCH (a:A)-[:E]->(b:A) RETURN a.id")


# --- (b) an r-only hop scans the relationship once ----------------------------------------------


def test_an_unseekable_hop_whose_where_reads_only_r_plans_a_relationship_scan(
    database,
) -> None:
    _graph(database)
    statement = "MATCH (a:A)-[r:E]->(b:B) WHERE r.w = 1 RETURN a.id, b.id"
    operators = _operators(database, statement)
    assert "RelationshipScan" in operators
    assert NodeScan.__name__ not in operators
    assert TraverseRelationship.__name__ not in operators
    # The r-only predicate is evaluated inside the scan, so no FilterRows remains above it.
    assert "FilterRows" not in operators


def test_a_no_predicate_hop_uses_one_relationship_scan_without_a_small_limit(
    database,
) -> None:
    _graph(database)
    statement = "MATCH (a:A)-[r:E]->(b:B) RETURN a.id, b.id, r.w"
    operators = _operators(database, statement)
    assert RELATIONSHIP_LOOKUP_FRONTIER_LIMIT == 64
    assert "RelationshipScan" in operators
    assert NodeScan.__name__ not in operators
    assert TraverseRelationship.__name__ not in operators
    assert sorted(database.execute(statement).rows) == [
        (1, 1, 1),
        (1, 2, 2),
        (2, 1, 1),
        (3, 3, 2),
        (4, 1, 1),
        (5, 2, 2),
    ]


def test_the_no_predicate_cost_boundary_is_literal_64_versus_65(database) -> None:
    _graph(database)
    at_boundary = _operators(
        database,
        "MATCH (a:A)-[r:E]->(b:B) RETURN a.id LIMIT 64",
    )
    above_boundary = _operators(
        database,
        "MATCH (a:A)-[r:E]->(b:B) RETURN a.id LIMIT 65",
    )
    assert "RelationshipScan" not in at_boundary
    assert TraverseRelationship.__name__ in at_boundary
    assert "RelationshipScan" in above_boundary
    assert TraverseRelationship.__name__ not in above_boundary


def test_an_aggregate_consumes_the_relationship_scan_even_with_limit_one(database) -> None:
    _graph(database)
    statement = "MATCH (a:A)-[r:E]->(b:B) RETURN count(r) LIMIT 1"
    operators = _operators(database, statement)
    assert "RelationshipScan" in operators
    assert TraverseRelationship.__name__ not in operators
    assert database.execute(statement).rows == ((6,),)


def test_the_relationship_scan_answers_the_same_rows_and_delete_works(database) -> None:
    _graph(database)
    matched = database.execute(
        "MATCH (a:A)-[r:E]->(b:B) WHERE r.w = 1 RETURN a.id, b.id"
    ).rows
    assert sorted(matched) == [(1, 1), (2, 1), (4, 1)]
    with database.begin("write") as txn:
        txn.execute("MATCH (a:A)-[r:E]->(b:B) WHERE r.w = 1 DELETE r")
    remaining = database.execute("MATCH (a:A)-[r:E]->(b:B) RETURN a.id, b.id").rows
    assert sorted(remaining) == [(1, 2), (3, 3), (5, 2)]


def test_edges_ended_earlier_in_the_transaction_are_not_rematched(database) -> None:
    _graph(database)
    with database.begin("write") as txn:
        txn.execute("MATCH (a:A)-[r:E]->(b:B) WHERE r.w = 1 DELETE r")
        txn.execute(
            "MATCH (a:A)-[r:E]->(b:B) WHERE r.w = 1 DELETE r"
        )  # second pass: none left
    remaining = database.execute("MATCH (a:A)-[r:E]->(b:B) RETURN a.id").rows
    assert len(remaining) == 3


def test_relationship_scan_includes_pending_edges_and_latest_owner_properties(
    database,
) -> None:
    _graph(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:A {id: 6})")
        txn.execute("CREATE (:B {id: 4})")
        txn.execute(
            "MATCH (a:A {id: 6}), (b:B {id: 4}) CREATE (a)-[:E {w: 7}]->(b)"
        )
        assert txn.execute(
            "MATCH (a:A)-[r:E]->(b:B) WHERE r.w = 7 RETURN a.id, b.id"
        ).rows == ((6, 4),)

        txn.execute("MATCH (a:A)-[r:E]->(b:B) WHERE r.w = 2 SET r.w = 8")
        assert sorted(
            txn.execute(
                "MATCH (a:A)-[r:E]->(b:B) WHERE r.w = 8 RETURN a.id, b.id"
            ).rows
        ) == [(1, 2), (3, 3), (5, 2)]


def test_a_predicate_reading_an_endpoint_keeps_the_traversal_shape(database) -> None:
    # Reading `a` in WHERE takes the hop out of (b): endpoints are part of the predicate, so
    # the plan keeps today's traversal and filter.
    _graph(database)
    statement = "MATCH (a:A)-[r:E]->(b:B) WHERE r.w = 1 AND a.id > 1 RETURN a.id"
    operators = _operators(database, statement)
    assert "RelationshipScan" not in operators
    assert TraverseRelationship.__name__ in operators
    assert sorted(database.execute(statement).rows) == [(2,), (4,)]


def test_multi_hop_and_untyped_hops_keep_their_operators(database) -> None:
    _graph(database)
    ranged = _operators(database, "MATCH (a:A)-[r:E*1..2]->(b) RETURN a.id")
    assert "RelationshipScan" not in ranged
    assert TraverseRelationship.__name__ in ranged
