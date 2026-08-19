"""Durability, the platform branch, and the structural absence of a positional write (C2).

FR-5 asks for something stronger than a passing test: no code path may write past the end of a
file, so the beyond end of file zero fill signature cannot be produced at all. The proof here is
behavioural: every public call of the device is exercised and only the two calls that are meant
to grow a file are allowed to change its size.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from okto_grafx.adapters import storage_local
from okto_grafx.adapters.storage_local import (
    IS_WINDOWS,
    MAX_RETRY_SLEEP_SECONDS,
    RETRY_ATTEMPTS,
    WRITE_CHUNK_BYTES,
    LocalStorageDevice,
)
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxDurabilityBarrierFailed,
    GrafxError,
    GrafxStorageError,
)

PAGE_SIZE: int = 512
"""Kept equal to the page size of the fixtures; a drift fails the device shape test."""

SEGMENT: str = "wal/000000000001.wal"
HEAP: str = "heap.dat"


# --- durability ---------------------------------------------------------------------------


def test_a_barrier_without_a_name_flushes_every_open_file(local_device: LocalStorageDevice) -> None:
    flushed: list[int] = []
    real_fsync = os.fsync

    def _recording_fsync(descriptor: int) -> None:
        flushed.append(descriptor)
        real_fsync(descriptor)

    local_device.create(SEGMENT)
    local_device.create(HEAP)
    local_device.append_log(SEGMENT, b"record")
    local_device.allocate(HEAP)
    original = storage_local.os.fsync
    storage_local.os.fsync = _recording_fsync
    try:
        local_device.durable_barrier()
    finally:
        storage_local.os.fsync = original
    # Two files, plus their directories on POSIX.
    assert len(flushed) >= 2


def test_a_barrier_that_the_platform_refuses_is_a_typed_failure(local_device: LocalStorageDevice) -> None:
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"record")

    def _failing_fsync(descriptor: int) -> None:
        raise OSError(5, "The device stopped answering.")

    original = storage_local.os.fsync
    storage_local.os.fsync = _failing_fsync
    try:
        with pytest.raises(GrafxDurabilityBarrierFailed) as raised:
            local_device.durable_barrier(SEGMENT)
    finally:
        storage_local.os.fsync = original
    assert raised.value.details["reason"] in {"fsync_failed", "directory_fsync_failed"}
    assert raised.value.retryable is False


@pytest.mark.platform_specific
@pytest.mark.skipif(IS_WINDOWS, reason="Only POSIX exposes a directory handle to flush.")
def test_a_barrier_flushes_the_parent_directory_on_posix(local_device: LocalStorageDevice) -> None:
    # Counterpart: test_a_barrier_flushes_no_directory_on_windows.
    # A file that was just created is only reachable after its directory entry is durable.
    kinds: list[bool] = []
    real_fsync = os.fsync

    def _recording_fsync(descriptor: int) -> None:
        kinds.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"record")
    original = storage_local.os.fsync
    storage_local.os.fsync = _recording_fsync
    try:
        local_device.durable_barrier(SEGMENT)
    finally:
        storage_local.os.fsync = original
    assert True in kinds, "the barrier did not flush any directory"
    assert False in kinds, "the barrier did not flush the file itself"


@pytest.mark.platform_specific
@pytest.mark.skipif(not IS_WINDOWS, reason="Windows has no directory handle to flush.")
def test_a_barrier_flushes_no_directory_on_windows(local_device: LocalStorageDevice) -> None:
    # Counterpart: test_a_barrier_flushes_the_parent_directory_on_posix.
    kinds: list[bool] = []
    real_fsync = os.fsync

    def _recording_fsync(descriptor: int) -> None:
        kinds.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"record")
    original = storage_local.os.fsync
    storage_local.os.fsync = _recording_fsync
    try:
        local_device.durable_barrier(SEGMENT)
    finally:
        storage_local.os.fsync = original
    assert kinds == [False], "Windows must flush the file and nothing else"


def test_the_bytes_of_a_barrier_survive_a_reopen(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as first:
        first.create(HEAP)
        first.allocate(HEAP, 2)
        first.write_page(HEAP, 1, bytes([0x5C]) * PAGE_SIZE)
        first.durable_barrier(HEAP)
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as second:
        assert second.page_count(HEAP) == 2
        assert second.read_page(HEAP, 1) == bytes([0x5C]) * PAGE_SIZE


def test_a_barrier_without_a_name_flushes_the_files_the_cache_evicted(tmp_path: Path) -> None:
    # M7: a descriptor evicted by the cache still owes its bytes to the next barrier. Tracking
    # the unflushed files, rather than the cached ones, is what keeps the promise.
    root = tmp_path / "db"
    with LocalStorageDevice(root, page_size=PAGE_SIZE, max_open_files=2) as device:
        names = [f"wal/{index:012d}.wal" for index in range(5)]
        for name in names:
            device.create(name)
            device.append_log(name, b"acknowledged")
        expected = {os.stat(root / "wal" / f"{index:012d}.wal").st_ino for index in range(5)}
        flushed: set[int] = set()
        real_fsync = os.fsync

        def _recording_fsync(descriptor: int) -> None:
            information = os.fstat(descriptor)
            if not stat.S_ISDIR(information.st_mode):
                flushed.add(information.st_ino)
            real_fsync(descriptor)

        original = storage_local.os.fsync
        storage_local.os.fsync = _recording_fsync
        try:
            device.durable_barrier()
        finally:
            storage_local.os.fsync = original
        assert expected <= flushed, "a file with unflushed writes was skipped by the barrier"


def test_a_barrier_stops_owing_a_file_it_already_flushed(local_device: LocalStorageDevice) -> None:
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"record")
    local_device.durable_barrier(SEGMENT)
    flushed: list[int] = []
    real_fsync = os.fsync

    def _recording_fsync(descriptor: int) -> None:
        flushed.append(descriptor)
        real_fsync(descriptor)

    original = storage_local.os.fsync
    storage_local.os.fsync = _recording_fsync
    try:
        local_device.durable_barrier()
    finally:
        storage_local.os.fsync = original
    # The file is still cached, so it is flushed again; what matters is that nothing was owed.
    assert len(flushed) <= 2


# --- an access failure is not corruption (A11-revised) -----------------------------------------


def test_an_access_failure_after_the_retries_is_a_retryable_storage_error(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M3: recovery turns corruption into truncation, quarantine and a forensic ledger entry.
    # An antivirus holding a handle must never be able to trigger that.
    local_device.create(SEGMENT)
    local_device.close()
    device = LocalStorageDevice(Path(local_device.root), page_size=PAGE_SIZE)
    monkeypatch.setattr(storage_local.time, "sleep", lambda seconds: None)

    def _refusing_open(path: str, *, create_new: bool) -> int:
        raise PermissionError(errno.EACCES, "The process cannot access the file.")

    monkeypatch.setattr(storage_local, "_open_descriptor", _refusing_open)
    try:
        for probe in (
            lambda: device.log_size(SEGMENT),
            lambda: device.read_log(SEGMENT, 0, 4),
            lambda: device.append_log(SEGMENT, b"x"),
            lambda: device.page_count(SEGMENT),
        ):
            with pytest.raises(GrafxStorageError) as raised:
                probe()
            assert not isinstance(raised.value, GrafxCorruptionDetected)
            assert raised.value.code == "storage_error"
            assert raised.value.retryable is True
            assert raised.value.details["errno"] == errno.EACCES
            assert raised.value.details["attempts"] == RETRY_ATTEMPTS
            assert "winerror" in raised.value.details
    finally:
        monkeypatch.undo()
        device.close()


def test_a_failing_write_call_is_a_storage_error_too(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"first")

    def _refusing_write(descriptor: int, payload: Any) -> int:
        raise PermissionError(errno.EACCES, "The process cannot access the file.")

    monkeypatch.setattr(storage_local.os, "write", _refusing_write)
    with pytest.raises(GrafxStorageError) as raised:
        local_device.append_log(SEGMENT, b"second")
    monkeypatch.undo()
    assert raised.value.retryable is True
    assert local_device.log_size(SEGMENT) == 5


# --- more than one thread ------------------------------------------------------------------------


def test_concurrent_appends_never_lose_a_byte(device: Any) -> None:
    # No clause asks for thread safety, but bytes may never vanish without an error, so the
    # device takes its own lock rather than documenting a restriction nobody would honour.
    device.create(SEGMENT)
    payload = b"0123456789"
    rounds = 150
    workers = 4
    failures: list[BaseException] = []

    def _append() -> None:
        try:
            for _ in range(rounds):
                device.append_log(SEGMENT, payload)
        except BaseException as failure:  # pragma: no cover - only on a real defect
            failures.append(failure)

    threads = [threading.Thread(target=_append) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert failures == []
    assert device.log_size(SEGMENT) == workers * rounds * len(payload)
    stored = device.read_log(SEGMENT, 0, device.log_size(SEGMENT))
    assert stored == payload * (workers * rounds)


# --- no positional write ---------------------------------------------------------------------


def test_only_allocate_and_append_can_ever_grow_a_file(device: Any) -> None:
    # FR-5: there is no positional write primitive, so no call other than these two may add a
    # byte to a file. A hole past the end of the file is therefore not reachable.
    device.create(HEAP)
    device.allocate(HEAP, 2)
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"record")
    sizes = (device.file_size(HEAP), device.file_size(SEGMENT))
    probes = (
        lambda: device.exists(HEAP),
        lambda: device.list_files(),
        lambda: device.page_count(HEAP),
        lambda: device.read_page(HEAP, 0),
        lambda: device.write_page(HEAP, 1, bytes([0x22]) * PAGE_SIZE),
        lambda: device.read_log(SEGMENT, 0, 6),
        lambda: device.log_size(SEGMENT),
        lambda: device.durable_barrier(HEAP),
        lambda: device.durable_barrier(),
        lambda: device.create(HEAP, exclusive=False),
        lambda: device.append_log(SEGMENT, b""),
    )
    for probe in probes:
        probe()
        assert (device.file_size(HEAP), device.file_size(SEGMENT)) == sizes


def test_a_page_write_far_past_the_end_never_extends_the_file(device: Any) -> None:
    device.create(HEAP)
    device.allocate(HEAP)
    for index in (1, 2, 1000, 1 << 20):
        with pytest.raises(GrafxCorruptionDetected):
            device.write_page(HEAP, index, bytes([0xFF]) * PAGE_SIZE)
    assert device.file_size(HEAP) == PAGE_SIZE
    assert device.read_page(HEAP, 0) == bytes(PAGE_SIZE)


def test_a_page_write_that_would_extend_the_file_is_cut_back_and_reported(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The last line of defence of FR-5: even if the offset the device positioned at stopped
    # being inside the file, the write is not allowed to leave the file longer than it was.
    local_device.create(HEAP)
    local_device.allocate(HEAP, 2)
    real_lseek = os.lseek
    hijacked = {"left": 1}

    def _drifting_lseek(descriptor: int, position: int, whence: int) -> int:
        if hijacked["left"] > 0 and whence == os.SEEK_SET:
            hijacked["left"] -= 1
            return real_lseek(descriptor, 0, os.SEEK_END)
        return real_lseek(descriptor, position, whence)

    monkeypatch.setattr(storage_local.os, "lseek", _drifting_lseek)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        local_device.write_page(HEAP, 0, bytes([0x33]) * PAGE_SIZE)
    monkeypatch.undo()
    assert raised.value.details["reason"] == "page_write_grew_file"
    assert local_device.file_size(HEAP) == 2 * PAGE_SIZE
    assert local_device.page_count(HEAP) == 2


def test_a_large_allocation_is_written_in_bounded_blocks(device: Any) -> None:
    pages = (WRITE_CHUNK_BYTES // PAGE_SIZE) + 7
    device.create(HEAP)
    assert device.allocate(HEAP, pages) == 0
    assert device.page_count(HEAP) == pages
    assert device.read_page(HEAP, pages - 1) == bytes(PAGE_SIZE)
    device.write_page(HEAP, pages - 1, bytes([0x9E]) * PAGE_SIZE)
    assert device.read_page(HEAP, pages - 1) == bytes([0x9E]) * PAGE_SIZE
    assert device.file_size(HEAP) == pages * PAGE_SIZE


# --- descriptors and retries -------------------------------------------------------------------


def test_the_descriptor_cache_is_bounded_and_loses_nothing(tmp_path: Path) -> None:
    with LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE, max_open_files=4) as device:
        names = [f"wal/{index:012d}.wal" for index in range(40)]
        for index, name in enumerate(names):
            device.create(name)
            device.append_log(name, f"segment-{index}".encode("ascii"))
        for index, name in enumerate(names):
            assert device.read_log(name, 0, 32) == f"segment-{index}".encode("ascii")
        assert len(device.list_files("wal/")) == 40


def test_a_transient_refusal_is_retried_and_then_succeeds(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_device.create(SEGMENT)
    local_device.close()
    device = LocalStorageDevice(Path(local_device.root), page_size=PAGE_SIZE)
    refusals = {"left": 2}
    real_open = storage_local._open_descriptor
    naps: list[float] = []

    def _flaky_open(path: str, *, create_new: bool) -> int:
        if str(path).endswith(".wal") and refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError(13, "The file is in use by another process.")
        return real_open(path, create_new=create_new)

    monkeypatch.setattr(storage_local, "_open_descriptor", _flaky_open)
    monkeypatch.setattr(storage_local.time, "sleep", naps.append)
    try:
        assert device.log_size(SEGMENT) == 0
    finally:
        device.close()
    assert refusals["left"] == 0
    assert len(naps) == 2
    assert all(nap <= MAX_RETRY_SLEEP_SECONDS for nap in naps)


def test_a_permanent_refusal_gives_up_after_the_bounded_number_of_tries(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_device.create(SEGMENT)
    local_device.close()
    device = LocalStorageDevice(Path(local_device.root), page_size=PAGE_SIZE)
    attempts: list[str] = []
    real_open = storage_local._open_descriptor
    naps: list[float] = []

    def _refusing_open(path: str, *, create_new: bool) -> int:
        if str(path).endswith(".wal"):
            attempts.append(str(path))
            raise PermissionError(13, "The file is in use by another process.")
        return real_open(path, create_new=create_new)

    monkeypatch.setattr(storage_local, "_open_descriptor", _refusing_open)
    monkeypatch.setattr(storage_local.time, "sleep", naps.append)
    try:
        with pytest.raises(GrafxError) as raised:
            device.log_size(SEGMENT)
    finally:
        device.close()
    assert len(attempts) == RETRY_ATTEMPTS
    assert raised.value.details["attempts"] == RETRY_ATTEMPTS
    assert all(nap <= MAX_RETRY_SLEEP_SECONDS for nap in naps)
    assert sum(naps) <= 1.0


def test_a_device_full_from_the_platform_becomes_a_retryable_failure(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"first")

    def _full_write(descriptor: int, payload: Any) -> int:
        raise OSError(errno.ENOSPC, "There is not enough space on the disk.")

    monkeypatch.setattr(storage_local.os, "write", _full_write)
    with pytest.raises(GrafxError) as raised:
        local_device.append_log(SEGMENT, b"second")
    monkeypatch.undo()
    assert raised.value.code == "device_full"
    assert raised.value.retryable is True
    # AC-10: nothing partial is left behind, and the device accepts work again.
    assert local_device.log_size(SEGMENT) == 5
    assert local_device.append_log(SEGMENT, b"second") == 11


def test_a_short_write_that_keeps_making_progress_completes_the_append(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A write system call is allowed to store less than it was given; as long as it keeps
    # making progress the adapter finishes the append instead of reporting a fragment.
    local_device.create(SEGMENT)
    real_write = os.write

    def _short_write(descriptor: int, payload: Any) -> int:
        return real_write(descriptor, bytes(payload)[:3])

    monkeypatch.setattr(storage_local.os, "write", _short_write)
    assert local_device.append_log(SEGMENT, b"second-record") == 13
    monkeypatch.undo()
    assert local_device.read_log(SEGMENT, 0, 13) == b"second-record"


def test_a_partial_append_is_never_reported_as_success(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"first")
    real_write = os.write
    stalled = {"after": 1}

    def _stalling_write(descriptor: int, payload: Any) -> int:
        if stalled["after"] <= 0:
            return 0
        stalled["after"] -= 1
        return real_write(descriptor, bytes(payload)[:3])

    monkeypatch.setattr(storage_local.os, "write", _stalling_write)
    with pytest.raises(GrafxError) as raised:
        local_device.append_log(SEGMENT, b"second-record")
    monkeypatch.undo()
    assert raised.value.code == "device_full"
    assert raised.value.retryable is True
    # The fragment was cut back off, so a later scan cannot mistake it for a record.
    assert local_device.log_size(SEGMENT) == 5
    assert local_device.read_log(SEGMENT, 0, 5) == b"first"
