"""Every device failure reaches the caller as the class the device chose (A47, A66).

The publish path learned this first: a barrier failure folded into one class by A28 carries its
classification in ``details["retryable"]``, and wrapping it would both lie about retryability and
keep ``oktografx_barrier_failures_total`` silent. The read path and the listing path make exactly
the same promise and are exercised here, because a rule that holds at one of three sites is not a
rule -- it is an accident that has not been tested yet.

The sharpest case is ``GrafxSchemaVersionMismatch``: permanent by section 2, and the typed refusal
the lease format docstring calls "never a guess". Reported as a retryable storage error, an
A47-obedient caller loops for ever on a condition that will never improve.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice, HookStorageDevice, ManualClock
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxSchemaVersionMismatch,
    GrafxStorageError,
    GrafxUnsupportedOperation,
)

READ_SITES: tuple[str, ...] = ("current_epoch", "detect_dead_owner", "acquire_writer_lease")
"""Public methods whose first act is to read the lease record."""


def _call(coordinator: object, method: str) -> object:
    """Invoke one of the read-path entry points with arguments it accepts."""
    arguments: dict[str, dict[str, object]] = {
        "detect_dead_owner": {"stall_threshold": 5.0},
        "acquire_writer_lease": {"timeout": 0.0},
    }
    return getattr(coordinator, method)(**arguments.get(method, {}))


def _stamp_version(raw: bytes, version: int, *, header_offset: int = 8) -> bytes:
    """Return the record with a different format version and a checksum that still matches.

    Damaged bytes and a newer format are different findings, and only a record whose checksum is
    correct proves the decoder refused on the version rather than on the damage.
    """
    body = bytearray(raw)
    struct.pack_into("<H", body, header_offset, version)
    struct.pack_into("<I", body, len(body) - 4, zlib.crc32(bytes(body[:-4])) & 0xFFFFFFFF)
    return bytes(body)


# --- the read path -----------------------------------------------------------------------------


@pytest.mark.parametrize("method", READ_SITES)
def test_a_newer_lease_format_reaches_the_caller_as_a_version_mismatch(
    make_coordinator: CoordinatorFactory, database_root: Path, method: str
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    coordinator.acquire_writer_lease(timeout=1.0)
    lease_file = database_root / "control" / "writer.lease"
    lease_file.write_bytes(_stamp_version(lease_file.read_bytes(), 2))

    reader = make_coordinator(owner_id="p2-bbbb", monotonic_origin=40.0)
    with pytest.raises(GrafxSchemaVersionMismatch) as failure:
        _call(reader, method)
    assert failure.value.retryable is False
    assert failure.value.details["found_version"] == 2
    assert not isinstance(failure.value, GrafxStorageError)


def test_a_newer_reader_format_reaches_the_caller_as_a_version_mismatch(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    handle = coordinator.register_reader(5)
    registration = database_root / "control" / "readers" / f"{handle.reader_id}.reader"
    registration.write_bytes(_stamp_version(registration.read_bytes(), 2))

    observer = make_coordinator(owner_id="p2-bbbb", monotonic_origin=40.0)
    with pytest.raises(GrafxSchemaVersionMismatch) as failure:
        observer.reader_horizon()
    assert failure.value.retryable is False


@pytest.mark.parametrize("operation", ["exists", "log_size", "read_log"])
def test_a_permanent_device_refusal_on_the_read_path_keeps_its_class(
    make_coordinator: CoordinatorFactory, database_root: Path, operation: str
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    coordinator.acquire_writer_lease(timeout=1.0)
    device.failure = GrafxUnsupportedOperation("This device cannot serve that name.")
    device.fail_next[operation] = 10_000
    with pytest.raises(GrafxUnsupportedOperation):
        coordinator.current_epoch()
    assert device.fail_next[operation] == 9_999, "a permanent refusal was retried"


# --- the listing path ---------------------------------------------------------------------------


def test_a_permanent_device_refusal_on_the_listing_keeps_its_class(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    coordinator.register_reader(5)
    device.failure = GrafxUnsupportedOperation("This device cannot list that prefix.")
    device.fail_next["list_files"] = 10_000
    with pytest.raises(GrafxUnsupportedOperation):
        coordinator.reader_horizon()
    assert device.fail_next["list_files"] == 9_999, "a permanent refusal was retried"


@pytest.mark.parametrize("carrier", ["storage", "other-class"])
def test_a_transient_device_failure_on_the_listing_is_still_ridden_out(
    make_coordinator: CoordinatorFactory, database_root: Path, carrier: str
) -> None:
    """The classification decides, and the class does not -- which is all A47 says.

    The second case is the one that matters. A28 fixed the CLASS of a whole family of failures
    and moved the classification into the details, so a retry arm that admits only the storage
    class rides out a transient condition when it happens to wear that class and gives up on the
    identical condition when the port contract dictated a different one. The listing path is a
    third site making this same promise, and a promise kept at two sites out of three is not a
    promise.
    """
    from okto_grafx.domain.errors import GrafxDurabilityBarrierFailed

    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    coordinator.register_reader(5)
    if carrier == "storage":
        transient: GrafxError = GrafxStorageError("A scanner holds the directory.", retryable=True)
    else:
        transient = GrafxDurabilityBarrierFailed("A scanner holds the directory.", reason="sharing")
    transient.details["retryable"] = True
    device.failure = transient
    device.fail_next["list_files"] = 2
    assert coordinator.reader_horizon() == 5
    assert device.fail_next["list_files"] == 0


# --- the rule itself, stated once and checked at every site -------------------------------------


@pytest.mark.parametrize(
    ("operation", "act"),
    [
        ("exists", "current_epoch"),
        ("log_size", "current_epoch"),
        ("list_files", "reader_horizon"),
        ("atomic_replace", "register_reader"),
        ("durable_barrier", "register_reader"),
    ],
)
def test_no_site_ever_reports_a_refusal_it_would_not_retry_as_retryable(
    make_coordinator: CoordinatorFactory, database_root: Path, operation: str, act: str
) -> None:
    """A refusal the adapter will not retry must never tell its caller to retry.

    That contradiction is the whole defect: the adapter decides the condition is permanent, stops
    at one attempt, and then hands the caller a flag that says to come back. A caller obeying A47
    reads the flag, not the adapter's mind.
    """
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    coordinator.acquire_writer_lease(timeout=1.0)
    permanent = GrafxUnsupportedOperation("This device refuses that operation for good.")
    device.failure = permanent
    device.fail_next[operation] = 10_000
    with pytest.raises(GrafxError) as raised:
        if act == "register_reader":
            coordinator.register_reader(9)
        else:
            getattr(coordinator, act)()
    escaped = raised.value
    attempts_made = 10_000 - device.fail_next[operation]
    # Unconditional on purpose (A48): a guard like "if attempts_made == 1" lets any change that
    # makes the adapter retry skip the very assertion this test is named for. The invariant holds
    # either way -- a refusal retried was classified retryable, one refused once was not -- so it
    # is stated as a correspondence rather than as a special case.
    assert escaped.retryable is (attempts_made > 1), (
        f"{escaped!r} was attempted {attempts_made} time(s) and reports retryable="
        f"{escaped.retryable}"
    )
    assert escaped.details.get("retryable", escaped.retryable) is escaped.retryable


def test_damaged_bytes_are_still_corruption_at_every_read_site(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # The other half of the classification: damage keeps ITS class too, because recovery turns
    # corruption into truncation, quarantine and forensic ledger entries.
    coordinator = make_coordinator(owner_id="p1-aaaa")
    coordinator.acquire_writer_lease(timeout=1.0)
    lease_file = database_root / "control" / "writer.lease"
    damaged = bytearray(lease_file.read_bytes())
    damaged[30] ^= 0xFF
    lease_file.write_bytes(bytes(damaged))
    with pytest.raises(GrafxCorruptionDetected):
        coordinator.current_epoch()


# --- D2: derived state may not be used without revalidating what it stands for (A63) -----------


def test_a_validate_only_caller_honours_a_restored_control_plane(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """The commit path of section 8.5 validates and never reads anything else.

    Steps 2 and 3.1 call ``validate_epoch`` and nothing more, so any shortcut that answers from
    remembered state has to be able to notice that the state it stands for has changed. C6
    restores the control plane from quarantine, which is the sanctioned way an epoch legitimately
    goes down; a guard that refuses from memory and never re-reads wedges the writer for the life
    of the process.
    """
    from okto_grafx.domain.errors import GrafxStaleEpoch

    lease_file = database_root / "control" / "writer.lease"
    survivor = make_coordinator(owner_id="p1-survivor")
    held = survivor.acquire_writer_lease(timeout=1.0)
    backup = lease_file.read_bytes()

    for index, name in enumerate(("p2-second", "p3-third"), start=2):
        clock = ManualClock(monotonic=1_000.0 * index)
        other = make_coordinator(owner_id=name, clock=clock)
        assert other.detect_dead_owner(stall_threshold=5.0) is None
        clock.advance(6.0)
        assert other.detect_dead_owner(stall_threshold=5.0) is not None
        assert other.takeover().epoch == index

    with pytest.raises(GrafxStaleEpoch):
        survivor.validate_epoch(held.epoch)

    lease_file.write_bytes(backup)  # the restore C6 performs
    survivor.validate_epoch(held.epoch)
    survivor.validate_epoch(held.epoch)


def test_a_stale_holder_is_refused_on_every_call_without_writing(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    from okto_grafx.domain.errors import GrafxStaleEpoch

    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    stale = make_coordinator(owner_id="p1-stale", storage=device)
    lease = stale.acquire_writer_lease(timeout=1.0)
    clock = ManualClock(monotonic=5_000.0)
    successor = make_coordinator(owner_id="p2-next", clock=clock)
    assert successor.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert successor.detect_dead_owner(stall_threshold=5.0) is not None
    successor.takeover()

    device.reset()
    device.forbid_writes = True
    for _attempt in range(10):
        with pytest.raises(GrafxStaleEpoch):
            stale.validate_epoch(lease.epoch)
    assert device.forbid_writes is True
    assert device.calls, "the guard reads the published record rather than trusting memory"
