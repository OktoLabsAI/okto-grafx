"""Acceptance contract for the bounded, snapshot-pinned ``scan_rows_v1`` door.

These tests intentionally exercise the public transaction surface.  The one private seam is the
``HeapStore._decode_version`` spy: without it a page that decodes ``limit + 1`` rows (or restarts
from the beginning for every cursor) returns the right values while silently violating the memory
and work bound that this door exists to provide.
"""

from __future__ import annotations

import pickle
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import ScanCursorV1, ScanPageV1, ScanRowV1, VectorValue, connect
from okto_grafx.errors import GrafxConfigurationError, GrafxTransactionStateError
from okto_grafx.engine.heap_store import HeapStore


def _node_schema(database: Any, table: str = "Item") -> None:
    with database.begin("write") as transaction:
        transaction.execute(
            f"CREATE NODE TABLE {table}(id INT64, label STRING, PRIMARY KEY(id))"
        )


def _insert_items(database: Any, *identities: int, table: str = "Item") -> None:
    with database.begin("write") as transaction:
        for identity in identities:
            transaction.execute(
                f"CREATE (:{table} {{id: $id, label: $label}})",
                {"id": identity, "label": f"item-{identity}"},
            )


def _all_pages(transaction: Any, table: str, *, limit: int) -> tuple[ScanRowV1, ...]:
    rows: list[ScanRowV1] = []
    cursor: ScanCursorV1 | None = None
    while True:
        page = transaction.scan_rows_v1(table, limit=limit, cursor=cursor)
        assert isinstance(page, ScanPageV1)
        rows.extend(page.rows)
        cursor = page.next_cursor
        if cursor is None:
            return tuple(rows)


def test_pages_stay_on_one_snapshot_while_a_writer_commits_between_them(
    tmp_path: Path,
) -> None:
    """C1: a cursor continues its transaction's snapshot, never the latest database view."""
    with connect(tmp_path / "db", page_size=512) as database:
        _node_schema(database)
        _insert_items(database, 1, 2, 3, 4)

        reader = database.begin("read")
        try:
            first = reader.scan_rows_v1("Item", limit=2)
            assert tuple(row.values[0] for row in first.rows) == (1, 2)
            assert first.next_cursor is not None

            # This commit happens while ``reader`` remains active and between its two pages.  The
            # large values force several new heap pages: the continuation must stop at the
            # physical ceiling captured by its first page, not misdiagnose the append as a cycle.
            with database.begin("write") as writer:
                for identity in range(5, 25):
                    writer.execute(
                        "CREATE (:Item {id: $id, label: $label})",
                        {"id": identity, "label": "x" * 180},
                    )

            second = reader.scan_rows_v1("Item", limit=2, cursor=first.next_cursor)
            assert tuple(row.values[0] for row in second.rows) == (3, 4)
            assert second.next_cursor is None
            assert tuple(row.values[0] for row in first.rows + second.rows) == (
                1,
                2,
                3,
                4,
            )
        finally:
            reader.rollback()

        with database.begin("read") as later:
            assert tuple(
                row.values[0] for row in _all_pages(later, "Item", limit=2)
            ) == (
                1,
                2,
                3,
                4,
                *range(5, 25),
            )


def test_rows_preserve_null_vectors_and_each_physical_relationship_occurrence(
    tmp_path: Path,
) -> None:
    """C2: values are complete and parallel, reverse and self edges remain distinct rows."""
    root = tmp_path / "db"
    with connect(root, page_size=512) as database:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE VECTOR SPACE semantic {dimension: 3, metric: 'cosine'}"
            )
            schema.execute(
                "CREATE NODE TABLE Entity("
                "id INT64, name STRING, enabled BOOL, score DOUBLE, note STRING, "
                "embedding VECTOR(semantic), PRIMARY KEY(id))"
            )
            schema.execute(
                "CREATE REL TABLE Links(FROM Entity TO Entity, kind STRING, weight DOUBLE)"
            )
        with database.begin("write") as writer:
            writer.execute(
                "CREATE (:Entity {id: 1, name: 'one', enabled: true, score: 1.5, "
                "note: NULL, embedding: [1.0, 0.0, 0.0]})"
            )
            writer.execute(
                "CREATE (:Entity {id: 2, name: '', enabled: false, score: -0.0})"
            )
        with database.begin("write") as writer:
            statements = (
                (1, 2, "same", 0.5),
                (1, 2, "same", 0.5),
                (1, 2, "other", 0.75),
                (2, 1, "reverse", 1.0),
                (1, 1, "loop", 2.0),
            )
            for source, target, kind, weight in statements:
                writer.execute(
                    "MATCH (a:Entity {id: $source}), (b:Entity {id: $target}) "
                    "CREATE (a)-[:Links {kind: $kind, weight: $weight}]->(b)",
                    {
                        "source": source,
                        "target": target,
                        "kind": kind,
                        "weight": weight,
                    },
                )

        with database.begin("read") as reader:
            nodes = _all_pages(reader, "Entity", limit=1)
            relationships = _all_pages(reader, "Links", limit=2)

        by_key = {row.values[0]: row for row in nodes}
        vector = VectorValue(
            values=(1.0, 0.0, 0.0),
            space_ref=database.catalog.catalog.space("semantic").space_id,
        )
        assert by_key[1].values == (1, "one", True, 1.5, None, vector)
        assert by_key[2].values == (2, "", False, -0.0, None, None)
        assert by_key[2].values[3] == 0.0

        first_id = by_key[1].record_id
        second_id = by_key[2].record_id
        assert tuple(row.values for row in relationships) == (
            (first_id, second_id, "same", 0.5),
            (first_id, second_id, "same", 0.5),
            (first_id, second_id, "other", 0.75),
            (second_id, first_id, "reverse", 1.0),
            (first_id, first_id, "loop", 2.0),
        )
        assert len({row.record_id for row in relationships}) == len(relationships) == 5

    # DTOs are detached and frozen: their contents survive both transaction and database close.
    assert by_key[1].values[-1] == vector
    assert relationships[0].values[:2] == (first_id, second_id)
    with pytest.raises(FrozenInstanceError):
        relationships[0].record_id = 999  # type: ignore[misc]


def test_each_page_decodes_only_its_rows_and_continuation_does_not_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3: neither look-ahead nor N-times-page rescans are hidden by the returned values."""
    with connect(tmp_path / "db", page_size=512) as database:
        _node_schema(database)
        _insert_items(database, *range(1, 10))
        decoded: list[int] = []
        original = HeapStore._decode_version_with_header

        def counted_decode(
            store: HeapStore, table: Any, header: Any, content: bytes
        ) -> Any:
            version = original(store, table, header, content)
            decoded.append(version.record_id)
            return version

        monkeypatch.setattr(HeapStore, "_decode_version_with_header", counted_decode)
        with database.begin("read") as reader:
            first = reader.scan_rows_v1("Item", limit=2)
            assert len(first.rows) == 2
            assert len(decoded) == 2, "the first page decoded a hidden limit+1 row"

            rows = list(first.rows)
            cursor = first.next_cursor
            while cursor is not None:
                page = reader.scan_rows_v1("Item", limit=2, cursor=cursor)
                rows.extend(page.rows)
                cursor = page.next_cursor

        assert tuple(row.values[0] for row in rows) == tuple(range(1, 10))
        assert (
            len(decoded) == 9
        ), "continuation restarted and decoded earlier pages again"
        assert len(set(decoded)) == 9


@pytest.mark.parametrize("invalid", [True, False, 0, -1, 1.5, "2", object()])
def test_scan_refuses_invalid_limits(tmp_path: Path, invalid: object) -> None:
    """C4: limits are positive built-in integers; bool is not accepted as an integer."""
    with connect(tmp_path / "db") as database:
        _node_schema(database)
        with database.begin("read") as reader:
            with pytest.raises(GrafxConfigurationError):
                reader.scan_rows_v1("Item", limit=invalid)  # type: ignore[arg-type]


def test_scan_is_read_only_and_cursors_are_nominal_scoped_capabilities(
    tmp_path: Path,
) -> None:
    """C4: lifecycle, mode and database/transaction/table ownership all fail closed."""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    with connect(first_root) as first_database, connect(second_root) as second_database:
        for database in (first_database, second_database):
            _node_schema(database)
            _node_schema(database, "Other")
            _insert_items(database, 1, 2, 3)

        write = first_database.begin("write")
        try:
            with pytest.raises(GrafxTransactionStateError):
                write.scan_rows_v1("Item", limit=1)
        finally:
            write.rollback()

        source = first_database.begin("read")
        first = source.scan_rows_v1("Item", limit=1)
        cursor = first.next_cursor
        assert cursor is not None
        with pytest.raises(GrafxConfigurationError):
            ScanCursorV1()
        with pytest.raises(TypeError, match="process-local"):
            pickle.dumps(cursor)

        with first_database.begin("read") as other_transaction:
            with pytest.raises(GrafxTransactionStateError):
                other_transaction.scan_rows_v1("Item", limit=1, cursor=cursor)
        with pytest.raises(GrafxTransactionStateError):
            source.scan_rows_v1("Other", limit=1, cursor=cursor)
        with second_database.begin("read") as other_database_transaction:
            with pytest.raises(GrafxTransactionStateError):
                other_database_transaction.scan_rows_v1("Item", limit=1, cursor=cursor)

        for forged in (object(), object.__new__(ScanCursorV1)):
            with pytest.raises(GrafxConfigurationError):
                source.scan_rows_v1("Item", limit=1, cursor=forged)  # type: ignore[arg-type]

        cursor_subtype = type("CursorSubtype", (ScanCursorV1,), {})
        subtype_instance = object.__new__(cursor_subtype)
        with pytest.raises(GrafxConfigurationError):
            source.scan_rows_v1(
                "Item", limit=1, cursor=subtype_instance  # type: ignore[arg-type]
            )

        continued = source.scan_rows_v1("Item", limit=1, cursor=cursor)
        assert tuple(row.values[0] for row in continued.rows) == (2,)
        with pytest.raises(GrafxTransactionStateError, match="cannot be reused"):
            source.scan_rows_v1("Item", limit=1, cursor=cursor)

        source.rollback()
        with pytest.raises(GrafxTransactionStateError):
            source.scan_rows_v1("Item", limit=1)


def test_relationship_scan_keeps_a_row_hidden_by_a_plain_deleted_endpoint(
    tmp_path: Path,
) -> None:
    """C5: the physical scan is not a traversal and therefore does not join endpoints."""
    with connect(tmp_path / "db", page_size=512) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE REL TABLE Knows(FROM Person TO Person, since INT64)")
        with database.begin("write") as writer:
            writer.execute("CREATE (:Person {id: 1})")
            writer.execute("CREATE (:Person {id: 2})")
        with database.begin("write") as writer:
            writer.execute(
                "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
                "CREATE (a)-[:Knows {since: 2026}]->(b)"
            )
        with database.begin("read") as before_delete:
            original = before_delete.scan_rows_v1("Knows", limit=1).rows
        assert len(original) == 1

        with database.begin("write") as writer:
            writer.execute("MATCH (p:Person {id: 1}) DELETE p")

        assert (
            database.execute(
                "MATCH (a:Person)-[r:Knows]->(b:Person) RETURN a.id, b.id"
            ).rows
            == ()
        )
        with database.begin("read") as after_delete:
            physical = after_delete.scan_rows_v1("Knows", limit=1)
        assert physical.rows == original
        assert physical.next_cursor is None
