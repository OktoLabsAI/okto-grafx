"""DETACH keeps fail-closed relationship validation while projecting only endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE


SCHEMA: tuple[str, ...] = (
    "CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))",
    "CREATE REL TABLE Knows(FROM Person TO Person, note STRING)",
)


class _EverythingVisible:
    def visible(self, _xmin: int, _xmax: int) -> bool:
        return True


@pytest.fixture
def database() -> Iterator[object]:
    handle = okto_grafx.connect(":memory:")
    try:
        with handle.begin("write") as schema:
            for statement in SCHEMA:
                schema.execute(statement)
        with handle.begin("write") as writer:
            for identity in (1, 2, 3, 4):
                writer.execute(f"CREATE (:Person {{id: {identity}}})")
        yield handle
    finally:
        handle.close()


def _edge(database: object, source: int, target: int, note: str) -> None:
    with database.begin("write") as writer:
        writer.execute(
            f"MATCH (a:Person {{id: {source}}}), (b:Person {{id: {target}}}) "
            f"CREATE (a)-[:Knows {{note: '{note}'}}]->(b)"
        )


def _corrupt_property_of(database: object, note: str) -> None:
    table = database.catalog.catalog.table("Knows")
    matches = [
        ref
        for ref, version in database._heap.scan(table, _EverythingVisible())
        if version.values[2] == note
    ]
    assert len(matches) == 1
    ref = matches[0]
    _table_id, content = database._heap._read_slot(ref)
    broken = bytearray(content)
    # Two fixed INT64 endpoints occupy 18 bytes; the next byte is the property tag.
    broken[RECORD_HEADER_SIZE + 18] = 0xFF
    with database._pool.pinned(database._heap.file, ref.page) as page:
        page.update_slot(ref.slot, bytes(broken))


@pytest.mark.parametrize("corrupt_position", ("before", "after"))
def test_detach_validates_a_nonincident_corrupt_row_before_releasing_any_end(
    database: object,
    corrupt_position: str,
) -> None:
    edges = (
        ((3, 4, "corrupt"), (1, 2, "incident"))
        if corrupt_position == "before"
        else ((1, 2, "incident"), (3, 4, "corrupt"))
    )
    for source, target, note in edges:
        _edge(database, source, target, note)
    _corrupt_property_of(database, "corrupt")

    writer = database.begin("write")
    try:
        with pytest.raises(GrafxCorruptionDetected) as raised:
            writer.execute("MATCH (p:Person {id: 1}) DETACH DELETE p")
        assert raised.value.details["field"] == "tag"
        assert tuple(writer._context.row_intents) == (), (
            "a failed statement released no partial deletes"
        )
    finally:
        writer.rollback()

    assert database.execute("MATCH (p:Person {id: 1}) RETURN p.id").rows == ((1,),)


def test_detach_after_a_relationship_update_preserves_the_transaction_overlay(
    database: object,
) -> None:
    _edge(database, 1, 2, "original")

    with database.begin("write") as writer:
        changed = writer.execute(
            "MATCH (a:Person)-[r:Knows]->(b:Person) "
            "WHERE a.id = 1 SET r.note = 'changed'"
        )
        detached = writer.execute("MATCH (p:Person {id: 1}) DETACH DELETE p")

    assert changed.statistics["rows_updated"] == 1
    assert detached.statistics["rows_deleted"] == 2
    assert database.execute(
        "MATCH (a:Person)-[r:Knows]->(b:Person) RETURN r.note"
    ).rows == ()
    assert database.verify("all").findings == ()
