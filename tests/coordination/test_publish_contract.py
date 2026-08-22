"""What a published control record owes the device, and how a failed publication is classified.

Two properties live here. The first is the shape of the publication itself: a whole record into a
temporary file, flushed, put in place atomically, and flushed AGAIN -- that second barrier is what
makes the rename itself durable on POSIX, where the containing directory has to reach the device
before the new name survives a power loss. Without it a crash can leave the previous record in
place, which is an epoch that went backwards, and the epoch is the one thing this component
promises never moves without evidence.

The second is amendment A47. A28 folded every barrier failure into one exception class and put the
access classification in ``details["retryable"]``, so a retry predicate that switches on the class
reports the antivirus touch TR-3 names as permanent on the barrier while riding out the identical
condition one call earlier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice, HookStorageDevice, ManualClock, owned_by
from okto_grafx.adapters.coordination_local import decode_lease_record
from okto_grafx.domain.errors import (
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxStorageError,
)

CONTROL_OPERATIONS: frozenset[str] = frozenset(
    {"create", "append_log", "durable_barrier", "atomic_replace", "remove"}
)
"""The calls that make up one publication, in the order the trail below asserts."""


def transient_barrier_failure() -> GrafxDurabilityBarrierFailed:
    """Build the failure C2 raises when a scanner holds the file during the flush (A28)."""
    failure = GrafxDurabilityBarrierFailed(
        "The barrier could not complete.",
        reason="sharing violation",
        errno=13,
        winerror=32,
        attempts=5,
    )
    failure.details["retryable"] = True
    return failure


def permanent_barrier_failure() -> GrafxDurabilityBarrierFailed:
    """Build the failure C2 raises when the flush itself failed, which no retry can fix."""
    failure = GrafxDurabilityBarrierFailed(
        "The barrier did not complete.", reason="fsync failure", errno=5, attempts=5
    )
    failure.details["retryable"] = False
    return failure


# --- the shape of a publication ---------------------------------------------------------------


def test_a_publication_flushes_the_target_after_the_replacement(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    device.reset()
    coordinator.acquire_writer_lease(timeout=1.0)

    trail = [
        (operation, name)
        for operation, name in device.touched
        if operation in CONTROL_OPERATIONS
    ]
    assert [operation for operation, _name in trail] == [
        "create",
        "append_log",
        "durable_barrier",
        "atomic_replace",
        "durable_barrier",
    ], trail
    temporary, target = trail[0][1], trail[-1][1]
    assert temporary.endswith(".tmp")
    assert target == "control/writer.lease"
    assert trail[2][1] == temporary, "the temporary is flushed before it is put in place"
    assert trail[3][1] == f"{temporary}->{target}"
    assert trail[4][1] == target, "the rename has to reach the device, on POSIX via its directory"


def test_every_publication_flushes_twice(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # Renewals and reader registrations are publications too, and the record they replace is just
    # as able to come back from the dead after a power loss.
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    publications = (
        lambda: coordinator.renew_lease(lease),
        lambda: coordinator.register_reader(4),
    )
    for publish in publications:
        device.reset()
        publish()
        barriers = [name for operation, name in device.touched if operation == "durable_barrier"]
        assert len(barriers) == 2, device.touched
        assert barriers[0].endswith(".tmp")
        assert not barriers[1].endswith(".tmp")


# --- A47: the classification travels in the details, not in the class -------------------------


def test_a_transient_barrier_failure_is_ridden_out(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    device.failure = transient_barrier_failure()
    device.fail_next["durable_barrier"] = 1
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    assert lease.epoch == 1
    assert device.fail_next["durable_barrier"] == 0
    assert clock.slept, "the retry did not back off"
    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert owned_by(published.owner_id, "p1-aaaa")


def test_a_transient_barrier_failure_does_not_cost_a_reader_its_registration(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    device.failure = transient_barrier_failure()
    device.fail_next["durable_barrier"] = 1
    handle = coordinator.register_reader(7)
    assert coordinator.reader_horizon() == 7
    assert handle.snapshot_lsn == 7


def test_a_transient_barrier_failure_does_not_cost_a_writer_its_heartbeat(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    device.failure = transient_barrier_failure()
    device.fail_next["durable_barrier"] = 1
    renewed = coordinator.renew_lease(lease)
    assert renewed.heartbeat_seq == lease.heartbeat_seq + 1


def test_a_permanent_barrier_failure_keeps_its_class(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # The caller counts exactly this type into oktografx_barrier_failures_total (A25/A28), so
    # wrapping it in a storage error would keep that metric silent for the case it exists for.
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    device.failure = permanent_barrier_failure()
    device.fail_next["durable_barrier"] = 10_000
    with pytest.raises(GrafxDurabilityBarrierFailed) as failure:
        coordinator.acquire_writer_lease(timeout=1.0)
    assert not isinstance(failure.value, GrafxStorageError)
    assert failure.value.details["reason"] == "fsync failure"
    assert device.fail_next["durable_barrier"] == 9_999, "a permanent failure was retried"


def test_an_endless_transient_barrier_failure_keeps_its_class_too(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    device.failure = transient_barrier_failure()
    device.fail_next["durable_barrier"] = 10_000
    with pytest.raises(GrafxDurabilityBarrierFailed) as failure:
        coordinator.acquire_writer_lease(timeout=1.0)
    assert failure.value.details["attempts"] >= 3
    assert device.fail_next["durable_barrier"] <= 9_996


def test_a_full_device_is_still_reported_at_once(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # A full device is an answer, not an obstacle, and it has to reach the caller as itself.
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    device.failure = GrafxDeviceFull("The device is full.")
    device.fail_next["append_log"] = 5
    with pytest.raises(GrafxDeviceFull):
        coordinator.acquire_writer_lease(timeout=1.0)
    assert device.fail_next["append_log"] == 4, "an answer was retried"
