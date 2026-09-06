"""Fast rejection of the transaction-wide ended-row view when no DELETE exists."""

from __future__ import annotations

from types import SimpleNamespace

from okto_grafx.domain.txn.context import RowOperation
from okto_grafx.engine import query_engine


def test_ended_precheck_does_not_build_dirty_table_views_without_delete(
    monkeypatch,
) -> None:
    tables = tuple(SimpleNamespace(table_id=position) for position in range(1, 31))
    context = SimpleNamespace(
        staged_rows=[],
        txn=SimpleNamespace(
            row_intents=[
                SimpleNamespace(table=table, operation=RowOperation.INSERT)
                for table in tables
                for _ in range(10)
            ]
        ),
    )

    def unexpected_view(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an INSERT-only transaction has no ended-row view to build")

    monkeypatch.setattr(query_engine, "_transaction_row_view", unexpected_view)

    assert query_engine._ended_by_this_transaction(context) == set()


def test_ended_precheck_delegates_to_canonical_views_when_delete_exists(
    monkeypatch,
) -> None:
    first = SimpleNamespace(table_id=1)
    second = SimpleNamespace(table_id=2)
    deleted_reference = object()
    context = SimpleNamespace(
        staged_rows=[],
        txn=SimpleNamespace(
            row_intents=[
                SimpleNamespace(table=first, operation=RowOperation.INSERT),
                SimpleNamespace(table=second, operation=RowOperation.DELETE),
            ]
        ),
    )
    visited: list[int] = []

    def canonical_view(_context: object, table: object):
        visited.append(table.table_id)
        state = {deleted_reference: None} if table is second else {}
        return state, []

    monkeypatch.setattr(query_engine, "_transaction_row_view", canonical_view)

    assert query_engine._ended_by_this_transaction(context) == {deleted_reference}
    assert visited == [1, 2]
