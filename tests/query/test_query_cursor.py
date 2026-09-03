"""Bounded public query streaming over one cursor-owned MVCC snapshot."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import okto_grafx
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxQueryBudgetExceeded,
    GrafxUnsupportedOperation,
)
from okto_grafx.engine.query_engine import QueryEngine


@pytest.fixture
def database() -> Iterator[okto_grafx.Database]:
    handle = okto_grafx.connect(":memory:", page_size=512)
    with handle.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
        )
    with handle.begin("write") as seed:
        for identity in range(12):
            seed.execute(
                "CREATE (:Person {id: $id, name: $name})",
                {"id": identity, "name": f"person-{identity}"},
            )
    try:
        yield handle
    finally:
        handle.close()


def test_cursor_streams_without_using_materialised_result_collection(
    database: okto_grafx.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_collection(self: QueryEngine, rows: object) -> object:
        raise AssertionError("streaming cursor reached materialised collection")

    monkeypatch.setattr(QueryEngine, "_collect_result_rows", refuse_collection)

    with database.query("MATCH (p:Person) RETURN p.id").cursor(batch_size=3) as rows:
        assert rows.columns == ("p.id",)
        assert tuple(value[0] for value in rows) == tuple(range(12))
        assert rows.closed is True

    assert database.transactions.open_transactions == 0


def test_cursor_owns_a_fixed_snapshot_until_it_is_exhausted(
    database: okto_grafx.Database,
) -> None:
    cursor = database.query("MATCH (p:Person) RETURN p.id").cursor(batch_size=1)
    try:
        first = cursor.fetchone()
        assert first == (0,)
        assert database.transactions.open_transactions == 1

        with database.begin("write") as writer:
            writer.execute("CREATE (:Person {id: 99, name: 'later'})")

        remaining = tuple(cursor)
        assert 99 not in {row[0] for row in remaining}
        assert len(remaining) == 11
        assert cursor.closed is True
        assert database.transactions.open_transactions == 0
    finally:
        cursor.close()


def test_early_close_discards_a_bounded_buffer_and_releases_the_reader(
    database: okto_grafx.Database,
) -> None:
    cursor = database.query("MATCH (p:Person) RETURN p.id").cursor(batch_size=4)
    assert next(cursor) == (0,)
    assert len(cursor._buffer) <= 4
    assert database.transactions.open_transactions == 1

    cursor.close()
    cursor.close()

    assert cursor.closed is True
    assert cursor.fetchone() is None
    assert database.transactions.open_transactions == 0


def test_database_close_makes_an_open_buffered_cursor_terminal() -> None:
    database = okto_grafx.connect(":memory:")
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
    with database.begin("write") as writer:
        for identity in range(3):
            writer.execute("CREATE (:P {id: $id})", {"id": identity})
    cursor = database.query("MATCH (p:P) RETURN p.id").cursor(batch_size=3)
    assert next(cursor) == (0,)

    database.close()

    with pytest.raises(GrafxUnsupportedOperation):
        next(cursor)
    assert cursor.closed is True
    cursor.close()


def test_query_copies_parameters_before_a_later_cursor_opens(
    database: okto_grafx.Database,
) -> None:
    supplied = {"ids": [2, 4, 6]}
    query = database.query("UNWIND $ids AS id RETURN id", supplied)
    supplied["ids"].append(8)

    with query.cursor(batch_size=2) as cursor:
        assert tuple(cursor) == ((2,), (4,), (6,))


def test_cursor_refuses_writes_before_any_row_is_staged(
    database: okto_grafx.Database,
) -> None:
    with pytest.raises(GrafxUnsupportedOperation) as refused:
        database.query("CREATE (:Person {id: 88, name: 'no'}) RETURN 88").cursor()

    assert refused.value.details["field"] == "cursor"
    assert database.transactions.open_transactions == 0
    assert database.execute("MATCH (p:Person) WHERE p.id = 88 RETURN p.id").rows == ()


def test_result_budget_refusal_closes_the_cursor_transaction() -> None:
    database = okto_grafx.connect(":memory:", max_result_rows=2)
    try:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        with database.begin("write") as writer:
            for identity in range(3):
                writer.execute("CREATE (:P {id: $id})", {"id": identity})

        cursor = database.query("MATCH (p:P) RETURN p.id").cursor(batch_size=2)
        assert cursor.fetchmany() == ((0,), (1,))
        with pytest.raises(GrafxQueryBudgetExceeded) as refused:
            cursor.fetchone()

        assert refused.value.details == {
            "field": "max_result_rows",
            "limit": 2,
            "observed": 3,
        }
        assert cursor.closed is True
        assert database.transactions.open_transactions == 0
    finally:
        database.close()


@pytest.mark.parametrize("batch_size", [0, -1, 65_537, True])
def test_cursor_batch_size_is_strictly_bounded(
    database: okto_grafx.Database, batch_size: object
) -> None:
    query = database.query("RETURN 1")
    with pytest.raises(GrafxConfigurationError):
        query.cursor(batch_size=batch_size)  # type: ignore[arg-type]
    assert database.transactions.open_transactions == 0
