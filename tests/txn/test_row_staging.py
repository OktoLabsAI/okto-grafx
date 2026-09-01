"""Rows are written at the number that makes them visible (SPEC-M1 FR-2, BR-9; C10 follow-up).

``HeapStore.insert`` stamps a version with the commit number that makes it visible, and that
number is the LSN of the COMMIT record, which the log assigns inside the commit itself
(CONTRACT.md section 8.5 step 3.4). So a row cannot be written when the caller asks for it. It is
staged, and written inside the commit section at the number the log actually assigned.

The property that matters is exact equality, in both directions: a stamp one below the commit
number makes the row visible to a snapshot that must not see it, and one above makes it
invisible to a snapshot that must. Both are wrong results, so both are probed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.domain.errors import (
    GrafxDeviceFull,
    GrafxRecoveryRefused,
    GrafxTransactionStateError,
    GrafxWriteConflict,
)
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.txn import Snapshot, TransactionState
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.verifier import Verifier
from txn_support import Stack, build_stack, make_page_image

HEAP = "heap.dat"


def _table(table_id: int = 1, name: str = "person") -> TableDef:
    """Return a minimal node table with one integer column."""
    return TableDef(
        table_id=table_id,
        name=name,
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="label", type=ValueType.STRING),
        ),
        primary_key="id",
        from_table=None,
        to_table=None,
    )


def _registered(stack: Stack, table: TableDef) -> TableDef:
    """Put the table in the catalog so the heap can resolve it, and make it durable.

    The flush is setup, not the property under test. C5 never writes the catalog -- carried
    finding CF-4 leaves that to whoever owns the schema -- so a test that wants a SECOND
    participant to resolve the table has to put the catalog on the device itself. Arranging
    state with a primitive the code under test does not compute is what A92 asks for.
    """
    catalog = stack.catalog.catalog
    catalog.add_table(table)
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)
    stack.pool.flush(stack.heap.file)
    return table


# --- the number a row is born at ----------------------------------------------------------------


def test_a_staged_row_is_written_at_exactly_the_commit_number(stack: Stack) -> None:
    table = _registered(stack, _table())
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (1, "ada"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)

    assert txn.state is TransactionState.COMMITTED
    assert len(txn.row_refs) == 1
    version = stack.heap.read(txn.row_refs[0])
    assert version.xmin == report.csn, "the birth stamp is the commit number, exactly"
    assert version.xmax == 0


def test_the_row_is_invisible_to_a_snapshot_that_opened_before_it(stack: Stack) -> None:
    """One below the commit number would show the row to a transaction that predates it."""
    table = _registered(stack, _table())
    earlier = stack.manager.begin("read")
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (1, "ada"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)
    version = stack.heap.read(txn.row_refs[0])

    assert earlier.snapshot.read_lsn < report.csn
    assert earlier.snapshot.visible(version.xmin, version.xmax) is False
    assert list(stack.heap.scan(table, earlier.snapshot)) == []
    stack.manager.rollback(earlier)


def test_the_row_is_visible_to_a_snapshot_that_opened_after_it(stack: Stack) -> None:
    """One above the commit number would hide the row from a transaction that follows it."""
    table = _registered(stack, _table())
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (1, "ada"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)

    later = stack.manager.begin("read")
    assert later.snapshot.read_lsn >= report.csn
    version = stack.heap.read(txn.row_refs[0])
    assert later.snapshot.visible(version.xmin, version.xmax) is True
    seen = [found.values for _ref, found in stack.heap.scan(table, later.snapshot)]
    assert seen == [(1, "ada")]
    stack.manager.rollback(later)


def test_a_snapshot_exactly_at_the_commit_number_sees_the_row(stack: Stack) -> None:
    """The boundary itself: visible() is inclusive at xmin, so the commit's own number sees it."""
    table = _registered(stack, _table())
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (1, "ada"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)
    version = stack.heap.read(txn.row_refs[0])
    assert Snapshot(report.csn).visible(version.xmin, version.xmax) is True
    assert Snapshot(report.csn - 1).visible(version.xmin, version.xmax) is False


def test_several_rows_of_one_commit_share_its_number(stack: Stack) -> None:
    table = _registered(stack, _table())
    txn = stack.manager.begin("write")
    for identity in range(1, 5):
        txn.stage_row_insert(table, (identity, f"row-{identity}"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"batch"))
    report = stack.manager.commit(txn)
    stamps = {stack.heap.read(ref).xmin for ref in txn.row_refs}
    assert stamps == {report.csn}
    assert len(txn.row_refs) == 4


def test_rows_of_later_commits_carry_later_numbers(stack: Stack) -> None:
    table = _registered(stack, _table())
    stamps = []
    for identity in range(1, 4):
        txn = stack.manager.begin("write")
        txn.stage_row_insert(table, (identity, f"row-{identity}"))
        txn.note_write(stack.manager.partition_of(table.table_id, bytes([identity])))
        report = stack.manager.commit(txn)
        stamps.append((stack.heap.read(txn.row_refs[0]).xmin, report.csn))
    assert [stamp for stamp, _csn in stamps] == [csn for _stamp, csn in stamps]
    assert [stamp for stamp, _csn in stamps] == sorted(stamp for stamp, _csn in stamps)


# --- nothing becomes reachable before the commit passes every refusal ----------------------------


def test_a_rolled_back_transaction_writes_no_row(stack: Stack) -> None:
    table = _registered(stack, _table())
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (1, "never"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    assert txn.row_intents != [], "this bench needs a row staged before the rollback"
    stack.manager.rollback(txn)
    reader = stack.manager.begin("read")
    assert list(stack.heap.scan_all(table)) == []
    assert list(stack.heap.scan(table, reader.snapshot)) == []
    assert txn.row_refs == []
    assert txn.row_intents == [], "an abandoned transaction holds nothing it staged"
    stack.manager.rollback(reader)


def test_a_refused_commit_writes_no_row(make_stack) -> None:
    """Optimistic validation refuses before the heap is touched, so nothing may appear."""
    first = make_stack()
    second = make_stack()
    table = _registered(first, _table())
    shared = first.manager.partition_of(table.table_id, b"contended")

    loser = second.manager.begin("write")
    loser.stage_row_insert(table, (2, "loser"))
    loser.note_write(shared)

    winner = first.manager.begin("write")
    winner.owner._stage_page_image(
        winner, HEAP, 9, make_page_image(first.codec, [b"w"], page_index=9)
    )
    winner.note_write(shared)
    first.manager.commit(winner)

    with pytest.raises(GrafxWriteConflict):
        second.manager.commit(loser)
    # The row was written before validation, because the page it lands on is part of what the
    # commit must declare (E1). What matters is that it is unreachable: the refusal put its
    # birth stamp beyond every snapshot, and nothing of it was ever made durable.
    reader = second.manager.begin("read")
    assert list(second.heap.scan(table, reader.snapshot)) == []
    assert all(version.xmin == 0 for _ref, version in second.heap.scan_all(table))
    second.manager.rollback(reader)


def test_an_identity_is_not_handed_out_twice(stack: Stack) -> None:
    """A gap in the sequence is harmless; a repeat would put two rows under one identity."""
    table = _registered(stack, _table())
    identities = []
    for round_number in range(4):
        txn = stack.manager.begin("write")
        txn.stage_row_insert(table, (round_number, f"row-{round_number}"))
        txn.note_write(
            stack.manager.partition_of(table.table_id, bytes([round_number]))
        )
        stack.manager.commit(txn)
        identities.append(stack.heap.read(txn.row_refs[0]).record_id)
    assert len(set(identities)) == len(identities)
    assert identities == sorted(identities)


def test_a_read_transaction_cannot_stage_a_row(stack: Stack) -> None:
    txn = stack.manager.begin("read")
    with pytest.raises(GrafxTransactionStateError):
        txn.stage_row_insert(_table(), (1, "no"))


def test_a_staged_row_needs_a_table(stack: Stack) -> None:
    from okto_grafx.domain.errors import GrafxConfigurationError

    txn = stack.manager.begin("write")
    with pytest.raises(GrafxConfigurationError) as raised:
        txn.stage_row_insert(None, (1, "no"))
    assert raised.value.details["field"] == "table"


# --- the row survives a reopen ---------------------------------------------------------------------


def test_a_committed_row_is_there_for_a_participant_that_opens_after_it(
    database_root: Path,
) -> None:
    """A row that only exists in the committing process is a commit nobody else has.

    The reader is opened AFTER the commit, which is what a reopen does and what FR-1 describes.
    A participant that had ALREADY read the table before the commit is a different question: its
    heap holds derived state keyed on an epoch its own pool advances, and no pool can see another
    process writing. That is C1's coherency to answer, and it is reported rather than worked
    around here.
    """
    writer = build_stack(database_root, owner_id="writer")
    table = _registered(writer, _table())
    txn = writer.manager.begin("write")
    txn.stage_row_insert(table, (1, "ada"))
    txn.note_write(writer.manager.partition_of(table.table_id, b"1"))
    report = writer.manager.commit(txn)

    reader = build_stack(database_root, owner_id="reader")
    fresh = reader.manager.begin("read")
    assert fresh.snapshot.read_lsn >= report.csn
    seen = [found.values for _ref, found in reader.heap.scan(table, fresh.snapshot)]
    assert seen == [(1, "ada")]
    version = reader.heap.read(txn.row_refs[0])
    assert version.xmin == report.csn
    reader.manager.rollback(fresh)


def test_the_page_the_row_landed_on_is_in_the_log(stack: Stack) -> None:
    """A change with no record to redo it is the failure the log exists to prevent."""
    from okto_grafx.domain.txn import WalRecordType, decode_page_write

    table = _registered(stack, _table())
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (1, "ada"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)

    logged = {
        decode_page_write(record.payload).page_index
        for record in stack.wal.records()
        if record.record_type == WalRecordType.WRITE_PAGE and record.lsn <= report.csn
    }
    assert txn.row_refs[0].page in logged, "the page the row landed on was never logged"


def test_the_logged_image_carries_the_corrected_birth_stamp(stack: Stack) -> None:
    """The provisional stamp must never reach the log, or a replay would install it."""
    from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
    from okto_grafx.domain.txn import WalRecordType, decode_page_write

    table = _registered(stack, _table())
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (1, "ada"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)
    reference = txn.row_refs[0]

    written = [
        decode_page_write(record.payload)
        for record in stack.wal.records()
        if record.record_type == WalRecordType.WRITE_PAGE
    ]
    holding = [item for item in written if item.page_index == reference.page]
    assert len(holding) == 1
    page = stack.codec.decode_page(holding[0].image, verify=True)
    header = RecordHeader.decode(page.read_slot(reference.slot)[:RECORD_HEADER_SIZE])
    assert header.xmin == report.csn
    assert header.record_id == stack.heap.read(reference).record_id


def test_a_row_written_by_a_commit_that_then_failed_is_unreachable(
    database_root: Path,
) -> None:
    """The invariant: nothing is reachable before every step that can still refuse has passed.

    A row reaches the buffer pool before the log is asked for anything, and the pool is shared:
    the next commit that succeeds flushes that file and would carry the abandoned row to the
    device with it. It must be unreachable to every snapshot by then, whatever happens to the
    page.
    """
    from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
    from okto_grafx.domain.errors import GrafxDeviceFull
    from shared_device import SharedDirectoryDevice
    from txn_support import DEFAULT_PAGE_SIZE

    device = FaultInjectingStorageDevice(
        SharedDirectoryDevice(database_root, page_size=DEFAULT_PAGE_SIZE), seed=5
    )
    stack = build_stack(database_root, storage=device)
    table = _registered(stack, _table())

    doomed = stack.manager.begin("write")
    doomed.stage_row_insert(table, (1, "never"))
    doomed.note_write(stack.manager.partition_of(table.table_id, b"1"))
    device.clear_trail()
    device.fill_device_on("append_log", 1)
    with pytest.raises(GrafxDeviceFull):
        stack.manager.commit(doomed)
    device.disarm()

    survivor = stack.manager.begin("write")
    survivor.stage_row_insert(table, (2, "kept"))
    survivor.note_write(stack.manager.partition_of(table.table_id, b"2"))
    report = stack.manager.commit(survivor)

    reader = build_stack(database_root, storage=device, owner_id="reader")
    fresh = reader.manager.begin("read")
    seen = [found.values for _ref, found in reader.heap.scan(table, fresh.snapshot)]
    assert seen == [(2, "kept")], (
        "the abandoned row must not be visible to any snapshot"
    )
    stored = [version.xmin for _ref, version in reader.heap.scan_all(table)]
    assert report.csn in stored
    # Every version the device ended up holding is either this commit's or unreachable. The
    # disjunction that used to be here ("or there is only one version") made the assertion pass
    # when the abandoned row never reached the device at all, which is vacuity (A72): the
    # mutation that removes the repair survives this bench, and that is recorded as an open
    # survivor rather than dressed up as covered.
    assert all(stamp in (0, report.csn) for stamp in stored)
    reader.manager.rollback(fresh)


def test_the_heap_header_page_is_logged_whenever_a_row_is_written(stack: Stack) -> None:
    """The directory page carries the extent and the identity counter, and both change here.

    A row write moves the table's extent hint and its ``next_record_id`` on the reserved header
    page. If that page is not in the log, a replay restores the row and loses the directory entry
    that reaches it -- the row is on a page no scan walks to, and the next identity is handed out
    a second time. Neither is visible without a replay, which is why the mutation that stopped
    logging this page survived until this test existed.
    """
    from okto_grafx.domain.page import HEADER_PAGE_INDEX
    from okto_grafx.domain.txn import WalRecordType, decode_page_write

    table = _registered(stack, _table())
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (1, "ada"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)

    logged = {
        decode_page_write(record.payload).page_index
        for record in stack.wal.records()
        if record.record_type == WalRecordType.WRITE_PAGE and record.lsn <= report.csn
    }
    assert HEADER_PAGE_INDEX in logged, "the table directory page was never logged"
    assert txn.row_refs[0].page in logged

    written = [
        decode_page_write(record.payload)
        for record in stack.wal.records()
        if record.record_type == WalRecordType.WRITE_PAGE
    ]
    header = next(item for item in written if item.page_index == HEADER_PAGE_INDEX)
    replayed = stack.codec.decode_page(header.image, verify=True)
    assert replayed.slot_count >= 2, "the logged directory page holds the table entry"
    assert replayed.page_lsn == report.csn


def test_a_participant_that_already_read_a_table_sees_a_foreign_commit(
    database_root: Path,
) -> None:
    """LESSONS L22: derived state needs a SHARED signal, and the published number is it.

    Every epoch a buffer pool keeps is process-local, so nothing in a participant moves when
    ANOTHER participant commits. A participant that had already read a table went on answering
    from frames it cached before that commit -- no error, no missing file, just fewer rows than
    exist. C1's ``begin_read_view`` closes it and takes the token from here, because the pool has
    nothing shared to watch and this component already watches the published commit number.
    """
    writer = build_stack(database_root, owner_id="writer")
    reader = build_stack(database_root, owner_id="reader")
    table = _registered(writer, _table())

    # The reader looks FIRST, so it is holding frames from before the commit.
    early = reader.manager.begin("read")
    assert list(reader.heap.scan(table, early.snapshot)) == []
    reader.manager.rollback(early)

    txn = writer.manager.begin("write")
    txn.stage_row_insert(table, (1, "ada"))
    txn.note_write(writer.manager.partition_of(table.table_id, b"1"))
    report = writer.manager.commit(txn)

    later = reader.manager.begin("read")
    assert later.snapshot.read_lsn >= report.csn
    seen = [found.values for _ref, found in reader.heap.scan(table, later.snapshot)]
    assert seen == [(1, "ada")], "a new read view must see what was committed since"
    reader.manager.rollback(later)


def test_a_read_view_is_not_dropped_when_nothing_has_committed(
    database_root: Path,
) -> None:
    """The token exists so a caller does not pay for a drop nothing needs.

    Same published number means no participant has committed, so the frames are still the truth
    and dropping them would be cost with no answer attached.
    """
    stack = build_stack(database_root)
    _registered(stack, _table())
    first = stack.manager.begin("read")
    stack.manager.rollback(first)
    current = stack.pool.read_view_token()
    assert stack.pool.begin_read_view(current) is False
    assert stack.pool.begin_read_view(object()) is True


def test_two_participants_inserting_from_one_old_extent_view_both_commit(
    make_stack,
) -> None:
    """A page image rebuilt from ``current`` incorporates the earlier disjoint writer.

    Two participants inserting into the same table declare disjoint ROW partitions, so the row
    predicate lets both through. The second commit then refreshes its pool under COMMIT_SECTION
    and materialises onto the page the first writer just published. Its full-page image therefore
    contains BOTH rows. Revalidating that newly discovered page from the transaction's older
    snapshot would refuse incorporated history and starve under a continuous appender; validating
    it from the durable materialisation baseline lets both commits finish without losing either.

    The exact same-page assertion is essential: without it this would only repeat the ordinary
    disjoint-writer test and could not kill a regression back to the old page-half floor.
    """
    first = make_stack()
    second = make_stack()
    table = _registered(first, _table())

    loser = second.manager.begin("write")
    loser.stage_row_insert(table, (2, "second"))
    loser.note_write(second.manager.partition_of(table.table_id, b"2"))

    winner = first.manager.begin("write")
    winner.stage_row_insert(table, (1, "first"))
    winner.note_write(first.manager.partition_of(table.table_id, b"1"))
    first_report = first.manager.commit(winner)

    # The logical row partitions really are disjoint; the shared physical page is proven below.
    assert first.manager.partition_of(
        table.table_id, b"1"
    ) != second.manager.partition_of(table.table_id, b"2")
    second_report = second.manager.commit(loser)
    assert second_report.csn > first_report.csn
    assert loser.row_refs[0].page == winner.row_refs[0].page

    reader = build_stack(second.root, owner_id="third")
    fresh = reader.manager.begin("read")
    seen = sorted(
        found.values for _ref, found in reader.heap.scan(table, fresh.snapshot)
    )
    assert seen == [(1, "first"), (2, "second")], "neither commit may be lost"
    reader.manager.rollback(fresh)


def _commit_row(stack: Stack, table: TableDef, values: tuple) -> tuple:
    """Commit one row and return its reference and commit number."""
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, values)
    txn.note_write(stack.manager.partition_of(table.table_id, bytes([values[0]])))
    report = stack.manager.commit(txn)
    return txn.row_refs[0], report.csn


@pytest.mark.parametrize("operation", ("insert", "update", "delete"))
@pytest.mark.parametrize(
    "cleanup_failure",
    (RuntimeError("foreign cleanup failed"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_provisional_rows_remain_logically_invisible_when_all_cleanup_fails(
    database_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    cleanup_failure: BaseException,
) -> None:
    """Even persisted pre-WAL frames encode no row outcome a snapshot can observe."""
    stack = build_stack(database_root, owner_id="doomed-writer")
    table = _registered(stack, _table())
    original = None
    expected: list[tuple] = []
    if operation != "insert":
        original, _born = _commit_row(stack, table, (1, "seed"))
        expected = [(1, "seed")]
        stack.pool.flush(HEAP)

    doomed = stack.manager.begin("write")
    if operation == "insert":
        doomed.stage_row_insert(table, (2, "never"))
    elif operation == "update":
        assert original is not None
        doomed.stage_row_update(table, original, (1, "never"))
    else:
        assert original is not None
        doomed.stage_row_delete(table, original)
    doomed.note_write(stack.manager.partition_of(table.table_id, b"doomed"))
    primary = GrafxDeviceFull("The WAL refused this batch.", free_bytes=0)

    def persist_provisional_then_fail(
        _records: object, *, expected_terminal_lsn: int | None = None
    ) -> int:
        # This is the eviction/crash shape: provisional heap/header frames reach the device before
        # append reports failure, and none of the compensating cleanup is allowed to help.
        assert expected_terminal_lsn is not None
        stack.pool.flush(HEAP)
        raise primary

    def fail_restamp(
        _manager: TransactionManager, _reference: object, **_changes: object
    ) -> None:
        raise cleanup_failure

    def fail_write_back(_pool: BufferPool, _file: str, _page_index: int) -> bool:
        raise cleanup_failure

    monkeypatch.setattr(stack.wal, "append_many", persist_provisional_then_fail)
    monkeypatch.setattr(TransactionManager, "_restamp", fail_restamp)
    monkeypatch.setattr(BufferPool, "write_back", fail_write_back)

    with pytest.raises(GrafxDeviceFull) as escaped:
        stack.manager.commit(doomed)

    assert escaped.value is primary
    assert stack.manager.recovery_required is True
    with pytest.raises(GrafxRecoveryRefused):
        stack.manager.begin("write")

    reopened = build_stack(database_root, owner_id="post-crash-reader")
    reader = reopened.manager.begin("read")
    seen = [
        version.values for _ref, version in reopened.heap.scan(table, reader.snapshot)
    ]
    assert seen == expected
    assert (
        Verifier(
            reopened.pool,
            reopened.metrics,
            heap=reopened.heap,
            catalog=reopened.catalog,
        )
        .verify()
        .findings
        == ()
    )
    reopened.manager.rollback(reader)


# --- update -----------------------------------------------------------------------------------


def test_an_update_ends_the_old_version_and_starts_the_new_one_at_one_number(
    stack: Stack,
) -> None:
    """Both halves of an update carry the same commit number, or a snapshot sees zero or two."""
    table = _registered(stack, _table())
    original, born_at = _commit_row(stack, table, (1, "ada"))

    txn = stack.manager.begin("write")
    txn.stage_row_update(table, original, (1, "ADA"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)

    old = stack.heap.read(original)
    new = stack.heap.read(txn.row_refs[0])
    assert old.xmin == born_at and old.xmax == report.csn
    assert new.xmin == report.csn and new.xmax == 0
    assert new.values == (1, "ADA")


def test_exactly_one_version_is_live_at_every_snapshot_across_an_update(
    stack: Stack,
) -> None:
    """The property an update exists to keep: never zero live versions, never two."""
    table = _registered(stack, _table())
    original, born_at = _commit_row(stack, table, (1, "ada"))
    before = stack.manager.begin("read")

    txn = stack.manager.begin("write")
    txn.stage_row_update(table, original, (1, "ADA"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)
    after = stack.manager.begin("read")

    assert [
        found.values for _ref, found in stack.heap.scan(table, before.snapshot)
    ] == [(1, "ada")]
    assert [found.values for _ref, found in stack.heap.scan(table, after.snapshot)] == [
        (1, "ADA")
    ]
    for snapshot in (Snapshot(born_at), Snapshot(report.csn - 1), Snapshot(report.csn)):
        live = [
            found.values
            for _ref, found in stack.heap.scan_all(table)
            if snapshot.visible(found.xmin, found.xmax)
        ]
        assert len(live) == 1, f"snapshot {snapshot.read_lsn} saw {live}"
    stack.manager.rollback(before)
    stack.manager.rollback(after)


def test_an_update_keeps_the_identity_of_the_row(stack: Stack) -> None:
    table = _registered(stack, _table())
    original, _born = _commit_row(stack, table, (1, "ada"))
    identity = stack.heap.read(original).record_id
    txn = stack.manager.begin("write")
    txn.stage_row_update(table, original, (1, "ADA"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    stack.manager.commit(txn)
    assert stack.heap.read(txn.row_refs[0]).record_id == identity


# --- delete -----------------------------------------------------------------------------------


def test_a_delete_ends_the_version_at_the_commit_number(stack: Stack) -> None:
    table = _registered(stack, _table())
    original, born_at = _commit_row(stack, table, (1, "ada"))

    txn = stack.manager.begin("write")
    txn.stage_row_delete(table, original)
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)

    version = stack.heap.read(original)
    assert version.xmin == born_at
    assert version.xmax == report.csn
    assert txn.row_refs == []


def test_a_snapshot_older_than_a_delete_still_reads_the_row(stack: Stack) -> None:
    """BR-9: the view a reader was given does not change because somebody deleted the row."""
    table = _registered(stack, _table())
    original, _born = _commit_row(stack, table, (1, "ada"))
    before = stack.manager.begin("read")

    txn = stack.manager.begin("write")
    txn.stage_row_delete(table, original)
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    stack.manager.commit(txn)
    after = stack.manager.begin("read")

    assert [
        found.values for _ref, found in stack.heap.scan(table, before.snapshot)
    ] == [(1, "ada")]
    assert list(stack.heap.scan(table, after.snapshot)) == []
    stack.manager.rollback(before)
    stack.manager.rollback(after)


def test_a_refused_delete_leaves_the_row_live(make_stack) -> None:
    """Undoing a delete has to put the version back, not merely leave a new one unreachable."""
    first = make_stack()
    second = make_stack()
    table = _registered(first, _table())
    original, _born = _commit_row(first, table, (1, "ada"))

    loser = second.manager.begin("write")
    loser.stage_row_delete(table, original)
    loser.note_write(second.manager.partition_of(table.table_id, b"1"))

    winner = first.manager.begin("write")
    winner.stage_row_update(table, original, (1, "ADA"))
    winner.note_write(first.manager.partition_of(table.table_id, b"1"))
    first.manager.commit(winner)

    with pytest.raises(GrafxWriteConflict):
        second.manager.commit(loser)

    reader = second.manager.begin("read")
    live = [found.values for _ref, found in second.heap.scan(table, reader.snapshot)]
    assert live == [(1, "ADA")], "the refused delete must not have ended anything"
    second.manager.rollback(reader)


def test_a_rolled_back_update_leaves_exactly_one_live_version(stack: Stack) -> None:
    table = _registered(stack, _table())
    original, born_at = _commit_row(stack, table, (1, "ada"))
    txn = stack.manager.begin("write")
    txn.stage_row_update(table, original, (1, "gone"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    stack.manager.rollback(txn)
    reader = stack.manager.begin("read")
    live = [found.values for _ref, found in stack.heap.scan(table, reader.snapshot)]
    assert live == [(1, "ada")]
    assert stack.heap.read(original).xmax == 0
    assert stack.heap.read(original).xmin == born_at
    stack.manager.rollback(reader)


# --- a statement is the unit a caller discards --------------------------------------------------


def test_a_statement_that_refuses_halfway_leaves_nothing_of_itself(
    stack: Stack,
) -> None:
    """A statement that stages part of itself and then refuses must leave nothing behind.

    C10 found a pattern staging its first node before refusing its second, so a caller that
    caught the refusal and committed made half a statement durable. A caller marks the
    transaction before a statement and discards back to that mark when the statement refuses, so
    a commit that follows carries whole statements only.
    """
    table = _registered(stack, _table())
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (1, "kept"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))

    mark = txn.staging_mark()
    txn.stage_row_insert(table, (2, "half"))
    txn.stage_row_insert(table, (3, "of a statement"))
    assert len(txn.row_intents) == 3
    txn.discard_since(mark)
    assert len(txn.row_intents) == 1

    report = stack.manager.commit(txn)
    reader = stack.manager.begin("read")
    seen = [found.values for _ref, found in stack.heap.scan(table, reader.snapshot)]
    assert seen == [(1, "kept")], "only the whole statement is durable"
    assert report.wrote is True
    stack.manager.rollback(reader)


def test_a_mark_this_transaction_never_passed_through_is_refused(stack: Stack) -> None:
    txn = stack.manager.begin("write")
    with pytest.raises(GrafxTransactionStateError) as raised:
        txn.discard_since((5, 0, 0))
    assert raised.value.details["field"] == "mark"


def test_the_page_of_the_version_an_update_ended_is_logged(stack: Stack) -> None:
    """A replay that restores the new version and not the end of the old one resurrects a row.

    An update writes two headers: the new version is born at the commit number and the old one
    ends at it. They can sit on different pages, and a commit that logged only the page holding
    the new version would replay into TWO live versions of one record -- a duplicate that no
    snapshot rule can resolve, from a commit that was acknowledged as durable.
    """
    from okto_grafx.domain.txn import WalRecordType, decode_page_write

    table = _registered(stack, _table())
    original, _born = _commit_row(stack, table, (1, "ada"))

    txn = stack.manager.begin("write")
    txn.stage_row_update(table, original, (1, "ADA"))
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)

    written = {
        decode_page_write(record.payload).page_index: decode_page_write(
            record.payload
        ).image
        for record in stack.wal.records()
        if record.record_type == WalRecordType.WRITE_PAGE and record.lsn <= report.csn
    }
    assert original.page in written, (
        "the page of the version this update ended was never logged"
    )
    page = stack.codec.decode_page(written[original.page], verify=True)
    header = RecordHeader.decode(page.read_slot(original.slot)[:RECORD_HEADER_SIZE])
    assert header.xmax == report.csn, (
        "the logged image must carry the end of the old version"
    )


def test_the_page_of_the_version_a_delete_ended_is_logged(stack: Stack) -> None:
    """Same for a delete: the end is the only thing it writes, so it must be the thing logged."""
    from okto_grafx.domain.txn import WalRecordType, decode_page_write

    table = _registered(stack, _table())
    original, _born = _commit_row(stack, table, (1, "ada"))

    txn = stack.manager.begin("write")
    txn.stage_row_delete(table, original)
    txn.note_write(stack.manager.partition_of(table.table_id, b"1"))
    report = stack.manager.commit(txn)

    written = {
        decode_page_write(record.payload).page_index: decode_page_write(
            record.payload
        ).image
        for record in stack.wal.records()
        if record.record_type == WalRecordType.WRITE_PAGE and record.lsn <= report.csn
    }
    assert original.page in written
    page = stack.codec.decode_page(written[original.page], verify=True)
    header = RecordHeader.decode(page.read_slot(original.slot)[:RECORD_HEADER_SIZE])
    assert header.xmax == report.csn


def test_two_transactions_inserting_one_primary_key_cannot_both_commit(
    make_stack,
) -> None:
    """Where PRIMARY KEY uniqueness has to be enforced, and what this component already gives.

    A uniqueness check taken under a SNAPSHOT can never be sufficient on its own: two concurrent
    transactions each read a snapshot that predates the other, each find no row, and each is
    entitled to insert. What makes the pair impossible is that they declare the SAME partition,
    because a partition is derived from the key -- so optimistic validation refuses the second,
    retryably, and its retry runs against a snapshot that contains the first row.

    So the serialisation is here and it is free; the CHECK belongs to whoever materialises the
    row, and making it cheap belongs to an index. This test is the evidence for that routing:
    the concurrency half already holds.
    """
    first = make_stack()
    second = make_stack()
    table = _registered(first, _table())
    key = b"1"
    partition = first.manager.partition_of(table.table_id, key)
    assert partition == second.manager.partition_of(table.table_id, key), (
        "both participants must derive the same partition from the same key"
    )

    loser = second.manager.begin("write")
    loser.stage_row_insert(table, (1, "second"))
    loser.note_write(partition)

    winner = first.manager.begin("write")
    winner.stage_row_insert(table, (1, "first"))
    winner.note_write(partition)
    first.manager.commit(winner)

    with pytest.raises(GrafxWriteConflict) as raised:
        second.manager.commit(loser)
    assert raised.value.retryable is True

    # The retry runs against a snapshot that HAS the first row, so a snapshot-time check by the
    # component that materialises the row can now see the duplicate and refuse it.
    successor = second.manager.retry(loser)
    existing = [
        found.values for _ref, found in second.heap.scan(table, successor.snapshot)
    ]
    assert existing == [(1, "first")], (
        "the retry must see the row that beat it, or no check could ever catch the duplicate"
    )
    second.manager.rollback(successor)
