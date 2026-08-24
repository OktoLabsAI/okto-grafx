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
from typing import Any

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.domain.errors import (
    GrafxDeviceFull,
    GrafxError,
    GrafxRecoveryRefused,
    GrafxWriteConflict,
)
from okto_grafx.domain.ids import PageIndex
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE
from okto_grafx.domain.txn import (
    CommitPayload,
    CommitState,
    WalRecord,
    WalRecordType,
    decode_page_write,
    encode_page_write,
    page_partition,
)
from okto_grafx.engine.wal_manager import WalManager
from shared_device import SharedDirectoryDevice
from txn_support import (
    DEFAULT_PAGE_SIZE,
    Stack,
    build_stack,
    make_page_image,
    page_lsn_of,
    read_page_payloads,
)

HEAP = "heap.dat"
SEGMENT_BYTES = 4096


class _PersistentPageFullDevice(FaultInjectingStorageDevice):
    """Refuse every page write until the test explicitly releases the device.

    The standard fault plan is deliberately one-shot.  This adapter models the longer P4 window
    in which both the original post-barrier flush and the transaction manager's immediate redo
    fail, leaving the acknowledged WAL as the only durable copy until checkpoint retries it.
    """

    def __init__(self, inner: StorageDevice) -> None:
        super().__init__(inner, seed=11)
        self.block_page_writes = False

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        if self.block_page_writes:
            raise GrafxDeviceFull(
                "The test device is persistently refusing page writes.",
                file=file,
                page=page_index,
            )
        super().write_page(file, page_index, data)


class _PostWriteWalFailureDevice:
    """Fail after WAL bytes land and optionally refuse the compensating truncation."""

    def __init__(self, inner: StorageDevice) -> None:
        self._inner = inner
        self.append_failure: BaseException | None = None
        self.rollback_failure: BaseException | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def page_size(self) -> int:
        return self._inner.page_size

    def append_log(self, file: str, payload: bytes) -> int:
        offset = self._inner.append_log(file, payload)
        if file.startswith("wal/") and self.append_failure is not None:
            failure = self.append_failure
            self.append_failure = None
            raise failure
        return offset

    def truncate_log(self, file: str, size: int) -> None:
        if file.startswith("wal/") and self.rollback_failure is not None:
            raise self.rollback_failure
        self._inner.truncate_log(file, size)


def _wal_bytes(root: Path) -> dict[str, bytes]:
    """Return the exact retained WAL namespace and contents."""
    return {path.name: path.read_bytes() for path in sorted((root / "wal").glob("*.wal"))}


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
    txn.owner._stage_page_image(txn,
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
    # Planning includes C4's own SEGMENT_HEADER, so every durable representation names the exact
    # terminal COMMIT LSN even when this first batch rolls into a fresh segment.
    assert before < page_lsn_of(real_stack.pool, HEAP, 3) == report.csn


@pytest.mark.parametrize(
    "rollback_failure",
    (RuntimeError("rollback failed"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_an_unrepaired_post_write_escape_latches_the_transaction_handle(
    database_root: Path,
    rollback_failure: BaseException,
) -> None:
    device = _PostWriteWalFailureDevice(
        SharedDirectoryDevice(database_root, page_size=DEFAULT_PAGE_SIZE)
    )
    stack = build_stack(
        database_root,
        storage=device,  # type: ignore[arg-type]
        wal_factory=_real_wal,
        owner_id="post-write",
    )
    txn = _stage(stack, 3, b"never-published")
    primary = RuntimeError("append escaped after storing the batch")
    device.append_failure = primary
    device.rollback_failure = rollback_failure

    with pytest.raises(RuntimeError) as escaped:
        stack.manager.commit(txn)

    assert escaped.value is primary
    assert stack.manager.recovery_required is True
    # A complete surviving COMMIT decodes cleanly; sticky outcome uncertainty, not scan damage,
    # is what closes the append door until recovery settles it.
    assert stack.wal.damage is None
    assert stack.wal.append_uncertain is True
    with pytest.raises(GrafxRecoveryRefused):
        stack.manager.begin("write")


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
    loser.owner._stage_page_image(loser, HEAP, 6, make_page_image(second.codec, [b"b"], page_index=6))
    loser.note_write(shared)
    winner = first.manager.begin("write")
    winner.owner._stage_page_image(winner, HEAP, 5, make_page_image(first.codec, [b"a"], page_index=5))
    winner.note_write(shared)
    first.manager.commit(winner)
    second.wal.open()  # a second participant re-reads what the first appended
    with pytest.raises(GrafxWriteConflict) as raised:
        second.manager.commit(loser)
    assert raised.value.retryable is True
    successor = second.manager.retry(loser)
    successor.owner._stage_page_image(successor, HEAP, 6, make_page_image(second.codec, [b"b"], page_index=6))
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

    C4's log inserts a SEGMENT_HEADER record of its own when a segment rolls. The batch planner
    includes that LSN before materialising its final images, so the page, WRITE_PAGE image and
    terminal COMMIT all carry one exact number.
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
    root = tmp_path / "db"
    device = _PersistentPageFullDevice(
        SharedDirectoryDevice(root, page_size=DEFAULT_PAGE_SIZE)
    )
    writer = build_stack(root, storage=device, wal_factory=_real_wal, owner_id="lost-b")
    doomed = writer.manager.begin("write")
    for page in (3, 4):
        doomed.owner._stage_page_image(doomed,
            "heap.dat", page, make_page_image(writer.codec, [b"lost"], page_index=page)
        )
    doomed.note_write(writer.manager.partition_of(1, b"lost"))
    # Keep both step 3.6 and the immediate post-barrier redo from reaching the device.  A
    # one-shot refusal is insufficient here: the hardened redo retries the same dirty file and
    # can complete as soon as that single injected failure has been consumed.
    device.block_page_writes = True
    with pytest.raises(GrafxError):
        writer.manager.commit(doomed)
    device.block_page_writes = False
    lost_lsn = max(
        record.lsn
        for record in writer.wal.read_from(1)
        if record.record_type == WalRecordType.COMMIT
    )
    # The number the lost pages carry is the exact COMMIT LSN planned with any segment header.
    logged_stamp = max(
        writer.codec.decode_page(decode_page_write(record.payload).image, verify=True).page_lsn
        for record in writer.wal.read_from(1)
        if record.record_type == WalRecordType.WRITE_PAGE
        and decode_page_write(record.payload).page_index == 3
    )
    assert logged_stamp == lost_lsn
    # Durable in the log, absent from the device: the window.
    assert page_lsn_of(writer.pool, "heap.dat", 3) != logged_stamp
    writer.manager.close()

    # Another participant commits above it and publishes, which is what a checkpoint would rest on.
    other = build_stack(root, storage=device, wal_factory=_real_wal, owner_id="other-a")
    later = other.manager.begin("write")
    later.owner._stage_page_image(later, HEAP, 5, make_page_image(other.codec, [b"later"], page_index=5))
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


def test_checkpoint_refuses_a_published_commit_with_its_commit_record_missing(
    tmp_path: Path,
) -> None:
    """Checkpoint never recycles a WAL suffix that cannot prove commit.state's watermark.

    Removing the complete final COMMIT record leaves a syntactically clean WAL ending in the
    commit's page image.  That is more dangerous than an obvious torn record: a reader could
    mistake the shorter stream for a valid history.  The published watermark says otherwise,
    so checkpoint must fail closed before changing any data page, state byte or WAL segment.
    """
    root = tmp_path / "db"
    writer = build_stack(root, wal_factory=_real_wal, owner_id="lineage-a")
    writer.manager.commit(_stage(writer, 3, b"published"))

    records = list(writer.wal.read_from(1))
    commit = records[-1]
    assert commit.record_type == WalRecordType.COMMIT
    segment = writer.wal.segments()[-1].name
    size = writer.storage.log_size(segment)
    writer.storage.truncate_log(segment, size - commit.encoded_length())

    state_path = root / Path(COMMIT_STATE_FILE)
    state_before = state_path.read_bytes()
    wal_before = _wal_bytes(root)
    heap_before = (root / HEAP).read_bytes()

    with pytest.raises(GrafxRecoveryRefused) as refused:
        writer.manager.checkpoint()

    assert refused.value.details["field"] == "wal_lineage"
    assert state_path.read_bytes() == state_before
    assert _wal_bytes(root) == wal_before
    assert (root / HEAP).read_bytes() == heap_before
    writer.manager.close()


def test_checkpoint_refuses_a_committed_page_image_for_a_control_file(
    tmp_path: Path,
) -> None:
    """Checkpoint and startup recovery admit exactly the same page-file namespace.

    A WRITE_PAGE is self-describing and checksum-valid even when its file name is wrong.  The
    recovery door refuses such a record before touching anything; checkpoint completion must not
    be a weaker replay door that writes an otherwise valid page over control or identity state and
    then publishes/recycles the commit that did it.
    """
    root = tmp_path / "db"
    stack = build_stack(root, wal_factory=_real_wal, owner_id="misroute-a")
    victim = "control/victim.page"
    stack.storage.create(victim)
    stack.storage.allocate(victim)
    before = make_page_image(
        stack.codec, [b"control-original"], page_index=0, page_lsn=0
    )
    forged = make_page_image(
        stack.codec, [b"wal-misroute"], page_index=0, page_lsn=100
    )
    stack.storage.write_page(victim, 0, before)

    committed = stack.wal.append_many(
        (
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                txn_id=77,
                payload=encode_page_write(victim, 0, forged),
            ),
            WalRecord(
                record_type=int(WalRecordType.COMMIT),
                txn_id=77,
                payload=CommitPayload(snapshot_lsn=0).encode(),
            ),
        )
    )
    stack.wal.barrier()
    stack.manager._publish(  # noqa: SLF001 - construct the cross-process published window
        CommitState(
            last_committed_lsn=committed,
            last_csn=committed,
            checkpoint_lsn=0,
        )
    )
    state_path = root / Path(COMMIT_STATE_FILE)
    state_before = state_path.read_bytes()
    wal_before = _wal_bytes(root)

    with pytest.raises(GrafxRecoveryRefused) as refused:
        stack.manager.checkpoint()

    assert refused.value.details["field"] == "file"
    assert stack.storage.read_page(victim, 0) == before
    assert state_path.read_bytes() == state_before
    assert _wal_bytes(root) == wal_before
    stack.manager.close()
