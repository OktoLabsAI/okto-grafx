"""Focused equivalence and complexity checks for incremental PK intent folding."""

from __future__ import annotations

from dataclasses import replace
import random
from types import SimpleNamespace

import pytest

from okto_grafx.domain.errors import GrafxQueryError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.txn.context import (
    RowIntent,
    RowOperation,
    TransactionContext,
    TransactionMode,
)
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.engine.query_engine import (
    _PrimaryKeyFoldState,
    _RevisionList,
    _primary_key_conflicts,
    _transaction_primary_key_state,
    _transaction_row_view,
    _equal,
)
from tests.query.stack import QueryStack, TransactionDouble, build_query_stack


@pytest.fixture
def stack() -> QueryStack:
    built = build_query_stack()
    built.insert("Person", 1, (1, "Ada", 36, "London"), csn=1)
    built.insert("Person", 2, (2, "Grace", 45, "New York"), csn=1)
    return built


def _context(stack: QueryStack, txn: TransactionDouble) -> SimpleNamespace:
    return SimpleNamespace(
        engine=stack.engine,
        txn=txn,
        staged_rows=_RevisionList(),
        phase_rows=(),
        primary_key_memos={},
    )


def _occupied_keys(state: _PrimaryKeyFoldState) -> list[object]:
    return sorted(
        observed
        for owners in state.key_owners.values()
        for observed in owners.values()
    )


def _assert_incremental_matches_canonical(
    stack: QueryStack, context: SimpleNamespace, table: object
) -> None:
    canonical_state, canonical_inserted = _transaction_row_view(
        context, table, include_held=False
    )
    incremental = _transaction_primary_key_state(
        stack.engine, context, table, table.column_index(table.primary_key)
    )
    incremental_state: dict[object, tuple[object, ...] | None] = {}
    incremental_inserted: list[tuple[object, tuple[object, ...]]] = []
    for reference, outcome in incremental.outcomes.items():
        if outcome.operation is RowOperation.INSERT:
            incremental_inserted.append((reference, outcome.values))
        else:
            incremental_state[reference] = (
                None if outcome.operation is RowOperation.DELETE else outcome.values
            )
    assert incremental_state == canonical_state
    assert incremental_inserted == canonical_inserted

    active_keys = [values[0] for _reference, values in canonical_inserted]
    active_keys.extend(
        values[0] for values in canonical_state.values() if values is not None
    )
    candidates = [
        *active_keys,
        -1,
        -1.0,
        7,
        7.0,
        float("nan"),
        [1, 2],
        (1, 2),
        {"seed": 9},
    ]
    for candidate in candidates:
        expected = any(_equal(candidate, occupied) for occupied in active_keys)
        assert (
            _primary_key_conflicts(incremental, candidate, replacing=None) is expected
        )


def test_incremental_fold_is_equivalent_to_the_canonical_transaction_view(
    stack: QueryStack,
) -> None:
    table = stack.table("Person")
    refs = tuple(ref for ref, _version in stack.heap.scan_all(table))
    txn = stack.transaction()
    txn.stage_row_update(table, refs[0], (10, "Ada", 36, "London"))
    txn.stage_row_delete(table, refs[1])
    txn.stage_row_insert(table, (3, "Lin", 30, "Paris"))
    context = _context(stack, txn)

    canonical_state, canonical_inserted = _transaction_row_view(
        context, table, include_held=False
    )
    incremental = _transaction_primary_key_state(stack.engine, context, table, 0)

    observed_state = {
        reference: None if outcome.operation.name == "DELETE" else outcome.values
        for reference, outcome in incremental.outcomes.items()
        if outcome.operation.name != "INSERT"
    }
    expected_keys = [values[0] for _reference, values in canonical_inserted]
    expected_keys.extend(
        values[0] for values in canonical_state.values() if values is not None
    )
    assert observed_state == canonical_state
    assert _occupied_keys(incremental) == sorted(expected_keys)


def test_update_and_delete_release_keys_while_later_duplicates_are_refused(
    stack: QueryStack,
) -> None:
    txn = stack.transaction()
    stack.engine.execute("MATCH (p:Person {id: 1}) SET p.id = 10", txn)
    stack.engine.execute("CREATE (:Person {id: 1, name: 'Again'})", txn)
    with pytest.raises(GrafxQueryError):
        stack.engine.execute("CREATE (:Person {id: 10, name: 'Duplicate'})", txn)

    stack.engine.execute("MATCH (p:Person {id: 2}) DELETE p", txn)
    stack.engine.execute("CREATE (:Person {id: 2, name: 'Reused'})", txn)
    with pytest.raises(GrafxQueryError):
        stack.engine.execute("CREATE (:Person {id: 2, name: 'Duplicate'})", txn)


def test_rewrite_and_truncate_force_a_canonical_rebuild(stack: QueryStack) -> None:
    table = stack.table("Person")
    txn = stack.transaction()
    for key in (1, 2, 3):
        txn.stage_row_insert(table, (key, None, None, None))
    context = _context(stack, txn)
    state = _transaction_primary_key_state(stack.engine, context, table, 0)
    assert _occupied_keys(state) == [1, 2, 3]

    txn.row_intents[0] = replace(txn.row_intents[0], values=(9, None, None, None))
    del txn.row_intents[1:]
    rebuilt = _transaction_primary_key_state(stack.engine, context, table, 0)

    canonical_state, canonical_inserted = _transaction_row_view(
        context, table, include_held=False
    )
    assert canonical_state == {}
    assert [values[0] for _reference, values in canonical_inserted] == [9]
    assert _occupied_keys(rebuilt) == [9]


def test_statement_savepoint_rollback_rebuilds_the_incremental_fold(
    stack: QueryStack,
) -> None:
    table = stack.table("Person")
    txn = TransactionContext(
        txn_id=77,
        mode=TransactionMode.WRITE,
        snapshot=Snapshot(0),
        epoch=0,
        owner=object(),
        page_staging_capability=object(),
    )
    context = SimpleNamespace(
        engine=stack.engine,
        txn=txn,
        staged_rows=_RevisionList(),
        phase_rows=(),
        primary_key_memos={},
    )
    txn.stage_row_insert(table, (1, None, None, None))
    _transaction_primary_key_state(stack.engine, context, table, 0)
    mark = txn.staging_mark()
    txn.stage_row_insert(table, (2, None, None, None))
    assert _occupied_keys(
        _transaction_primary_key_state(stack.engine, context, table, 0)
    ) == [1, 2]

    txn.discard_since(mark)
    assert _occupied_keys(
        _transaction_primary_key_state(stack.engine, context, table, 0)
    ) == [1]


@pytest.mark.parametrize("count", (250, 1000))
def test_each_appended_intent_is_folded_once_in_the_accumulated_hot_path(
    stack: QueryStack, count: int
) -> None:
    table = stack.table("Person")
    txn = stack.transaction()
    context = _context(stack, txn)
    state = _transaction_primary_key_state(stack.engine, context, table, 0)
    for key in range(count):
        txn.stage_row_insert(table, (key, None, None, None))
        state = _transaction_primary_key_state(stack.engine, context, table, 0)

    assert state.cursor == count
    assert state.fold_count == count


def test_primary_key_memos_do_not_cross_tables_or_transaction_objects(
    stack: QueryStack,
) -> None:
    person = stack.table("Person")
    chunk = stack.table("Chunk")
    first = stack.transaction()
    first.stage_row_insert(person, (101, None, None, None))
    first.stage_row_insert(chunk, (202, None, None))
    first_context = _context(stack, first)
    person_state = _transaction_primary_key_state(
        stack.engine, first_context, person, 0
    )
    chunk_state = _transaction_primary_key_state(stack.engine, first_context, chunk, 0)
    assert _occupied_keys(person_state) == [101]
    assert _occupied_keys(chunk_state) == [202]

    second = stack.transaction()  # same synthetic txn_id, different owner object
    second.stage_row_insert(person, (303, None, None, None))
    second_state = _transaction_primary_key_state(
        stack.engine, _context(stack, second), person, 0
    )
    assert not _primary_key_conflicts(second_state, 101, replacing=None)
    assert _primary_key_conflicts(second_state, 303, replacing=None)


@pytest.mark.parametrize("committed", (False, True))
def test_terminal_settlement_drops_the_transaction_primary_key_memo(
    stack: QueryStack, committed: bool
) -> None:
    table = stack.table("Person")
    txn = stack.transaction()
    txn.stage_row_insert(table, (99, None, None, None))
    _transaction_primary_key_state(stack.engine, _context(stack, txn), table, 0)
    assert txn.txn_id in stack.engine._primary_key_memos

    stack.engine.settle_schema(txn.txn_id, committed=committed)
    assert txn.txn_id not in stack.engine._primary_key_memos


def test_an_in_place_intent_replacement_cannot_leave_a_stale_key_owner(
    stack: QueryStack,
) -> None:
    table = stack.table("Person")
    txn = stack.transaction()
    txn.stage_row_insert(table, (7, None, None, None))
    context = _context(stack, txn)
    _transaction_primary_key_state(stack.engine, context, table, 0)

    txn.row_intents[0] = RowIntent(table=table, values=(8, None, None, None))
    state = _transaction_primary_key_state(stack.engine, context, table, 0)
    assert not _primary_key_conflicts(state, 7, replacing=None)
    assert _primary_key_conflicts(state, 8, replacing=None)


def test_seeded_incremental_fold_matches_the_canonical_reducer(stack: QueryStack) -> None:
    rng = random.Random(0x1AD3_0006)
    tables = (stack.table("Person"), stack.table("Chunk"))
    txn = TransactionContext(
        txn_id=606,
        mode=TransactionMode.WRITE,
        snapshot=Snapshot(0),
        epoch=0,
        owner=object(),
        page_staging_capability=object(),
    )
    context = SimpleNamespace(
        engine=stack.engine,
        txn=txn,
        staged_rows=_RevisionList(),
        phase_rows=(),
        primary_key_memos={},
    )
    stored = {
        table.table_id: [RecordRef(page=table.table_id * 100 + index, slot=1) for index in range(8)]
        for table in tables
    }
    pending: dict[int, list[object]] = {table.table_id: [] for table in tables}

    def values(table: object, key: object, step: int) -> tuple[object, ...]:
        if table.name == "Person":
            return (key, f"person-{step}", step, None)
        return (key, step, None)

    # Pin numeric equivalence and terminal repeated operations before the random interleaving.
    for table in tables:
        ref = stored[table.table_id][0]
        txn.stage_row_update(table, ref, values(table, 7, 0))
        txn.stage_row_update(table, ref, values(table, 7.0, 1))
        txn.stage_row_delete(table, ref)
        txn.stage_row_delete(table, ref)
        created = txn.stage_row_insert(table, values(table, 11, 2))
        pending[table.table_id].append(created)
        txn.stage_row_update(table, created, values(table, 11.0, 3))
        txn.stage_row_delete(table, created)
        txn.stage_row_update(table, created, values(table, 99, 4))

    mutable_key = [1, 2]
    mutable_ref = txn.stage_row_insert(tables[0], values(tables[0], mutable_key, 5))
    pending[tables[0].table_id].append(mutable_ref)
    nan_ref = txn.stage_row_insert(
        tables[1], values(tables[1], float("nan"), 6)
    )
    pending[tables[1].table_id].append(nan_ref)

    for step in range(320):
        table = tables[rng.randrange(len(tables))]
        table_pending = pending[table.table_id]
        choice = rng.randrange(5)
        key: object = rng.randrange(80)
        if step % 29 == 0:
            key = float(key)
        if choice == 0 or not table_pending:
            created = txn.stage_row_insert(table, values(table, key, step))
            table_pending.append(created)
        elif choice in (1, 2):
            reference = rng.choice(stored[table.table_id])
            if choice == 1:
                txn.stage_row_update(table, reference, values(table, key, step))
            else:
                txn.stage_row_delete(table, reference)
        else:
            reference = rng.choice(table_pending)
            if choice == 3:
                txn.stage_row_update(table, reference, values(table, key, step))
            else:
                txn.stage_row_delete(table, reference)

        _transaction_primary_key_state(
            stack.engine, context, table, table.column_index(table.primary_key)
        )
        if step % 37 == 0:
            for compared in tables:
                _assert_incremental_matches_canonical(stack, context, compared)

    for table in tables:
        _assert_incremental_matches_canonical(stack, context, table)

    # Nested caller-owned values do not move the list revision; the defensive mutable-key lane
    # must nevertheless observe their current value with the same _equal semantics.
    mutable_key[:] = [8, 9]
    _assert_incremental_matches_canonical(stack, context, tables[0])

    # Public list replacement and suffix rollback both force a reducer-backed rebuild.
    first = txn.row_intents[0]
    txn.row_intents[0] = replace(first, values=values(tables[0], 123.0, 900))
    for table in tables:
        _assert_incremental_matches_canonical(stack, context, table)
    del txn.row_intents[-17:]
    for table in tables:
        _assert_incremental_matches_canonical(stack, context, table)


@pytest.mark.parametrize(
    "invalid",
    ("duplicate_pending_insert", "stored_becomes_insert", "pending_update_first"),
)
def test_invalid_sequences_raise_the_same_canonical_exception(
    stack: QueryStack, invalid: str
) -> None:
    table = stack.table("Person")
    txn = TransactionContext(
        txn_id=707,
        mode=TransactionMode.WRITE,
        snapshot=Snapshot(0),
        epoch=0,
        owner=object(),
        page_staging_capability=object(),
    )
    if invalid == "duplicate_pending_insert":
        reference = txn.stage_row_insert(table, (1, None, None, None))
        txn.row_intents.append(
            RowIntent(table=table, values=(2, None, None, None), reference=reference)
        )
    elif invalid == "stored_becomes_insert":
        reference = RecordRef(page=90, slot=1)
        txn.stage_row_update(table, reference, (1, None, None, None))
        txn.row_intents.append(
            RowIntent(table=table, values=(2, None, None, None), reference=reference)
        )
    else:
        reference = txn.stage_row_insert(table, (1, None, None, None))
        txn.row_intents[:] = [
            RowIntent(
                table=table,
                values=(2, None, None, None),
                operation=RowOperation.UPDATE,
                reference=reference,
            )
        ]
    context = SimpleNamespace(
        engine=stack.engine,
        txn=txn,
        staged_rows=_RevisionList(),
        phase_rows=(),
        primary_key_memos={},
    )

    with pytest.raises(Exception) as canonical:
        _transaction_row_view(context, table, include_held=False)
    with pytest.raises(type(canonical.value)) as incremental:
        _transaction_primary_key_state(stack.engine, context, table, 0)

    assert incremental.value.args == canonical.value.args
    assert incremental.value.details == canonical.value.details
