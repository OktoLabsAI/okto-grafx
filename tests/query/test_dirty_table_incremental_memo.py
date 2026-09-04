"""Dirty-table planning state grows with new intents, not with transaction history squared."""

from __future__ import annotations

from types import SimpleNamespace

from okto_grafx.engine.query_engine import _RevisionList, _intent_table_ids


class _ObservedTable:
    def __init__(self, table_id: object) -> None:
        self._table_id = table_id
        self.reads = 0

    @property
    def table_id(self) -> object:
        self.reads += 1
        return self._table_id


class _IntentIterable:
    def __init__(self, *values: object) -> None:
        self._values = values

    def __iter__(self):
        return iter(self._values)


def _intent(table: object) -> SimpleNamespace:
    return SimpleNamespace(table=table)


def test_dirty_tables_scan_only_the_new_suffix_until_an_intent_rewrite() -> None:
    first = _ObservedTable(7)
    second = _ObservedTable(9)
    engine = SimpleNamespace(_primary_key_memos={})
    txn = SimpleNamespace(txn_id=41, row_intents=[_intent(first), _intent(second)])

    initial = _intent_table_ids(engine, txn)

    assert initial == frozenset({7, 9})
    assert isinstance(txn.row_intents, _RevisionList)
    assert (first.reads, second.reads) == (1, 1)
    assert _intent_table_ids(engine, txn) is initial
    assert (first.reads, second.reads) == (1, 1)

    appended = _ObservedTable(11)
    txn.row_intents.append(_intent(appended))

    grown = _intent_table_ids(engine, txn)

    assert grown == frozenset({7, 9, 11})
    assert (first.reads, second.reads, appended.reads) == (1, 1, 1)

    replacement = _ObservedTable(13)
    txn.row_intents[0] = _intent(replacement)

    rebuilt = _intent_table_ids(engine, txn)

    assert rebuilt == frozenset({9, 11, 13})
    assert first.reads == 1
    assert (second.reads, appended.reads, replacement.reads) == (2, 2, 1)


def test_dirty_table_memo_is_bound_to_the_exact_transaction_object() -> None:
    engine = SimpleNamespace(_primary_key_memos={})
    old_table = _ObservedTable(3)
    old = SimpleNamespace(txn_id=8, row_intents=[_intent(old_table)])
    assert _intent_table_ids(engine, old) == frozenset({3})

    new_table = _ObservedTable(5)
    reused_number = SimpleNamespace(txn_id=8, row_intents=[_intent(new_table)])

    assert _intent_table_ids(engine, reused_number) == frozenset({5})
    assert new_table.reads == 1
    assert engine._primary_key_memos[8].txn is reused_number


def test_untrackable_intents_keep_the_complete_conservative_scan() -> None:
    valid = _ObservedTable(17)
    boolean = _ObservedTable(True)
    negative = _ObservedTable(-1)
    engine = SimpleNamespace(_primary_key_memos={})
    txn = SimpleNamespace(
        txn_id=1,
        row_intents=_IntentIterable(
            _intent(valid), _intent(boolean), _intent(negative)
        ),
    )

    first = _intent_table_ids(engine, txn)
    second = _intent_table_ids(engine, txn)

    assert first == second == frozenset({17})
    assert (valid.reads, boolean.reads, negative.reads) == (2, 2, 2)
    assert engine._primary_key_memos == {}
