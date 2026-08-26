"""Pulse-compatible whole-payload replacement through the current public query API.

The Pulse adapter owns two immutable identity fields and replaces every other field with one
``MATCH ... SET`` statement.  These regressions deliberately stay outside Grafx internals: a
successful replacement is proved by public statistics, owner/outsider reads, the directed edge
multiset, a cold reopen, and ``verify()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.errors import GrafxWriteConflict


@dataclass(frozen=True)
class _NodeIdentity:
    """Fields the adapter carries across replacement instead of assigning."""

    id: str
    source_session_id: str


@dataclass(frozen=True)
class _Payload:
    """The complete mutable picture replaced by one adapter call."""

    kind: str
    payload_json: str
    score: float
    revision: int
    active: bool


TARGET = _NodeIdentity("memory-1", "session-canonical")
OUTBOUND = _NodeIdentity("memory-2", "session-outbound")
INBOUND = _NodeIdentity("memory-3", "session-inbound")
OTHER = _NodeIdentity("memory-4", "session-other")

ORIGINAL = _Payload("memory", '{"text":"before"}', 0.25, 1, True)
REPLACEMENT = _Payload("spec", '{"text":"after","rank":2}', 0.875, 9, False)
WINNER = _Payload("winner", '{"writer":"winner"}', 0.9, 20, True)
LOSER = _Payload("loser", '{"writer":"loser"}', 0.1, 19, False)

EXPECTED_EDGES: tuple[tuple[object, ...], ...] = tuple(
    sorted(
        (
            ("memory-1", "memory-1", "self", 4, 1.0),
            ("memory-1", "memory-2", "supports", 1, 0.5),
            ("memory-1", "memory-2", "supports", 2, 0.7),
            ("memory-2", "memory-3", "unrelated", 5, 0.25),
            ("memory-3", "memory-1", "derives", 3, 0.9),
            ("memory-3", "memory-1", "references", 6, 0.8),
        )
    )
)


@pytest.fixture
def graph_path(tmp_path: Path) -> Path:
    """Install the durable node/relationship schema used by one regression."""
    root = tmp_path / "pulse-replace"
    with okto_grafx.connect(root) as database:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE PulseNode("
                "id STRING, source_session_id STRING, kind STRING, payload_json STRING, "
                "score DOUBLE, revision INT64, active BOOL, PRIMARY KEY(id))"
            )
            schema.execute(
                "CREATE REL TABLE RELATED("
                "FROM PulseNode TO PulseNode, relation_kind STRING, ordinal INT64, "
                "weight DOUBLE)"
            )
    return root


def _payload_parameters(
    identity: _NodeIdentity, payload: _Payload
) -> dict[str, object]:
    return {
        "id": identity.id,
        "source_session_id": identity.source_session_id,
        "kind": payload.kind,
        "payload_json": payload.payload_json,
        "score": payload.score,
        "revision": payload.revision,
        "active": payload.active,
    }


def _seed(database: object) -> None:
    """Commit four nodes, then every incident shape plus an unrelated control edge."""
    with database.begin("write") as transaction:
        for identity, payload in (
            (TARGET, ORIGINAL),
            (OUTBOUND, _Payload("peer", "{}", 0.2, 1, True)),
            (INBOUND, _Payload("peer", "{}", 0.3, 1, True)),
            (OTHER, _Payload("peer", "{}", 0.4, 1, True)),
        ):
            transaction.execute(
                "CREATE (:PulseNode {"
                "id: $id, source_session_id: $source_session_id, kind: $kind, "
                "payload_json: $payload_json, score: $score, revision: $revision, "
                "active: $active})",
                _payload_parameters(identity, payload),
            )

    edges = (
        (TARGET, TARGET, "self", 4, 1.0),
        (TARGET, OUTBOUND, "supports", 1, 0.5),
        (TARGET, OUTBOUND, "supports", 2, 0.7),
        (OUTBOUND, INBOUND, "unrelated", 5, 0.25),
        (INBOUND, TARGET, "derives", 3, 0.9),
        (INBOUND, TARGET, "references", 6, 0.8),
    )
    with database.begin("write") as transaction:
        for source, target, relation_kind, ordinal, weight in edges:
            transaction.execute(
                "MATCH (a:PulseNode {id: $source}), (b:PulseNode {id: $target}) "
                "CREATE (a)-[:RELATED {"
                "relation_kind: $relation_kind, ordinal: $ordinal, weight: $weight}]->(b)",
                {
                    "source": source.id,
                    "target": target.id,
                    "relation_kind": relation_kind,
                    "ordinal": ordinal,
                    "weight": weight,
                },
            )


def _replace_node_payload(
    transaction: object, identity: _NodeIdentity, payload: _Payload
) -> object:
    """Model the adapter operation: one update, with neither identity field assigned."""
    return transaction.execute(
        "MATCH (n:PulseNode {id: $id, source_session_id: $source_session_id}) "
        "SET n.kind = $kind, n.payload_json = $payload_json, n.score = $score, "
        "n.revision = $revision, n.active = $active",
        _payload_parameters(identity, payload),
    )


def _node(subject: object, identity: _NodeIdentity) -> tuple[object, ...] | None:
    rows = subject.execute(
        "MATCH (n:PulseNode {id: $id, source_session_id: $source_session_id}) "
        "RETURN n.id, n.source_session_id, n.kind, n.payload_json, n.score, "
        "n.revision, n.active",
        {"id": identity.id, "source_session_id": identity.source_session_id},
    ).rows
    assert len(rows) <= 1
    return rows[0] if rows else None


def _expected_node(identity: _NodeIdentity, payload: _Payload) -> tuple[object, ...]:
    return (
        identity.id,
        identity.source_session_id,
        payload.kind,
        payload.payload_json,
        payload.score,
        payload.revision,
        payload.active,
    )


def _edges(subject: object) -> tuple[tuple[object, ...], ...]:
    rows = subject.execute(
        "MATCH (a:PulseNode)-[r:RELATED]->(b:PulseNode) "
        "RETURN a.id, b.id, r.relation_kind, r.ordinal, r.weight"
    ).rows
    return tuple(sorted(rows))


def test_replace_preserves_identity_and_edge_multiset_through_cold_reopen(
    graph_path: Path,
) -> None:
    """Incoming, outgoing, self and parallel edges survive one whole-payload SET."""
    with okto_grafx.connect(graph_path) as database:
        _seed(database)
        assert _edges(database) == EXPECTED_EDGES

        writer = database.begin("write")
        changed = _replace_node_payload(writer, TARGET, REPLACEMENT)

        assert changed.statistics["rows_updated"] == 1
        assert changed.statistics["properties_set"] == 5
        assert changed.statistics.get("rows_created", 0) == 0
        assert changed.statistics.get("rows_deleted", 0) == 0
        assert _node(writer, TARGET) == _expected_node(TARGET, REPLACEMENT)
        assert _edges(writer) == EXPECTED_EDGES

        outsider = database.begin("read")
        assert _node(outsider, TARGET) == _expected_node(TARGET, ORIGINAL)
        assert _edges(outsider) == EXPECTED_EDGES
        writer.commit()
        # A reader keeps its earlier snapshot even after the writer publishes.
        assert _node(outsider, TARGET) == _expected_node(TARGET, ORIGINAL)
        assert _edges(outsider) == EXPECTED_EDGES
        outsider.commit()

        assert _node(database, TARGET) == _expected_node(TARGET, REPLACEMENT)
        assert _edges(database) == EXPECTED_EDGES

    with okto_grafx.connect(graph_path) as reopened:
        assert _node(reopened, TARGET) == _expected_node(TARGET, REPLACEMENT)
        assert _edges(reopened) == EXPECTED_EDGES
        assert reopened.verify("all").findings == ()


def test_replace_rollback_restores_payload_identity_and_edges(graph_path: Path) -> None:
    with okto_grafx.connect(graph_path) as database:
        _seed(database)

        writer = database.begin("write")
        _replace_node_payload(writer, TARGET, REPLACEMENT)
        assert _node(writer, TARGET) == _expected_node(TARGET, REPLACEMENT)
        assert _node(database, TARGET) == _expected_node(TARGET, ORIGINAL)
        assert _edges(database) == EXPECTED_EDGES
        writer.rollback()

        assert _node(database, TARGET) == _expected_node(TARGET, ORIGINAL)
        assert _edges(database) == EXPECTED_EDGES
        assert database.verify("all").findings == ()


def test_conflicting_replacements_publish_only_the_winner(graph_path: Path) -> None:
    with okto_grafx.connect(graph_path) as database:
        _seed(database)

        loser = database.begin("write")
        winner = database.begin("write")
        _replace_node_payload(loser, TARGET, LOSER)
        _replace_node_payload(winner, TARGET, WINNER)
        assert _node(loser, TARGET) == _expected_node(TARGET, LOSER)
        assert _node(winner, TARGET) == _expected_node(TARGET, WINNER)

        winner.commit()
        with pytest.raises(GrafxWriteConflict):
            loser.commit()
        loser.rollback()

        assert _node(database, TARGET) == _expected_node(TARGET, WINNER)
        assert _edges(database) == EXPECTED_EDGES
        assert database.verify("all").findings == ()


def test_zero_match_replacement_is_a_successful_no_op(graph_path: Path) -> None:
    with okto_grafx.connect(graph_path) as database:
        _seed(database)
        mismatched_identity = _NodeIdentity(TARGET.id, "different-source-session")

        with database.begin("write") as writer:
            unchanged = _replace_node_payload(writer, mismatched_identity, REPLACEMENT)
            assert unchanged.statistics.get("rows_updated", 0) == 0
            assert unchanged.statistics.get("properties_set", 0) == 0

        assert _node(database, TARGET) == _expected_node(TARGET, ORIGINAL)
        assert _node(database, mismatched_identity) is None
        assert _edges(database) == EXPECTED_EDGES
        assert database.verify("all").findings == ()
