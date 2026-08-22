"""Horizon based recycling (SPEC-M1 FR-6, BR-10, AC-8, AC-9, TS-8, TS-9).

BR-10 is the rule with the sharpest edge in this component: a segment goes when every record it
holds sits below the snapshot horizon, never because no reader is present, and never by evicting
one. Getting it wrong in one direction loses a live reader's work; getting it wrong in the other
grows the disk forever. Both directions are asserted.

AC-8 is the long-lived reader: the segments it needs stay, the ones above it are reclaimed
normally, the total stays bounded, and the lag gauge names the situation. AC-9 is the same pass
with a real second process holding a segment open, which on Windows means a pending delete and on
POSIX an ordinary unlink -- one test, both mechanisms, because the contract is the same sentence
on both families.

The horizon itself comes from C3. It is computed here with the real
:func:`okto_grafx.engine.coordination.recyclable_horizon` rather than with a number chosen to make
the test pass, so a change in that function's truth table shows up here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxError
from okto_grafx.domain.wal import segment_name
from okto_grafx.engine.coordination import recyclable_horizon
from okto_grafx.engine.wal_manager import (
    WAL_SEGMENTS,
    WAL_SIZE_BYTES,
    WAL_TRUNCATION_LAG_SEGMENTS,
    WalManager,
)

from .conftest import FrozenClock, RecordingMetricsSink, make_record

SEGMENT_BYTES: int = 512
"""Small enough that a handful of records makes several segments."""


def _grow(manager: WalManager, records: int) -> None:
    """Write enough records to make several segments, and make them durable."""
    for index in range(records):
        manager.append(make_record(index))
    manager.barrier()


# --- the rule ------------------------------------------------------------------------------


def test_only_segments_entirely_below_the_horizon_are_reclaimed(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The rule of CONTRACT.md section 8.3: a segment goes when its last record is below it."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 20)
    segments = manager.segments()
    assert len(segments) >= 4
    horizon = segments[1].last_lsn + 1
    report = manager.recycle(horizon)
    assert report.recycled == (segments[0].name, segments[1].name)
    assert report.horizon_lsn == horizon
    assert [segment.name for segment in manager.segments()] == [
        segment.name for segment in segments[2:]
    ]
    assert all(segment.last_lsn >= horizon for segment in manager.segments())


def test_the_newest_segment_is_never_reclaimed(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """It is where the next record goes, so a horizon past the end must still leave it."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 16)
    report = manager.recycle(manager.last_lsn + 1_000)
    assert len(manager.segments()) == 1
    assert report.retained == (manager.segments()[0].name,)
    assert manager.append(make_record(99)) == manager.last_lsn


def test_a_horizon_that_has_not_moved_reclaims_nothing(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Nothing published yet is not a licence to reclaim, and BR-10 says so explicitly."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 16)
    before = manager.segments()
    report = manager.recycle(0)
    assert report.recycled == ()
    assert manager.segments() == before
    assert manager.total_bytes() == sum(segment.size_bytes for segment in before)


def test_recycling_is_driven_by_the_horizon_function_of_the_coordinator(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The number comes from C3's rule, not from one chosen to make this test pass."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    segments = manager.segments()
    reader = segments[1].first_lsn
    checkpoint = segments[-1].first_lsn
    horizon = recyclable_horizon(reader, checkpoint)
    assert horizon == reader
    manager.recycle(horizon, reader_present=True)
    kept = manager.segments()
    assert kept[0].first_lsn <= reader <= kept[0].last_lsn
    assert all(segment.last_lsn >= horizon for segment in kept)


def test_a_reader_that_never_moves_keeps_exactly_its_own_segments(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """AC-8 and TS-8: the reader is never evicted and the log above it is still reclaimed."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 12)
    reader_snapshot = manager.segments()[0].last_lsn
    sizes: list[int] = []
    for round_index in range(6):
        _grow(manager, 6)
        manager.recycle(
            recyclable_horizon(reader_snapshot, manager.last_lsn), reader_present=True
        )
        sizes.append(manager.total_bytes())
        assert manager.segments()[0].first_lsn <= reader_snapshot
        assert [record.lsn for record in manager.read_from(reader_snapshot)][0] == reader_snapshot
    assert sizes == sorted(sizes), "a pinned reader means the log grows rather than shrinks"
    moved = recyclable_horizon(manager.last_lsn, manager.last_lsn)
    manager.recycle(moved, reader_present=True)
    assert len(manager.segments()) == 1, "the log collapses once the horizon moves"


def test_the_log_stays_bounded_when_the_horizon_follows_the_writer(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """AC-8's other half: with no reader holding it back, the size does not grow forever."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    observed: list[int] = []
    for round_index in range(8):
        _grow(manager, 8)
        manager.recycle(recyclable_horizon(None, manager.last_lsn))
        observed.append(manager.total_bytes())
    assert max(observed) <= 4 * SEGMENT_BYTES
    assert len(manager.segments()) <= 2


def test_a_segment_created_and_never_written_is_ignored_by_the_log(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A crash between creating a segment and writing to it leaves a file holding nothing.

    Registering it would put an unreadable range at the head of the log: the recyclable prefix
    stops at the first segment whose records could not be read, so one empty file would stop
    reclamation for good, and the next append would land in it without a segment header.
    """
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 20)
    planted = segment_name("wal", manager.segments()[-1].number + 1)
    memory_device.create(planted)
    reopened = make_wal(memory_device)
    assert planted not in {segment.name for segment in reopened.segments()}
    assert reopened.damage is None
    assert reopened.last_lsn == manager.last_lsn
    reopened.append(make_record(1))
    reopened.barrier()
    report = reopened.recycle(reopened.segments()[-1].first_lsn)
    assert report.recycled, "an empty file must not stop the log from reclaiming space"
    assert reopened.damage is None


def test_a_segment_that_could_not_be_read_is_never_reclaimed(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """An unknown range is not a range below the horizon, so damage stops reclamation."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 20)
    oldest = manager.segments()[0].name
    damaged = manager.segments()[1].name
    data = memory_device.read_log(damaged, 0, memory_device.log_size(damaged))
    memory_device.truncate_log(damaged, 0)
    memory_device.append_log(damaged, bytes(len(data)))
    reopened = make_wal(memory_device)
    report = reopened.recycle(reopened.last_lsn + 1_000)
    assert damaged not in report.recycled
    assert damaged in report.retained
    assert report.recycled == (oldest,)


# --- what the platform defers (AC-9) ----------------------------------------------------------


class DeferringDevice:
    """A device whose recycle answers False, either keeping the name or letting it go."""

    def __init__(self, inner: MemoryStorageDevice, *, keep_name: bool) -> None:
        """Wrap a real device and choose which kind of deferral to model."""
        self._inner = inner
        self._keep_name = keep_name
        self.attempts: list[str] = []
        self.flushed: list[str | None] = []

    def __getattr__(self, name: str) -> Any:
        """Forward everything this wrapper does not override."""
        return getattr(self._inner, name)

    def recycle(self, file: str) -> bool:
        """Report a deferral, having freed the name or not."""
        self.attempts.append(file)
        if not self._keep_name:
            self._inner.recycle(file)
        return False

    def durable_barrier(self, file: str | None = None) -> None:
        """Flush, remembering which name was asked for."""
        self.flushed.append(file)
        self._inner.durable_barrier(file)


def test_a_deferral_that_kept_the_name_stops_the_walk(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A27: recycling behind a segment that stayed would leave the log with a hole in it."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    before = [segment.name for segment in manager.segments()]
    device = DeferringDevice(memory_device, keep_name=True)
    stubborn = make_wal(device, segment_bytes=SEGMENT_BYTES)
    report = stubborn.recycle(stubborn.last_lsn)
    assert report.recycled == ()
    assert report.deferred == (before[0],)
    assert [segment.name for segment in stubborn.segments()] == before
    assert device.attempts == [before[0]]


def test_a_deferral_that_freed_the_name_carries_on(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The Windows pending delete: the name is gone at once and the space follows later."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    before = [segment.name for segment in manager.segments()]
    device = DeferringDevice(memory_device, keep_name=False)
    pending = make_wal(device, segment_bytes=SEGMENT_BYTES)
    report = pending.recycle(pending.last_lsn)
    assert report.recycled == ()
    assert len(report.deferred) == len(before) - 1
    assert [segment.name for segment in pending.segments()] == before[-1:]


def test_a_segment_the_pass_kept_keeps_its_durability_obligation(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A27: when the platform will not release a segment, that segment STAYS in the log.

    Staying means it is still owed a barrier. Forgetting it because the pass called it deferred
    would leave it volatile for good, and a crash would take records this manager still counts
    in ``last_lsn`` -- a durable commit that is not durable, which is the one thing BR-4 forbids.

    The records here are appended WITHOUT a barrier on purpose: a log whose segments were all
    flushed before the pass has no obligation to drop, which is exactly why the one-line fix
    survives a suite that only ever recycles a flushed log.
    """
    device = DeferringDevice(memory_device, keep_name=True)
    manager = make_wal(device, segment_bytes=SEGMENT_BYTES)
    for index in range(24):
        manager.append(make_record(index))
    assert len(manager.segments()) >= 3, "the obligation must belong to a segment that is not the tail"

    report = manager.recycle(manager.last_lsn)

    kept = report.deferred[0]
    assert report.recycled == ()
    assert kept in report.retained, "a deferral that kept its name kept its segment"
    device.flushed.clear()
    manager.barrier()
    assert kept in device.flushed, "the segment the pass kept is still owed a barrier"
    assert manager.segments()[0].name == kept


def test_a_reclaimed_segment_is_never_flushed_afterwards(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The other side of the same rule: a name that is gone must leave the pending set.

    Asking the device to flush a file it has just reclaimed would raise a barrier failure on an
    ordinary checkpoint, so the obligation has to be dropped for exactly the segments that went.
    """
    device = DeferringDevice(memory_device, keep_name=False)
    manager = make_wal(device, segment_bytes=SEGMENT_BYTES)
    for index in range(24):
        manager.append(make_record(index))
    report = manager.recycle(manager.last_lsn)
    assert report.deferred
    device.flushed.clear()
    manager.barrier()
    assert set(device.flushed).isdisjoint(set(report.deferred))
    assert device.flushed == [manager.segments()[-1].name]


def test_a_deferred_segment_is_offered_again_on_the_next_pass(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A27 makes reclaiming the caller's business, so the next pass has to ask again."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    device = DeferringDevice(memory_device, keep_name=True)
    stubborn = make_wal(device, segment_bytes=SEGMENT_BYTES)
    first = stubborn.recycle(stubborn.last_lsn)
    second = stubborn.recycle(stubborn.last_lsn)
    assert first.deferred == second.deferred
    assert len(device.attempts) == 2


def test_recycling_a_segment_a_second_grafx_process_holds_open(
    make_wal: Callable[..., WalManager],
    local_device: LocalStorageDevice,
    holder_process: Callable[..., Any],
) -> None:
    """AC-9 and TS-9, on both families: the name leaves the log at once and the space follows.

    The holder is a real second process reading through this project's own adapter, which is
    what a slow reader is. On Windows that adapter shares delete (A16), so the platform arms a
    pending delete behind the reader and the entry leaves the namespace immediately; on POSIX the
    unlink does the same in one step. The contract this component keeps is identical on both: no
    error reaches the caller, the log keeps working, and the segment is gone from it.
    """
    manager = make_wal(local_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    oldest = manager.segments()[0]
    held = Path(local_device.root) / "wal" / oldest.name.split("/")[-1]
    holder = holder_process(held, mode="grafx")
    try:
        report = manager.recycle(manager.last_lsn)
        assert oldest.name in report.recycled or oldest.name in report.deferred
        assert local_device.exists(oldest.name) is False
        assert oldest.name not in {segment.name for segment in manager.segments()}
        assert manager.damage is None
        manager.append(make_record(1))
        manager.barrier()
    finally:
        holder.release()
    local_device.retry_pending_deletes()
    assert local_device.pending_deletes() == ()
    reopened = make_wal(local_device)
    assert reopened.damage is None
    lsns = [record.lsn for record in reopened.read_from(0)]
    assert lsns == list(range(lsns[0], lsns[0] + len(lsns)))


def test_a_holder_that_denies_deletion_delays_the_space_and_nothing_else(
    make_wal: Callable[..., WalManager],
    local_device: LocalStorageDevice,
    holder_process: Callable[..., Any],
) -> None:
    """A23 and A27: an antivirus-shaped holder keeps the name, and the log stays whole.

    The device queues nothing it cannot key on an unforgeable name, so reclaiming that space
    becomes this component's business: it reports the deferral, stops the walk there so the
    surviving log has no hole, keeps the segment readable, and asks again on the next pass. On a
    family where the platform always lets go, the first pass simply succeeds -- which is the same
    contract with a shorter story.
    """
    manager = make_wal(local_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    oldest = manager.segments()[0]
    held = Path(local_device.root) / "wal" / oldest.name.split("/")[-1]
    holder = holder_process(held)
    try:
        first = manager.recycle(manager.last_lsn)
        if local_device.exists(oldest.name):
            assert first.deferred == (oldest.name,)
            assert first.recycled == ()
            assert [segment.name for segment in manager.segments()][0] == oldest.name
            assert [record.lsn for record in manager.read_from(0)][0] == oldest.first_lsn
    finally:
        holder.release()
    second = manager.recycle(manager.last_lsn)
    assert local_device.exists(oldest.name) is False
    assert oldest.name in second.recycled or oldest.name in first.recycled
    reopened = make_wal(local_device)
    assert reopened.damage is None
    lsns = [record.lsn for record in reopened.read_from(0)]
    assert lsns == list(range(lsns[0], lsns[0] + len(lsns)))


def test_recycling_survives_a_reopen_on_a_real_directory(
    make_wal: Callable[..., WalManager], local_device: LocalStorageDevice
) -> None:
    """What the log believes has to match what the directory holds after a restart."""
    manager = make_wal(local_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    manager.recycle(manager.segments()[-1].first_lsn)
    surviving = [segment.name for segment in manager.segments()]
    reopened = make_wal(local_device)
    assert [segment.name for segment in reopened.segments()] == surviving
    assert reopened.damage is None
    assert reopened.last_lsn == manager.last_lsn


def test_a_recycled_log_still_reads_back_what_is_left(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Recycling removes records nobody needs; what remains must still be a contiguous run."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    manager.recycle(manager.segments()[2].first_lsn)
    lsns = [record.lsn for record in manager.read_from(0)]
    assert lsns == list(range(lsns[0], lsns[0] + len(lsns)))
    assert lsns[0] == manager.segments()[0].first_lsn
    assert manager.damage is None


def test_the_horizon_of_a_live_coordinator_is_what_drives_a_pass(
    make_wal: Callable[..., WalManager],
    local_device: LocalStorageDevice,
    clock: FrozenClock,
) -> None:
    """The reader registry really is C3's, so the pass is driven by a live one, not by a fake."""
    coordinator = LocalProcessCoordinator(
        local_device,
        clock,
        owner_id="writer-one",
        lock_directory=str(Path(local_device.root) / "control"),
    )
    manager = make_wal(local_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    pinned = manager.segments()[1].first_lsn
    handle = coordinator.register_reader(pinned)
    try:
        horizon = recyclable_horizon(coordinator.reader_horizon(), manager.last_lsn)
        assert horizon == pinned
        manager.recycle(horizon, reader_present=coordinator.reader_horizon() is not None)
        assert manager.segments()[0].first_lsn <= pinned <= manager.segments()[0].last_lsn
    finally:
        coordinator.unregister_reader(handle)
    assert coordinator.reader_horizon() is None
    manager.recycle(recyclable_horizon(coordinator.reader_horizon(), manager.last_lsn))
    assert len(manager.segments()) == 1


# --- metrics -------------------------------------------------------------------------------


def test_the_lag_gauge_names_whether_a_reader_is_holding_the_log(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    metrics: RecordingMetricsSink,
) -> None:
    """AC-8 asks for the retaining snapshot to be visible, and the label is the bounded half."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    pinned = manager.segments()[0].last_lsn
    report = manager.recycle(pinned, reader_present=True)
    assert metrics.gauge(WAL_TRUNCATION_LAG_SEGMENTS, reader_present="true") == float(
        report.lag_segments
    )
    assert report.lag_segments == len(manager.segments()) - 1
    assert report.reader_present is True
    manager.recycle(manager.last_lsn + 1, reader_present=False)
    assert metrics.gauge(WAL_TRUNCATION_LAG_SEGMENTS, reader_present="false") == 0.0


def test_the_size_and_segment_gauges_fall_when_space_comes_back(
    make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice,
    metrics: RecordingMetricsSink,
) -> None:
    """A reclamation nobody can see is indistinguishable from one that never happened."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)
    before = metrics.gauge(WAL_SIZE_BYTES)
    report = manager.recycle(manager.segments()[-1].first_lsn)
    assert report.reclaimed_bytes > 0
    assert metrics.gauge(WAL_SIZE_BYTES) == float(manager.total_bytes())
    assert metrics.gauge(WAL_SIZE_BYTES) < before
    assert metrics.gauge(WAL_SEGMENTS) == float(len(manager.segments()))


def test_the_reader_present_label_changes_nothing_but_the_label(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """BR-10 says absence of readers is not an input, so the flag must not steer the decision."""
    first = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(first, 24)
    horizon = first.segments()[2].first_lsn
    told_true = first.recycle(horizon, reader_present=True)
    second_device = MemoryStorageDevice(page_size=512)
    second = make_wal(second_device, segment_bytes=SEGMENT_BYTES)
    _grow(second, 24)
    told_false = second.recycle(horizon, reader_present=False)
    assert told_true.recycled == told_false.recycled
    assert told_true.retained == told_false.retained
    assert told_true.lag_segments == told_false.lag_segments
    second_device.close()


class NameRecordingDevice:
    """A device that remembers every port call and every argument it was given.

    Every call is recorded, whatever the argument's type. Recording only calls whose first
    argument is a string would structurally hide ``durable_barrier(None)`` -- the one call that
    flushes every open file rather than a named one -- which is precisely the call the claim
    below has to rule out.
    """

    def __init__(self, inner: MemoryStorageDevice) -> None:
        """Wrap a real device and start with an empty record."""
        self._inner = inner
        self.calls: list[tuple[str, object]] = []

    def __getattr__(self, name: str) -> Any:
        """Forward every call, remembering the argument it carried."""
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def _remember(*arguments: Any, **keywords: Any) -> Any:
            self.calls.append((name, arguments[0] if arguments else None))
            return attribute(*arguments, **keywords)

        return _remember

    def durable_barrier(self, file: str | None = None) -> None:
        """Record the barrier explicitly, so a call with no name is recorded as one."""
        self.calls.append(("durable_barrier", file))
        self._inner.durable_barrier(file)


SEGMENT_NAME = re.compile(r"^wal/\d{12}\.wal$")
"""The only shape of name this component may ever hand to the storage port."""

NAMELESS_CALLS: frozenset[str] = frozenset({"pending_deletes", "retry_pending_deletes"})
"""Port calls that legitimately carry no file name at all."""


def test_the_log_never_reaches_outside_its_own_directory(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Carried finding CF-1: this component owns ``wal/`` and touches nothing else.

    C4's position on a damaged reader record rests on this claim, and C6 now rests on C4's: a
    recycling pass cannot retire the control-plane file that is refusing it, because it never
    names one. Two things make the test as strong as the claim rather than weaker:

    * every call is recorded whatever its argument type, so ``durable_barrier(None)`` -- which
      would flush every open file, including the lease and the reader records -- is visible and
      asserted absent;
    * the ERROR and RECOVERY paths are driven too, not just the happy one. A component that
      stays inside its directory while things go well and reaches outside while repairing would
      pass a test that only ever succeeds.
    """
    device = NameRecordingDevice(memory_device)
    memory_device.create("heap.dat")
    memory_device.create("control/writer.lease")
    manager = make_wal(device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 24)

    # the ordinary paths
    list(manager.read_from(0))
    list(manager.scan_all())
    manager.recycle(manager.segments()[1].first_lsn, reader_present=True)
    manager.truncate_after(manager.last_lsn - 1)
    manager.barrier()

    # a refused append, so the undo path is recorded as well
    memory_device.close()
    with pytest.raises(GrafxError):
        manager.append(make_record(1))
    memory_device.reopen()

    # a damaged open, so the repair path is recorded as well
    damaged = make_wal(device, segment_bytes=SEGMENT_BYTES)
    tail = damaged.segments()[-1].name
    memory_device.append_log(tail, bytes(48))
    reopened = make_wal(device, segment_bytes=SEGMENT_BYTES)
    assert reopened.damage is not None
    reopened.truncate_after(reopened.last_lsn)

    assert device.calls, "the recording device must have seen the calls it is judging"
    barriers = [argument for method, argument in device.calls if method == "durable_barrier"]
    assert barriers, "the barrier path has to have been exercised"
    assert None not in barriers, "a nameless barrier would flush the control plane too"
    for method, argument in device.calls:
        if method == "list_files":
            assert argument == "wal/"
            continue
        if method in NAMELESS_CALLS:
            continue
        assert isinstance(argument, str), f"{method} was given {argument!r}"
        assert SEGMENT_NAME.match(argument), f"{method} reached outside the log: {argument!r}"
    assert memory_device.exists("heap.dat")
    assert memory_device.exists("control/writer.lease")


def test_the_reader_present_label_must_be_a_bool(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The label domain is bounded at registration, so the value must be one of two things."""
    manager = make_wal(memory_device, segment_bytes=SEGMENT_BYTES)
    _grow(manager, 8)
    with pytest.raises(GrafxConfigurationError) as caught:
        manager.recycle(1, reader_present="true")  # type: ignore[arg-type]
    assert caught.value.details["field"] == "reader_present"
