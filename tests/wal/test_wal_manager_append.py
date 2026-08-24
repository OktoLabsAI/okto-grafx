"""Appending, rolling and the durability barrier (SPEC-M1 FR-5, FR-6, BR-4, BR-7, TR-4).

FR-5 is the shape of this file: a record carries its length, a monotonic sequence number, a
CRC-32C and the epoch of its writer, and nothing is durable until the barrier returns. BR-4 is the
sentence after it -- no acknowledgement before the barrier succeeds -- which for this component
means the barrier must really call the device, must count a failure, and must not forget what is
still unflushed when it fails.

BR-7 arrives here as well: a writer holding an older epoch is refused before a byte reaches the
device, and the test asserts the size of the file rather than the exception alone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from okto_grafx.engine import wal_manager as wal_module

from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDurabilityBarrierFailed,
    GrafxPortNotConfigured,
    GrafxStaleEpoch,
    GrafxTransactionStateError,
)
from okto_grafx.domain.wal import (
    FailureReason,
    WalRecord,
    WalRecordType,
    segment_name,
)
from okto_grafx.engine.wal_manager import (
    BARRIER_FAILURES_TOTAL,
    MAX_SEGMENT_READ_BYTES,
    CHECKSUM_VERIFICATIONS_TOTAL,
    FSYNC_DURATION_SECONDS,
    MIN_SEGMENT_BYTES,
    WAL_METRICS,
    WAL_SEGMENTS,
    WAL_SIZE_BYTES,
    SegmentHeader,
    WalManager,
)

from .conftest import DESCRIPTOR, FrozenClock, RecordingMetricsSink, make_record


class BarrierRefusingDevice:
    """A device that behaves exactly like the in-memory one until it is told to refuse a flush."""

    def __init__(self, inner: MemoryStorageDevice) -> None:
        """Wrap a real device and start out honest."""
        self._inner = inner
        self.refuse = False
        self.flushed: list[str | None] = []

    def __getattr__(self, name: str) -> Any:
        """Forward everything this wrapper does not override."""
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        """Return the label of the wrapped device."""
        return self._inner.name

    @property
    def page_size(self) -> int:
        """Return the page size of the wrapped device."""
        return self._inner.page_size

    def durable_barrier(self, file: str | None = None) -> None:
        """Flush, or refuse the way the storage port says a failing barrier refuses."""
        self.flushed.append(file)
        if self.refuse:
            raise GrafxDurabilityBarrierFailed(
                f"The durability barrier of {file!r} did not complete.",
                reason="fsync_failed",
                retryable=True,
                file=file,
            )
        self._inner.durable_barrier(file)


class PostWriteFailingDevice:
    """Raise a foreign exception only after the wrapped append stored every byte."""

    def __init__(
        self,
        inner: MemoryStorageDevice,
        failure: BaseException,
        *,
        rollback_failure: BaseException | None = None,
    ) -> None:
        self._inner = inner
        self.failure: BaseException | None = failure
        self.rollback_failure = rollback_failure

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
        failure = self.failure
        self.failure = None
        if failure is not None:
            raise failure
        return offset

    def truncate_log(self, file: str, size: int) -> None:
        if self.rollback_failure is not None:
            raise self.rollback_failure
        self._inner.truncate_log(file, size)


class CallRecordingDevice:
    """A device that remembers every port call it served, with the argument that mattered."""

    def __init__(self, inner: MemoryStorageDevice) -> None:
        """Wrap a real device and start with an empty record."""
        self._inner = inner
        self.calls: list[tuple[str, object]] = []

    def __getattr__(self, name: str) -> Any:
        """Forward every call, remembering the name it carried."""
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def _remember(*arguments: Any, **keywords: Any) -> Any:
            self.calls.append((name, arguments[0] if arguments else None))
            return attribute(*arguments, **keywords)

        return _remember

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Record how MANY bytes were read, which is the cost this suite watches."""
        self.calls.append(("read_log", length))
        return self._inner.read_log(file, offset, length)


# --- opening -------------------------------------------------------------------------------


def test_an_empty_directory_opens_with_no_records(wal: WalManager) -> None:
    """A fresh database has no log, and that is not a failure of any kind."""
    assert wal.last_lsn == 0
    assert wal.segments() == ()
    assert wal.total_bytes() == 0
    assert wal.damage is None
    assert list(wal.read_from(0)) == []
    assert list(wal.scan_all()) == []


def test_every_door_refuses_before_open(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Discovery is explicit, so a manager that skipped it must not guess at the state."""
    manager = make_wal(memory_device, open_now=False)
    for call in (
        lambda: manager.append(make_record()),
        lambda: manager.barrier(),
        lambda: manager.read_from(0),
        lambda: manager.scan_all(),
        lambda: manager.truncate_after(0),
        lambda: manager.recycle(0),
    ):
        with pytest.raises(GrafxTransactionStateError) as caught:
            call()
        assert caught.value.details["state"] == "closed"


# --- appending -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("post-write runtime failure"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_a_foreign_escape_after_the_physical_append_rolls_the_whole_batch_back(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    failure: BaseException,
) -> None:
    device = PostWriteFailingDevice(memory_device, failure)
    manager = make_wal(device)

    with pytest.raises(type(failure)) as escaped:
        manager.append_many((make_record(1), make_record(2)))

    assert escaped.value is failure
    assert manager.last_lsn == 0
    assert manager.damage is None
    assert memory_device.list_files("wal/") == ()
    assert manager.append(make_record(3)) == 2


def test_failed_rollback_marks_the_wal_damaged_without_replacing_the_append_failure(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    primary = RuntimeError("post-write append failure")
    rollback = KeyboardInterrupt()
    manager = make_wal(
        PostWriteFailingDevice(
            memory_device,
            primary,
            rollback_failure=rollback,
        )
    )

    with pytest.raises(RuntimeError) as escaped:
        manager.append(make_record())

    assert escaped.value is primary
    # The surviving batch is complete and decodes cleanly, so scan damage alone cannot remember
    # that the caller observed a failed outcome. The independent uncertainty latch must.
    assert manager.damage is None
    assert manager.append_uncertain is True
    with pytest.raises(GrafxCorruptionDetected) as refused:
        manager.append(make_record(2))
    assert refused.value.details["reason"] == "append_uncertain"


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("registration failed"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_a_foreign_escape_after_registration_restores_bytes_and_memory_exactly(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    manager = make_wal(memory_device)
    original = WalManager._register_append

    def register_then_fail(wal: WalManager, *args: Any, **kwargs: Any) -> None:
        original(wal, *args, **kwargs)
        raise failure

    monkeypatch.setattr(WalManager, "_register_append", register_then_fail)

    with pytest.raises(type(failure)) as escaped:
        manager.append_many((make_record(1), make_record(2)))

    assert escaped.value is failure
    assert manager.last_lsn == 0
    assert manager.segments() == ()
    assert manager.total_bytes() == 0
    assert manager.append_uncertain is False
    assert memory_device.list_files("wal/") == ()


def test_metrics_failure_after_registration_cannot_make_a_successful_append_ambiguous(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    metrics: RecordingMetricsSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_wal(memory_device)
    # Finish the deferred read-only index pass before arming the sink, so the injected escape is
    # specifically the post-append publication inside _register_append.
    assert manager.last_lsn == 0

    def fail_gauge(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metrics sink failed")

    monkeypatch.setattr(metrics, "set_gauge", fail_gauge)

    committed = manager.append(make_record(1))

    assert committed == manager.last_lsn == 2
    assert manager.append_uncertain is False


def test_planning_includes_the_segment_header_and_append_revalidates_it(
    wal: WalManager,
) -> None:
    records = (make_record(1), make_record(2, record_type=WalRecordType.COMMIT))

    planned = wal.planned_terminal_lsn(records)

    assert planned == 3  # SEGMENT_HEADER + two caller records
    assert wal.append_many(records, expected_terminal_lsn=planned) == planned


def test_tail_drift_after_planning_is_refused_before_this_writer_adds_a_byte(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    first = make_wal(memory_device)
    records = (make_record(1), make_record(2, record_type=WalRecordType.COMMIT))
    planned = first.planned_terminal_lsn(records)
    second = make_wal(memory_device)
    second.append(make_record(9))
    sizes_before = {
        name: memory_device.log_size(name) for name in memory_device.list_files("wal/")
    }

    with pytest.raises(GrafxTransactionStateError) as refused:
        first.append_many(records, expected_terminal_lsn=planned)

    assert refused.value.details["field"] == "expected_terminal_lsn"
    assert {
        name: memory_device.log_size(name) for name in memory_device.list_files("wal/")
    } == sizes_before


def test_sequence_numbers_start_at_one_and_never_skip(wal: WalManager) -> None:
    """Contiguity is the property every later rule rests on, so it is asserted first."""
    assigned = [wal.append(make_record(index)) for index in range(5)]
    assert assigned == [2, 3, 4, 5, 6]
    assert wal.last_lsn == 6
    lsns = [record.lsn for record in wal.read_from(0)]
    assert lsns == [1, 2, 3, 4, 5, 6]


def test_a_configured_segment_can_never_exceed_the_reader_ceiling(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wal_module, "MAX_SEGMENT_READ_BYTES", MIN_SEGMENT_BYTES)
    with pytest.raises(GrafxConfigurationError) as raised:
        make_wal(memory_device, segment_bytes=MIN_SEGMENT_BYTES + 1)
    assert raised.value.details["field"] == "segment_bytes"
    assert memory_device.list_files("wal/") == ()


def test_an_oversized_batch_is_refused_before_it_creates_an_unreadable_segment(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wal_module, "MAX_SEGMENT_READ_BYTES", MIN_SEGMENT_BYTES)
    manager = make_wal(memory_device, segment_bytes=MIN_SEGMENT_BYTES)

    with pytest.raises(GrafxConfigurationError) as raised:
        manager.append(make_record(payload=bytes(MIN_SEGMENT_BYTES)))

    assert raised.value.details["field"] == "records"
    assert raised.value.details["projected_segment_bytes"] > MIN_SEGMENT_BYTES
    assert manager.last_lsn == 0
    assert manager.total_bytes() == 0
    assert memory_device.list_files("wal/") == ()


def test_a_record_subclass_cannot_lie_about_the_segment_size(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LyingRecord(WalRecord):
        def encoded_length(self) -> int:
            return 1

    monkeypatch.setattr(wal_module, "MAX_SEGMENT_READ_BYTES", MIN_SEGMENT_BYTES)
    manager = make_wal(memory_device, segment_bytes=MIN_SEGMENT_BYTES)
    record = LyingRecord(
        record_type=WalRecordType.WRITE_PAGE,
        payload=bytes(220),
        epoch=1,
        txn_id=1,
    )

    with pytest.raises(GrafxConfigurationError) as raised:
        manager.append(record)

    assert raised.value.details["field"] == "records"
    assert manager.last_lsn == 0
    assert manager.total_bytes() == 0
    assert memory_device.list_files("wal/") == ()


def test_a_tampered_record_with_an_unreadable_payload_fails_typed_before_write(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
) -> None:
    manager = make_wal(memory_device)
    record = make_record()
    released = memoryview(b"payload")
    released.release()
    object.__setattr__(record, "payload", released)

    with pytest.raises(GrafxConfigurationError) as raised:
        manager.append(record)

    assert raised.value.details["field"] == "payload"
    assert raised.value.details["position"] == 0
    assert isinstance(raised.value.__cause__, ValueError)
    assert manager.last_lsn == 0
    assert manager.total_bytes() == 0
    assert memory_device.list_files("wal/") == ()


def test_the_first_record_of_a_segment_describes_the_segment(wal: WalManager) -> None:
    """A segment that says which one it is and where the previous one ended is self-describing."""
    wal.append(make_record())
    first = next(iter(wal.read_from(0)))
    assert first.record_type == WalRecordType.SEGMENT_HEADER
    assert first.lsn == 1
    header = SegmentHeader.decode(first.payload)
    assert header.number == 1
    assert header.previous_last_lsn == 0
    assert header.created_at_wall == 1_700_000_000.0


def test_a_second_segment_names_where_the_first_one_ended(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The header is what tells a scan "a new run starts here" rather than "records are gone"."""
    manager = make_wal(memory_device, segment_bytes=MIN_SEGMENT_BYTES)
    for index in range(6):
        manager.append(make_record(index))
    headers = [
        SegmentHeader.decode(record.payload)
        for record in manager.read_from(0)
        if record.record_type == WalRecordType.SEGMENT_HEADER
    ]
    assert len(headers) >= 2
    assert [header.number for header in headers] == list(range(1, len(headers) + 1))
    assert headers[0].previous_last_lsn == 0
    assert all(header.previous_last_lsn > 0 for header in headers[1:])


def test_a_record_without_a_descriptor_is_stamped_with_the_log_one(
    wal: WalManager,
) -> None:
    """TR-4 puts the granularity in the record, so a record read alone still describes itself."""
    wal.append(make_record(descriptor=""))
    assert all(record.descriptor == DESCRIPTOR for record in wal.read_from(0))


def test_a_record_that_carries_its_own_descriptor_keeps_it(wal: WalManager) -> None:
    """An index writes its own granularity, and the log must not overwrite what it was told."""
    wal.append(make_record(descriptor="hash-v1;buckets=8"))
    written = [
        record
        for record in wal.read_from(0)
        if record.record_type != WalRecordType.SEGMENT_HEADER
    ]
    assert [record.descriptor for record in written] == ["hash-v1;buckets=8"]


def test_changing_the_granularity_changes_only_the_descriptor(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """CONTRACT.md section 6.5: no format version moves and no migration runs."""
    first = make_wal(memory_device, descriptor="hash-v1;partitions_per_table=64")
    first.append(make_record())
    second = make_wal(memory_device, descriptor="hash-v1;partitions_per_table=256")
    second.append(make_record())
    descriptors = [record.descriptor for record in second.read_from(0)]
    assert "hash-v1;partitions_per_table=64" in descriptors
    assert "hash-v1;partitions_per_table=256" in descriptors
    assert second.damage is None
    assert {record.format_version for record in second.read_from(0)} == {1}


def test_a_batch_gets_consecutive_numbers_and_returns_the_last(wal: WalManager) -> None:
    """The commit protocol reads the return value as the commit sequence number."""
    last = wal.append_many(
        [
            make_record(1),
            make_record(2),
            make_record(3, record_type=WalRecordType.COMMIT),
        ]
    )
    assert last == wal.last_lsn
    assert [record.lsn for record in wal.read_from(0)] == [1, 2, 3, 4]


def test_the_log_assigns_sequence_numbers_and_refuses_one_from_the_caller(
    wal: WalManager,
) -> None:
    """A caller-chosen number is how the same number gets written twice."""
    with pytest.raises(GrafxConfigurationError) as caught:
        wal.append(WalRecord(record_type=WalRecordType.COMMIT, lsn=7))
    assert caught.value.details["field"] == "lsn"


def test_an_empty_batch_is_refused(wal: WalManager) -> None:
    """A commit with no records would return a sequence number nothing was written under."""
    with pytest.raises(GrafxConfigurationError) as caught:
        wal.append_many([])
    assert caught.value.details["field"] == "records"


@pytest.mark.parametrize("records", ["abc", b"abc", 5, make_record()])
def test_a_batch_that_is_not_a_sequence_of_records_is_refused(
    wal: WalManager, records: object
) -> None:
    """A single record where a batch belongs would be iterated as something else entirely."""
    with pytest.raises(GrafxConfigurationError) as caught:
        wal.append_many(records)  # type: ignore[arg-type]
    assert caught.value.details["field"] == "records"


def test_a_record_type_this_build_cannot_write_is_refused(wal: WalManager) -> None:
    """Writing an unknown type would put a meaning on disk this build does not have."""
    with pytest.raises(GrafxConfigurationError) as caught:
        wal.append(WalRecord(record_type=250))
    assert caught.value.details["field"] == "record_type"


def test_nothing_of_a_refused_batch_reaches_the_device(
    wal: WalManager, memory_device: MemoryStorageDevice
) -> None:
    """Validation happens before the first byte, so a bad entry loses the whole batch."""
    wal.append(make_record())
    before = memory_device.log_size(wal.segments()[-1].name)
    with pytest.raises(GrafxConfigurationError):
        wal.append_many([make_record(1), WalRecord(record_type=250)])
    assert memory_device.log_size(wal.segments()[-1].name) == before
    assert wal.last_lsn == 2


# --- more than one participant (CF-6, D1) -----------------------------------------------------


def test_a_second_participant_never_writes_a_sequence_number_the_first_used(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """D1 asks for real multi-process writing, and this is what makes it real (CF-6).

    The segment index is built at open. A participant that never looks again keeps assigning
    numbers from a tail that has moved, and two writers then write the same number -- silent
    duplication, discovered much later by a cold reader as a log that lost records. The second
    manager here opens BEFORE the first appends again, which is exactly the window.
    """
    first = make_wal(memory_device)
    first.append(make_record(1))
    first.barrier()
    second = make_wal(memory_device)
    assert second.last_lsn == first.last_lsn

    third = first.append(make_record(2))
    first.barrier()
    fourth = second.append(make_record(3))
    second.barrier()

    assert fourth == third + 1, "the second participant must not reuse a number"
    cold = make_wal(memory_device)
    assert cold.damage is None
    lsns = [record.lsn for record in cold.read_from(0)]
    assert lsns == list(range(1, len(lsns) + 1))
    assert len(set(lsns)) == len(lsns)


def test_a_tail_that_grows_after_truncated_observation_is_rebuilt_from_its_record_start(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    writer = make_wal(memory_device)
    writer.append(make_record(1))
    observer = make_wal(memory_device)
    assert observer.last_lsn == 2
    segment = writer.segments()[-1].name
    completed = make_record(2).with_lsn(3).encode()

    memory_device.append_log(segment, completed[:-1])
    assert observer.damage is not None
    assert observer.damage.reason is FailureReason.TRUNCATED_TAIL

    memory_device.append_log(segment, completed[-1:])

    assert observer.damage is None
    assert observer.last_lsn == 3
    assert [record.lsn for record in observer.read_from(1)] == [1, 2, 3]


def test_two_participants_taking_turns_keep_the_log_contiguous(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The same property under a longer interleaving, across segment rolls."""
    first = make_wal(memory_device, segment_bytes=512)
    second = make_wal(memory_device, segment_bytes=512)
    assigned: list[int] = []
    for round_index in range(12):
        writer = first if round_index % 2 == 0 else second
        assigned.append(writer.append(make_record(round_index)))
        writer.barrier()
    assert assigned == sorted(set(assigned))
    cold = make_wal(memory_device, segment_bytes=512)
    assert cold.damage is None
    lsns = [record.lsn for record in cold.read_from(0)]
    assert lsns == list(range(1, len(lsns) + 1))
    assert set(assigned).issubset(set(lsns))


def test_a_participant_sees_a_segment_another_one_rolled(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A roll by somebody else moves the tail into a file this participant has never seen."""
    first = make_wal(memory_device, segment_bytes=512)
    second = make_wal(memory_device, segment_bytes=512)
    for index in range(10):
        first.append(make_record(index))
    first.barrier()
    assert len(first.segments()) > 1
    before = first.last_lsn

    last = second.append(make_record(99))
    second.barrier()

    assert last == before + 1
    assert first.last_lsn == last, (
        "the first participant must see the second one's record"
    )
    assert [segment.name for segment in second.segments()] == [
        segment.name for segment in first.segments()
    ]
    cold = make_wal(memory_device, segment_bytes=512)
    assert cold.damage is None
    assert cold.last_lsn == last


def test_a_participant_adopts_the_epoch_another_one_raised(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """BR-7 across participants: a takeover raises the epoch and a stale writer must see it."""
    first = make_wal(memory_device)
    second = make_wal(memory_device)
    first.append(make_record(1, epoch=9))
    first.barrier()
    with pytest.raises(GrafxStaleEpoch) as caught:
        second.append(make_record(2, epoch=8))
    assert caught.value.details["current_epoch"] == 9


def test_an_unchanged_log_is_not_read_again_on_every_append(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Re-deriving the tail sits on the commit path, so it has to cost almost nothing.

    With nothing changed the whole check is one directory listing and one size query, and not a
    single byte of the log is read. A re-derivation that rescanned would put the cost of the
    whole log on every commit, which is the reason the index was cached in the first place.
    """
    device = CallRecordingDevice(memory_device)
    manager = make_wal(device, segment_bytes=512)
    for index in range(20):
        manager.append(make_record(index))
    manager.barrier()
    assert len(manager.segments()) >= 3, (
        "one size query must be cheaper than sizing them all"
    )

    device.calls.clear()
    manager.append(make_record(99))

    assert [call for call in device.calls if call[0] == "read_log"] == []
    assert len([call for call in device.calls if call[0] == "list_files"]) == 1
    assert len([call for call in device.calls if call[0] == "log_size"]) <= 2


def test_only_the_new_bytes_are_read_when_another_participant_appended(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """When the tail HAS moved, the cost is the other participant's work, not the log's size."""
    first = make_wal(memory_device, segment_bytes=65536)
    for index in range(40):
        first.append(make_record(index, payload=bytes(200)))
    first.barrier()
    device = CallRecordingDevice(memory_device)
    second = make_wal(device, segment_bytes=65536)
    # The claim is about an INCREMENTAL re-derivation, so this participant must first HAVE a
    # picture to bring up to date. open() surveys the segments and reads what is inside them at
    # the first question, which is this one; everything the measurement below sees is what
    # arrived afterwards.
    assert second.last_lsn == first.last_lsn
    written = first.append(make_record(99))
    first.barrier()
    grown = memory_device.log_size(first.segments()[-1].name)

    device.calls.clear()
    second.append(make_record(100))

    reads = [call for call in device.calls if call[0] == "read_log"]
    assert len(reads) == 1
    assert 0 < int(reads[0][1]) < grown // 4, "only the new tail may be read"
    assert second.last_lsn == written + 1


def test_damage_left_by_another_participant_refuses_the_append_without_wedging(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Re-deriving the tail must not put the unopenable failure onto the commit path.

    A participant that meets damage while looking for the tail records it and refuses the
    append. What it must never do is raise out of the re-derivation in a way that leaves the
    manager unusable, because the repair door is behind the same object.
    """
    first = make_wal(memory_device)
    first.append(make_record(1))
    first.barrier()
    second = make_wal(memory_device)
    memory_device.append_log(first.segments()[-1].name, bytes(64))

    with pytest.raises(GrafxCorruptionDetected):
        second.append(make_record(2))

    assert second.damage is not None
    report = second.truncate_after(second.last_lsn)
    assert report.completed is True
    assert second.damage is None
    assert second.append(make_record(3)) == second.last_lsn


def test_a_truncation_by_another_participant_is_picked_up(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A segment that SHRANK cannot be resolved by reading forward, so it falls back to a full pass."""
    first = make_wal(memory_device, segment_bytes=4096)
    for index in range(8):
        first.append(make_record(index))
    first.barrier()
    second = make_wal(memory_device, segment_bytes=4096)
    keep = first.segments()[0].first_lsn + 2
    first.truncate_after(keep)

    following = second.append(make_record(99))

    assert second.last_lsn == following
    assert following == keep + 1
    cold = make_wal(memory_device, segment_bytes=4096)
    assert cold.damage is None
    assert [record.lsn for record in cold.read_from(0)] == list(range(1, following + 1))


def test_recycling_by_another_participant_leaves_the_log_readable(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Segments that went away must leave this index too, or a read fails on a ghost."""
    first = make_wal(memory_device, segment_bytes=512)
    for index in range(20):
        first.append(make_record(index))
    first.barrier()
    second = make_wal(memory_device, segment_bytes=512)
    report = first.recycle(first.segments()[-1].first_lsn)
    recycled = report.recycled
    surviving = [segment.name for segment in first.segments()]
    assert recycled

    second.append(make_record(99))

    held = [segment.name for segment in second.segments()]
    assert set(surviving).issubset(set(held)), (
        "a surviving segment must stay in the index"
    )
    assert set(held).isdisjoint(set(recycled)), (
        "a recycled segment must leave the index"
    )
    assert second.damage is None
    lsns = [record.lsn for record in second.read_from(0)]
    assert lsns == list(range(lsns[0], lsns[0] + len(lsns)))


def test_a_segment_that_cannot_be_read_refuses_the_append_without_wedging(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Damage met while re-deriving the tail is RECORDED, not thrown through the commit path.

    Both behaviours refuse the append with the same error class, so the class is not the
    property (A62). What separates them is what the manager is left holding: a re-derivation
    that records the damage leaves a manager that still knows it is damaged and can still be
    repaired, while one that lets the failure fly leaves nothing behind at all.
    """

    class OversizedTailDevice:
        """A real device that claims one segment grew past what a segment can be."""

        def __init__(self, inner: MemoryStorageDevice) -> None:
            """Wrap a device that starts out honest."""
            self._inner = inner
            self.oversized: str | None = None

        def __getattr__(self, attribute: str) -> Any:
            """Forward everything this wrapper does not override."""
            return getattr(self._inner, attribute)

        def log_size(self, file: str) -> int:
            """Answer honestly until a segment is nominated, so open() sees a healthy log.

            The claim has to exceed the ceiling by more than the bytes already accounted for:
            the incremental pass reads from where it left off, so what is refused is the RANGE
            it would have to read, not the size of the file.
            """
            if file == self.oversized:
                return MAX_SEGMENT_READ_BYTES * 2
            return self._inner.log_size(file)

    first = make_wal(memory_device)
    first.append(make_record(1))
    first.barrier()
    tail = first.segments()[-1].name
    lying = OversizedTailDevice(memory_device)
    second = make_wal(lying)
    assert second.damage is None, (
        "the log has to be healthy when this participant opens it"
    )
    lying.oversized = tail

    with pytest.raises(GrafxCorruptionDetected):
        second.append(make_record(2))

    assert second.damage is not None, "the damage has to be recorded, not merely raised"
    assert second.damage.reason is FailureReason.UNREADABLE_SEGMENT
    assert second.damage.segment == tail


def test_a_gap_left_by_a_stale_participant_is_refused_and_never_absorbed(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Re-deriving the tail carries the numbering forward, so a hole cannot be adopted.

    The incremental pass reads only what is new, which means it starts in the middle of the log
    and has to be TOLD where the numbering stood. Without that, the first record it meets defines
    the expectation, and a segment that does not follow is silently accepted -- the participant
    then appends after a gap and the log is permanently short of records nobody will miss until
    replay.
    """
    first = make_wal(memory_device, segment_bytes=4096)
    first.append(make_record(1))
    first.barrier()
    second = make_wal(memory_device, segment_bytes=4096)
    planted = segment_name("wal", first.segments()[-1].number + 1)
    memory_device.create(planted)
    memory_device.append_log(
        planted,
        WalRecord(
            record_type=WalRecordType.WRITE_PAGE,
            payload=bytes(8),
            descriptor=DESCRIPTOR,
            lsn=first.last_lsn + 5,
            epoch=1,
        ).encode(),
    )

    with pytest.raises(GrafxCorruptionDetected):
        second.append(make_record(2))

    assert second.damage is not None
    assert second.damage.reason is FailureReason.LSN_DISCONTINUITY
    assert second.damage.expected_lsn == first.last_lsn + 1


def test_a_segment_another_participant_reclaimed_leaves_the_pending_set(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """An obligation to flush a file that no longer exists breaks an ordinary checkpoint.

    This participant has unflushed work in several segments when another one reclaims the oldest.
    Re-deriving the tail has to drop those names as well as the records: asking the device for a
    barrier on a reclaimed file fails the whole barrier, and with it every commit behind it.
    """
    first = make_wal(memory_device, segment_bytes=512)
    for index in range(20):
        first.append(make_record(index))
    assert len(first.segments()) >= 3
    second = make_wal(memory_device, segment_bytes=512)
    reclaimed = second.recycle(second.segments()[-1].first_lsn)
    assert reclaimed.recycled, "the other participant has to have taken segments away"

    first.append(make_record(99))
    first.barrier()

    cold = make_wal(memory_device, segment_bytes=512)
    assert cold.damage is None
    lsns = [record.lsn for record in cold.read_from(0)]
    assert lsns == list(range(lsns[0], lsns[0] + len(lsns)))


def test_a_repair_that_removed_a_whole_segment_is_seen(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A cut on a segment boundary removes a whole file and shrinks nothing at all.

    This is the ordinary shape when damage sits at the start of a segment after a roll, and it
    is invisible to a change token that only watches the segments still present: every surviving
    name keeps its size, so a participant comparing sizes concludes that nothing happened and
    keeps assigning sequence numbers from a tail that was thrown away.
    """
    first = make_wal(memory_device, segment_bytes=512)
    for index in range(20):
        first.append(make_record(index))
    first.barrier()
    assert len(first.segments()) >= 3
    second = make_wal(memory_device, segment_bytes=512)
    assert second.last_lsn == first.last_lsn
    target = first.segments()[-2].last_lsn

    report = first.truncate_after(target)

    assert report.completed is True
    assert report.truncated_segment is None, (
        "the cut fell on a boundary, so nothing was rolled"
    )
    following = second.append(make_record(99))
    second.barrier()
    # The number follows the repaired tail. It is not target + 1 exactly, because a full tail
    # segment makes this append roll and the new segment's own header takes a number first.
    assert following > target
    assert following == second.last_lsn
    cold = make_wal(memory_device, segment_bytes=512)
    assert cold.damage is None
    assert [record.lsn for record in cold.read_from(0)] == list(range(1, following + 1))


def test_a_cut_never_rewrites_a_segment_that_keeps_its_name(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The invariant the incremental re-derivation rests on, asserted directly.

    Reading only what is new is sound exactly while an observed byte range of a segment never
    changes. Shortening a segment in place breaks it, and no comparison of names and sizes can
    detect the break -- a rewrite can land on the same length by ordinary coincidence. So the
    cut rolls its survivors into a new segment instead, and this is the test that says a name
    that survives a repair still holds the bytes it held.
    """
    first = make_wal(memory_device, segment_bytes=512)
    for index in range(20):
        first.append(make_record(index))
    first.barrier()
    before = {
        segment.name: memory_device.read_log(segment.name, 0, segment.size_bytes)
        for segment in first.segments()
    }
    target = first.segments()[0].first_lsn + 1

    first.truncate_after(target)

    for name, content in before.items():
        if memory_device.exists(name):
            held = memory_device.read_log(name, 0, len(content))
            assert held == content, f"{name} kept its name and changed its bytes"


def test_a_repair_followed_by_growth_never_wedges_another_participant(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Cut, then grow past the old size: the worst face of an unsound token.

    A participant resuming at a remembered byte offset lands in the middle of a record that was
    written after the repair. It declares a HEALTHY log damaged, and because its own repair then
    finds nothing above the cut to remove, the damage never clears and the participant is stuck
    for good -- the one dead end this component must not have.
    """
    first = make_wal(memory_device, segment_bytes=8192)
    for index in range(6):
        first.append(make_record(index, payload=bytes(64)))
    first.barrier()
    second = make_wal(memory_device, segment_bytes=8192)
    assert second.last_lsn == first.last_lsn
    target = first.segments()[0].first_lsn + 1

    first.truncate_after(target)
    for index in range(12):
        first.append(make_record(index, payload=bytes(200)))
    first.barrier()

    assert second.damage is None, "a healthy log must never be reported as damaged"
    following = second.append(make_record(99))
    second.barrier()
    assert following == first.last_lsn
    cold = make_wal(memory_device, segment_bytes=8192)
    assert cold.damage is None
    assert [record.lsn for record in cold.read_from(0)] == list(range(1, following + 1))


def test_a_participant_whose_segments_were_all_reclaimed_is_not_wedged(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Ordinary reclamation leaves an idle participant with no anchor at all (FR-6, BR-10).

    It committed once and then sat still while another participant checkpointed past it. Nothing
    it remembers is on disk any more, so carrying its old sequence number forward describes a
    log that no longer exists: a healthy log is refused, and the repair that refusal suggests
    would destroy every live record.
    """
    first = make_wal(memory_device, segment_bytes=512)
    first.append(make_record(1))
    first.barrier()
    second = make_wal(memory_device, segment_bytes=512)
    anchored_at = second.last_lsn
    for index in range(30):
        first.append(make_record(index))
    first.barrier()
    first.recycle(first.segments()[-1].first_lsn)
    surviving = {segment.name for segment in first.segments()}
    assert len(surviving) == 1
    assert first.segments()[0].first_lsn > anchored_at, "the anchor must really be gone"

    assert second.damage is None, "a healthy log must never be reported as damaged"
    following = second.append(make_record(99))
    second.barrier()

    assert following == first.last_lsn
    assert second.segments()[0].name in surviving
    cold = make_wal(memory_device, segment_bytes=512)
    assert cold.damage is None
    lsns = [record.lsn for record in cold.read_from(0)]
    assert lsns == list(range(lsns[0], lsns[0] + len(lsns)))
    assert lsns[-1] == following


@pytest.mark.parametrize(
    "door", ["last_lsn", "segments", "total_bytes", "read_from", "scan_all"]
)
def test_each_door_answers_from_the_device_and_not_from_memory(
    door: str, make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The log belongs to the database, so no door may answer from a private opinion.

    One door per run, on a participant that has touched nothing else. Asserting them together
    proves only the first: every door re-derives as a side effect, so whichever runs first makes
    the rest pass whatever they do. A battery found exactly that -- four of these doors could be
    reverted with the combined test still green (A34).

    Recovery scans through these doors, so a participant that reports the log ending before it
    does hands recovery a shorter log than exists.
    """
    first = make_wal(memory_device, segment_bytes=512)
    second = make_wal(memory_device, segment_bytes=512)
    for index in range(12):
        first.append(make_record(index))
    first.barrier()
    truth = first.last_lsn
    expected = list(range(1, truth + 1))

    if door == "last_lsn":
        assert second.last_lsn == truth
    elif door == "segments":
        assert [segment.name for segment in second.segments()] == [
            segment.name for segment in first.segments()
        ]
        assert second.segments()[-1].last_lsn == truth
    elif door == "total_bytes":
        assert second.total_bytes() == first.total_bytes()
        assert second.total_bytes() > 0
    elif door == "read_from":
        assert [record.lsn for record in second.read_from(0)] == expected
    else:
        assert [
            item.record.lsn for item in second.scan_all() if item.record is not None
        ] == expected


def test_the_public_re_derivation_is_what_brings_a_participant_up_to_date(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """``refresh()`` has to do the work itself, not leave it to whoever asks next.

    Every observing door re-derives, so observing through one cannot tell whether ``refresh``
    did anything at all. The index is read directly instead -- through the one accessor that
    reports what this participant currently believes without going to the device.
    """
    first = make_wal(memory_device, segment_bytes=512)
    second = make_wal(memory_device, segment_bytes=512)
    for index in range(12):
        first.append(make_record(index))
    first.barrier()
    assert f"last_lsn={first.last_lsn}" not in repr(second)

    second.refresh()

    assert f"last_lsn={first.last_lsn}" in repr(second)


def test_reclamation_uses_the_log_as_it_is_now(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A pass that reclaims against a remembered index reclaims nothing and says so cheerfully.

    This participant knows one segment. Another has written twenty records since. Asked to
    reclaim below a horizon that covers most of them, a pass working from memory finds only its
    own single segment -- which is the newest it knows, and therefore never offered -- and
    reports an empty, contented result while the disk keeps growing.
    """
    first = make_wal(memory_device, segment_bytes=512)
    first.append(make_record(1))
    first.barrier()
    second = make_wal(memory_device, segment_bytes=512)
    assert len(second.segments()) == 1
    for index in range(20):
        first.append(make_record(index))
    first.barrier()
    assert len(first.segments()) >= 3

    report = second.recycle(first.segments()[-1].first_lsn)

    assert report.recycled, "the pass has to see the segments that appeared since"
    assert report.retained
    cold = make_wal(memory_device, segment_bytes=512)
    assert cold.damage is None
    lsns = [record.lsn for record in cold.read_from(0)]
    assert lsns == list(range(lsns[0], lsns[0] + len(lsns)))


def test_a_segment_that_lost_bytes_forces_a_full_pass(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Reading forward from a remembered offset is only sound while the range is still there.

    Nothing this component does shortens a segment in place any more -- a cut rolls into a new
    one -- but the log directory is shared, and a participant that resumed at an offset past the
    end of a shortened file would report a healthy log as damaged. When bytes have gone, the
    remembered offset is worthless and the answer comes from a full pass.
    """
    first = make_wal(memory_device, segment_bytes=8192)
    for index in range(8):
        first.append(make_record(index))
    first.barrier()
    second = make_wal(memory_device, segment_bytes=8192)
    assert second.last_lsn == first.last_lsn
    name = first.segments()[-1].name
    keep = _first_boundary_of(memory_device, name)
    original = first.last_lsn

    memory_device.truncate_log(name, keep)

    assert second.damage is None, "a shortened log is shorter, not damaged"
    assert second.last_lsn < original
    assert [record.lsn for record in second.read_from(0)] == list(
        range(1, second.last_lsn + 1)
    )


def _first_boundary_of(device: MemoryStorageDevice, name: str) -> int:
    """Return the offset just past the second record of a segment."""
    data = device.read_log(name, 0, device.log_size(name))
    offset = 0
    for _ in range(2):
        offset += int.from_bytes(data[offset + 12 : offset + 16], "little")
    return offset


def test_a_full_pass_also_drops_an_obligation_for_a_file_that_is_gone(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The cheap path prunes reclaimed names; the full pass has to prune them as well.

    Two paths, one invariant (A66.1). This participant holds unflushed work in several segments
    when another one repairs the log out from under it, which takes its anchor away and forces
    the full pass. If only the cheap path dropped the names, the next barrier would ask the
    device to flush a file that no longer exists and fail an ordinary commit.
    """
    first = make_wal(memory_device, segment_bytes=512)
    for index in range(20):
        first.append(make_record(index))
    assert len(first.segments()) >= 3
    second = make_wal(memory_device, segment_bytes=512)
    target = second.segments()[0].first_lsn + 1

    second.truncate_after(target)
    assert first.segments()[-1].last_lsn == target

    first.append(make_record(99))
    first.barrier()

    cold = make_wal(memory_device, segment_bytes=512)
    assert cold.damage is None
    assert [record.lsn for record in cold.read_from(0)] == list(
        range(1, cold.last_lsn + 1)
    )


def test_a_repair_reports_only_the_success_it_really_had(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A cut taken against a remembered index removes what it can see, and claims the rest.

    The report said completed with records still above the cut, which is worse than failing:
    recovery believes the log ends where it asked, and every later decision rests on it.
    """
    first = make_wal(memory_device, segment_bytes=512)
    first.append(make_record(1))
    first.barrier()
    second = make_wal(memory_device, segment_bytes=512)
    target = second.last_lsn
    for index in range(20):
        first.append(make_record(index))
    first.barrier()
    assert first.last_lsn > target

    report = second.truncate_after(target)

    assert report.completed is True
    cold = make_wal(memory_device, segment_bytes=512)
    assert cold.last_lsn == target, (
        "a repair that reported success must have taken effect"
    )
    assert [record.lsn for record in cold.read_from(0)] == list(range(1, target + 1))


def test_refresh_is_a_door_of_its_own(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The public re-derivation refuses before open and records damage rather than raising."""
    unopened = make_wal(memory_device, open_now=False)
    with pytest.raises(GrafxTransactionStateError):
        unopened.refresh()

    first = make_wal(memory_device)
    first.append(make_record(1))
    first.barrier()
    second = make_wal(memory_device)
    memory_device.append_log(first.segments()[-1].name, bytes(64))

    second.refresh()

    assert second.damage is not None
    assert second.damage.reason is FailureReason.BAD_MAGIC


# --- the epoch guard (BR-7) ------------------------------------------------------------------


def test_an_older_epoch_is_refused_and_no_byte_reaches_the_device(
    wal: WalManager, memory_device: MemoryStorageDevice
) -> None:
    """BR-7: the epoch decides, and the rejection happens before anything is written."""
    wal.append(make_record(epoch=4))
    name = wal.segments()[-1].name
    before = memory_device.log_size(name)
    with pytest.raises(GrafxStaleEpoch) as caught:
        wal.append(make_record(epoch=3))
    assert caught.value.code == "stale_epoch"
    assert caught.value.retryable is False
    assert caught.value.details["current_epoch"] == 4
    assert memory_device.log_size(name) == before
    assert wal.last_lsn == 2


def test_the_same_epoch_and_a_newer_one_are_both_accepted(wal: WalManager) -> None:
    """A takeover raises the epoch, and the successor must be able to write immediately."""
    wal.append(make_record(epoch=2))
    wal.append(make_record(epoch=2))
    wal.append(make_record(epoch=5))
    assert wal.last_lsn == 4


def test_a_stale_epoch_anywhere_in_a_batch_refuses_the_whole_batch(
    wal: WalManager, memory_device: MemoryStorageDevice
) -> None:
    """Half a batch on disk is the shape that survives as a torn transaction."""
    wal.append(make_record(epoch=9))
    name = wal.segments()[-1].name
    before = memory_device.log_size(name)
    with pytest.raises(GrafxStaleEpoch) as caught:
        wal.append_many([make_record(1, epoch=9), make_record(2, epoch=1)])
    assert caught.value.details["position"] == 1
    assert memory_device.log_size(name) == before


def test_the_epoch_guard_survives_a_reopen(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The epoch the log already holds is read back off the device, not kept in a variable."""
    first = make_wal(memory_device)
    first.append(make_record(epoch=7))
    second = make_wal(memory_device)
    with pytest.raises(GrafxStaleEpoch):
        second.append(make_record(epoch=6))


# --- rolling -------------------------------------------------------------------------------


def test_the_log_rolls_when_a_segment_is_full(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """FR-6: the log is segmented, which is what makes reclaiming space possible at all."""
    manager = make_wal(memory_device, segment_bytes=512)
    for index in range(20):
        manager.append(make_record(index))
    segments = manager.segments()
    assert len(segments) > 1
    assert all(segment.size_bytes <= 512 for segment in segments)
    assert [segment.number for segment in segments] == list(range(1, len(segments) + 1))
    assert [segment.first_lsn for segment in segments[1:]] == [
        segment.last_lsn + 1 for segment in segments[:-1]
    ]


def test_a_batch_never_lands_in_two_segments(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """One torn write must damage one file, or the repair is not a single truncation."""
    manager = make_wal(memory_device, segment_bytes=512)
    for round_index in range(6):
        manager.append_many([make_record(round_index), make_record(round_index + 100)])
    by_segment: dict[str, list[int]] = {}
    for item in manager.scan_all():
        assert item.failure is None
        assert item.record is not None
        by_segment.setdefault(item.segment, []).append(item.record.lsn)
    for lsns in by_segment.values():
        assert lsns == list(range(lsns[0], lsns[0] + len(lsns)))


def test_a_batch_larger_than_a_segment_is_written_whole(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Splitting it would mean a torn write could damage two files at once."""
    manager = make_wal(memory_device, segment_bytes=MIN_SEGMENT_BYTES)
    manager.append_many([make_record(index, payload=bytes(200)) for index in range(4)])
    assert len(manager.segments()) == 1
    assert manager.segments()[0].size_bytes > MIN_SEGMENT_BYTES
    assert [record.lsn for record in manager.read_from(0)] == [1, 2, 3, 4, 5]


def test_segment_numbers_are_never_reused(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A recycled name may still be held open, so re-claiming it would write into a grave."""
    manager = make_wal(memory_device, segment_bytes=512)
    for index in range(12):
        manager.append(make_record(index))
    highest = manager.segments()[-1].number
    manager.recycle(manager.segments()[-1].first_lsn)
    manager.append(make_record(99))
    assert manager.segments()[-1].number >= highest


# --- the barrier (BR-4) ----------------------------------------------------------------------


def test_the_barrier_flushes_every_segment_written_since_the_last_one(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A barrier that flushed only the newest segment would leave a hole in front of it."""
    device = BarrierRefusingDevice(memory_device)
    manager = make_wal(device, segment_bytes=512)
    for index in range(12):
        manager.append(make_record(index))
    manager.barrier()
    names = [segment.name for segment in manager.segments()]
    assert device.flushed == names
    assert device.flushed == sorted(device.flushed)


def test_a_barrier_with_nothing_pending_touches_no_device(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """An fsync of an unchanged file is latency FR-5 is measured on, bought for nothing."""
    device = BarrierRefusingDevice(memory_device)
    manager = make_wal(device)
    manager.append(make_record())
    manager.barrier()
    device.flushed.clear()
    manager.barrier()
    assert device.flushed == []


def test_a_forced_range_barriers_again_after_the_pending_cache_was_emptied(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    device = BarrierRefusingDevice(memory_device)
    manager = make_wal(device)
    through = manager.append_many(
        (make_record(1), make_record(2, record_type=WalRecordType.COMMIT))
    )
    manager.barrier()
    device.flushed.clear()

    forced = manager.force_barrier_range(1, through)

    assert forced == (manager.segments()[0].name,)
    assert device.flushed == list(forced)


def test_a_failed_barrier_is_counted_and_re_raised(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    metrics: RecordingMetricsSink,
) -> None:
    """A25: the device has no metrics slot, so the caller of the barrier counts the failure."""
    device = BarrierRefusingDevice(memory_device)
    manager = make_wal(device)
    manager.append(make_record())
    device.refuse = True
    with pytest.raises(GrafxDurabilityBarrierFailed) as caught:
        manager.barrier()
    assert caught.value.code == "durability_barrier_failed"
    assert metrics.counter(BARRIER_FAILURES_TOTAL) == 1.0


def test_a_failed_barrier_keeps_the_work_pending(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """BR-4: nothing may be treated as durable, so a retry must flush the same files again."""
    device = BarrierRefusingDevice(memory_device)
    manager = make_wal(device)
    manager.append(make_record())
    device.refuse = True
    with pytest.raises(GrafxDurabilityBarrierFailed):
        manager.barrier()
    device.refuse = False
    device.flushed.clear()
    manager.barrier()
    assert device.flushed == [manager.segments()[-1].name]


def test_the_barrier_is_timed_as_the_write_ahead_log_target(
    wal: WalManager, metrics: RecordingMetricsSink
) -> None:
    """A25 splits fsync timing into wal and data, and this component owns the wal half."""
    wal.append(make_record())
    wal.barrier()
    assert metrics.timed == [FSYNC_DURATION_SECONDS]


def test_appending_alone_never_takes_a_barrier(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """CONTRACT.md section 8.3 says append takes no barrier, so a batch can be grouped."""
    device = BarrierRefusingDevice(memory_device)
    manager = make_wal(device)
    manager.append_many([make_record(1), make_record(2)])
    assert device.flushed == []


# --- metrics -------------------------------------------------------------------------------


def test_the_manager_registers_exactly_the_catalog_metrics_it_emits(
    wal: WalManager, metrics: RecordingMetricsSink
) -> None:
    """G7: every metric is looked up in the frozen catalog rather than declared here."""
    assert tuple(metrics.registered) == WAL_METRICS
    assert {descriptor.name for descriptor in WAL_METRICS} == {
        "oktografx_fsync_duration_seconds",
        "oktografx_barrier_failures_total",
        "oktografx_wal_size_bytes",
        "oktografx_wal_segments",
        "oktografx_wal_truncation_lag_segments",
        "oktografx_checksum_verifications_total",
        "oktografx_checksum_failures_total",
    }


def test_the_size_and_segment_gauges_follow_the_log(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    metrics: RecordingMetricsSink,
) -> None:
    """OR-2 asks for the size and the live segment count, and both have to move."""
    manager = make_wal(memory_device, segment_bytes=512)
    assert metrics.gauge(WAL_SIZE_BYTES) == 0.0
    for index in range(10):
        manager.append(make_record(index))
    assert metrics.gauge(WAL_SIZE_BYTES) == float(manager.total_bytes())
    assert metrics.gauge(WAL_SEGMENTS) == float(len(manager.segments()))


def test_record_checksums_are_counted_under_their_own_kind(
    wal: WalManager, metrics: RecordingMetricsSink
) -> None:
    """The kind label separates a record checksum from a page checksum, which C1 counts."""
    wal.append_many([make_record(1), make_record(2)])
    before = metrics.counter(CHECKSUM_VERIFICATIONS_TOTAL, kind="record")
    assert list(wal.read_from(0))
    after = metrics.counter(CHECKSUM_VERIFICATIONS_TOTAL, kind="record")
    assert after - before == 3.0


def test_a_disabled_sink_is_never_asked_to_do_anything(
    clock: FrozenClock, memory_device: MemoryStorageDevice
) -> None:
    """The no-op sink must not allocate on the hot path, so the guard is asserted directly."""
    manager = WalManager(
        memory_device,
        clock,
        NoOpMetricsSink(),
        segment_bytes=512,
        descriptor=DESCRIPTOR,
    )
    manager.open()
    manager.append(make_record())
    manager.barrier()
    assert manager.last_lsn == 2


# --- construction --------------------------------------------------------------------------


@pytest.mark.parametrize("slot", ["storage", "clock", "metrics"])
def test_an_unfilled_port_refuses_construction(
    slot: str, memory_device: MemoryStorageDevice, clock: FrozenClock
) -> None:
    """G5: an empty slot refuses startup, with no silent default and no no-op fallback."""
    ports: dict[str, object] = {
        "storage": memory_device,
        "clock": clock,
        "metrics": NoOpMetricsSink(),
    }
    ports[slot] = None
    with pytest.raises(GrafxPortNotConfigured) as caught:
        WalManager(
            ports["storage"],  # type: ignore[arg-type]
            ports["clock"],  # type: ignore[arg-type]
            ports["metrics"],  # type: ignore[arg-type]
            segment_bytes=4096,
            descriptor=DESCRIPTOR,
        )
    assert caught.value.details["slot"] == slot


@pytest.mark.parametrize(
    "segment_bytes", [0, -1, MIN_SEGMENT_BYTES - 1, True, "4096", 1.5]
)
def test_a_segment_size_too_small_to_hold_a_header_is_refused(
    segment_bytes: object, memory_device: MemoryStorageDevice, clock: FrozenClock
) -> None:
    """A segment that cannot hold its own header would roll forever without progressing."""
    with pytest.raises(GrafxConfigurationError) as caught:
        WalManager(
            memory_device,
            clock,
            NoOpMetricsSink(),
            segment_bytes=segment_bytes,  # type: ignore[arg-type]
            descriptor=DESCRIPTOR,
        )
    assert caught.value.details["field"] == "segment_bytes"


@pytest.mark.parametrize("descriptor", ["", None, 7, "d" * 5000])
def test_a_descriptor_the_record_could_not_carry_is_refused(
    descriptor: object, memory_device: MemoryStorageDevice, clock: FrozenClock
) -> None:
    """The descriptor is what makes a record readable without its configuration."""
    with pytest.raises(GrafxConfigurationError) as caught:
        WalManager(
            memory_device,
            clock,
            NoOpMetricsSink(),
            segment_bytes=4096,
            descriptor=descriptor,  # type: ignore[arg-type]
        )
    assert caught.value.details["field"] == "descriptor"


@pytest.mark.parametrize("directory", ["", "wal/", "/wal", "wal\\segments", None])
def test_a_directory_the_storage_port_could_not_use_is_refused(
    directory: object, memory_device: MemoryStorageDevice, clock: FrozenClock
) -> None:
    """A logical name is relative and uses forward slashes on every platform."""
    with pytest.raises(GrafxConfigurationError) as caught:
        WalManager(
            memory_device,
            clock,
            NoOpMetricsSink(),
            directory=directory,  # type: ignore[arg-type]
            segment_bytes=4096,
            descriptor=DESCRIPTOR,
        )
    assert caught.value.details["field"] == "directory"


def test_the_log_lives_where_it_was_told_to(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Two logs in one database would collide if the directory were not honoured."""
    manager = make_wal(memory_device, directory="journal")
    manager.append(make_record())
    assert memory_device.list_files("journal/") == ("journal/000000000001.wal",)
    assert memory_device.list_files("wal/") == ()


def test_a_foreign_file_in_the_directory_is_ignored(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A quarantine copy or an operator's note must not stop the log from opening."""
    memory_device.create("wal/notes.txt")
    memory_device.append_log("wal/notes.txt", b"not a segment")
    manager = make_wal(memory_device)
    manager.append(make_record())
    assert [segment.name for segment in manager.segments()] == ["wal/000000000001.wal"]
    assert manager.damage is None
