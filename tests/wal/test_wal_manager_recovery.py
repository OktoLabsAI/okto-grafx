"""Crash consistency, damage and repair (SPEC-M1 FR-5, FR-8 boundary, TR-4, AC-5, TS-4, TS-5).

The question every test here asks is the same one: interrupt the log between any write and the
state that describes it, and what does the next open see? The answers this component owes are
narrow, and each has its own section below.

* A batch is all or nothing, so no sequence number is ever written twice.
* A damaged tail is found, named, and closes the door to further appends.
* Bytes that are not a record never make a scan loop forever, whatever they are.
* The repair removes the newest segments first, so an interrupted repair leaves a log that is
  longer than asked rather than one with a hole in the middle.

The fault bench of FR-16 is what makes these ordinary tests instead of stories: it crashes at a
chosen call, stores only part of an append, and fills the device on demand, with the same seed
producing the same decisions every run.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

import pytest

from okto_grafx.adapters.storage_fault import (
    FaultInjectingStorageDevice,
    SimulatedCrash,
)
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.wal import (
    CHECKSUM_LENGTH,
    MAGIC_BYTES,
    WAL_FORMAT_VERSION,
    WAL_HEADER_LENGTH,
    WAL_MAGIC,
    WAL_V2_FLAG_REQUIRED,
    FailureReason,
    WalRecord,
    WalRecordType,
)
from okto_grafx.engine.wal_manager import (
    CHECKSUM_FAILURES_TOTAL,
    CHECKSUM_VERIFICATIONS_TOTAL,
    MAX_SEGMENT_READ_BYTES,
    MIN_SEGMENT_BYTES,
    WalManager,
)

from .conftest import (
    DESCRIPTOR,
    PAGE_SIZE,
    RecordingMetricsSink,
    make_record,
)


def _fill(manager: WalManager, count: int, *, epoch: int = 1) -> None:
    """Write a few ordinary records and make them durable."""
    for index in range(count):
        manager.append(make_record(index, epoch=epoch))
    manager.barrier()


def _rewrite(device: MemoryStorageDevice, name: str, data: bytes) -> None:
    """Replace the whole content of one segment, which is what damage looks like from outside."""
    device.truncate_log(name, 0)
    device.append_log(name, data)


def _bytes_of(device: MemoryStorageDevice, name: str) -> bytes:
    """Return everything one segment holds."""
    return device.read_log(name, 0, device.log_size(name))


def _lsns(manager: WalManager) -> list[int]:
    """Return the sequence numbers of every record the log reads back through the strict door."""
    return [record.lsn for record in manager.read_from(0)]


def _good_lsns(manager: WalManager) -> list[int]:
    """Return the sequence numbers of the good run, stopping where the tolerant scan reports."""
    found: list[int] = []
    for item in manager.scan_all():
        if item.failure is not None:
            break
        assert item.record is not None
        found.append(item.record.lsn)
    return found


# --- a batch is all or nothing --------------------------------------------------------------


def test_a_device_full_append_consumes_no_sequence_number(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A number consumed by a failed write is a number a retry writes twice."""
    device = FaultInjectingStorageDevice(memory_device, seed=11)
    manager = make_wal(device)
    _fill(manager, 3)
    name = manager.segments()[-1].name
    before_lsn = manager.last_lsn
    before_size = device.log_size(name)
    device.clear_trail()
    device.fill_device_on("append_log")
    with pytest.raises(GrafxDeviceFull) as caught:
        manager.append_many([make_record(90), make_record(91)])
    assert caught.value.retryable is True
    device.disarm()
    assert manager.last_lsn == before_lsn
    assert device.log_size(name) == before_size
    assert manager.damage is None


def test_a_partial_append_is_taken_back_before_the_failure_is_reported(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The fragment a short write leaves would sit in front of every later record."""
    device = FaultInjectingStorageDevice(memory_device, seed=5)
    manager = make_wal(device)
    _fill(manager, 3)
    name = manager.segments()[-1].name
    before_lsn = manager.last_lsn
    before_size = device.log_size(name)
    device.clear_trail()
    device.write_partially(20, method="append_log")
    with pytest.raises(GrafxDeviceFull):
        manager.append_many([make_record(90), make_record(91)])
    device.disarm()
    assert device.log_size(name) == before_size
    assert manager.last_lsn == before_lsn
    manager.append(make_record(92))
    assert manager.last_lsn == before_lsn + 1
    assert _lsns(manager) == list(range(1, before_lsn + 2))


def test_a_retry_after_a_failed_batch_writes_each_number_once(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The duplication this component must make impossible, asserted end to end."""
    device = FaultInjectingStorageDevice(memory_device, seed=23)
    manager = make_wal(device)
    _fill(manager, 2)
    device.clear_trail()
    device.write_partially(30, method="append_log")
    with pytest.raises(GrafxDeviceFull):
        manager.append_many(
            [make_record(1), make_record(2, record_type=WalRecordType.COMMIT)]
        )
    device.disarm()
    manager.append_many(
        [make_record(1), make_record(2, record_type=WalRecordType.COMMIT)]
    )
    manager.barrier()
    reopened = make_wal(memory_device)
    lsns = _lsns(reopened)
    assert lsns == sorted(set(lsns))
    assert lsns == list(range(1, len(lsns) + 1))
    assert reopened.damage is None


def test_a_failed_roll_leaves_no_segment_that_stops_recycling(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A segment born for a batch that never landed holds nothing and must not be registered."""
    device = FaultInjectingStorageDevice(memory_device, seed=31)
    manager = make_wal(device, segment_bytes=MIN_SEGMENT_BYTES)
    _fill(manager, 2)
    before = [segment.name for segment in manager.segments()]
    device.clear_trail()
    device.fill_device_on("append_log")
    with pytest.raises(GrafxDeviceFull):
        manager.append_many(
            [make_record(index, payload=bytes(120)) for index in range(3)]
        )
    device.disarm()
    assert [segment.name for segment in manager.segments()] == before
    # The index would look right either way: re-deriving the tail moves the segment number past
    # a leftover file, so only the DEVICE can say whether the empty segment was cleaned up, and
    # a roll that failed once per full device would otherwise leave one behind for good.
    assert memory_device.list_files("wal/") == tuple(before)
    manager.append(make_record(50))
    manager.barrier()
    reopened = make_wal(memory_device)
    assert reopened.damage is None
    assert _lsns(reopened) == list(range(1, reopened.last_lsn + 1))


def test_an_append_that_cannot_be_repaired_closes_the_door(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Writing after a fragment nobody could remove would bury every later record."""
    device = FaultInjectingStorageDevice(memory_device, seed=41)
    manager = make_wal(device)
    _fill(manager, 2)
    device.clear_trail()
    device.write_partially(24, method="append_log")
    truncations: list[str] = []

    def _refuse_truncation(file: str, size: int) -> None:
        truncations.append(file)
        raise GrafxCorruptionDetected(
            "The truncation could not complete.", reason="planted", file=file
        )

    original = memory_device.truncate_log
    memory_device.truncate_log = _refuse_truncation  # type: ignore[method-assign]
    try:
        with pytest.raises(GrafxDeviceFull):
            manager.append_many([make_record(1)])
    finally:
        memory_device.truncate_log = original  # type: ignore[method-assign]
    device.disarm()
    assert truncations, "the repair must have tried to remove the fragment"
    assert manager.damage is not None
    with pytest.raises(GrafxCorruptionDetected) as caught:
        manager.append(make_record(2))
    assert caught.value.details["reason"] == "truncated_tail"


# --- crashes ---------------------------------------------------------------------------------


@pytest.mark.parametrize("moment", ["before", "after"])
def test_a_crash_at_the_append_leaves_a_log_that_reads_back_contiguously(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice, moment: str
) -> None:
    """A crash is process death: whatever reached the disk has to be a valid prefix."""
    device = FaultInjectingStorageDevice(memory_device, seed=13)
    manager = make_wal(device, segment_bytes=512)
    _fill(manager, 4)
    device.clear_trail()
    device.crash_on("append_log", moment=moment)
    with pytest.raises(SimulatedCrash):
        manager.append_many(
            [
                make_record(1),
                make_record(2),
                make_record(3, record_type=WalRecordType.COMMIT),
            ]
        )
    reopened = make_wal(memory_device)
    assert reopened.damage is None
    lsns = _lsns(reopened)
    assert lsns == list(range(1, len(lsns) + 1))


@pytest.mark.parametrize("occurrence", [1, 2, 3])
def test_a_crash_at_each_write_point_of_a_roll_is_recoverable(
    make_wal: Callable[..., WalManager],
    tmp_path_factory: pytest.TempPathFactory,
    occurrence: int,
) -> None:
    """TS-4 in miniature: crash at each write of a segment roll and reopen every time."""
    device = FaultInjectingStorageDevice(
        MemoryStorageDevice(page_size=PAGE_SIZE), seed=17
    )
    manager = make_wal(device, segment_bytes=MIN_SEGMENT_BYTES)
    _fill(manager, 2)
    surviving = _lsns(manager)
    device.clear_trail()
    device.crash_on("append_log", occurrence=occurrence, moment="after")
    try:
        for index in range(6):
            manager.append(make_record(index, payload=bytes(80)))
    except SimulatedCrash:
        pass
    reopened = make_wal(device.inner)
    assert reopened.damage is None
    lsns = _lsns(reopened)
    assert lsns[: len(surviving)] == surviving
    assert lsns == list(range(1, len(lsns) + 1))


def test_a_lying_barrier_loses_only_whole_records(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A device that acknowledges a barrier it never took is FR-16's fourth signature."""
    device = FaultInjectingStorageDevice(memory_device, seed=29)
    manager = make_wal(device, segment_bytes=1024)
    _fill(manager, 2)
    honest = _lsns(manager)
    device.clear_trail()
    device.lie_on_barrier()
    for index in range(4):
        manager.append(make_record(index))
    manager.barrier()
    # Re-derive the tail on the commit path means append reads sizes too, so the occurrence
    # counter is restarted here: the crash must land on the call this test makes next.
    device.clear_trail()
    device.crash_on("log_size")
    with pytest.raises(SimulatedCrash):
        device.log_size(manager.segments()[-1].name)
    device.disarm()
    reopened = make_wal(memory_device)
    lsns = _lsns(reopened)
    assert lsns[: len(honest)] == honest
    assert lsns == list(range(1, len(lsns) + 1))
    assert reopened.damage is None


# --- damage found at open ---------------------------------------------------------------------


def test_a_torn_tail_is_found_and_named(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The record that was being written when the machine died is the ordinary case."""
    manager = make_wal(memory_device)
    _fill(manager, 4)
    name = manager.segments()[-1].name
    size = memory_device.log_size(name)
    memory_device.truncate_log(name, size - 12)
    reopened = make_wal(memory_device)
    assert reopened.damage is not None
    assert reopened.damage.reason is FailureReason.TRUNCATED_TAIL
    assert reopened.damage.segment == name
    assert reopened.damage.offset + reopened.damage.length == size - 12
    assert reopened.last_lsn == 4
    assert _good_lsns(reopened) == [1, 2, 3, 4]


def test_a_damaged_log_refuses_every_append(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Appending behind garbage would put good records where no scan can reach them."""
    manager = make_wal(memory_device)
    _fill(manager, 3)
    name = manager.segments()[-1].name
    memory_device.append_log(name, b"\x00" * 20)
    reopened = make_wal(memory_device)
    assert reopened.damage is not None
    with pytest.raises(GrafxCorruptionDetected) as caught:
        reopened.append(make_record(9))
    assert caught.value.code == "corruption_detected"
    assert caught.value.details["file"] == name


def test_the_strict_reader_raises_where_the_tolerant_one_reports(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A caller deciding something must never be handed a silently shortened stream."""
    manager = make_wal(memory_device)
    _fill(manager, 3)
    name = manager.segments()[-1].name
    memory_device.append_log(name, b"\x00" * 40)
    reopened = make_wal(memory_device)
    with pytest.raises(GrafxCorruptionDetected):
        list(reopened.read_from(0))
    items = list(reopened.scan_all())
    assert any(item.failure is not None for item in items)
    assert [item.record.lsn for item in items if item.record is not None] == [
        1,
        2,
        3,
        4,
    ]


def test_a_hole_inside_a_segment_keeps_the_records_after_it_readable(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """AC-5 and TS-5: the reference engine's signature is a page of zeros with good records after.

    The good run ends at the hole, and everything after it is reported as well, because the
    ledger classifies a record that decodes but sits after the truncation point as reapplicable
    and needs to see it (CONTRACT.md section 8.6, SD-4).
    """
    manager = make_wal(memory_device, segment_bytes=8192)
    _fill(manager, 8)
    name = manager.segments()[0].name
    data = _bytes_of(memory_device, name)
    hole_at = 200
    _rewrite(
        memory_device,
        name,
        data[:hole_at] + bytes(PAGE_SIZE) + data[hole_at + PAGE_SIZE :],
    )
    reopened = make_wal(memory_device)
    assert reopened.damage is not None
    assert reopened.damage.offset <= hole_at
    items = list(reopened.scan_all())
    failures = [item.failure for item in items if item.failure is not None]
    records = [item.record for item in items if item.record is not None]
    assert failures, "the hole must be reported"
    assert failures[0].length > 0
    assert records, "the records after the hole must still be reachable"
    assert records[-1].lsn == 9
    assert any(
        failure.reason is FailureReason.LSN_DISCONTINUITY for failure in failures
    )


def test_a_missing_segment_reads_as_records_lost_rather_than_as_nothing(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A hole between segments is exactly what recycling must never create."""
    manager = make_wal(memory_device, segment_bytes=512)
    for index in range(20):
        manager.append(make_record(index))
    manager.barrier()
    assert len(manager.segments()) >= 3
    memory_device.remove(manager.segments()[1].name)
    reopened = make_wal(memory_device)
    assert reopened.damage is not None
    assert reopened.damage.reason is FailureReason.LSN_DISCONTINUITY
    assert reopened.damage.expected_lsn == manager.segments()[0].last_lsn + 1


def test_a_record_from_a_later_build_is_a_version_question(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A11-revised: corruption sends an operator to quarantine and the answer here is upgrade."""
    manager = make_wal(memory_device)
    _fill(manager, 2)
    name = manager.segments()[-1].name
    data = bytearray(_bytes_of(memory_device, name))
    last_record_at = _offset_of_last_record(bytes(data))
    struct.pack_into("<H", data, last_record_at + 4, WAL_FORMAT_VERSION + 1)
    body = bytes(data[: len(data) - 4])
    data[-4:] = struct.pack("<I", crc32c(body))
    _rewrite(memory_device, name, bytes(data))
    reopened = make_wal(memory_device)
    assert reopened.damage is not None
    assert reopened.damage.reason is FailureReason.UNSUPPORTED_VERSION
    with pytest.raises(GrafxSchemaVersionMismatch):
        list(reopened.read_from(0))


@pytest.mark.parametrize("door", ["read_from", "append", "recycle"])
def test_an_unknown_required_v2_record_refuses_without_changing_the_log(
    door: str,
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
) -> None:
    """Required-but-unknown semantics are an upgrade refusal through every mutating door."""

    manager = make_wal(memory_device, segment_bytes=512)
    _fill(manager, 20)
    assert len(manager.segments()) >= 2
    name = manager.segments()[-1].name
    data = bytearray(_bytes_of(memory_device, name))
    last_record_at = _offset_of_last_record(bytes(data))
    struct.pack_into("<H", data, last_record_at + 4, WAL_FORMAT_VERSION)
    struct.pack_into("<H", data, last_record_at + 6, 250)
    struct.pack_into("<H", data, last_record_at + 10, WAL_V2_FLAG_REQUIRED)
    record_length = struct.unpack_from("<I", data, last_record_at + 12)[0]
    record_end = last_record_at + record_length
    body_end = record_end - CHECKSUM_LENGTH
    data[body_end:record_end] = struct.pack(
        "<I", crc32c(bytes(data[last_record_at:body_end]))
    )
    _rewrite(memory_device, name, bytes(data))

    reopened = make_wal(memory_device, segment_bytes=512)
    assert reopened.damage is not None
    assert reopened.damage.reason is FailureReason.UNSUPPORTED_REQUIRED_RECORD
    before = {
        file: _bytes_of(memory_device, file)
        for file in memory_device.list_files("wal/")
    }

    with pytest.raises(GrafxSchemaVersionMismatch) as raised:
        if door == "read_from":
            list(reopened.read_from(0))
        elif door == "append":
            reopened.append(make_record(999))
        else:
            reopened.recycle(reopened.last_lsn + 1_000)

    assert raised.value.details["reason"] == "unsupported_required_record"
    assert {
        file: _bytes_of(memory_device, file)
        for file in memory_device.list_files("wal/")
    } == before


def _offset_of_last_record(data: bytes) -> int:
    """Return where the last record of a segment starts, by walking its declared lengths."""
    offset = 0
    previous = 0
    while offset < len(data):
        total = struct.unpack_from("<I", data, offset + 12)[0]
        previous = offset
        offset += total
    return previous


def test_a_decode_that_never_reached_a_checksum_is_not_counted_as_one(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    metrics: RecordingMetricsSink,
) -> None:
    """The metric counts checksums, not decode attempts, and the difference is observable.

    A stretch of bytes refused on its magic never had a checksum taken over it. Counting one
    there would make ``oktografx_checksum_verifications_total`` a count of something else under
    a name that promises this.
    """
    manager = make_wal(memory_device)
    _fill(manager, 3)
    name = manager.segments()[-1].name
    memory_device.append_log(name, b"\x00" * 64)
    metrics.counters.clear()
    reopened = make_wal(memory_device)
    assert reopened.damage is not None
    assert reopened.damage.reason is FailureReason.BAD_MAGIC
    assert metrics.counter(CHECKSUM_VERIFICATIONS_TOTAL, kind="record") == 4.0


def test_a_resynchronisation_proves_the_candidate_it_lands_on(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """After damage the scan looks for the next record and PROVES it with its checksum.

    The payload here holds a byte pattern that looks exactly like a record header and adds up
    exactly like one -- only its checksum is wrong. A scan that trusted the pattern would
    resynchronise into the middle of a payload and report a second, invented failure. Asserting
    that exactly one failure is reported is what tells the two apart.
    """
    decoy = struct.pack(
        "<IHHHHIQQQII", 0x5852474F, 1, 2, 48, 0, 52, 0, 0, 0, 0, 0
    ) + bytes(4)
    manager = make_wal(memory_device, segment_bytes=16384)
    manager.append(make_record(1))
    manager.append(make_record(2, payload=decoy))
    manager.append(make_record(3))
    manager.barrier()
    name = manager.segments()[0].name
    data = bytearray(_bytes_of(memory_device, name))
    wrecked = _record_offsets(bytes(data))[2]
    data[wrecked : wrecked + 4] = bytes(4)
    _rewrite(memory_device, name, bytes(data))
    reopened = make_wal(memory_device)
    items = list(reopened.scan_all())
    failures = [item.failure for item in items if item.failure is not None]
    records = [item.record for item in items if item.record is not None]
    # One hole, then the gap it leaves in the numbering. A scan that trusted the decoy would
    # resynchronise inside the payload and report a checksum failure between the two.
    assert [failure.reason for failure in failures] == [
        FailureReason.BAD_MAGIC,
        FailureReason.LSN_DISCONTINUITY,
    ]
    assert [record.lsn for record in records] == [1, 2, 4]
    assert failures[0].offset + failures[0].length == _record_offsets(bytes(data))[3]


def _record_offsets(data: bytes) -> list[int]:
    """Return where every record of a segment starts, by walking its declared lengths."""
    offsets: list[int] = []
    offset = 0
    while offset < len(data):
        offsets.append(offset)
        offset += struct.unpack_from("<I", data, offset + 12)[0]
    return offsets


def test_a_checksum_failure_is_counted_under_the_record_kind(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    metrics: RecordingMetricsSink,
) -> None:
    """OR-3 asks for checksum failures by kind, and a scan is where a record one is seen."""
    manager = make_wal(memory_device)
    _fill(manager, 3)
    name = manager.segments()[-1].name
    data = bytearray(_bytes_of(memory_device, name))
    data[_offset_of_last_record(bytes(data)) + 60] ^= 0xFF
    _rewrite(memory_device, name, bytes(data))
    reopened = make_wal(memory_device)
    assert reopened.damage is not None
    assert metrics.counter(CHECKSUM_FAILURES_TOTAL, kind="record") >= 1.0


# --- a scan always terminates ------------------------------------------------------------------


@pytest.mark.parametrize(
    "planted",
    [
        MAGIC_BYTES * 2048,
        bytes(4096),
        b"\xff" * 4096,
        bytes(range(256)) * 16,
    ],
    ids=["only-magic", "only-zeros", "only-ones", "every-byte"],
)
def test_a_segment_of_rubbish_terminates_and_reports(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    planted: bytes,
) -> None:
    """A88: a hang is the loudest failure, so a scan over anything at all must stop."""
    manager = make_wal(memory_device)
    _fill(manager, 2)
    name = manager.segments()[-1].name
    _rewrite(memory_device, name, planted)
    reopened = make_wal(memory_device)
    items = list(reopened.scan_all())
    assert items, "a scan of rubbish still has to report something"
    assert all((item.record is None) != (item.failure is None) for item in items), (
        "every item carries exactly one of a record and a failure"
    )
    assert reopened.damage is not None


def test_a_scan_never_revisits_an_offset(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Every step of the walk moves forward, which is what makes the loop bounded."""
    manager = make_wal(memory_device, segment_bytes=8192)
    _fill(manager, 6)
    name = manager.segments()[0].name
    data = _bytes_of(memory_device, name)
    _rewrite(memory_device, name, data[:150] + MAGIC_BYTES * 30 + data[150 + 120 :])
    reopened = make_wal(memory_device)
    positions = [
        (segment_index(reopened, item.segment), item.offset)
        for item in reopened.scan_all()
    ]
    assert positions == sorted(positions)
    failures = [
        position
        for position, item in zip(positions, reopened.scan_all())
        if item.failure
    ]
    assert len(set(positions)) >= len(failures)


def segment_index(manager: WalManager, name: str) -> int:
    """Return where a segment sits in the log, so two offsets can be compared across files."""
    return [segment.name for segment in manager.segments()].index(name)


def test_a_segment_too_large_to_be_one_is_refused_and_still_repairable(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Reading a file of any size into memory is how a scan becomes a memory incident.

    The refusal is damage, not an escape: the log still opens, so recovery can reach it. This
    drives the whole repair, because a scan that RAISES has to end the good run exactly as a
    scan that reports one does -- otherwise the repair sits behind the damage it exists to
    repair, which is the dead end this component must not have.
    """

    class OversizedSegmentDevice:
        """A real device that claims one of its segments is larger than a segment can be."""

        def __init__(self, inner: MemoryStorageDevice, name: str) -> None:
            """Wrap a device and choose the segment that will lie about its size."""
            self._inner = inner
            self._name = name

        def __getattr__(self, attribute: str) -> object:
            """Forward everything this wrapper does not override."""
            return getattr(self._inner, attribute)

        def log_size(self, file: str) -> int:
            """Answer honestly, except for the one segment under test."""
            if file == self._name:
                return MAX_SEGMENT_READ_BYTES + 1
            return self._inner.log_size(file)

    manager = make_wal(memory_device, segment_bytes=512)
    for index in range(16):
        manager.append(make_record(index))
    manager.barrier()
    assert len(manager.segments()) >= 3
    oversized = manager.segments()[-1]
    surviving = manager.segments()[-2]

    device = OversizedSegmentDevice(memory_device, oversized.name)
    reopened = make_wal(device, segment_bytes=512)

    assert reopened.damage is not None
    assert reopened.damage.reason is FailureReason.UNREADABLE_SEGMENT
    assert reopened.damage.segment == oversized.name
    assert "oversized_segment" in reopened.damage.detail
    assert isinstance(reopened.damage.as_error(), GrafxCorruptionDetected)
    with pytest.raises(GrafxCorruptionDetected):
        reopened.append(make_record(1))

    report = reopened.truncate_after(surviving.last_lsn)

    assert report.completed is True
    assert oversized.name in report.removed_segments
    assert memory_device.exists(oversized.name) is False
    healthy = make_wal(memory_device, segment_bytes=512)
    assert healthy.damage is None
    assert healthy.last_lsn == surviving.last_lsn
    assert _lsns(healthy) == list(range(1, surviving.last_lsn + 1))


def _planted_record(*, descriptor: bytes, lsn: int, epoch: int = 1) -> bytes:
    """Return a checksum-valid record built without the write side's limits.

    A record the WRITE side refuses can still be sitting on a disk, put there by another build
    or by a configuration this one no longer allows. Building it by hand is the only way to
    plant one.
    """
    total = WAL_HEADER_LENGTH + len(descriptor) + CHECKSUM_LENGTH
    body = (
        struct.pack(
            "<IHHHHIQQQII",
            WAL_MAGIC,
            1,
            int(WalRecordType.WRITE_PAGE),
            WAL_HEADER_LENGTH,
            0,
            total,
            lsn,
            epoch,
            1,
            len(descriptor),
            0,
        )
        + descriptor
    )
    return body + struct.pack("<I", crc32c(body))


def test_a_record_this_build_cannot_hold_is_stepped_over_and_the_rest_still_reads(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The checksum proved the length, so the scan steps over the record and carries on.

    This is the manager-side destination of the decoder's refusal, and it is a different claim
    from the decoder's own: that a record this build cannot hold does not end the walk, so the
    ledger still sees what came after it. The planted record sits in the MIDDLE of a segment
    for exactly that reason.
    """
    manager = make_wal(memory_device)
    _fill(manager, 2)
    name = manager.segments()[-1].name
    held = _bytes_of(memory_device, name)
    planted = _planted_record(descriptor=b"d" * 6000, lsn=manager.last_lsn + 1)
    follower = WalRecord(
        record_type=WalRecordType.WRITE_PAGE,
        payload=bytes(16),
        descriptor=DESCRIPTOR,
        lsn=manager.last_lsn + 2,
        epoch=1,
    ).encode()
    _rewrite(memory_device, name, held + planted + follower)

    reopened = make_wal(memory_device)
    items = list(reopened.scan_all())

    failures = [item.failure for item in items if item.failure is not None]
    records = [item.record for item in items if item.record is not None]
    assert failures[0].reason is FailureReason.UNREPRESENTABLE_RECORD
    assert failures[0].length == len(planted), (
        "the step must be the record's own length"
    )
    assert [record.lsn for record in records] == [1, 2, 3, 5]
    assert reopened.damage is not None
    report = reopened.truncate_after(3)
    assert report.completed is True
    assert reopened.damage is None
    assert reopened.append(make_record(1)) == 4


# --- repair --------------------------------------------------------------------------------


def test_truncating_after_the_last_good_record_clears_the_damage(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """This is recovery's door, and passing through it is what reopens the log for writing."""
    manager = make_wal(memory_device)
    _fill(manager, 4)
    name = manager.segments()[-1].name
    memory_device.append_log(name, b"\x00" * 30)
    reopened = make_wal(memory_device)
    report = reopened.truncate_after(reopened.last_lsn)
    assert report.completed is True
    assert report.removed_bytes == 30
    # A cut never rewrites a segment in place: the records it keeps are rolled into a NEW
    # segment and the old name is released, which is what makes another participant's change
    # token see the repair at all.
    assert report.truncated_segment is not None
    assert report.truncated_segment != name
    assert name in report.removed_segments
    assert memory_device.exists(name) is False
    assert memory_device.exists(report.truncated_segment) is True
    assert reopened.damage is None
    assert reopened.append(make_record(1)) == 6
    assert _lsns(reopened) == [1, 2, 3, 4, 5, 6]


def test_truncating_removes_the_newest_segments_first(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """An interrupted repair must leave a longer valid log, never a log with a hole."""
    manager = make_wal(memory_device, segment_bytes=512)
    for index in range(20):
        manager.append(make_record(index))
    manager.barrier()
    order: list[str] = []
    original = memory_device.recycle

    def _watch(file: str) -> bool:
        order.append(file)
        return original(file)

    memory_device.recycle = _watch  # type: ignore[method-assign]
    try:
        target = manager.segments()[0].last_lsn
        report = manager.truncate_after(target)
    finally:
        memory_device.recycle = original  # type: ignore[method-assign]
    assert order == sorted(order, reverse=True)
    assert report.completed is True
    assert manager.last_lsn == target
    assert _lsns(manager) == list(range(1, target + 1))


def test_truncating_to_nothing_empties_the_log_and_it_still_reopens(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The worst recovery outcome is an empty log, and an empty log must still work."""
    manager = make_wal(memory_device, segment_bytes=512)
    for index in range(12):
        manager.append(make_record(index))
    manager.barrier()
    used = {segment.name for segment in manager.segments()}
    report = manager.truncate_after(0)
    assert report.completed is True
    assert manager.segments() == ()
    assert manager.last_lsn == 0
    assert manager.total_bytes() == 0
    # The log is empty, but the DIRECTORY is not: what is left is the burned number, an empty
    # file that is no part of the log and holds no record. It is there because a directory with
    # nothing in it numbers its next segment from the beginning, and re-issuing a name this log
    # has already handed out is the one thing segment.py says must never happen. Asserting the
    # directory empty here is what let that reset be shipped as correct.
    left = memory_device.list_files("wal/")
    assert len(left) == 1
    assert set(left).isdisjoint(used)
    assert memory_device.log_size(left[0]) == 0
    assert manager.append(make_record(1)) == 2
    assert set(segment.name for segment in manager.segments()).isdisjoint(
        used | set(left)
    )
    reopened = make_wal(memory_device)
    assert reopened.damage is None
    assert _lsns(reopened) == [1, 2]


def test_truncating_twice_changes_nothing_the_second_time(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Recovery is re-run after a crash, so its repair has to be idempotent."""
    manager = make_wal(memory_device, segment_bytes=512)
    for index in range(12):
        manager.append(make_record(index))
    manager.barrier()
    target = manager.segments()[0].last_lsn
    first = manager.truncate_after(target)
    second = manager.truncate_after(target)
    assert first.completed and second.completed
    assert second.removed_bytes == 0
    assert second.removed_segments == ()
    assert manager.last_lsn == target


def test_truncating_above_the_end_of_the_log_does_nothing(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A sequence number beyond the log describes no record, so nothing is removed."""
    manager = make_wal(memory_device)
    _fill(manager, 3)
    report = manager.truncate_after(manager.last_lsn + 100)
    assert report.removed_bytes == 0
    assert report.removed_records == 0
    assert manager.last_lsn == 4


@pytest.mark.parametrize("keep", [1, 4, 8, 11])
def test_removed_records_is_the_drop_the_log_actually_took(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice, keep: int
) -> None:
    """The number the operator reads must be what went, on the cut that ROLLS a segment.

    Counting from a position in the log cannot answer this, because the cut segment is rolled
    into a new one and released -- afterwards its name resolves to nothing and every surviving
    record reads as still-to-go, which is how this number reached the FR-8 finding as a negative.
    The no-op path was the only shape asserted, and it is the one shape where no roll happens.
    """
    manager = make_wal(memory_device, segment_bytes=512)
    _fill(manager, 12)
    before = _lsns(manager)
    report = manager.truncate_after(before[keep - 1])
    after = _lsns(manager)
    assert report.completed is True
    assert len(after) == keep
    assert report.removed_records == len(before) - len(after)
    assert report.removed_records > 0


def test_a_repair_that_cannot_release_a_segment_leaves_the_log_longer(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Shortening below a segment that survived would be the hole this rule exists to prevent."""
    manager = make_wal(memory_device, segment_bytes=512)
    for index in range(16):
        manager.append(make_record(index))
    manager.barrier()
    # The cut falls INSIDE the first segment, not at its edge, so the shortening this rule
    # forbids is a shortening that would really happen (A72: reach the branch you are naming).
    target = manager.segments()[0].first_lsn
    assert target < manager.segments()[0].last_lsn
    original = memory_device.recycle
    memory_device.recycle = lambda file: False  # type: ignore[method-assign,assignment]
    try:
        report = manager.truncate_after(target)
    finally:
        memory_device.recycle = original  # type: ignore[method-assign]
    assert report.completed is False
    assert report.truncated_segment is None
    assert report.deferred_segments
    reopened = make_wal(memory_device)
    assert reopened.damage is None
    assert _lsns(reopened) == list(range(1, reopened.last_lsn + 1))
    assert reopened.last_lsn > target


def test_a_rolled_cut_is_incomplete_while_the_old_segment_name_still_exists(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A Windows-held old name must not turn two copies of one LSN range into success."""
    manager = make_wal(memory_device)
    _fill(manager, 6)
    assert len(manager.segments()) == 1
    old = manager.segments()[0].name
    old_bytes = _bytes_of(memory_device, old)
    before_last = manager.last_lsn
    target = before_last - 2
    original = memory_device.recycle

    def _keep_the_old_name(file: str) -> bool:
        if file == old:
            return False
        return original(file)

    memory_device.recycle = _keep_the_old_name  # type: ignore[method-assign]
    try:
        report = manager.truncate_after(target)
    finally:
        memory_device.recycle = original  # type: ignore[method-assign]

    replacement = report.truncated_segment
    assert replacement is not None and replacement != old
    assert report.completed is False
    assert report.deferred_segments == (old,)
    assert old not in report.removed_segments
    assert memory_device.exists(old) is True
    assert memory_device.exists(replacement) is True
    assert _bytes_of(memory_device, old) == old_bytes
    assert report.last_lsn == before_last > target
    assert manager.damage is not None
    with pytest.raises(GrafxCorruptionDetected):
        list(manager.read_from(0))


def test_a_truncation_is_made_durable_before_it_is_reported(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A repair a crash can undo is not a repair, so the barrier happens inside the call."""
    flushed: list[str | None] = []
    original = memory_device.durable_barrier

    def _watch(file: str | None = None) -> None:
        flushed.append(file)
        original(file)

    manager = make_wal(memory_device)
    _fill(manager, 3)
    name = manager.segments()[-1].name
    memory_device.append_log(name, b"\x00" * 16)
    reopened = make_wal(memory_device)
    memory_device.durable_barrier = _watch  # type: ignore[method-assign]
    try:
        report = reopened.truncate_after(reopened.last_lsn)
    finally:
        memory_device.durable_barrier = original  # type: ignore[method-assign]
    # The segment that now carries the records the cut KEPT is the one that has to be durable
    # before the old one is released, or a crash in that window loses them.
    assert report.truncated_segment in flushed


def test_the_main_data_file_is_never_touched_by_a_repair(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """G6 and BR-1: no sanctioned operation destroys the main data file."""
    memory_device.create("heap.dat")
    memory_device.allocate("heap.dat", 2)
    memory_device.write_page("heap.dat", 0, bytes(range(256)) * 2)
    before = memory_device.read_page("heap.dat", 0)
    manager = make_wal(memory_device, segment_bytes=512)
    for index in range(12):
        manager.append(make_record(index))
    manager.barrier()
    name = manager.segments()[-1].name
    memory_device.append_log(name, b"\x00" * 24)
    reopened = make_wal(memory_device)
    reopened.truncate_after(reopened.last_lsn)
    reopened.recycle(reopened.last_lsn)
    assert memory_device.exists("heap.dat")
    assert memory_device.read_page("heap.dat", 0) == before


@pytest.mark.parametrize("value", [-1, True, "3", 1.5, None])
@pytest.mark.parametrize("door", ["read_from", "truncate_after", "recycle"])
def test_a_sequence_number_that_is_not_one_is_refused_by_every_door(
    wal: WalManager, door: str, value: object
) -> None:
    """A bool would mean one, which is a horizon and a cut point that quietly does damage."""
    with pytest.raises(GrafxConfigurationError) as caught:
        getattr(wal, door)(value)
    assert caught.value.details["field"] in {"lsn", "horizon_lsn"}


def test_a_record_written_by_this_build_is_read_back_identically(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The end-to-end round trip TR-4 asks for, through the device rather than in memory."""
    manager = make_wal(memory_device)
    written = WalRecord(
        record_type=WalRecordType.COMMIT,
        payload=bytes(range(200)),
        descriptor="hash-v1;partitions_per_table=128",
        epoch=6,
        txn_id=4242,
        flags=9,
    )
    manager.append(written)
    manager.barrier()
    reopened = make_wal(memory_device)
    read_back = [
        record
        for record in reopened.read_from(0)
        if record.record_type == WalRecordType.COMMIT
    ]
    assert len(read_back) == 1
    assert read_back[0] == written.with_lsn(2)
