"""Transaction-local insert identities and their single logical reducer."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxTransactionStateError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.txn.context import (
    PendingRowRef,
    RowIntent,
    RowOperation,
    TransactionContext,
    TransactionMode,
    TransactionState,
)
from okto_grafx.domain.txn.intents import reduce_row_intents
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.verifier import Verifier
from txn_support import Stack, build_stack


def _table(table_id: int = 1) -> TableDef:
    return TableDef(
        table_id=table_id,
        name=f"node_{table_id}",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
        from_table=None,
        to_table=None,
    )


def _context(*, txn_id: int = 7) -> TransactionContext:
    return TransactionContext(
        txn_id=txn_id,
        mode=TransactionMode.WRITE,
        snapshot=Snapshot(0),
        epoch=0,
        owner=object(),
        page_staging_capability=object(),
    )


def test_staged_inserts_receive_unique_private_negative_references() -> None:
    transaction = _context()
    table = _table()

    transaction.stage_row_insert(table, (1,))
    transaction.stage_row_insert(table, (2,))

    first, second = (intent.reference for intent in transaction.row_intents)
    assert first == PendingRowRef(txn_id=7, table_id=1, token=-1)
    assert second == PendingRowRef(txn_id=7, table_id=1, token=-2)
    assert first != second
    assert all(intent.record_id is None for intent in transaction.row_intents)


def _registered(stack: Stack, table: TableDef) -> TableDef:
    stack.catalog.catalog.add_table(table)
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)
    stack.pool.flush(stack.heap.file)
    return table


def test_statement_discard_does_not_reuse_a_pending_identity() -> None:
    transaction = _context()
    table = _table()
    transaction.stage_row_insert(table, (1,))
    mark = transaction.staging_mark()
    transaction.stage_row_insert(table, (2,))
    discarded = transaction.row_intents[-1].reference

    transaction.discard_since(mark)
    transaction.stage_row_insert(table, (3,))
    replacement = transaction.row_intents[-1].reference

    assert replacement != discarded
    assert isinstance(replacement, PendingRowRef)
    assert isinstance(discarded, PendingRowRef)
    assert replacement.token < discarded.token
    assert transaction.owns_pending_row_ref(replacement)
    assert not transaction.owns_pending_row_ref(discarded)
    assert [intent.values for intent in transaction.row_intents] == [(1,), (3,)]
    with pytest.raises(GrafxTransactionStateError):
        transaction.stage_row_update(table, discarded, (4,))


def test_abort_drops_pending_references_and_resets_the_private_sequence() -> None:
    transaction = _context()
    transaction.stage_row_insert(_table(), (1,))

    transaction.mark_aborted()

    assert transaction.row_intents == []
    assert transaction._next_pending_token == -1
    assert transaction._pending_row_refs == {}


@pytest.mark.parametrize(
    "case",
    ("cross_table", "foreign_txn", "orphan", "equal_clone", "duplicate_insert"),
)
def test_commit_refuses_unproved_pending_references_before_the_commit_window(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    first_table = _table(1)
    second_table = _table(2)
    transaction = stack.manager.begin("write")
    transaction.stage_row_insert(first_table, (1,))
    issued = transaction.row_intents[0].reference
    assert isinstance(issued, PendingRowRef)

    if case == "cross_table":
        suspect = RowIntent(
            table=second_table,
            values=(2,),
            operation=RowOperation.UPDATE,
            reference=issued,
        )
    elif case == "foreign_txn":
        suspect = RowIntent(
            table=first_table,
            values=(2,),
            operation=RowOperation.UPDATE,
            reference=PendingRowRef(
                txn_id=transaction.txn_id + 1,
                table_id=first_table.table_id,
                token=issued.token,
            ),
        )
    elif case == "orphan":
        suspect = RowIntent(
            table=first_table,
            operation=RowOperation.DELETE,
            reference=PendingRowRef(
                txn_id=transaction.txn_id,
                table_id=first_table.table_id,
                token=-999,
            ),
        )
    elif case == "equal_clone":
        suspect = RowIntent(
            table=first_table,
            values=(2,),
            operation=RowOperation.UPDATE,
            reference=PendingRowRef(
                txn_id=issued.txn_id,
                table_id=issued.table_id,
                token=issued.token,
            ),
        )
    else:
        suspect = RowIntent(table=first_table, values=(2,), reference=issued)
    transaction.row_intents.append(suspect)
    transaction.note_write(stack.manager.partition_of(first_table.table_id, b"1"))

    def commit_window_was_reached(_manager: TransactionManager) -> object:
        pytest.fail("pending-reference validation ran after the commit window opened")

    monkeypatch.setattr(TransactionManager, "_hold_lease", commit_window_was_reached)
    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager.commit(transaction)

    assert raised.value.details["field"] == "pending_row_reference"
    assert transaction.state is TransactionState.ACTIVE
    assert transaction.row_refs == []
    stack.manager.rollback(transaction)


def test_pending_insert_update_commits_one_physical_row_and_reopens_clean(
    stack: Stack,
    database_root: Path,
) -> None:
    table = _registered(stack, _table())
    transaction = stack.manager.begin("write")
    transaction.stage_row_insert(table, (1,))
    pending = transaction.row_intents[0].reference
    transaction.stage_row_update(table, pending, (2,))
    transaction.note_write(stack.manager.partition_of(table.table_id, b"2"))

    stack.manager.commit(transaction)

    assert len(transaction.row_refs) == 1
    assert all(isinstance(reference, RecordRef) for reference in transaction.row_refs)
    assert stack.heap.read(transaction.row_refs[0]).values == (2,)

    reopened = build_stack(database_root, owner_id="pending-update-reopen")
    reader = reopened.manager.begin("read")
    assert [version.values for _ref, version in reopened.heap.scan(table, reader.snapshot)] == [
        (2,)
    ]
    assert Verifier(
        reopened.pool,
        reopened.metrics,
        heap=reopened.heap,
        catalog=reopened.catalog,
    ).verify("all").findings == ()
    reopened.manager.rollback(reader)


def test_pending_insert_delete_commits_no_row_and_reopens_clean(
    stack: Stack,
    database_root: Path,
) -> None:
    table = _registered(stack, _table())
    transaction = stack.manager.begin("write")
    transaction.stage_row_insert(table, (1,))
    pending = transaction.row_intents[0].reference
    transaction.stage_row_delete(table, pending)
    transaction.note_write(stack.manager.partition_of(table.table_id, b"1"))

    stack.manager.commit(transaction)

    assert transaction.row_refs == []
    reopened = build_stack(database_root, owner_id="pending-delete-reopen")
    reader = reopened.manager.begin("read")
    assert list(reopened.heap.scan(table, reader.snapshot)) == []
    assert Verifier(
        reopened.pool,
        reopened.metrics,
        heap=reopened.heap,
        catalog=reopened.catalog,
    ).verify("all").findings == ()
    reopened.manager.rollback(reader)


def test_pending_insert_update_reduces_to_one_insert_with_final_values() -> None:
    table = _table()
    pending = PendingRowRef(txn_id=7, table_id=1, token=-1)
    intents = (
        RowIntent(table=table, values=(1,), reference=pending),
        RowIntent(
            table=table,
            values=(2,),
            operation=RowOperation.UPDATE,
            reference=pending,
        ),
    )

    assert reduce_row_intents(intents) == (
        RowIntent(table=table, values=(2,), reference=pending),
    )


def test_pending_insert_delete_reduces_to_no_effect_and_cannot_be_resurrected() -> None:
    table = _table()
    pending = PendingRowRef(txn_id=7, table_id=1, token=-1)
    intents = (
        RowIntent(table=table, values=(1,), reference=pending),
        RowIntent(table=table, operation=RowOperation.DELETE, reference=pending),
        RowIntent(
            table=table,
            values=(2,),
            operation=RowOperation.UPDATE,
            reference=pending,
        ),
    )

    assert reduce_row_intents(intents) == ()


def test_committed_update_delete_keeps_the_established_delete_wins_contract() -> None:
    table = _table()
    stored = object()
    intents = (
        RowIntent(
            table=table,
            values=(2,),
            operation=RowOperation.UPDATE,
            reference=stored,
        ),
        RowIntent(table=table, operation=RowOperation.DELETE, reference=stored),
        RowIntent(
            table=table,
            values=(3,),
            operation=RowOperation.UPDATE,
            reference=stored,
        ),
    )

    assert reduce_row_intents(intents) == (
        RowIntent(table=table, operation=RowOperation.DELETE, reference=stored),
    )


@pytest.mark.parametrize(
    "kwargs",
    (
        {"txn_id": 0, "table_id": 1, "token": -1},
        {"txn_id": 1, "table_id": 0, "token": -1},
        {"txn_id": 1, "table_id": 1, "token": 0},
    ),
)
def test_pending_reference_refuses_the_durable_identity_domain(
    kwargs: dict[str, int],
) -> None:
    with pytest.raises(GrafxConfigurationError):
        PendingRowRef(**kwargs)
