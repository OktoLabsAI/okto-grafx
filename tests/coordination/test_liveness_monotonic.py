"""Liveness reads the local monotonic clock and nothing else (FR-7, TR-2).

The two properties proved here are the ones that make cross-process liveness sound at all:
a decision needs two samples separated by the observer own monotonic clock, and no reading of
any wall clock, however violently it moves, participates in any decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice, ManualClock
from okto_grafx.adapters.coordination_local import decode_lease_record
from okto_grafx.domain.ports.coordination import DeadOwnerReport

HOUR: float = 3_600.0


def test_a_first_observation_can_only_establish_the_baseline(
    make_coordinator: CoordinatorFactory
) -> None:
    # An observer that has seen the heartbeat once has no evidence of a stall yet: proof needs a
    # second sample taken later on its own clock.
    owner = make_coordinator(owner_id="p1-aaaa")
    owner.acquire_writer_lease(timeout=1.0)
    observer = make_coordinator(owner_id="p2-bbbb", monotonic_origin=-500.0)
    assert observer.detect_dead_owner(stall_threshold=5.0) is None


def test_a_stalled_heartbeat_is_reported_after_the_threshold(
    make_coordinator: CoordinatorFactory
) -> None:
    owner = make_coordinator(owner_id="p1-aaaa")
    owner.acquire_writer_lease(timeout=1.0)
    observer_clock = ManualClock(monotonic=8_000_000.0)
    observer = make_coordinator(owner_id="p2-bbbb", clock=observer_clock)
    assert observer.detect_dead_owner(stall_threshold=5.0) is None
    observer_clock.advance(4.9)
    assert observer.detect_dead_owner(stall_threshold=5.0) is None
    observer_clock.advance(0.2)
    report = observer.detect_dead_owner(stall_threshold=5.0)
    assert isinstance(report, DeadOwnerReport)
    assert report.owner_id == "p1-aaaa"
    assert report.last_heartbeat_seq == 1
    assert report.observed_stall_seconds == pytest.approx(5.1)


def test_a_heartbeat_that_moves_clears_the_stall(make_coordinator: CoordinatorFactory) -> None:
    owner_clock = ManualClock(monotonic=1_000.0)
    owner = make_coordinator(owner_id="p1-aaaa", clock=owner_clock)
    lease = owner.acquire_writer_lease(timeout=1.0)
    observer_clock = ManualClock(monotonic=-9_000.0)
    observer = make_coordinator(owner_id="p2-bbbb", clock=observer_clock)
    for _round in range(6):
        observer.detect_dead_owner(stall_threshold=5.0)
        observer_clock.advance(4.0)
        lease = owner.renew_lease(lease)
        assert observer.detect_dead_owner(stall_threshold=5.0) is None
    observer_clock.advance(9.0)
    report = observer.detect_dead_owner(stall_threshold=5.0)
    assert report is not None
    assert report.last_heartbeat_seq == lease.heartbeat_seq


@pytest.mark.parametrize("jump", [HOUR, -HOUR, 48 * HOUR, -48 * HOUR])
def test_a_wall_clock_jump_changes_no_liveness_decision(
    make_coordinator: CoordinatorFactory, jump: float
) -> None:
    owner_clock = ManualClock(monotonic=1_000.0)
    owner = make_coordinator(owner_id="p1-aaaa", clock=owner_clock)
    lease = owner.acquire_writer_lease(timeout=1.0)
    observer_clock = ManualClock(monotonic=77.0)
    observer = make_coordinator(owner_id="p2-bbbb", clock=observer_clock)
    assert observer.detect_dead_owner(stall_threshold=5.0) is None

    # Both wall clocks leap. Nothing else moves.
    owner_clock.jump_wall(jump)
    observer_clock.jump_wall(-jump)
    lease = owner.renew_lease(lease)
    assert observer.detect_dead_owner(stall_threshold=5.0) is None
    observer_clock.jump_wall(jump * 3)
    assert observer.detect_dead_owner(stall_threshold=5.0) is None

    # Only monotonic time makes the difference, and it makes it in both directions.
    observer_clock.advance(5.5)
    assert observer.detect_dead_owner(stall_threshold=5.0) is not None
    observer_clock.jump_wall(-jump * 10)
    assert observer.detect_dead_owner(stall_threshold=5.0) is not None
    lease = owner.renew_lease(lease)
    assert observer.detect_dead_owner(stall_threshold=5.0) is None


def test_a_wall_clock_jump_changes_no_acquisition_decision(
    make_coordinator: CoordinatorFactory
) -> None:
    owner = make_coordinator(owner_id="p1-aaaa")
    owner.acquire_writer_lease(timeout=1.0)
    contender_clock = ManualClock(monotonic=3.0)
    contender = make_coordinator(owner_id="p2-bbbb", clock=contender_clock)
    contender_clock.jump_wall(72 * HOUR)
    with pytest.raises(Exception) as failure:
        contender.acquire_writer_lease(timeout=1.0)
    assert failure.value.code == "lease_timeout"


def test_no_monotonic_reading_ever_reaches_the_device(tmp_path: Path) -> None:
    # Two participants with wildly different monotonic origins and the same wall reading publish
    # byte-identical lease files. A monotonic value on disk would make that impossible.
    from okto_grafx.adapters.coordination_local import LocalProcessCoordinator

    files: list[bytes] = []
    for origin in (1_000.0, 9_000_000_000.0):
        root = tmp_path / f"db{int(origin)}"
        device = DirectoryStorageDevice(root)
        clock = ManualClock(monotonic=origin, wall=1_700_000_000.25)
        coordinator = LocalProcessCoordinator(
            device,
            clock,
            owner_id="p1-aaaa",
            lock_directory=str(root / "control"),
            sleeper=clock.sleep,
        )
        lease = coordinator.acquire_writer_lease(timeout=1.0)
        coordinator.renew_lease(lease)
        files.append((root / "control" / "writer.lease").read_bytes())
    assert files[0] == files[1]
    assert decode_lease_record(files[0]).wall_stamp == pytest.approx(1_700_000_000.25)


def test_an_absent_or_vacant_lease_reports_nobody_dead(make_coordinator: CoordinatorFactory) -> None:
    observer = make_coordinator(owner_id="p2-bbbb")
    assert observer.detect_dead_owner(stall_threshold=0.001) is None
    owner = make_coordinator(owner_id="p1-aaaa")
    lease = owner.acquire_writer_lease(timeout=1.0)
    owner.release_lease(lease)
    observer_clock = ManualClock(monotonic=5.0)
    observer = make_coordinator(owner_id="p3-cccc", clock=observer_clock)
    observer.detect_dead_owner(stall_threshold=1.0)
    observer_clock.advance(3_600.0)
    assert observer.detect_dead_owner(stall_threshold=1.0) is None


def test_an_owner_that_keeps_renewing_is_never_reported(make_coordinator: CoordinatorFactory) -> None:
    owner_clock = ManualClock(monotonic=1_000.0)
    owner = make_coordinator(owner_id="p1-aaaa", clock=owner_clock)
    lease = owner.acquire_writer_lease(timeout=1.0)
    observer_clock = ManualClock(monotonic=0.0)
    observer = make_coordinator(owner_id="p2-bbbb", clock=observer_clock)
    for _tick in range(50):
        observer_clock.advance(1.0)
        owner_clock.advance(1.0)
        lease = owner.renew_lease(lease)
        assert observer.detect_dead_owner(stall_threshold=5.0) is None
    assert lease.heartbeat_seq == 51
