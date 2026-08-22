"""Regressions for the minor defects of the critic report.

Same rule as its sibling module: each test failed before its fix. They cover the publish path
under a sharing violation, the epoch ceiling after a control-plane restore, stray temporary
files, the bounds of the two identifier doors, the metric of the takeover path, and the typed
classification of a lock file that cannot be opened at all.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import (
    DirectoryStorageDevice,
    HookStorageDevice,
    ManualClock,
    RecordingMetricsSink,
    sharing_violation,
)
from okto_grafx.adapters.coordination_local import (
    LEASE_WAIT_METRIC,
    LocalProcessCoordinator,
    LeaseRecord,
    decode_lease_record,
    encode_lease_record,
)
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxStorageError,
)

# --- the publish path survives a sharing violation ---------------------------------------------


def test_a_sharing_violation_during_publish_is_retried(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # On Windows another process holding the lease file open for reading makes os.replace fail
    # with a sharing violation. It is transient by nature, and the control plane must ride it out
    # rather than let a raw OSError escape a public method.
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    device.fail_next["atomic_replace"] = 3
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    assert lease.epoch == 1
    assert device.fail_next["atomic_replace"] == 0
    assert clock.slept, "the retry did not back off at all"
    assert decode_lease_record(
        (database_root / "control" / "writer.lease").read_bytes()
    ).owner_id.startswith("p1-aaaa-")


def test_an_endless_sharing_violation_becomes_a_typed_storage_error(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    device.fail_next["atomic_replace"] = 10_000
    with pytest.raises(GrafxStorageError) as failure:
        coordinator.acquire_writer_lease(timeout=1.0)
    assert failure.value.retryable is True
    assert failure.value.details["attempts"] >= 3
    assert failure.value.details["file"].endswith("writer.lease")


def test_a_reader_registration_survives_a_sharing_violation(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    device.fail_next["atomic_replace"] = 2
    handle = coordinator.register_reader(77)
    assert coordinator.reader_horizon() == 77
    assert handle.snapshot_lsn == 77


def test_a_full_device_during_publish_is_not_retried(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # Only access failures are transient. A full device and a failed barrier are answers, and
    # retrying them would turn a clear refusal into a delayed one.
    from okto_grafx.domain.errors import GrafxDeviceFull

    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    device.fail_next["append_log"] = 5
    device.failure = GrafxDeviceFull("The device is full.")
    with pytest.raises(GrafxDeviceFull):
        coordinator.acquire_writer_lease(timeout=1.0)
    assert device.fail_next["append_log"] == 4, "the refusal was retried"


# --- the epoch ceiling must not outlive the control plane it summarises ------------------------


def test_a_restored_control_plane_does_not_wedge_the_epoch_guard(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # C6 restores the control plane from quarantine under a live coordinator, so the published
    # epoch can legitimately go down. A ceiling that only ever rises makes acquire_writer_lease
    # hand back a lease that its own validate_epoch rejects for the rest of the process life.
    lease_file = database_root / "control" / "writer.lease"
    survivor = make_coordinator(owner_id="p1-survivor")
    first = survivor.acquire_writer_lease(timeout=1.0)
    backup = lease_file.read_bytes()
    assert first.epoch == 1

    for index, name in enumerate(("p2-second", "p3-third"), start=2):
        clock = ManualClock(monotonic=1_000.0 * index)
        other = make_coordinator(owner_id=name, clock=clock)
        assert other.detect_dead_owner(stall_threshold=5.0) is None
        clock.advance(6.0)
        assert other.detect_dead_owner(stall_threshold=5.0) is not None
        assert other.takeover().epoch == index
    assert survivor.current_epoch() == 3

    lease_file.write_bytes(backup)  # the restore C6 performs
    restored = survivor.acquire_writer_lease(timeout=1.0)
    survivor.validate_epoch(restored.epoch)
    assert survivor.current_epoch() == restored.epoch


# --- temporary files of crashed participants are bounded ---------------------------------------


def test_temporary_files_of_dead_participants_do_not_accumulate(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    control = database_root / "control"
    readers = control / "readers"
    readers.mkdir(parents=True, exist_ok=True)
    zombie = make_coordinator(owner_id="p0-dead", monotonic_origin=500.0)
    zombie.acquire_writer_lease(timeout=1.0)
    live = make_coordinator(owner_id="p9-live", monotonic_origin=77.0)
    live_handle = live.register_reader(500)

    strays = []
    for index in range(5):
        owner = f"p{index}-crashed"
        for path in (
            control / f"writer.lease.{owner}.tmp",
            readers / f"{owner}-r0001.reader.{owner}.tmp",
        ):
            path.write_bytes(b"half a record")
            strays.append(path)
    assert all(path.exists() for path in strays)

    clock = ManualClock(monotonic=1_000.0)
    survivor = make_coordinator(owner_id="p8-survivor", clock=clock)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is not None
    survivor.takeover()
    assert survivor.reader_horizon() == 500

    # The lease temporaries go with the takeover, which happens inside the lease section where no
    # live participant can hold one. The reader temporaries have no such section -- readers never
    # block writers -- so they go by age instead: a first pass only records when they were seen.
    lease_strays = [path for path in strays if path.name.startswith("writer.lease")]
    reader_strays = [path for path in strays if path not in lease_strays]
    assert [path.name for path in lease_strays if path.exists()] == []
    assert all(path.exists() for path in reader_strays)
    clock.advance(16.0)
    live.refresh_reader(live_handle)  # a live reader proves it is alive; a stray cannot
    assert survivor.reader_horizon() == 500
    assert [path.name for path in reader_strays if path.exists()] == []
    assert (readers / f"{live_handle.reader_id}.reader").exists(), "a live reader was not touched"


# --- the two identifier doors agree ------------------------------------------------------------


def test_a_snapshot_above_the_record_range_is_refused(
    make_coordinator: CoordinatorFactory
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    with pytest.raises(GrafxConfigurationError):
        coordinator.register_reader(2**64)
    with pytest.raises(GrafxConfigurationError):
        coordinator.validate_epoch(2**64)


def test_an_owner_identifier_that_would_break_reader_names_is_refused(
    make_coordinator: CoordinatorFactory
) -> None:
    with pytest.raises(GrafxConfigurationError):
        make_coordinator(owner_id="p" + "a" * 95)


def test_the_longest_accepted_owner_identifier_still_registers_readers(
    make_coordinator: CoordinatorFactory
) -> None:
    coordinator = make_coordinator(owner_id="p" + "a" * 75)
    handle = coordinator.register_reader(9)
    assert coordinator.reader_horizon() == 9
    coordinator.refresh_reader(handle)
    coordinator.unregister_reader(handle)


# --- the takeover path is measured -------------------------------------------------------------


def test_the_takeover_path_reports_its_wait(
    make_coordinator: CoordinatorFactory, metrics: RecordingMetricsSink
) -> None:
    # TS-7 measures the wait of the surviving process in oktografx_lease_wait_seconds, and the
    # explicit takeover is the literal path of AC-7.
    zombie = make_coordinator(owner_id="p0-dead", monotonic_origin=500.0)
    zombie.acquire_writer_lease(timeout=1.0)
    clock = ManualClock(monotonic=1_000.0)
    survivor = make_coordinator(owner_id="p1-alive", clock=clock, metrics=metrics)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is not None
    assert survivor.takeover().epoch == 2
    assert metrics.outcomes(LEASE_WAIT_METRIC) == ["takeover"]


def test_a_refused_takeover_reports_the_wait_it_spent(
    make_coordinator: CoordinatorFactory, metrics: RecordingMetricsSink
) -> None:
    owner = make_coordinator(owner_id="p0-owner")
    owner.acquire_writer_lease(timeout=1.0)
    contender = make_coordinator(owner_id="p1-alive", monotonic_origin=3.0, metrics=metrics)
    contender.detect_dead_owner(stall_threshold=5.0)
    with pytest.raises(Exception) as failure:
        contender.takeover()
    assert failure.value.code == "lease_timeout"
    assert metrics.outcomes(LEASE_WAIT_METRIC) == ["timeout"]


# --- a lock file that cannot be opened is a storage failure, not a phantom holder ---------------


def test_a_lock_file_that_never_opens_is_a_typed_storage_error(
    make_coordinator: CoordinatorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    real_open = os.open

    def refuse(path: object, flags: int, mode: int = 0o777, *, dir_fd: object = None) -> int:
        if str(path).endswith(".lock"):
            raise sharing_violation(str(path))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, poll_interval=0.25)
    monkeypatch.setattr(os, "open", refuse)
    with pytest.raises(GrafxStorageError) as failure:
        with coordinator.exclusive("commit", timeout=1.0):
            pass
    monkeypatch.undo()
    assert failure.value.retryable is True
    assert failure.value.details["attempts"] >= 2
    assert failure.value.details["section"] == "commit"


def test_a_lock_file_on_a_path_that_cannot_exist_is_permanent(
    make_coordinator: CoordinatorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    real_open = os.open

    def refuse(path: object, flags: int, mode: int = 0o777, *, dir_fd: object = None) -> int:
        if str(path).endswith(".lock"):
            raise FileNotFoundError(2, "The system cannot find the path specified.")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    coordinator = make_coordinator(owner_id="p1-aaaa")
    monkeypatch.setattr(os, "open", refuse)
    with pytest.raises(GrafxStorageError) as failure:
        with coordinator.exclusive("commit", timeout=0.1):
            pass
    monkeypatch.undo()
    assert failure.value.retryable is False


# --- the decoder accepts exactly what the encoder can write ------------------------------------


def _reseal(raw: bytes) -> bytes:
    """Return the record with its checksum recomputed, so only the field under test is wrong."""
    body = bytearray(raw)
    checksum = zlib.crc32(bytes(body[:-4])) & 0xFFFFFFFF
    struct.pack_into("<I", body, len(body) - 4, checksum)
    return bytes(body)


@pytest.mark.parametrize("forged", ["a/b", "a\\b", "a\x00b", "a\x7fb", "A-b", "..x"])
def test_a_stored_identifier_the_encoder_would_refuse_reads_as_corruption(forged: str) -> None:
    valid = encode_lease_record(
        LeaseRecord(
            owner_id="abcdef",
            epoch=1,
            heartbeat_seq=1,
            ttl_seconds=5.0,
            wall_stamp=0.0,
            held=True,
            superseded_epoch=0,
        )
    )
    # The encoder refuses this identifier, which is the whole asymmetry under test: what cannot
    # be written must not be accepted on the way back in.
    with pytest.raises(GrafxConfigurationError):
        encode_lease_record(
            LeaseRecord(
                owner_id=forged.ljust(6, "z"),
                epoch=1,
                heartbeat_seq=1,
                ttl_seconds=5.0,
                wall_stamp=0.0,
                held=True,
                superseded_epoch=0,
            )
        )
    body = bytearray(valid)
    body[64 : 64 + 6] = forged.ljust(6, "z").encode("ascii")
    with pytest.raises(GrafxCorruptionDetected):
        decode_lease_record(_reseal(bytes(body)))


def test_the_round_trip_of_a_legitimate_identifier_still_works() -> None:
    record = LeaseRecord(
        owner_id="p1-abc.def_ghi",
        epoch=3,
        heartbeat_seq=9,
        ttl_seconds=5.0,
        wall_stamp=1.5,
        held=True,
        superseded_epoch=2,
    )
    assert decode_lease_record(encode_lease_record(record)) == record


def test_the_adapter_is_still_the_port(make_coordinator: CoordinatorFactory) -> None:
    from okto_grafx.domain.ports.coordination import ProcessCoordinator

    assert isinstance(make_coordinator(owner_id="p1-aaaa"), ProcessCoordinator)
    assert isinstance(make_coordinator(owner_id="p2-bbbb"), LocalProcessCoordinator)


def test_an_unreadable_control_file_is_a_storage_error_not_corruption(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # A11-revised: corruption is reserved for damaged bytes, because in this engine it drives
    # truncation, quarantine and forensic ledger entries. Not being allowed to open a file says
    # nothing at all about what is inside it.
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    coordinator.acquire_writer_lease(timeout=1.0)
    device.fail_next["read_log"] = 10_000
    with pytest.raises(GrafxStorageError) as failure:
        coordinator.current_epoch()
    assert not isinstance(failure.value, GrafxCorruptionDetected)
    assert failure.value.details["attempts"] >= 2
    assert failure.value.details["file"].endswith("writer.lease")


def test_a_transient_read_failure_is_ridden_out(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    device.fail_next["read_log"] = 2
    assert coordinator.current_epoch() == lease.epoch == 1


def test_damaged_bytes_are_still_reported_as_corruption(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    coordinator.acquire_writer_lease(timeout=1.0)
    target = database_root / "control" / "writer.lease"
    damaged = bytearray(target.read_bytes())
    damaged[20] ^= 0xFF
    target.write_bytes(bytes(damaged))
    with pytest.raises(GrafxCorruptionDetected):
        coordinator.current_epoch()
