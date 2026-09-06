"""Closed multi-key incident-edge access path used by the Pulse graph projection."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.plan import (
    FilterRows,
    RelationshipIncidentSeek,
    TraverseRelationship,
)
from okto_grafx.engine.index_manager import IndexManager


QUERY = (
    "MATCH (a:A)-[r:R]->(b:B) "
    "WHERE a.id IN $node_ids OR b.id IN $node_ids "
    "RETURN a.id, b.id, r.confidence LIMIT 5000"
)
INCOMING_QUERY = (
    "MATCH (b:B)<-[r:R]-(a:A) "
    "WHERE a.id IN $node_ids OR b.id IN $node_ids "
    "RETURN a.id, b.id, r.confidence LIMIT 5000"
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    with handle.begin("write") as transaction:
        transaction.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
        transaction.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
        transaction.execute("CREATE REL TABLE R(FROM A TO B, confidence DOUBLE)")
    with handle.begin("write") as transaction:
        for name in ("a1", "a2", "a3"):
            transaction.execute("CREATE (:A {id: $id})", {"id": name})
        for name in ("b1", "b2", "b3"):
            transaction.execute("CREATE (:B {id: $id})", {"id": name})
        for source, target, confidence in (
            ("a1", "b1", 0.4),
            ("a1", "b1", 0.5),
            ("a2", "b1", 0.6),
            ("a3", "b3", 0.7),
        ):
            transaction.execute(
                "MATCH (a:A {id: $source}), (b:B {id: $target}) "
                "CREATE (a)-[:R {confidence: $confidence}]->(b)",
                {
                    "source": source,
                    "target": target,
                    "confidence": confidence,
                },
            )
    handle.ensure_identity_indexes()
    try:
        yield handle
    finally:
        handle.close()


def test_closed_endpoint_union_retains_a_canonical_fallback(database: object) -> None:
    planned = database.explain(QUERY)
    incident = next(
        node for node in planned.walk() if type(node) is RelationshipIncidentSeek
    )

    assert type(incident.fallback) is FilterRows
    assert any(type(node) is TraverseRelationship for node in incident.fallback.walk())
    assert incident.from_table.name == "A"
    assert incident.to_table.name == "B"


def test_incident_union_preserves_parallel_edges_and_either_endpoint(
    database: object,
) -> None:
    rows = database.execute(QUERY, {"node_ids": ["a1", "b1"]}).rows

    assert sorted(rows) == [
        ("a1", "b1", 0.4),
        ("a1", "b1", 0.5),
        ("a2", "b1", 0.6),
    ]


def test_incoming_syntax_keeps_stored_endpoint_roles(database: object) -> None:
    incident = next(
        node
        for node in database.explain(INCOMING_QUERY).walk()
        if type(node) is RelationshipIncidentSeek
    )

    assert incident.from_variable == "a"
    assert incident.to_variable == "b"
    assert database.execute(INCOMING_QUERY, {"node_ids": ["a3"]}).rows == (
        ("a3", "b3", 0.7),
    )


def test_missing_multi_key_capability_executes_the_retained_fallback(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(IndexManager, "validated_versions_many", None, raising=False)

    assert database.execute(QUERY, {"node_ids": ["a3"]}).rows == (
        ("a3", "b3", 0.7),
    )


def test_non_exact_probe_values_return_to_canonical_membership(database: object) -> None:
    assert database.execute(QUERY, {"node_ids": [None, 1, "b3"]}).rows == (
        ("a3", "b3", 0.7),
    )


def test_non_list_parameter_preserves_the_public_in_refusal(database: object) -> None:
    with pytest.raises(GrafxPlanError) as raised:
        database.execute(QUERY, {"node_ids": "a1"})

    assert raised.value.details["field"] == "operator"
    assert raised.value.details["value"] == "IN"


def test_opposite_landings_share_one_identity_certificate(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IndexManager.validated_versions_many
    calls: list[tuple[int, ...]] = []

    def observed(manager, index, keys, snapshot):
        calls.append(index.definition.positions)
        return original(manager, index, keys, snapshot)

    monkeypatch.setattr(IndexManager, "validated_versions_many", observed)

    assert database.execute(QUERY, {"node_ids": ["a1"]}).rows == (
        ("a1", "b1", 0.4),
        ("a1", "b1", 0.5),
    )
    assert calls.count(()) == 1
    assert len(calls) == 5


@pytest.mark.parametrize(
    "node_ids",
    ([], ["a1"], ["b1"], ["a1", "b1"], ["a3"], [None, 1, "b3"]),
)
def test_incident_path_is_differentially_equal_to_its_canonical_fallback(
    database: object,
    monkeypatch: pytest.MonkeyPatch,
    node_ids: list[object],
) -> None:
    accelerated = database.execute(QUERY, {"node_ids": node_ids}).rows
    monkeypatch.setattr(IndexManager, "validated_versions_many", None, raising=False)
    canonical = database.execute(QUERY, {"node_ids": node_ids}).rows

    assert sorted(accelerated) == sorted(canonical)


def test_owner_dirty_tables_keep_read_your_writes_on_the_canonical_path(
    database: object,
) -> None:
    with database.begin("write") as transaction:
        transaction.execute(
            "MATCH (a:A {id: 'a2'}), (b:B {id: 'b2'}) "
            "CREATE (a)-[:R {confidence: 0.8}]->(b)"
        )

        assert transaction.execute(QUERY, {"node_ids": ["b2"]}).rows == (
            ("a2", "b2", 0.8),
        )


@pytest.mark.parametrize(
    "text",
    (
        "MATCH (a:A)-[r:R]->(b:B) WHERE a.id IN $ids RETURN a.id LIMIT 5000",
        "MATCH (a:A)-[r:R]->(b:B) WHERE a.id IN $ids AND b.id IN $ids "
        "RETURN a.id LIMIT 5000",
        "MATCH (a:A)-[r:R]->(b:B) WHERE a.id IN $ids OR b.id = 'b1' "
        "RETURN a.id LIMIT 5000",
        "MATCH (a:A)-[r:R]->(b:B) WHERE a.id IN $ids OR b.id IN $ids "
        "RETURN a.id LIMIT 64",
    ),
)
def test_near_misses_keep_the_existing_traversal(text: str, database: object) -> None:
    planned = database.explain(text)

    assert not any(type(node) is RelationshipIncidentSeek for node in planned.walk())
    assert any(type(node) is TraverseRelationship for node in planned.walk())
