"""A stale epoch is refused before any byte reaches the device (BR-7, AC-6, TS-6).

The proof is not that an exception is raised somewhere: it is that the device the stale writer
holds records no write at all, and refuses to perform one if asked. The commit protocol of
CONTRACT.md section 8.5 validates twice, once before the critical section and once inside it;
both are exercised here, including with the staleness window of the epoch cache turned up.
"""

from __future__ import annotations

import pytest

from conftest import CoordinatorFactory
from coordination_support import (
    WRITE_OPERATIONS,
    DirectoryStorageDevice,
    HookStorageDevice,
    ManualClock,
)
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.domain.errors import GrafxStaleEpoch
from okto_grafx.domain.ports.coordination import Lease


def _overthrown(
    make_coordinator: CoordinatorFactory,
    database_root: object,
    *,
    epoch_cache_seconds: float = 0.0,
) -> tuple[LocalProcessCoordinator, Lease, HookStorageDevice]:
    """Return a participant whose epoch was taken over, with a device that records every call."""
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    stale_clock = ManualClock(monotonic=1_000.0)
    stale = make_coordinator(
        owner_id="p1-stale",
        clock=stale_clock,
        storage=device,
        epoch_cache_seconds=epoch_cache_seconds,
    )
    lease = stale.acquire_writer_lease(timeout=1.0)

    successor_clock = ManualClock(monotonic=6_000_000.0)
    successor = make_coordinator(owner_id="p2-next", clock=successor_clock)
    assert successor.detect_dead_owner(stall_threshold=5.0) is None
    successor_clock.advance(6.0)
    assert successor.detect_dead_owner(stall_threshold=5.0) is not None
    assert successor.takeover().epoch == 2

    device.reset()
    device.forbid_writes = True
    return stale, lease, device


def test_the_stale_holder_is_refused_and_writes_nothing(
    make_coordinator: CoordinatorFactory, database_root: object
) -> None:
    stale, lease, device = _overthrown(make_coordinator, database_root)
    with pytest.raises(GrafxStaleEpoch) as failure:
        stale.validate_epoch(lease.epoch)
    assert failure.value.retryable is False
    assert failure.value.code == "stale_epoch"
    assert failure.value.details["epoch"] == 1
    assert failure.value.details["published_epoch"] == 2
    assert device.write_calls == []
    assert set(device.calls) <= {"exists", "log_size", "read_log", "file_size"}
    assert set(device.calls) & WRITE_OPERATIONS == set()


def test_the_refusal_repeats_without_touching_the_device_at_all(
    make_coordinator: CoordinatorFactory, database_root: object
) -> None:
    # Once the higher epoch has been observed, the guard is free: epochs never decrease, so an
    # epoch below the highest one ever seen is refused with no device call whatsoever.
    stale, lease, device = _overthrown(make_coordinator, database_root)
    with pytest.raises(GrafxStaleEpoch):
        stale.validate_epoch(lease.epoch)
    device.reset()
    for _attempt in range(10):
        with pytest.raises(GrafxStaleEpoch):
            stale.validate_epoch(lease.epoch)
    assert device.calls == []


def test_the_commit_shaped_double_validation_refuses_and_writes_nothing(
    make_coordinator: CoordinatorFactory, database_root: object
) -> None:
    # The shape of CONTRACT.md section 8.5: validate, take the commit section, validate again.
    stale, lease, device = _overthrown(make_coordinator, database_root)
    reached_the_section = False
    with pytest.raises(GrafxStaleEpoch):
        stale.validate_epoch(lease.epoch)
        with stale.exclusive("commit", timeout=1.0):
            reached_the_section = True
            stale.validate_epoch(lease.epoch)
    assert reached_the_section is False
    assert device.write_calls == []


def test_a_cached_epoch_window_never_survives_the_commit_section(
    make_coordinator: CoordinatorFactory, database_root: object
) -> None:
    # With a staleness window configured, a validation outside a section may answer from the
    # cache. Inside a section the published epoch is always re-read, which is what makes the
    # window safe: every device write of the commit protocol happens inside that section.
    stale, lease, device = _overthrown(
        make_coordinator, database_root, epoch_cache_seconds=60.0
    )
    stale.validate_epoch(lease.epoch)  # the cache still says epoch one
    assert device.write_calls == []
    with pytest.raises(GrafxStaleEpoch):
        with stale.exclusive("commit", timeout=1.0):
            stale.validate_epoch(lease.epoch)
    assert device.write_calls == []
    # Having seen the truth once, the cheap path refuses from then on.
    with pytest.raises(GrafxStaleEpoch):
        stale.validate_epoch(lease.epoch)


def test_the_successor_passes_its_own_validation(
    make_coordinator: CoordinatorFactory, database_root: object
) -> None:
    stale, lease, _device = _overthrown(make_coordinator, database_root)
    successor = make_coordinator(owner_id="p3-third", monotonic_origin=10.0)
    assert successor.current_epoch() == 2
    with pytest.raises(GrafxStaleEpoch):
        successor.validate_epoch(1)
    with pytest.raises(GrafxStaleEpoch):
        successor.validate_epoch(3)
    successor.validate_epoch(2)
    assert stale.owner_id() == "p1-stale"
    assert lease.epoch == 1


def test_no_epoch_is_valid_before_one_is_published(make_coordinator: CoordinatorFactory) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    for epoch in (0, 1, 2):
        with pytest.raises(GrafxStaleEpoch):
            coordinator.validate_epoch(epoch)


def test_no_epoch_is_valid_while_the_lease_is_vacant(make_coordinator: CoordinatorFactory) -> None:
    # A released lease leaves its epoch published for the next participant to build on, but it
    # authorises nobody to write in the meantime.
    coordinator = make_coordinator(owner_id="p1-aaaa")
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    coordinator.validate_epoch(lease.epoch)
    coordinator.release_lease(lease)
    with pytest.raises(GrafxStaleEpoch):
        coordinator.validate_epoch(lease.epoch)


def test_the_holder_of_the_current_epoch_is_never_refused(
    make_coordinator: CoordinatorFactory
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    for _round in range(20):
        coordinator.validate_epoch(lease.epoch)
        lease = coordinator.renew_lease(lease)
        coordinator.validate_epoch(lease.epoch)
