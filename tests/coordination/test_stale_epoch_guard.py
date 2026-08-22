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
    DirectoryStorageDevice,
    HookStorageDevice,
    ManualClock,
    WRITE_OPERATIONS,
    owned_by,
)
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.domain.errors import GrafxStaleEpoch
from okto_grafx.domain.ports.coordination import Lease


def _overthrown(
    make_coordinator: CoordinatorFactory, database_root: object
) -> tuple[LocalProcessCoordinator, Lease, HookStorageDevice]:
    """Return a participant whose epoch was taken over, with a device that records every call."""
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    stale_clock = ManualClock(monotonic=1_000.0)
    stale = make_coordinator(owner_id="p1-stale", clock=stale_clock, storage=device)
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


def _successor_of(
    make_coordinator: CoordinatorFactory, database_root: object
) -> LocalProcessCoordinator:
    """Return a participant that has watched the current owner stall and may take over."""
    clock = ManualClock(monotonic=9_000_000.0)
    successor = make_coordinator(owner_id="p3-third", clock=clock)
    assert successor.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert successor.detect_dead_owner(stall_threshold=5.0) is not None
    return successor


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
    assert device.forbid_writes is True, "the device was not armed, so nothing was proved"
    assert device.calls, "the guard did read the published record"
    assert set(device.calls) <= {"exists", "log_size", "read_log", "file_size"}


def test_the_refusal_repeats_and_reads_the_record_every_time(
    make_coordinator: CoordinatorFactory, database_root: object
) -> None:
    # The guard used to answer a repeat refusal from a remembered high-water mark, with no device
    # call at all. That shortcut is gone: it could not see a control plane restored from
    # quarantine, and a committing writer only ever validates. Reading a small record is the
    # price of being right about who may write.
    stale, lease, device = _overthrown(make_coordinator, database_root)
    with pytest.raises(GrafxStaleEpoch):
        stale.validate_epoch(lease.epoch)
    device.reset()
    for _attempt in range(10):
        with pytest.raises(GrafxStaleEpoch):
            stale.validate_epoch(lease.epoch)
    assert device.calls, "the refusal must rest on the published record, not on memory"
    assert set(device.calls) & WRITE_OPERATIONS == set()


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
    assert device.forbid_writes is True
    assert set(device.calls) & WRITE_OPERATIONS == set()


def test_no_setting_can_put_a_staleness_window_in_front_of_the_guard(
    make_coordinator: CoordinatorFactory, database_root: object
) -> None:
    # There is no cache and no knob that introduces one. The validation that CONTRACT.md section
    # 8.5 places BEFORE the commit section is the guard that matters for a stale holder: it reads
    # the published record whenever the free ceiling refusal cannot already answer, and no
    # setting can put a staleness window in front of either.
    stale, lease, device = _overthrown(make_coordinator, database_root)
    for _attempt in range(5):
        with pytest.raises(GrafxStaleEpoch):
            stale.validate_epoch(lease.epoch)
    with pytest.raises(GrafxStaleEpoch):
        with stale.exclusive("commit", timeout=1.0):
            stale.validate_epoch(lease.epoch)
    assert device.forbid_writes is True
    assert set(device.calls) & WRITE_OPERATIONS == set()


def test_the_successor_passes_its_own_validation(
    make_coordinator: CoordinatorFactory, database_root: object
) -> None:
    # The successor is the participant that took the lease over, which is what its name says and
    # what A74 requires: validation asks "may I commit", so the answer belongs to a holder.
    stale, lease, _device = _overthrown(make_coordinator, database_root)
    successor = _successor_of(make_coordinator, database_root)
    held = successor.acquire_writer_lease(timeout=1.0)
    assert held.epoch == successor.current_epoch()

    successor.validate_epoch(held.epoch)
    with pytest.raises(GrafxStaleEpoch):
        successor.validate_epoch(held.epoch - 1)
    with pytest.raises(GrafxStaleEpoch):
        successor.validate_epoch(held.epoch + 1)

    # And the participant it replaced is refused at the epoch it still remembers holding.
    with pytest.raises(GrafxStaleEpoch):
        stale.validate_epoch(lease.epoch)
    assert owned_by(stale.owner_id(), "p1-stale")


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
