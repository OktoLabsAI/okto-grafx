"""Durable, burn-only row identity leasing without heap page-zero false sharing (CN-1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.domain.errors import GrafxTransactionStateError, GrafxWriteConflict
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.txn import page_partition
from okto_grafx.domain.txn.records import WalRecordType, decode_page_write
from okto_grafx.engine.heap_store import FIRST_RECORD_ID, HeapStore
from txn_support import Stack, build_stack


def _table(table_id: int = 1, name: str = "Person") -> TableDef:
    return TableDef(
        table_id=table_id,
        name=name,
        kind="node",
        columns=(ColumnDef(name="value", type=ValueType.STRING, nullable=False),),
        primary_key=None,
        from_table=None,
        to_table=None,
    )


def _register(stack: Stack, *tables: TableDef) -> None:
    for table in tables:
        stack.catalog.catalog.add_table(table)
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)


def _insert(
    stack: Stack,
    table: TableDef,
    value: str,
    *,
    record_id: int | None = None,
    key: bytes = b"row",
):
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (value,), record_id=record_id)
    txn.note_write(stack.manager.partition_of(table.table_id, key))
    report = stack.manager.commit(txn)
    return txn, report


def _identities(stack: Stack, table: TableDef) -> list[int]:
    return sorted(version.record_id for _ref, version in stack.heap.scan_all(table))


def _page_writes(records: tuple[object, ...]) -> list[tuple[str, int]]:
    return [
        (write.file, write.page_index)
        for record in records
        if record.record_type == WalRecordType.WRITE_PAGE
        for write in (decode_page_write(record.payload),)
    ]


def test_first_extent_stays_atomic_then_one_refill_serves_the_local_range() -> None:
    stack = build_stack(identity_lease_size=4)
    table = _table()
    _register(stack, table)

    before = len(stack.wal.records())
    _insert(stack, table, "first")
    first_records = stack.wal.records()[before:]
    assert (
        sum(record.record_type == WalRecordType.COMMIT for record in first_records) == 1
    )
    assert stack.heap.next_record_id(table) == 2

    before = len(stack.wal.records())
    _insert(stack, table, "second")
    refill_records = stack.wal.records()[before:]
    assert (
        sum(record.record_type == WalRecordType.COMMIT for record in refill_records)
        == 2
    )
    assert _page_writes(refill_records).count(("heap.dat", 0)) == 1
    assert stack.heap.next_record_id(table) == 6

    before = len(stack.wal.records())
    _insert(stack, table, "third")
    cached_records = stack.wal.records()[before:]
    assert (
        sum(record.record_type == WalRecordType.COMMIT for record in cached_records)
        == 1
    )
    assert ("heap.dat", 0) not in _page_writes(cached_records)
    assert stack.heap.next_record_id(table) == 6
    assert _identities(stack, table) == [1, 2, 3]


def test_first_batch_installs_one_atomic_floor_without_a_metadata_subcommit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_stack(identity_lease_size=4)
    table = _table()
    _register(stack, table)
    initial_calls = 0
    reserved_calls = 0
    ordinary_calls = 0
    proof_calls = 0
    supplied_proofs: list[object] = []
    original_initial = HeapStore.insert_initial_reserved
    original_reserved = HeapStore.insert_reserved
    original_ordinary = HeapStore.insert
    original_proof = HeapStore.reserved_extent_proof

    def counted_initial(store: HeapStore, *args: object, **kwargs: object):
        nonlocal initial_calls
        initial_calls += 1
        return original_initial(store, *args, **kwargs)

    def counted_reserved(store: HeapStore, *args: object, **kwargs: object):
        nonlocal reserved_calls
        reserved_calls += 1
        supplied_proofs.append(kwargs.get("extent_proof"))
        return original_reserved(store, *args, **kwargs)

    def counted_ordinary(store: HeapStore, *args: object, **kwargs: object):
        nonlocal ordinary_calls
        ordinary_calls += 1
        return original_ordinary(store, *args, **kwargs)

    def counted_proof(store: HeapStore, *args: object, **kwargs: object):
        nonlocal proof_calls
        proof_calls += 1
        return original_proof(store, *args, **kwargs)

    monkeypatch.setattr(HeapStore, "insert_initial_reserved", counted_initial)
    monkeypatch.setattr(HeapStore, "insert_reserved", counted_reserved)
    monkeypatch.setattr(HeapStore, "insert", counted_ordinary)
    monkeypatch.setattr(HeapStore, "reserved_extent_proof", counted_proof)

    txn = stack.manager.begin("write")
    for identity in range(1, 17):
        txn.stage_row_insert(table, (f"row-{identity}",))
        txn.note_write(
            stack.manager.partition_of(table.table_id, str(identity).encode())
        )
    before = len(stack.wal.records())

    report = stack.manager.commit(txn)
    records = stack.wal.records()[before:]

    assert report.durable is True
    assert initial_calls == 1
    assert reserved_calls == 15
    assert ordinary_calls == 0
    assert proof_calls == 1
    assert supplied_proofs[0] is not None
    assert all(proof is supplied_proofs[0] for proof in supplied_proofs)
    assert sum(record.record_type == WalRecordType.COMMIT for record in records) == 1
    assert _page_writes(records).count(("heap.dat", 0)) == 1
    assert stack.heap.next_record_id(table) == 17
    assert _identities(stack, table) == list(range(1, 17))


def test_one_batch_combines_an_atomic_first_extent_with_an_existing_cn1_range() -> None:
    stack = build_stack(identity_lease_size=4)
    existing = _table(1, "Existing")
    initial = _table(2, "Initial")
    _register(stack, existing, initial)
    _insert(stack, existing, "seed", key=b"existing-seed")
    txn = stack.manager.begin("write")
    for table, value in (
        (initial, "new-1"),
        (existing, "old-2"),
        (initial, "new-2"),
        (existing, "old-3"),
        (initial, "new-3"),
    ):
        txn.stage_row_insert(table, (value,))
        txn.note_write(stack.manager.partition_of(table.table_id, value.encode()))
    before = len(stack.wal.records())

    report = stack.manager.commit(txn)
    records = stack.wal.records()[before:]

    assert report.durable is True
    assert sum(record.record_type == WalRecordType.COMMIT for record in records) == 2
    assert _identities(stack, initial) == [1, 2, 3]
    assert stack.heap.next_record_id(initial) == 4
    assert _identities(stack, existing) == [1, 2, 3]
    assert stack.heap.next_record_id(existing) == 6


@pytest.mark.parametrize("lease_size", [1, 64])
def test_explicit_identity_below_the_floor_is_always_fail_closed(
    lease_size: int,
) -> None:
    stack = build_stack(identity_lease_size=lease_size)
    table = _table()
    _register(stack, table)
    _insert(stack, table, "five", record_id=5)

    refused = stack.manager.begin("write")
    refused.stage_row_insert(table, ("gap",), record_id=3)
    refused.note_write(stack.manager.partition_of(table.table_id, b"gap"))
    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager.commit(refused)

    assert raised.value.details["field"] == "record_id"
    assert raised.value.details["durable_floor"] == 6
    assert _identities(stack, table) == [5]
    stack.manager.rollback(refused)


def test_explicit_identity_above_the_floor_is_reserved_before_mixed_use() -> None:
    stack = build_stack(identity_lease_size=4)
    table = _table()
    _register(stack, table)
    _insert(stack, table, "seed")

    mixed = stack.manager.begin("write")
    mixed.stage_row_insert(table, ("explicit",), record_id=100)
    mixed.stage_row_insert(table, ("implicit",))
    mixed.note_write(stack.manager.partition_of(table.table_id, b"mixed"))
    stack.manager.commit(mixed)

    assert _identities(stack, table) == [1, 100, 101]
    assert stack.heap.next_record_id(table) == 106


def test_a_conflict_visible_before_row_planning_reserves_no_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "burn-conflict"
    root.mkdir()
    winner = build_stack(root, owner_id="winner", identity_lease_size=4)
    table = _table()
    _register(winner, table)
    seed, _report = _insert(winner, table, "seed", key=b"same")
    original = seed.row_refs[0]

    loser = build_stack(root, owner_id="loser", identity_lease_size=4)
    doomed = loser.manager.begin("write")
    doomed.stage_row_insert(table, ("doomed",))
    doomed.note_write(loser.manager.partition_of(table.table_id, b"same"))

    update = winner.manager.begin("write")
    update.stage_row_update(table, original, ("updated",))
    update.note_write(winner.manager.partition_of(table.table_id, b"same"))
    winner.manager.commit(update)

    with pytest.raises(GrafxWriteConflict):
        loser.manager.commit(doomed)
    # The first OCC runs before cache consumption/refill. A conflict already visible there has
    # consumed no identity and emits no metadata commit merely to burn a range.
    assert loser.heap.next_record_id(table) == 2

    retry = loser.manager.retry(doomed)
    retry.stage_row_insert(table, ("kept",))
    retry.note_write(loser.manager.partition_of(table.table_id, b"other"))
    loser.manager.commit(retry)

    stored = _identities(loser, table)
    assert stored.count(2) == 1


def test_a_pre_staged_page_zero_cannot_overwrite_its_own_later_refill() -> None:
    """Pre-staged bytes stay on the transaction snapshot side of the OCC boundary.

    The second insert needs a CN-1 refill, which durably advances heap page 0 after the first OCC
    pass. This transaction also carries an authenticated image of the older page 0. Treating every
    page touched after the refill as fresh would let that old staged image overwrite the durable
    floor and make row identities reusable. The incremental old-interest pass must instead see the
    refill as a conflict; only the private metadata COMMIT survives and the user row does not.
    """
    stack = build_stack(identity_lease_size=4)
    table = _table()
    _register(stack, table)
    _insert(stack, table, "seed")

    with stack.pool.pinned(stack.heap.file, 0) as page:
        stale_page_zero = stack.codec.encode_page(page)

    txn = stack.manager.begin("write")
    txn.owner._stage_page_image(txn, stack.heap.file, 0, stale_page_zero)
    txn.stage_row_insert(table, ("must-not-land",))
    txn.note_write(stack.manager.partition_of(table.table_id, b"pre-staged-page-zero"))
    before = len(stack.wal.records())

    with pytest.raises(GrafxWriteConflict) as raised:
        stack.manager.commit(txn)

    assert raised.value.details["partitions"] == [page_partition(stack.heap.file, 0)]
    reservation = stack.wal.records()[before:]
    assert (
        sum(record.record_type == WalRecordType.COMMIT for record in reservation) == 1
    )
    assert _page_writes(reservation) == [(stack.heap.file, 0)]
    assert stack.heap.next_record_id(table) == 6
    assert _identities(stack, table) == [FIRST_RECORD_ID]
    stack.manager.rollback(txn)


def test_an_inherited_manager_refuses_and_its_unused_range_stays_burned(
    tmp_path: Path,
) -> None:
    root = tmp_path / "burn-lifecycle"
    root.mkdir()
    process = [10]
    first = build_stack(
        root,
        owner_id="first",
        identity_lease_size=4,
        process_identity_provider=lambda: process[0],
    )
    table = _table()
    _register(first, table)
    _insert(first, table, "seed")
    _insert(first, table, "leased")
    assert first.heap.next_record_id(table) == 6

    process[0] = 11
    with pytest.raises(GrafxTransactionStateError) as inherited:
        _insert(first, table, "must-not-use-inherited-range")
    assert inherited.value.details["field"] == "process_identity"
    assert inherited.value.details["inherited_process"] is True

    # The refusal is irreversible on this inherited object. In a real fork the parent's copy is
    # unaffected and the child exits without releasing the parent's locks/readers.
    process[0] = 10
    with pytest.raises(GrafxTransactionStateError):
        first.manager.close()

    reopened = build_stack(root, owner_id="reopened", identity_lease_size=4)
    _insert(reopened, table, "after-reopen")
    assert _identities(reopened, table) == [1, 2, 6]
    assert reopened.heap.next_record_id(table) == 10


def test_distinct_tables_do_not_conflict_only_because_their_floors_share_page_zero(
    tmp_path: Path,
) -> None:
    root = tmp_path / "distinct-tables"
    root.mkdir()
    first = build_stack(root, owner_id="first", identity_lease_size=4)
    left = _table(1, "Left")
    right = _table(2, "Right")
    _register(first, left, right)
    _insert(first, left, "left-seed", key=b"left")
    _insert(first, right, "right-seed", key=b"right")

    second = build_stack(root, owner_id="second", identity_lease_size=4)
    left_txn = first.manager.begin("write")
    left_txn.stage_row_insert(left, ("left-next",))
    left_txn.note_write(first.manager.partition_of(left.table_id, b"left-next"))
    right_txn = second.manager.begin("write")
    right_txn.stage_row_insert(right, ("right-next",))
    right_txn.note_write(second.manager.partition_of(right.table_id, b"right-next"))

    first.manager.commit(left_txn)
    second.manager.commit(right_txn)

    assert _identities(second, left) == [1, 2]
    assert _identities(second, right) == [1, 2]


def test_tail_growth_keeps_real_page_zero_interest_without_self_conflicting() -> None:
    stack = build_stack(page_size=512, identity_lease_size=4)
    table = _table()
    _register(stack, table)
    _insert(stack, table, "seed")

    before = len(stack.wal.records())
    _txn, report = _insert(stack, table, "x" * 400)
    records = stack.wal.records()[before:]

    assert report.durable is True
    assert ("heap.dat", 0) in _page_writes(records)
    assert _identities(stack, table) == [FIRST_RECORD_ID, FIRST_RECORD_ID + 1]
