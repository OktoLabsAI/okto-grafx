"""The commit protocol driven by C4's real write-ahead log, not by a double.

Everything else in this suite stands a log in, because a component must be provable against the
FROZEN interface of CONTRACT.md section 8.3 rather than against whatever a sibling currently
does. This module answers the other half: the two really do fit together -- the records this
component builds are records that log accepts, the sequence numbers it assigns are the ones the
pages are stamped with, and the COMMIT records it stores are the ones optimistic validation reads
back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.domain.errors import GrafxWriteConflict
from okto_grafx.domain.txn import (
    CommitPayload,
    WalRecordType,
    decode_page_write,
    page_partition,
)
from okto_grafx.engine.wal_manager import WalManager
from txn_support import Stack, build_stack, make_page_image, page_lsn_of, read_page_payloads

HEAP = "heap.dat"
SEGMENT_BYTES = 4096


def _real_wal(device: object, clock: object, metrics: object) -> WalManager:
    """Build C4's log over the same device the rest of the stack uses, and open it."""
    manager = WalManager(
        device,  # type: ignore[arg-type]
        clock,  # type: ignore[arg-type]
        metrics,  # type: ignore[arg-type]
        segment_bytes=SEGMENT_BYTES,
        descriptor="hash-v1;partitions_per_table=8",
    )
    manager.open()
    return manager


def _stage(stack: Stack, page_index: int, payload: bytes) -> object:
    """Open a write transaction that changes one page of the heap."""
    txn = stack.manager.begin("write")
    txn.stage_page_image(
        HEAP, page_index, make_page_image(stack.codec, [payload], page_index=page_index)
    )
    txn.note_write(stack.manager.partition_of(1, payload))
    return txn


@pytest.fixture
def real_stack(database_root: Path) -> Stack:
    """Return a participant whose log is the real WalManager."""
    return build_stack(database_root, wal_factory=_real_wal, owner_id="real-a")


def test_a_commit_through_the_real_log_lands_its_page_and_its_commit_number(
    real_stack: Stack,
) -> None:
    before = real_stack.wal.last_lsn
    report = real_stack.manager.commit(_stage(real_stack, 3, b"real"))
    assert report.wrote is True and report.durable is True
    assert report.csn == real_stack.wal.last_lsn
    assert read_page_payloads(real_stack.pool, HEAP, 3) == (b"real",)
    # The page carries the batch stamp, which is above everything the log had assigned before
    # the batch and at most the number the COMMIT record itself got. The two are the same value
    # unless the log inserted a record of its own into the batch, which C4's does when a segment
    # rolls -- and either way the redo rule of step 6 has what it needs.
    assert before < page_lsn_of(real_stack.pool, HEAP, 3) <= report.csn


def test_the_records_this_component_builds_read_back_from_the_real_log(
    real_stack: Stack,
) -> None:
    report = real_stack.manager.commit(_stage(real_stack, 4, b"records"))
    records = list(real_stack.wal.read_from(1))
    writes = [item for item in records if item.record_type == WalRecordType.WRITE_PAGE]
    commits = [item for item in records if item.record_type == WalRecordType.COMMIT]
    assert len(writes) == 1 and len(commits) == 1
    assert commits[0].lsn == report.csn
    written = decode_page_write(writes[0].payload)
    assert written.file == HEAP and written.page_index == 4
    payload = CommitPayload.decode(commits[0].payload)
    # The write set carries the row partition the caller declared and the page this commit
    # overwrote: a page image replaces the whole page, so the page is part of the claim any
    # other participant must be able to conflict against (defect E1).
    assert real_stack.manager.partition_of(1, b"records") in payload.write_partitions
    assert page_partition(HEAP, 4) in payload.write_partitions


def test_optimistic_validation_reads_the_real_log_and_refuses_a_real_conflict(
    database_root: Path,
) -> None:
    """The end-to-end shape of AC-2, with nothing standing in for the log."""
    first = build_stack(database_root, wal_factory=_real_wal, owner_id="real-a")
    second = build_stack(database_root, wal_factory=_real_wal, owner_id="real-b")
    shared = first.manager.partition_of(1, b"contended")
    loser = second.manager.begin("write")
    loser.stage_page_image(HEAP, 6, make_page_image(second.codec, [b"b"], page_index=6))
    loser.note_write(shared)
    winner = first.manager.begin("write")
    winner.stage_page_image(HEAP, 5, make_page_image(first.codec, [b"a"], page_index=5))
    winner.note_write(shared)
    first.manager.commit(winner)
    second.wal.open()  # a second participant re-reads what the first appended
    with pytest.raises(GrafxWriteConflict) as raised:
        second.manager.commit(loser)
    assert raised.value.retryable is True
    successor = second.manager.retry(loser)
    successor.stage_page_image(HEAP, 6, make_page_image(second.codec, [b"b"], page_index=6))
    successor.note_write(shared)
    assert second.manager.commit(successor).wrote is True
    assert read_page_payloads(first.pool, HEAP, 5) == (b"a",)
    assert read_page_payloads(first.pool, HEAP, 6) == (b"b",)


def test_a_reopened_log_finds_every_commit_this_component_wrote(
    database_root: Path,
) -> None:
    stack = build_stack(database_root, wal_factory=_real_wal, owner_id="real-a")
    numbers = [
        stack.manager.commit(_stage(stack, page, bytes([page]))).csn for page in (3, 4, 5)
    ]
    reopened = build_stack(database_root, wal_factory=_real_wal, owner_id="real-c")
    assert reopened.wal.last_lsn == numbers[-1]
    assert reopened.manager.published_lsn() == numbers[-1]
    commits = [
        record.lsn
        for record in reopened.wal.read_from(1)
        if record.record_type == WalRecordType.COMMIT
    ]
    assert commits == numbers


def test_the_page_on_the_device_is_byte_identical_to_the_image_in_the_log(
    real_stack: Stack,
) -> None:
    """A replay must reproduce the page this commit wrote, not one that merely looks like it.

    Every field the redo installs is compared, the sequence counter excepted: A21 has the pool
    advance that by two on every write-back, so it is the one field a page that went through the
    pool cannot share with the image that went into the log.

    C4's log inserts a SEGMENT_HEADER record of its own when a segment rolls, so the COMMIT
    record can land on a higher number than the batch position this component predicted. What
    must not follow is the page and the record disagreeing about the page: the image is stamped
    once and the same bytes go to both.
    """
    real_stack.manager.commit(_stage(real_stack, 7, b"identical"))
    logged = [
        decode_page_write(record.payload)
        for record in real_stack.wal.read_from(1)
        if record.record_type == WalRecordType.WRITE_PAGE
    ]
    assert len(logged) == 1
    on_device = real_stack.codec.decode_page(real_stack.storage.read_page(HEAP, 7), verify=True)
    in_log = real_stack.codec.decode_page(logged[0].image, verify=True)
    assert on_device.page_lsn == in_log.page_lsn
    assert on_device.page_type == in_log.page_type
    assert on_device.next_page == in_log.next_page
    assert list(on_device.iter_slots()) == list(in_log.iter_slots())
    # The sequence counter is deliberately NOT compared: amendment A21 has the buffer pool
    # advance it by two on every write-back, so the field is expected to move and asserting
    # either way about it would assert nothing (A72).
    assert on_device.seq % 2 == 0, "a durable image always carries an even counter (A21)"


def test_the_stamp_is_above_every_number_the_log_had_assigned_before_the_batch(
    real_stack: Stack,
) -> None:
    """The property the redo rule of step 6 rests on, stated as the rule rather than as a symptom."""
    numbers = []
    for page in (3, 4, 5, 6):
        before = real_stack.wal.last_lsn
        real_stack.manager.commit(_stage(real_stack, page, bytes([page])))
        stamped = page_lsn_of(real_stack.pool, HEAP, page)
        assert stamped > before
        numbers.append(stamped)
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)


# --- the checkpoint redoes the log onto the device before it reclaims anything (BR-10, CF-11) ----


def test_a_checkpoint_installs_a_durable_commit_whose_pages_never_reached_the_device(
    tmp_path: Path,
) -> None:
    """The window the checkpoint's redo exists for: logged and barriered, then never applied.

    The commit protocol barriers the log (step 3.5) and only THEN writes the pages (step 3.6). A
    device that refuses the page write leaves a commit that is durable in the log, acknowledged
    as such, and absent from every heap page -- the P4 window the module docstring names. A later
    commit publishes a number above it, and recovery would redo it from the log.

    A checkpoint that flushed only its own pool and then recycled the segments below the number
    it published would destroy the only copy of those pages. So the checkpoint replays the log
    onto the device first, through the same idempotent door recovery uses, and this test reads
    the page numbers back from the DEVICE -- never from any component's report of itself.
    """
    from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
    from okto_grafx.domain.errors import GrafxError
    from shared_device import SharedDirectoryDevice
    from txn_support import DEFAULT_PAGE_SIZE

    root = tmp_path / "db"
    device = FaultInjectingStorageDevice(
        SharedDirectoryDevice(root, page_size=DEFAULT_PAGE_SIZE), seed=11
    )
    writer = build_stack(root, storage=device, wal_factory=_real_wal, owner_id="lost-b")
    doomed = writer.manager.begin("write")
    for page in (3, 4):
        doomed.stage_page_image(
            "heap.dat", page, make_page_image(writer.codec, [b"lost"], page_index=page)
        )
    doomed.note_write(writer.manager.partition_of(1, b"lost"))
    # Occurrences count from the start of the trail, and the bootstrap already wrote pages; the
    # first write_page AFTER this point is step 3.6 of the commit, past the log barrier.
    device.clear_trail()
    device.fill_device_on("write_page", 1)
    with pytest.raises(GrafxError):
        writer.manager.commit(doomed)
    device.disarm()
    lost_lsn = max(
        record.lsn
        for record in writer.wal.read_from(1)
        if record.record_type == WalRecordType.COMMIT
    )
    # The number the lost pages carry is the image's own stamp, which the log holds; it is one
    # below the COMMIT record whenever the log inserted a segment header into the batch.
    logged_stamp = max(
        writer.codec.decode_page(decode_page_write(record.payload).image, verify=True).page_lsn
        for record in writer.wal.read_from(1)
        if record.record_type == WalRecordType.WRITE_PAGE
        and decode_page_write(record.payload).page_index == 3
    )
    # Durable in the log, absent from the device: the window.
    assert page_lsn_of(writer.pool, "heap.dat", 3) != logged_stamp
    writer.manager.close()

    # Another participant commits above it and publishes, which is what a checkpoint would rest on.
    other = build_stack(root, storage=device, wal_factory=_real_wal, owner_id="other-a")
    later = other.manager.begin("write")
    later.stage_page_image(HEAP, 5, make_page_image(other.codec, [b"later"], page_index=5))
    # A different table's partition: the lost commit's COMMIT record sits in the log above this
    # snapshot, and optimistic validation would refuse a write to the partition it named.
    later.note_write(other.manager.partition_of(2, b"later"))
    other.manager.commit(later)
    assert other.manager.published_lsn() > lost_lsn

    report = other.manager.checkpoint()
    assert other.manager.published_state().checkpoint_lsn >= lost_lsn
    # The redo installed the lost commit's pages before anything was published or reclaimed.
    assert page_lsn_of(other.pool, "heap.dat", 3) == logged_stamp
    assert page_lsn_of(other.pool, "heap.dat", 4) == logged_stamp
    assert read_page_payloads(other.pool, "heap.dat", 3) == (b"lost",)
    assert report.horizon_lsn >= lost_lsn
    other.manager.close()
