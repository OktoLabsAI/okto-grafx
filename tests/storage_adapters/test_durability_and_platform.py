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


def _record_barrier(device: LocalStorageDevice, file: str | None) -> tuple[set[int], int]:
    """Run one barrier and return which file inodes and how many directories it flushed.

    Each descriptor is classified while it is still open, because a small cache closes the one
    it just flushed before the barrier returns.
    """
    files: set[int] = set()
    directories = [0]
    real_fsync = os.fsync

    def _recording_fsync(descriptor: int) -> None:
        information = os.fstat(descriptor)
        if stat.S_ISDIR(information.st_mode):
            directories[0] += 1
        else:
            files.add(information.st_ino)
        real_fsync(descriptor)

    original = storage_local.os.fsync
    storage_local.os.fsync = _recording_fsync
    try:
        device.durable_barrier(file)
    finally:
        storage_local.os.fsync = original
    return files, directories[0]


def test_a_barrier_without_a_name_flushes_every_open_file(local_device: LocalStorageDevice) -> None:
    local_device.create(SEGMENT)
    local_device.create(HEAP)
    local_device.append_log(SEGMENT, b"record")
    local_device.allocate(HEAP)
    root = Path(local_device.root)
    expected = {
        os.stat(root / "wal" / "000000000001.wal").st_ino,
        os.stat(root / "heap.dat").st_ino,
    }
    files, directories = _record_barrier(local_device, None)
    assert files == expected
    # The cost is decided by the code, not by the data: POSIX also flushes the directory of
    # each file plus the device root, and Windows has no directory handle to flush at all.
    assert directories == (0 if IS_WINDOWS else 2)


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
    # The file is flushed before any directory, so the failure is always the file one.
    assert raised.value.details["reason"] == "fsync_failed"
    assert raised.value.details["errno"] == 5
    # A28: the access classification travels in the details, not in the class.
    assert raised.value.details["retryable"] is False


def test_the_directory_flush_covers_the_parent_and_the_device_root(
    local_device: LocalStorageDevice, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The POSIX half of the barrier, exercised on EVERY family so its cost cannot drift unseen
    # on the machine that does not run it: two directories per named file, never more.
    local_device.create(SEGMENT)
    local_device.create(HEAP)
    stand_in = tmp_path / "stand-in"
    stand_in.write_bytes(b"")
    opened: list[str] = []
    real_open = os.open

    def _recording_open(path: Any, flags: int, *rest: Any) -> int:
        opened.append(str(path))
        # A writable stand-in: Windows cannot open a directory, and fsync needs write access.
        return real_open(stand_in, os.O_RDWR | getattr(os, "O_BINARY", 0))

    monkeypatch.setattr(storage_local.os, "open", _recording_open)
    local_device._synchronize_directories((SEGMENT,))
    monkeypatch.undo()
    root = Path(local_device.root)
    assert sorted(opened) == sorted([str(root), str(root / "wal")])

    opened.clear()
    monkeypatch.setattr(storage_local.os, "open", _recording_open)
    local_device._synchronize_directories((SEGMENT, HEAP))
    monkeypatch.undo()
    assert sorted(opened) == sorted([str(root), str(root / "wal")])


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


def test_a_barrier_flushes_the_one_file_it_names_and_its_directories(
    local_device: LocalStorageDevice,
) -> None:
    local_device.create(SEGMENT)
    local_device.create(HEAP)
    local_device.append_log(SEGMENT, b"record")
    local_device.allocate(HEAP)
    segment_inode = os.stat(Path(local_device.root) / "wal" / "000000000001.wal").st_ino
    files, directories = _record_barrier(local_device, SEGMENT)
    assert files == {segment_inode}
    # POSIX flushes the directory that holds the file and the device root; Windows neither.
    assert directories == (0 if IS_WINDOWS else 2)
    # The other file was never named, so the barrier did not touch it.
    assert os.stat(Path(local_device.root) / "heap.dat").st_ino not in files


ACKNOWLEDGING_OPERATIONS: tuple[str, ...] = (
    "append_log",
    "allocate",
    "write_page",
    "truncate_log",
    "create",
    "atomic_replace",
)
"""Every operation A26.1 calls acknowledging; each must make the barrier owe the file."""


@pytest.mark.parametrize("operation", ACKNOWLEDGING_OPERATIONS)
def test_every_acknowledging_operation_makes_the_next_barrier_owe_the_file(
    tmp_path: Path, operation: str
) -> None:
    # A26.1 per method. The device is REOPENED and its descriptor cache holds one entry, so
    # neither an earlier create nor the handle cache can stand in for the acknowledgement of
    # the operation under test: if it does not say "write", the barrier never reaches the file.
    root = tmp_path / "db"
    names = [f"wal/{index:012d}.wal" for index in range(4)]
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as setup:
        for name in names:
            setup.create(name)
            setup.allocate(name, 1)
        setup.create("wal/staging.tmp")
        setup.durable_barrier()
    device = LocalStorageDevice(root, page_size=PAGE_SIZE, max_open_files=1)
    target = names[0]
    try:
        if operation == "append_log":
            device.append_log(target, b"record")
        elif operation == "allocate":
            device.allocate(target, 1)
        elif operation == "write_page":
            device.write_page(target, 0, bytes([0x5C]) * PAGE_SIZE)
        elif operation == "truncate_log":
            device.truncate_log(target, PAGE_SIZE // 2)
        elif operation == "create":
            target = "wal/000000000009.wal"
            device.create(target)
        else:
            device.atomic_replace("wal/staging.tmp", target)
        # Push the descriptor of the target out of the cache with reads, which acknowledge
        # nothing at all, so only the operation under test can still owe the barrier.
        for name in names[1:]:
            device.read_log(name, 0, 1)
        expected = os.stat(Path(device.root) / target.replace("/", os.sep)).st_ino
        files, _ = _record_barrier(device, None)
        assert expected in files, f"{operation} did not make the barrier owe {target}"
    finally:
        device.close()


def test_the_descriptor_cache_stays_inside_its_bound(tmp_path: Path) -> None:
    # M29: the previous test only proved that nothing was lost, which an unbounded cache also
    # satisfies. The bound itself is the property that keeps a long WAL from exhausting them.
    with LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE, max_open_files=4) as device:
        names = [f"wal/{index:012d}.wal" for index in range(40)]
        for index, name in enumerate(names):
            device.create(name)
            device.append_log(name, f"segment-{index}".encode("ascii"))
            assert len(device._handles) <= 4
        for index, name in enumerate(names):
            assert device.read_log(name, 0, 32) == f"segment-{index}".encode("ascii")
            assert len(device._handles) <= 4
        assert len(device.list_files("wal/")) == 40


def test_a_payload_that_carries_the_text_mode_end_of_file_byte_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A15. The flag only matters on the os.open fallback, so the fallback is forced here: a
    # file opened in text mode on Windows stops at the first 0x1A, which ordinary record
    # payloads carry about once every 256 bytes.
    monkeypatch.setattr(storage_local, "_WINDOWS_OPENER", None)
    payload = bytes([0x1A]) * 4 + b"after the end of file byte" + bytes([0x1A, 0x0D, 0x0A])
    root = tmp_path / "db"
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as device:
        device.create(SEGMENT)
        assert device.append_log(SEGMENT, payload) == len(payload)
        device.durable_barrier(SEGMENT)
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as reopened:
        assert reopened.log_size(SEGMENT) == len(payload)
        assert reopened.read_log(SEGMENT, 0, len(payload)) == payload
    monkeypatch.undo()


def test_a_write_that_fails_halfway_leaves_the_file_at_the_size_it_had(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The undo of a refused append: without it a fragment survives, and a WAL scan reads it as
    # the start of a record that was never written.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"first")
    real_write = os.write
    written = {"calls": 0}

    def _failing_write(descriptor: int, payload: Any) -> int:
        written["calls"] += 1
        if written["calls"] == 1:
            return real_write(descriptor, bytes(payload)[:3])
        raise OSError(errno.EACCES, "The process cannot access the file.")

    monkeypatch.setattr(storage_local.os, "write", _failing_write)
    with pytest.raises(GrafxError):
        local_device.append_log(SEGMENT, b"second-record")
    monkeypatch.undo()
    assert local_device.log_size(SEGMENT) == 5
    assert local_device.read_log(SEGMENT, 0, 16) == b"first"


PUBLISHED_NAMES: tuple[str, ...] = ("control/writer.lease", "heap.dat", "wal/000000000001.wal")
"""A66.1: every kind of name this device may be asked to publish over, not only the control ones."""


@pytest.mark.parametrize("name", PUBLISHED_NAMES)
def test_a_control_file_is_published_over_a_reader_in_another_process(
    tmp_path: Path, port_holder: Any, name: str
) -> None:
    # CF-5. Section 6.1 publishes the control files with atomic_replace and D1 requires
    # multi-process writing, so a participant that has merely READ a file must not be able to
    # stop another from publishing it. The payloads are single-byte fills, so a torn or mixed
    # read is visible in the distinct-byte summary rather than merely improbable.
    root = tmp_path / "db"
    device = LocalStorageDevice(root, page_size=PAGE_SIZE)
    try:
        device.create(name)
        device.append_log(name, b"A" * 4096)
        device.durable_barrier(name)

        reader = port_holder(root, name)
        assert reader.first == "len=4096,bytes=A"

        device.create("control/staging.tmp")
        device.append_log("control/staging.tmp", b"B" * 4096)
        device.atomic_replace("control/staging.tmp", name)

        # The publication is visible whole to anyone opening the name from now on.
        assert device.read_log(name, 0, 8192) == b"B" * 4096
        # The namespace holds exactly the published name and nothing else: a rename that
        # reports success while writing a garbled directory entry passes every content
        # assertion and fails this one.
        assert device.list_files() == (name,)
        # And the reader that opened the file before it happened keeps reading what it opened,
        # whole: old or new, never a mix.
        assert reader.release() == "len=4096,bytes=A"
    finally:
        device.close()


ROCKET: str = "🚀"
FOLDER: str = "📁"
"""Two characters outside the basic plane: one code point, two UTF-16 code units each.

A logical name can never carry one, because _validate_segment refuses anything outside printable
ASCII. The database root can: it is whatever string the caller passed to open the database, and
that is the surface this corpus exists to reach (L5).
"""

NON_ASCII_ROOTS: tuple[tuple[str, str], ...] = tuple(
    (f"{count}-non-bmp-pad-{pad}", (ROCKET + " project" if count == 1 else ROCKET + " a " + FOLDER + " b") + "x" * pad)
    for count in (1, 2)
    for pad in range(4)
)
"""Both failure shapes, across every residue of the physical path length modulo four.

One character outside the plane makes the name fill the array with no room for the terminator,
and whether that bites depends on the padding ctypes puts after the struct, hence the residues.
Two overflow the array outright.
"""


@pytest.mark.parametrize(("label", "root_name"), NON_ASCII_ROOTS, ids=[row[0] for row in NON_ASCII_ROOTS])
def test_a_control_record_is_published_through_a_root_outside_the_ascii_plane(
    tmp_path: Path, label: str, root_name: str
) -> None:
    # The publication path measures a name in UTF-16 code units. Sizing it in code points made
    # this either raise a bare ValueError out of a port door or, with no terminator in the
    # buffer, report success while the old record survived and a junk name entered the
    # namespace. The directory listing is what catches the second shape; the bytes alone do not.
    root = tmp_path / root_name
    device = LocalStorageDevice(root, page_size=PAGE_SIZE)
    try:
        device.create("control/commit.state")
        device.append_log("control/commit.state", b"OLD" * 8)
        device.durable_barrier("control/commit.state")
        device.create("control/commit.state.new")
        device.append_log("control/commit.state.new", b"NEW" * 8)

        device.atomic_replace("control/commit.state.new", "control/commit.state")

        assert device.read_log("control/commit.state", 0, 64) == b"NEW" * 8
        assert device.list_files("control/") == ("control/commit.state",)
        # Read the real directory too: a name the port cannot even spell would not show up in a
        # listing filtered by prefix, and that is precisely the residue this bug leaves behind.
        assert sorted(os.listdir(Path(device.root) / "control")) == ["commit.state"]
    finally:
        device.close()


def test_a_request_this_platform_cannot_express_publishes_through_the_ordinary_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DoD 5: a port door raises Grafx errors or nothing at all. Building the rename request is
    # the one step that can fail on a value rather than on the device, and the shipped version
    # let a bare ValueError out of atomic_replace for a database root holding an emoji. The
    # fallback answers with the ordinary primitive instead, and this pins that arm, which is
    # otherwise reachable only on a Windows build without the rename flag.
    def _refuses(ctypes_module: Any, target: str) -> object:
        raise ValueError("string too long")

    monkeypatch.setattr(storage_local, "_rename_request", _refuses)
    root = tmp_path / (ROCKET + " project")
    device = LocalStorageDevice(root, page_size=PAGE_SIZE)
    try:
        device.create("control/commit.state")
        device.append_log("control/commit.state", b"OLD" * 8)
        device.create("control/commit.state.new")
        device.append_log("control/commit.state.new", b"NEW" * 8)
        device.atomic_replace("control/commit.state.new", "control/commit.state")
        assert device.read_log("control/commit.state", 0, 64) == b"NEW" * 8
        assert device.list_files("control/") == ("control/commit.state",)
        assert sorted(os.listdir(Path(device.root) / "control")) == ["commit.state"]
    finally:
        device.close()


def test_a_publication_survives_a_reader_in_this_process_holding_another_device(
    tmp_path: Path,
) -> None:
    # The same defect without a subprocess: two device instances are two independent handle
    # caches, so releasing our own descriptor cannot be what makes the publication work.
    root = tmp_path / "db"
    publisher = LocalStorageDevice(root, page_size=PAGE_SIZE)
    reader = LocalStorageDevice(root, page_size=PAGE_SIZE)
    try:
        publisher.create("control/commit.state")
        publisher.append_log("control/commit.state", b"A" * 512)
        publisher.durable_barrier("control/commit.state")
        assert reader.read_log("control/commit.state", 0, 512) == b"A" * 512

        publisher.create("control/commit.state.new")
        publisher.append_log("control/commit.state.new", b"B" * 512)
        publisher.atomic_replace("control/commit.state.new", "control/commit.state")

        assert publisher.read_log("control/commit.state", 0, 512) == b"B" * 512
        assert publisher.list_files() == ("control/commit.state",)
        assert reader.read_log("control/commit.state", 0, 512) == b"A" * 512
        with LocalStorageDevice(root, page_size=PAGE_SIZE) as fresh:
            assert fresh.read_log("control/commit.state", 0, 512) == b"B" * 512
    finally:
        publisher.close()
        reader.close()


def test_the_publication_fallback_still_replaces_when_the_platform_lacks_the_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The POSIX families, and any Windows build without the rename flag, publish through
    # os.replace. Forcing that branch keeps it from rotting unnoticed on this machine.
    monkeypatch.setattr(storage_local, "_WINDOWS_OPENER", None)
    root = tmp_path / "db"
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as device:
        device.create("control/commit.state")
        device.append_log("control/commit.state", b"A" * 64)
        device.create("control/commit.state.new")
        device.append_log("control/commit.state.new", b"B" * 64)
        device.atomic_replace("control/commit.state.new", "control/commit.state")
        assert device.read_log("control/commit.state", 0, 64) == b"B" * 64
        assert device.exists("control/commit.state.new") is False
        assert device.list_files() == ("control/commit.state",)


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
        # A28: the barrier door carries the same classification, in one type of its own.
        with pytest.raises(GrafxDurabilityBarrierFailed) as barrier:
            device.durable_barrier(SEGMENT)
        assert barrier.value.details["reason"] == "access_failed"
        assert barrier.value.details["errno"] == errno.EACCES
        assert barrier.value.details["attempts"] == RETRY_ATTEMPTS
        assert barrier.value.details["retryable"] is True
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


def test_a_failure_the_retry_cannot_win_is_refused_at_once(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A11-revised reports how many tries were spent. A permanent condition must cost exactly
    # one: sleeping four times over an EISDIR helps nobody and misreports the attempt count.
    local_device.create(SEGMENT)
    local_device.close()
    device = LocalStorageDevice(Path(local_device.root), page_size=PAGE_SIZE)
    opens: list[int] = []
    naps: list[float] = []
    monkeypatch.setattr(storage_local.time, "sleep", naps.append)

    def _refuse_with(code: int) -> Any:
        def _open(path: str, *, create_new: bool) -> int:
            opens.append(code)
            raise OSError(code, "The platform refuses this permanently.")

        return _open

    try:
        for permanent in (errno.EISDIR, errno.EBADF, errno.ENOTDIR):
            opens.clear()
            naps.clear()
            monkeypatch.setattr(storage_local, "_open_descriptor", _refuse_with(permanent))
            with pytest.raises(GrafxStorageError) as raised:
                device.log_size(SEGMENT)
            assert len(opens) == 1, f"errno {permanent} was retried"
            assert naps == []
            assert raised.value.details["attempts"] == 1
            assert raised.value.details["errno"] == permanent
        # The transient classification still spends the whole budget, which is the other branch.
        opens.clear()
        naps.clear()
        monkeypatch.setattr(storage_local, "_open_descriptor", _refuse_with(errno.EACCES))
        with pytest.raises(GrafxStorageError) as raised:
            device.log_size(SEGMENT)
        assert len(opens) == RETRY_ATTEMPTS
        assert raised.value.details["attempts"] == RETRY_ATTEMPTS
    finally:
        monkeypatch.undo()
        device.close()


def test_a_nameless_barrier_flushes_a_file_that_is_open_but_owes_nothing(
    local_device: LocalStorageDevice,
) -> None:
    # CONTRACT section 4.1: None means every OPEN file, not merely every unflushed one. A file
    # that was read since its last barrier is open, and the port says it is flushed.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"record")
    local_device.durable_barrier(SEGMENT)
    local_device.read_log(SEGMENT, 0, 6)
    inode = os.stat(Path(local_device.root) / "wal" / "000000000001.wal").st_ino
    files, _ = _record_barrier(local_device, None)
    assert inode in files


PERMANENT_CONDITIONS: tuple[tuple[int, str], ...] = (
    (errno.EEXIST, "a regular file where the directory belongs"),
    (errno.ENOTDIR, "a path component that is not a directory"),
    (errno.EISDIR, "a directory where a file belongs"),
    (errno.ENAMETOOLONG, "a name the file system cannot hold"),
    (errno.EBADF, "a descriptor that is not valid"),
    (errno.EROFS, "a volume mounted read only"),
)
"""Conditions no amount of waiting resolves. A47 tells a caller to retry whatever says it may be."""

CLEARABLE_CONDITIONS: tuple[tuple[int, str], ...] = (
    (errno.EACCES, "a scanner holding the file for a moment"),
    (errno.EBUSY, "a resource in use right now"),
    (errno.EMFILE, "too many descriptors open at this instant"),
    (errno.EIO, "an unexplained failure nobody has classified"),
)
"""Conditions that can clear on their own, including the unclassified ones that keep the default."""


def test_the_two_classifications_of_a_failure_cannot_both_claim_it() -> None:
    # The classifier has no precedence rule, which is only safe while the sets stay disjoint.
    # A mutation that deleted the old precedence branch survived the whole suite, because no
    # failure can reach it; this asserts the property that made the branch unnecessary, so an
    # editor who lists a number in both tables is told at once.
    assert storage_local._TRANSIENT_ERRNOS.isdisjoint(storage_local._PERMANENT_ERRNOS)
    assert storage_local._TRANSIENT_WINERRORS.isdisjoint(storage_local._PERMANENT_WINERRORS)
    assert storage_local._DEVICE_FULL_ERRNOS.isdisjoint(storage_local._PERMANENT_ERRNOS)
    assert storage_local._DEVICE_FULL_WINERRORS.isdisjoint(storage_local._PERMANENT_WINERRORS)
    # And the transient table stays non-empty, so the disjointness above is not vacuous.
    assert storage_local._TRANSIENT_ERRNOS
    assert storage_local._PERMANENT_ERRNOS


@pytest.mark.parametrize(("code", "label"), PERMANENT_CONDITIONS, ids=[row[1] for row in PERMANENT_CONDITIONS])
def test_a_condition_that_cannot_clear_is_not_reported_as_retryable(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch, code: int, label: str
) -> None:
    # C11 hit this by opening a database on a path that was a regular file: the device said
    # retryable, and a caller obeying A47 retries a condition that never changes, forever.
    local_device.create(SEGMENT)
    local_device.close()
    device = LocalStorageDevice(Path(local_device.root), page_size=PAGE_SIZE)
    monkeypatch.setattr(storage_local.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        storage_local,
        "_open_descriptor",
        lambda path, *, create_new: (_ for _ in ()).throw(OSError(code, "refused")),
    )
    try:
        with pytest.raises(GrafxStorageError) as raised:
            device.log_size(SEGMENT)
        assert raised.value.retryable is False, label
        assert raised.value.details["reason"] == "permanently_refused"
        assert raised.value.details["attempts"] == 1
    finally:
        monkeypatch.undo()
        device.close()


@pytest.mark.parametrize(("code", "label"), CLEARABLE_CONDITIONS, ids=[row[1] for row in CLEARABLE_CONDITIONS])
def test_a_condition_that_can_clear_stays_retryable(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch, code: int, label: str
) -> None:
    local_device.create(SEGMENT)
    local_device.close()
    device = LocalStorageDevice(Path(local_device.root), page_size=PAGE_SIZE)
    monkeypatch.setattr(storage_local.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        storage_local,
        "_open_descriptor",
        lambda path, *, create_new: (_ for _ in ()).throw(OSError(code, "refused")),
    )
    try:
        with pytest.raises(GrafxStorageError) as raised:
            device.log_size(SEGMENT)
        assert raised.value.retryable is True, label
        assert raised.value.details["reason"] == "access_failed"
    finally:
        monkeypatch.undo()
        device.close()


def test_a_database_opened_on_a_regular_file_refuses_permanently(tmp_path: Path) -> None:
    # The shape C11 met, through the door C11 used.
    occupied = tmp_path / "not-a-directory"
    occupied.write_bytes(b"i am a file")
    with pytest.raises(GrafxStorageError) as raised:
        LocalStorageDevice(occupied, page_size=PAGE_SIZE)
    assert raised.value.retryable is False
    assert raised.value.details["errno"] == errno.EEXIST


LOCALIZED: str = "Não é possível criar um arquivo já existente"
"""A platform message as a non-English Windows supplies it: useful, and not ours to speak."""


def test_a_localized_platform_message_never_reaches_the_grafx_sentence(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G1 and A7 ask for en-US ASCII in what the engine says. The source gate cannot see this
    # one: the string is ASCII in the file and only becomes localized when the OS answers.
    local_device.create(SEGMENT)
    local_device.close()
    device = LocalStorageDevice(Path(local_device.root), page_size=PAGE_SIZE)
    monkeypatch.setattr(storage_local.time, "sleep", lambda seconds: None)

    def _localized(path: str, *, create_new: bool) -> int:
        raise OSError(errno.EEXIST, LOCALIZED)

    monkeypatch.setattr(storage_local, "_open_descriptor", _localized)
    try:
        with pytest.raises(GrafxStorageError) as raised:
            device.log_size(SEGMENT)
        assert raised.value.message.isascii(), raised.value.message
        assert LOCALIZED not in raised.value.message
        assert raised.value.details["platform_message"] == LOCALIZED
        assert str(raised.value).isascii()
        # The barrier door rebuilds a message from this one, so it inherits the property (A28).
        with pytest.raises(GrafxDurabilityBarrierFailed) as barrier:
            device.durable_barrier(SEGMENT)
        assert barrier.value.message.isascii(), barrier.value.message
    finally:
        monkeypatch.undo()
        device.close()


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
