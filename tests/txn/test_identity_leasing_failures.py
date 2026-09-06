"""Failure boundaries and WAL ordering for durable identity leasing (CN-1)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from okto_grafx.domain.errors import (
    GrafxDeviceFull,
    GrafxError,
    GrafxTransactionStateError,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.txn import CommitState, TransactionState
from okto_grafx.domain.txn.records import (
    WalRecordLike,
    WalRecordType,
    decode_page_write,
)
from okto_grafx.engine.txn_manager import TransactionManager
from txn_support import Stack, build_stack


def _table() -> TableDef:
    return TableDef(
        table_id=1,
        name="Person",
        kind="node",
        columns=(ColumnDef(name="value", type=ValueType.STRING, nullable=False),),
        primary_key=None,
        from_table=None,
        to_table=None,
    )


def _register(stack: Stack, table: TableDef) -> None:
    stack.catalog.catalog.add_table(table)
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)


def _stage_insert(stack: Stack, table: TableDef, value: str):  # noqa: ANN202
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (value,))
    txn.note_write(stack.manager.partition_of(table.table_id, value.encode()))
    return txn


def _seed_extent(stack: Stack, table: TableDef) -> None:
    stack.manager.commit(_stage_insert(stack, table, "seed"))
    assert stack.heap.next_record_id(table) == 2


def _stored_values(stack: Stack, table: TableDef) -> list[tuple[object, ...]]:
    return [version.values for _reference, version in stack.heap.scan_all(table)]


def test_reservation_append_failure_installs_neither_grant_nor_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refill that fails before its barrier leaves the user transaction wholly active."""
    stack = build_stack(identity_lease_size=4)
    table = _table()
    _register(stack, table)
    _seed_extent(stack, table)
    txn = _stage_insert(stack, table, "never")

    records_before = stack.wal.records()
    barriers_before = stack.wal.barriers
    failure = GrafxDeviceFull("The identity reservation append failed.", free_bytes=0)
    attempted = 0

    def refuse_reservation(
        records: Sequence[WalRecordLike],
        *,
        expected_terminal_lsn: int | None = None,
    ) -> int:
        nonlocal attempted
        attempted += 1
        assert expected_terminal_lsn is not None
        assert [record.record_type for record in records] == [
            WalRecordType.WRITE_PAGE,
            WalRecordType.COMMIT,
        ]
        write = decode_page_write(records[0].payload)
        assert (write.file, write.page_index) == ("heap.dat", 0)
        raise failure

    monkeypatch.setattr(stack.wal, "append_many", refuse_reservation)

    with pytest.raises(GrafxDeviceFull) as raised:
        stack.manager.commit(txn)

    assert raised.value is failure
    assert attempted == 1
    assert stack.wal.records() == records_before
    assert stack.wal.barriers == barriers_before
    assert stack.heap.next_record_id(table) == 2
    assert stack.manager._identity_leases == {}
    assert _stored_values(stack, table) == [("seed",)]
    assert txn.row_refs == []
    assert txn.state is TransactionState.ACTIVE
    assert stack.manager.open_transactions == 1
    assert stack.manager.recovery_required is False


def test_first_extent_append_failure_rolls_back_the_atomic_batch_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new table's batch floor is part of the user commit, never prior authority."""
    stack = build_stack(identity_lease_size=4)
    table = _table()
    _register(stack, table)
    txn = stack.manager.begin("write")
    for value in ("one", "two", "three"):
        txn.stage_row_insert(table, (value,))
        txn.note_write(stack.manager.partition_of(table.table_id, value.encode()))
    original_append = stack.wal.append_many
    records_before = stack.wal.records()
    barriers_before = stack.wal.barriers
    failure = GrafxDeviceFull("The first user batch append failed.", free_bytes=0)

    def refuse_user_batch(
        records: Sequence[WalRecordLike],
        *,
        expected_terminal_lsn: int | None = None,
    ) -> int:
        assert expected_terminal_lsn is not None
        assert records[-1].record_type == WalRecordType.COMMIT
        assert records[-1].txn_id == txn.txn_id
        raise failure

    monkeypatch.setattr(stack.wal, "append_many", refuse_user_batch)

    with pytest.raises(GrafxDeviceFull) as raised:
        stack.manager.commit(txn)

    assert raised.value is failure
    assert stack.wal.records() == records_before
    assert stack.wal.barriers == barriers_before
    assert stack.heap.next_record_id(table) == 1
    assert _stored_values(stack, table) == []
    assert txn.state is TransactionState.ACTIVE

    monkeypatch.setattr(stack.wal, "append_many", original_append)
    report = stack.manager.commit(txn)

    assert report.durable is True
    assert len(txn.row_refs) == 3
    assert stack.heap.next_record_id(table) == 4
    assert sorted(_stored_values(stack, table)) == [("one",), ("three",), ("two",)]


@pytest.mark.parametrize("door", ("apply", "publish"))
def test_post_barrier_reservation_failure_never_commits_the_user_transaction(
    monkeypatch: pytest.MonkeyPatch,
    door: str,
) -> None:
    """A durable metadata commit is recovered and reported separately from the user commit."""
    stack = build_stack(identity_lease_size=4)
    table = _table()
    _register(stack, table)
    _seed_extent(stack, table)
    txn = _stage_insert(stack, table, "never")
    barriers_before = stack.wal.barriers
    injected = 0

    if door == "apply":
        original_apply = TransactionManager._apply_images

        def fail_first_apply(
            manager: TransactionManager,
            images: Sequence[tuple[str, int, bytes]],
        ) -> None:
            nonlocal injected
            if manager is stack.manager and injected == 0:
                injected += 1
                assert [(file, page) for file, page, _image in images] == [
                    ("heap.dat", 0)
                ]
                raise GrafxError("Identity-floor apply failed after its WAL barrier.")
            original_apply(manager, images)

        monkeypatch.setattr(TransactionManager, "_apply_images", fail_first_apply)
    else:
        original_publish = TransactionManager._publish_commit_state

        def fail_first_publish(
            manager: TransactionManager,
            previous: CommitState,
            committed: int,
        ) -> None:
            nonlocal injected
            if manager is stack.manager and injected == 0:
                injected += 1
                assert committed > previous.last_committed_lsn
                raise GrafxError(
                    "Identity-floor publication failed after its WAL barrier."
                )
            original_publish(manager, previous, committed)

        monkeypatch.setattr(
            TransactionManager,
            "_publish_commit_state",
            fail_first_publish,
        )

    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager.commit(txn)

    refusal = raised.value
    assert injected == 1
    assert refusal.retryable is False
    assert refusal.details["metadata_committed"] is True
    assert refusal.details["user_transaction_committed"] is False
    assert refusal.details["retryable"] is False
    reservation_csn = refusal.details["reservation_csn"]
    assert isinstance(reservation_csn, int)
    assert stack.wal.barriers == barriers_before + 1
    metadata_commit = next(
        record for record in stack.wal.records() if record.lsn == reservation_csn
    )
    assert metadata_commit.record_type == WalRecordType.COMMIT
    assert metadata_commit.txn_id != txn.txn_id
    recovery_required = refusal.details["recovery_required"]
    assert isinstance(recovery_required, bool)
    assert stack.manager.recovery_required is recovery_required
    if not recovery_required:
        assert stack.manager._read_commit_state().last_committed_lsn == reservation_csn
        assert stack.heap.next_record_id(table) == 6
    assert _stored_values(stack, table) == [("seed",)]
    assert stack.manager._identity_leases == {}
    assert txn.row_refs == []
    assert txn.state is TransactionState.ACTIVE
    assert stack.manager.open_transactions == 1


def test_floor_reservation_precedes_the_user_commit_in_the_wal() -> None:
    stack = build_stack(identity_lease_size=4)
    table = _table()
    _register(stack, table)
    _seed_extent(stack, table)
    before = len(stack.wal.records())
    txn = _stage_insert(stack, table, "leased")

    report = stack.manager.commit(txn)
    records = stack.wal.records()[before:]
    user_commit = next(
        record
        for record in records
        if record.record_type == WalRecordType.COMMIT and record.txn_id == txn.txn_id
    )
    metadata_commit = next(
        record
        for record in records
        if record.record_type == WalRecordType.COMMIT and record.txn_id != txn.txn_id
    )
    metadata_records = tuple(
        record for record in records if record.txn_id == metadata_commit.txn_id
    )

    assert [record.record_type for record in metadata_records] == [
        WalRecordType.WRITE_PAGE,
        WalRecordType.COMMIT,
    ]
    floor_write = decode_page_write(metadata_records[0].payload)
    assert (floor_write.file, floor_write.page_index) == ("heap.dat", 0)
    assert metadata_commit.lsn < user_commit.lsn == report.csn
    assert max(record.lsn for record in metadata_records) < min(
        record.lsn for record in records if record.txn_id == txn.txn_id
    )
    assert txn.state is TransactionState.COMMITTED
    assert _stored_values(stack, table) == [("seed",), ("leased",)]
