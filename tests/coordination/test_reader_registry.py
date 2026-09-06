"""The reader registry: horizon by minimum, pruning of the dead, no eviction of the live.

BR-10 is the rule under test. A registration that stopped proving it is alive is **pruned**,
because it describes a process that is gone. A registration that is alive is never removed, never
overridden and never limited, however far behind its snapshot has fallen: recycling follows the
horizon, and the horizon follows the readers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice, HookStorageDevice, ManualClock
from okto_grafx.adapters.coordination_local import decode_reader_record
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ports.coordination import ReaderHandle
from okto_grafx.engine.coordination import recyclable_horizon


def reader_files(database_root: Path) -> list[str]:
    """Return the names of the reader registrations currently on disk."""
    directory = database_root / "control" / "readers"
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir() if path.name.endswith(".reader"))


def test_no_reader_means_no_horizon(make_coordinator: CoordinatorFactory) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    assert coordinator.reader_horizon() is None


def test_clock_failure_precedes_reader_publication(
    make_coordinator: CoordinatorFactory,
    database_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register cannot leave an own durable record behind without returning its handle."""
    clock = ManualClock()
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock)
    failure = RuntimeError("reader clock failed")

    def fail() -> float:
        raise failure

    monkeypatch.setattr(clock, "monotonic", fail)
    with pytest.raises(RuntimeError, match="reader clock failed"):
        coordinator.register_reader(7)

    assert reader_files(database_root) == []


def test_the_horizon_is_the_minimum_over_live_readers(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    high = coordinator.register_reader(900)
    assert coordinator.reader_horizon() == 900
    low = coordinator.register_reader(120)
    middle = coordinator.register_reader(400)
    assert coordinator.reader_horizon() == 120
    assert len(reader_files(database_root)) == 3
    coordinator.unregister_reader(low)
    assert coordinator.reader_horizon() == 400
    coordinator.unregister_reader(middle)
    assert coordinator.reader_horizon() == 900
    coordinator.unregister_reader(high)
    assert coordinator.reader_horizon() is None
    assert reader_files(database_root) == []


def test_readers_of_another_participant_count_towards_the_horizon(
    make_coordinator: CoordinatorFactory
) -> None:
    writer = make_coordinator(owner_id="p1-writer")
    reader_side = make_coordinator(owner_id="p2-reader", monotonic_origin=8_000.0)
    handle = reader_side.register_reader(42)
    assert writer.reader_horizon() == 42
    reader_side.unregister_reader(handle)
    assert writer.reader_horizon() is None


def test_a_stalled_reader_is_pruned_and_the_horizon_advances(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    observer_clock = ManualClock(monotonic=1_000.0)
    observer = make_coordinator(owner_id="p1-writer", clock=observer_clock)
    gone = make_coordinator(owner_id="p2-gone", monotonic_origin=90.0)
    alive = make_coordinator(owner_id="p3-alive", monotonic_origin=500_000.0)
    stale_handle = gone.register_reader(10)
    live_handle = alive.register_reader(500)

    assert observer.reader_horizon() == 10
    observer_clock.advance(14.0)
    alive.refresh_reader(live_handle)
    assert observer.reader_horizon() == 10, "a reader inside the threshold is still live"

    observer_clock.advance(2.0)
    alive.refresh_reader(live_handle)
    assert observer.reader_horizon() == 500
    assert reader_files(database_root) == [f"{live_handle.reader_id}.reader"]
    assert stale_handle.reader_id not in reader_files(database_root)


def test_observing_the_horizon_keeps_stalled_empty_and_temporary_records(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    observer_clock = ManualClock(monotonic=1_000.0)
    observer = make_coordinator(owner_id="p1-observer", clock=observer_clock)
    gone = make_coordinator(owner_id="p2-gone", monotonic_origin=90.0)
    stale = gone.register_reader(10)
    assert observer.reader_horizon() == 10  # establish the ordinary liveness sample
    observer_clock.advance(16.0)

    readers = database_root / "control" / "readers"
    empty = readers / "p8-crashed-r0001.reader"
    temporary = readers / "p9-crashed-r0001.reader.p9-crashed.tmp"
    empty.write_bytes(b"")
    temporary.write_bytes(b"partial")
    before = sorted(path.name for path in readers.iterdir())

    assert observer.observe_reader_horizon() == 10
    assert sorted(path.name for path in readers.iterdir()) == before
    assert f"{stale.reader_id}.reader" in before

    # The ordinary lifecycle operation retains its established pruning semantics.
    assert observer.reader_horizon() is None
    assert f"{stale.reader_id}.reader" not in reader_files(database_root)


def test_a_live_reader_is_never_pruned_however_far_behind_it_falls(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # BR-10 in one test: the checkpoint runs away and the reader stays exactly where it is.
    observer_clock = ManualClock(monotonic=1_000.0)
    observer = make_coordinator(owner_id="p1-writer", clock=observer_clock)
    lagging = make_coordinator(owner_id="p2-lagging", monotonic_origin=3.0)
    handle = lagging.register_reader(1)

    checkpoint = 1
    for _round in range(100):
        checkpoint += 100
        observer_clock.advance(14.0)
        lagging.refresh_reader(handle)
        horizon = observer.reader_horizon()
        assert horizon == 1
        assert recyclable_horizon(horizon, checkpoint) == 1
    assert reader_files(database_root) == [f"{handle.reader_id}.reader"]
    assert checkpoint == 10_001

    # Only when the reader itself moves on does the horizon move.
    lagging.unregister_reader(handle)
    assert recyclable_horizon(observer.reader_horizon(), checkpoint) == checkpoint


def test_a_registration_of_this_participant_is_never_pruned(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # For its own registrations the coordinator has ground truth rather than an inference: the
    # process is running this very code, so no amount of silence makes it dead.
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock)
    handle = coordinator.register_reader(7)
    clock.advance(10_000.0)
    assert coordinator.reader_horizon() == 7
    assert reader_files(database_root) == [f"{handle.reader_id}.reader"]


def test_refreshing_a_pruned_registration_brings_it_back(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # A reader that went quiet and then spoke again was never dead, and BR-10 forbids ending its
    # snapshot behind its back, so a refresh republishes the registration.
    observer_clock = ManualClock(monotonic=1_000.0)
    observer = make_coordinator(owner_id="p1-writer", clock=observer_clock)
    quiet = make_coordinator(owner_id="p2-quiet", monotonic_origin=44.0)
    handle = quiet.register_reader(64)
    assert observer.reader_horizon() == 64
    observer_clock.advance(16.0)
    assert observer.reader_horizon() is None
    assert reader_files(database_root) == []

    quiet.refresh_reader(handle)
    assert reader_files(database_root) == [f"{handle.reader_id}.reader"]
    assert observer.reader_horizon() == 64


def test_registering_a_reader_takes_no_section_and_touches_no_lease(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # Readers never block writers and writers never block readers (FR-2). The proof is that a
    # registration happens while a writer holds the commit section, and that it never reads or
    # writes the lease file.
    writer = make_coordinator(owner_id="p1-writer")
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    reader_side = make_coordinator(
        owner_id="p2-reader", monotonic_origin=12.0, storage=device
    )
    with writer.exclusive("commit", timeout=1.0):
        device.reset()
        handle = reader_side.register_reader(11)
        reader_side.refresh_reader(handle)
        assert reader_side.reader_horizon() == 11
    assert device.calls, "the registration did reach the device"
    lease_touched = [entry for entry in device.touched if "writer.lease" in entry[1]]
    assert lease_touched == [], "a reader must not read or write the lease file"
    assert all("control/readers/" in name for _operation, name in device.touched if name)
    assert (database_root / "control" / "readers" / f"{handle.reader_id}.reader").exists()


def test_a_reader_registration_carries_the_snapshot_it_pins(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    handle = coordinator.register_reader(4_096)
    assert isinstance(handle, ReaderHandle)
    assert handle.snapshot_lsn == 4_096
    raw = (database_root / "control" / "readers" / f"{handle.reader_id}.reader").read_bytes()
    record = decode_reader_record(raw)
    assert record.reader_id == handle.reader_id
    assert record.snapshot_lsn == 4_096
    assert record.active is True
    assert record.heartbeat_seq == 1
    coordinator.refresh_reader(handle)
    refreshed = decode_reader_record(
        (database_root / "control" / "readers" / f"{handle.reader_id}.reader").read_bytes()
    )
    assert refreshed.heartbeat_seq == 2
    assert refreshed.snapshot_lsn == 4_096


def test_every_registration_gets_its_own_identity(make_coordinator: CoordinatorFactory) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    handles = [coordinator.register_reader(index) for index in range(1, 21)]
    assert len({handle.reader_id for handle in handles}) == 20
    assert coordinator.reader_horizon() == 1


def test_unregistering_twice_is_quiet(make_coordinator: CoordinatorFactory) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    handle = coordinator.register_reader(3)
    coordinator.unregister_reader(handle)
    coordinator.unregister_reader(handle)
    assert coordinator.reader_horizon() is None


def test_a_snapshot_that_is_not_an_lsn_is_refused(make_coordinator: CoordinatorFactory) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    for value in (-1, "9", None, 1.5):
        with pytest.raises(GrafxConfigurationError):
            coordinator.register_reader(value)  # type: ignore[arg-type]


def test_a_snapshot_of_zero_is_a_legitimate_horizon(make_coordinator: CoordinatorFactory) -> None:
    # LSN zero means "none" (CONTRACT.md section 3), and a reader that pins it pins everything.
    coordinator = make_coordinator(owner_id="p1-aaaa")
    coordinator.register_reader(0)
    assert coordinator.reader_horizon() == 0
    assert recyclable_horizon(coordinator.reader_horizon(), 5_000) == 0


def test_a_temporary_file_is_not_mistaken_for_a_registration(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    handle = coordinator.register_reader(30)
    leftovers = database_root / "control" / "readers"
    (leftovers / "p9-crashed-r0001.reader.p9-crashed.tmp").write_bytes(b"partial")
    assert coordinator.reader_horizon() == 30
    assert handle.snapshot_lsn == 30


def test_a_corrupt_registration_fails_closed_with_its_location(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # Recycling must not proceed on a control file nobody can read: the horizon it carries is
    # unknown, and guessing it is how a live reader loses the segments it still needs.
    coordinator = make_coordinator(owner_id="p1-aaaa")
    coordinator.register_reader(30)
    (database_root / "control" / "readers" / "p9-other-r0001.reader").write_bytes(b"not a record")
    with pytest.raises(GrafxCorruptionDetected) as failure:
        coordinator.reader_horizon()
    assert "p9-other-r0001.reader" in str(failure.value.details["file"])


def test_an_empty_registration_file_is_discarded(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    coordinator.register_reader(30)
    empty = database_root / "control" / "readers" / "p9-other-r0002.reader"
    empty.write_bytes(b"")
    assert coordinator.reader_horizon() == 30
    assert not empty.exists()
