"""Blocking acceptance tests for the two opt-in query row budgets."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxQueryBudgetExceeded
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.query_engine import QueryResult
from tests.query.stack import QueryStack, build_query_stack


def _stack(
    count: int,
    *,
    max_result_rows: int | None = None,
    max_intermediate_rows: int | None = None,
) -> QueryStack:
    """Return a real query stack with a predictable number of visible people."""
    stack = build_query_stack(
        max_result_rows=max_result_rows,
        max_intermediate_rows=max_intermediate_rows,
    )
    for identity in range(1, count + 1):
        stack.insert(
            "Person",
            identity,
            (identity, f"p{identity}", 20 + identity, "Sao Paulo"),
            csn=1,
        )
    return stack


def _run(stack: QueryStack, text: str) -> QueryResult:
    """Execute one read under the fixture's complete snapshot."""
    return stack.engine.execute(text, stack.transaction(read_lsn=1000))


def test_result_limit_accepts_exactly_and_stops_consuming_at_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal happens during iteration, not after materialising the remaining rows."""
    exact = _stack(3, max_result_rows=3)
    assert len(_run(exact, "MATCH (p:Person) RETURN p.id").rows) == 3

    refused = _stack(5, max_result_rows=2)
    original_scan = HeapStore.scan
    consumed = 0

    def counted_scan(
        heap: HeapStore, table: TableDef, snapshot: Snapshot
    ) -> Iterator[tuple[object, object]]:
        nonlocal consumed
        for found in original_scan(heap, table, snapshot):
            consumed += 1
            yield found

    monkeypatch.setattr(HeapStore, "scan", counted_scan)
    with pytest.raises(GrafxQueryBudgetExceeded) as raised:
        _run(refused, "MATCH (p:Person) RETURN p.id")

    assert raised.value.details == {
        "field": "max_result_rows",
        "limit": 2,
        "observed": 3,
    }
    assert consumed == 3


def test_intermediate_limit_is_per_operator_and_catches_work_hidden_by_limit() -> None:
    """A small final result does not excuse a child operator that emits too many rows."""
    text = "MATCH (p:Person) RETURN p.id ORDER BY p.id LIMIT 1"
    exact = _stack(3, max_result_rows=1, max_intermediate_rows=3)
    assert _run(exact, text).rows == ((1,),)

    refused = _stack(3, max_result_rows=1, max_intermediate_rows=2)
    with pytest.raises(GrafxQueryBudgetExceeded) as raised:
        _run(refused, text)

    assert raised.value.details == {
        "field": "max_intermediate_rows",
        "limit": 2,
        "observed": 3,
        "operator": "NodeScan",
    }


@pytest.mark.parametrize("field", ["max_result_rows", "max_intermediate_rows"])
def test_query_budget_refusal_never_releases_partial_writes(
    tmp_path: Path, field: str
) -> None:
    """Both refusal points preserve prior work and discard the whole current statement."""
    with connect(tmp_path / field, **{field: 2}) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE NODE TABLE Copy(id INT64, PRIMARY KEY(id))")
        for identity in (1, 2, 3):
            with database.begin("write") as seed:
                seed.execute(f"CREATE (:Person {{id: {identity}}})")

        transaction = database.begin("write")
        transaction.execute("CREATE (:Copy {id: 100})")
        accepted = tuple(transaction._context.row_intents)

        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            transaction.execute(
                "MATCH (p:Person) CREATE (:Copy {id: p.id}) "
                "RETURN p.id ORDER BY p.id"
            )

        assert raised.value.details["field"] == field
        assert raised.value.details["limit"] == 2
        assert raised.value.details["observed"] == 3
        assert tuple(transaction._context.row_intents) == accepted
        assert transaction.commit().wrote is True

        result = database.execute("MATCH (c:Copy) RETURN c.id ORDER BY c.id")
        assert result.rows == ((100,),)
