"""Transaction row views index append-only intent history once instead of once per seek."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from okto_grafx.domain.txn.context import RowIntent, RowOperation
from okto_grafx.engine.query_engine import (
    _RevisionList,
    _ended_by_this_transaction,
    _transaction_row_view,
)


class _ObservedTable:
    def __init__(self, table_id: int) -> None:
        self._table_id = table_id
        self.reads = 0

    @property
    def table_id(self) -> int:
        self.reads += 1
        return self._table_id


def _context(*intents: RowIntent) -> SimpleNamespace:
    return SimpleNamespace(
        engine=SimpleNamespace(_primary_key_memos={}),
        txn=SimpleNamespace(txn_id=41, row_intents=list(intents)),
        staged_rows=[],
    )


def test_table_local_view_scans_only_the_new_cross_table_suffix() -> None:
    first = _ObservedTable(1)
    second = _ObservedTable(2)
    absent = SimpleNamespace(table_id=99)
    context = _context(
        RowIntent(table=first, values=(1,)),
        RowIntent(table=second, values=(2,)),
    )

    assert _transaction_row_view(context, absent) == ({}, [])
    assert isinstance(context.txn.row_intents, _RevisionList)
    assert (first.reads, second.reads) == (1, 1)

    assert _transaction_row_view(context, absent) == ({}, [])
    assert (first.reads, second.reads) == (1, 1)

    appended = _ObservedTable(3)
    context.txn.row_intents.append(RowIntent(table=appended, values=(3,)))
    assert _transaction_row_view(context, absent) == ({}, [])
    assert (first.reads, second.reads, appended.reads) == (1, 1, 1)

    replacement = _ObservedTable(4)
    context.txn.row_intents[0] = RowIntent(table=replacement, values=(4,))
    assert _transaction_row_view(context, absent) == ({}, [])
    assert first.reads == 1
    assert (second.reads, appended.reads, replacement.reads) == (2, 2, 1)


def test_many_empty_table_views_do_one_history_walk_not_one_per_view() -> None:
    tables = tuple(_ObservedTable(position) for position in range(1, 31))
    context = _context(
        *(
            RowIntent(table=table, values=(row,))
            for table in tables
            for row in range(50)
        )
    )
    absent = SimpleNamespace(table_id=99)

    for _ in range(500):
        assert _transaction_row_view(context, absent) == ({}, [])

    assert sum(table.reads for table in tables) == 1_500


def test_table_local_view_preserves_the_canonical_reduction() -> None:
    table = SimpleNamespace(table_id=7)
    stored = object()
    context = _context(
        RowIntent(
            table=table, values=(1,), operation=RowOperation.UPDATE, reference=stored
        ),
        RowIntent(
            table=table, values=(), operation=RowOperation.DELETE, reference=stored
        ),
        RowIntent(table=table, values=(2,)),
    )

    assert _transaction_row_view(context, table) == ({stored: None}, [(None, (2,))])


def test_delete_precheck_reuses_the_same_append_only_index() -> None:
    first = _ObservedTable(1)
    second = _ObservedTable(2)
    context = _context(
        RowIntent(table=first, values=(1,)),
        RowIntent(table=second, values=(2,)),
    )

    assert _ended_by_this_transaction(context) == set()
    assert _ended_by_this_transaction(context) == set()
    assert (first.reads, second.reads) == (1, 1)

    deleted = object()
    context.txn.row_intents.append(
        RowIntent(
            table=second,
            operation=RowOperation.DELETE,
            reference=deleted,
        )
    )

    assert _ended_by_this_transaction(context) == {deleted}
    assert context.engine._primary_key_memos[41].has_delete_intent is True


def test_exact_intent_with_forged_table_id_keeps_the_canonical_answer() -> None:
    forged = SimpleNamespace(table_id=True)
    context = _context(RowIntent(table=forged, values=(7,)))
    view = SimpleNamespace(table_id=1)

    assert _transaction_row_view(context, view) == ({}, [(None, (7,))])


def test_interrupted_index_walk_does_not_duplicate_intents() -> None:
    class _FlakyTable:
        armed = True

        @property
        def table_id(self) -> int:
            if self.armed:
                self.armed = False
                raise RuntimeError("transient")
            return 1

    view = SimpleNamespace(table_id=1)
    context = _context(
        RowIntent(table=SimpleNamespace(table_id=1), values=(1,)),
        RowIntent(table=_FlakyTable(), values=(2,)),
    )

    with pytest.raises(RuntimeError, match="transient"):
        _transaction_row_view(context, view)
    assert _transaction_row_view(context, view) == (
        {},
        [(None, (1,)), (None, (2,))],
    )
