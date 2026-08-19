"""Segment recycling on both operating system families (C2, FR-6, AC-9, TS-9, A16, A17).

The contract is one sentence: recycle releases a file, frees its logical name at once, answers
True when the space is already back and False when the platform still holds it, and never raises
because somebody else has a handle open. The mechanism behind that sentence is where the
families differ, and the tests marked platform specific are the counterpart pairs that prove
each mechanism (G4).

TS-9 says "another process", so the holder here really is one: a child process that announces
it holds the file and only lets go when the test tells it to.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from okto_grafx.adapters import storage_local
from okto_grafx.adapters.storage_local import (
    IS_WINDOWS,
    MAX_PENDING_DELETE_ATTEMPTS,
    PENDING_DELETE_MARKER,
    SHARE_DELETE_AVAILABLE,
    LocalStorageDevice,
)
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import GrafxUnsupportedOperation

PAGE_SIZE: int = 512
"""Kept equal to the page size of the fixtures; a drift fails the device shape test."""

SEGMENT: str = "wal/000000000001.wal"
OTHER: str = "wal/000000000002.wal"
COMMITTED: bytes = b"BRAND-NEW-COMMITTED-DATA"


def _pending_files(root: Path) -> tuple[Path, ...]:
    """Return every file the device renamed out of the way, still waiting for its deletion."""
    return tuple(sorted(path for path in root.rglob("*") if PENDING_DELETE_MARKER in path.name))


def _segment_path(device: LocalStorageDevice) -> Path:
    """Return the real path of the recycled segment."""
    return Path(device.root) / "wal" / "000000000001.wal"


def _publish(device: LocalStorageDevice, name: str, payload: bytes) -> None:
    """Publish a file the way CONTRACT section 6.1 does: write a staging file, then replace."""
    device.create("wal/staging.tmp")
    device.append_log("wal/staging.tmp", payload)
    device.atomic_replace("wal/staging.tmp", name)


# --- the contract, on every family --------------------------------------------------------


def test_recycle_reclaims_a_file_nobody_holds(device: Any) -> None:
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"records")
    assert device.recycle(SEGMENT) is True
    assert device.exists(SEGMENT) is False
    assert device.list_files() == ()
    assert device.pending_deletes() == ()


def test_recycle_is_idempotent_over_a_name_that_is_already_gone(device: Any) -> None:
    device.create(SEGMENT)
    assert device.recycle(SEGMENT) is True
    assert device.recycle(SEGMENT) is True
    assert device.recycle(OTHER) is True


def test_recycle_still_refuses_an_inadmissible_name(device: Any) -> None:
    with pytest.raises(GrafxUnsupportedOperation):
        device.recycle("../escape.wal")


# --- A17: the name leaves the namespace at once --------------------------------------------


def test_recycle_frees_the_logical_name_at_once_even_while_a_process_holds_it(
    local_device: LocalStorageDevice, holder_process: Any
) -> None:
    # A17: a caller that recycles a segment must be able to create the next one under the same
    # name straight away, and must never keep writing to a file queued for destruction.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"old-records")
    local_device.durable_barrier(SEGMENT)
    holder = holder_process(_segment_path(local_device), share_delete=IS_WINDOWS)
    try:
        assert local_device.recycle(SEGMENT) in (True, False)
        assert local_device.exists(SEGMENT) is False
        assert local_device.list_files() == ()
        local_device.create(SEGMENT)
        local_device.append_log(SEGMENT, b"new-records")
        assert local_device.read_log(SEGMENT, 0, 32) == b"new-records"
    finally:
        holder.release()
    local_device.retry_pending_deletes()
    assert local_device.read_log(SEGMENT, 0, 32) == b"new-records"


def test_the_memory_twin_frees_the_name_the_same_way(memory_device: MemoryStorageDevice) -> None:
    memory_device.create(SEGMENT)
    assert memory_device.recycle(SEGMENT) is True
    assert memory_device.exists(SEGMENT) is False
    assert memory_device.list_files() == ()
    memory_device.create(SEGMENT)
    assert memory_device.exists(SEGMENT) is True


# --- B1: a queued deletion never destroys a file published later ----------------------------


def test_a_queued_deletion_never_destroys_a_file_published_later(
    local_device: LocalStorageDevice, holder_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The blocker, reproduced end to end: recycle defers, the name is reused by a freshly
    # published file holding committed data, and no later deletion pass may touch it (A17).
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"old-records")
    holder = holder_process(_segment_path(local_device), share_delete=False)
    # Nothing may delete the file for now, on either family: the deferral has to survive.
    monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
    try:
        assert local_device.recycle(SEGMENT) is False
        assert SEGMENT in local_device.pending_deletes()
    finally:
        holder.release()
    _publish(local_device, SEGMENT, COMMITTED)
    assert local_device.read_log(SEGMENT, 0, len(COMMITTED)) == COMMITTED
    monkeypatch.undo()
    # Every door that drives a deletion pass, including the one that used to destroy the file.
    local_device.retry_pending_deletes()
    local_device.recycle(OTHER)
    local_device.retry_pending_deletes(force=True)
    assert local_device.exists(SEGMENT) is True
    assert local_device.read_log(SEGMENT, 0, len(COMMITTED)) == COMMITTED
    assert _segment_path(local_device).read_bytes() == COMMITTED


def test_a_deletion_pass_proves_identity_before_it_unlinks(
    tmp_path: Path, holder_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The mechanism on its own, in the shape that a second process produces: this device queued
    # a deletion under a live name, another device published a different file under that name,
    # and the pass must refuse to unlink a file it can no longer identify.
    root = tmp_path / "db"
    owner = LocalStorageDevice(root, page_size=PAGE_SIZE)
    publisher = LocalStorageDevice(root, page_size=PAGE_SIZE)
    try:
        owner.create(SEGMENT)
        owner.append_log(SEGMENT, b"old-records")
        holder = holder_process(_segment_path(owner), share_delete=False)
        monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
        try:
            assert owner.recycle(SEGMENT) is False
            assert SEGMENT in owner.pending_deletes()
        finally:
            holder.release()
            monkeypatch.undo()
        publisher.remove(SEGMENT)
        _publish(publisher, SEGMENT, COMMITTED)
        # The owner knows nothing about the publication: only the identity check saves the file.
        assert owner.retry_pending_deletes(force=True) == 0
        assert SEGMENT in owner.pending_deletes()
        assert publisher.read_log(SEGMENT, 0, len(COMMITTED)) == COMMITTED
    finally:
        owner.close()
        publisher.close()


# --- the two mechanisms ----------------------------------------------------------------------


@pytest.mark.platform_specific
@pytest.mark.skipif(not IS_WINDOWS, reason="The pending delete mechanism only exists on Windows.")
def test_recycle_reclaims_through_the_pending_delete_on_windows(
    local_device: LocalStorageDevice, holder_process: Any
) -> None:
    # Counterpart: test_recycle_unlinks_immediately_on_posix.
    # A16: this device opens with FILE_SHARE_DELETE, so a holder that does the same never blocks
    # a deletion; the name goes away at once and the space returns when the last handle closes.
    assert SHARE_DELETE_AVAILABLE is True
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"records")
    root = Path(local_device.root)
    holder = holder_process(_segment_path(local_device), share_delete=True)
    try:
        answer = local_device.recycle(SEGMENT)
        assert local_device.exists(SEGMENT) is False
        assert local_device.list_files() == ()
        if answer is False:
            # The platform armed its own pending delete: the file is queued under a reserved
            # name that no logical name can ever take over.
            assert all(PENDING_DELETE_MARKER in name for name in local_device.pending_deletes())
    finally:
        holder.release()
    local_device.retry_pending_deletes()
    assert local_device.pending_deletes() == ()
    assert _pending_files(root) == ()
    assert local_device.list_files() == ()


@pytest.mark.platform_specific
@pytest.mark.skipif(not IS_WINDOWS, reason="Only Windows can refuse both the delete and the rename.")
def test_a_holder_without_delete_sharing_is_survived_on_windows(
    local_device: LocalStorageDevice, holder_process: Any
) -> None:
    # Counterpart: test_recycle_unlinks_immediately_on_posix.
    # An antivirus that opens without delete sharing blocks the deletion AND the rename. The
    # only promises left are the ones that still hold: no exception, a bounded retry, and no
    # destruction of whatever ends up carrying that name later.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"records")
    holder = holder_process(_segment_path(local_device), share_delete=False)
    try:
        assert local_device.recycle(SEGMENT) is False
        assert local_device.pending_deletes() != ()
    finally:
        holder.release()
    assert local_device.recycle(SEGMENT) is True
    assert local_device.exists(SEGMENT) is False
    assert _pending_files(Path(local_device.root)) == ()


@pytest.mark.platform_specific
@pytest.mark.skipif(IS_WINDOWS, reason="Direct unlink of an open file only works on POSIX.")
def test_recycle_unlinks_immediately_on_posix(
    local_device: LocalStorageDevice, holder_process: Any
) -> None:
    # Counterparts: test_recycle_reclaims_through_the_pending_delete_on_windows and
    # test_a_holder_without_delete_sharing_is_survived_on_windows.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"records")
    root = Path(local_device.root)
    holder = holder_process(_segment_path(local_device), share_delete=False)
    try:
        assert local_device.recycle(SEGMENT) is True
        assert local_device.exists(SEGMENT) is False
        assert local_device.list_files() == ()
        assert local_device.pending_deletes() == ()
        assert _pending_files(root) == ()
    finally:
        holder.release()


# --- the deferral itself, on every family ----------------------------------------------------


def test_a_deletion_the_platform_refuses_is_deferred_and_retried_later(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"records")
    root = Path(local_device.root)
    monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
    assert local_device.recycle(SEGMENT) is False
    assert len(local_device.pending_deletes()) == 1
    assert local_device.retry_pending_deletes() == 0
    monkeypatch.undo()
    assert local_device.retry_pending_deletes() == 1
    assert local_device.pending_deletes() == ()
    assert _pending_files(root) == ()
    assert local_device.list_files() == ()


def test_a_deferred_deletion_stops_retrying_after_its_bounded_number_of_attempts(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def _counting_remove(path: str) -> None:
        attempts.append(path)
        raise PermissionError(13, "The file is in use by another process.")

    local_device.create(SEGMENT)
    monkeypatch.setattr(storage_local, "_remove_file", _counting_remove)
    assert local_device.recycle(SEGMENT) is False
    attempts.clear()
    for _ in range(MAX_PENDING_DELETE_ATTEMPTS + 4):
        local_device.retry_pending_deletes()
    assert len(attempts) == MAX_PENDING_DELETE_ATTEMPTS
    # The file is never forgotten: an operator can still see it and ask for another pass.
    assert len(local_device.pending_deletes()) == 1
    local_device.retry_pending_deletes(force=True)
    assert len(attempts) == MAX_PENDING_DELETE_ATTEMPTS + 1


def test_a_deletion_pass_runs_from_more_doors_than_recycle(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A caller that recycles once and never again still gets its space back.
    for door in ("create", "remove", "close"):
        device = LocalStorageDevice(Path(local_device.root) / door, page_size=PAGE_SIZE)
        device.create(SEGMENT)
        monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
        assert device.recycle(SEGMENT) is False
        monkeypatch.undo()
        if door == "create":
            device.create(OTHER)
        elif door == "remove":
            device.create(OTHER)
            device.remove(OTHER)
        else:
            device.close()
        assert device.pending_deletes() == (), f"the {door} door did not drive a deletion pass"
        if door != "close":
            device.close()


def test_a_new_device_adopts_the_deletions_the_previous_one_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "db"
    first = LocalStorageDevice(root, page_size=PAGE_SIZE)
    first.create(SEGMENT)
    monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
    assert first.recycle(SEGMENT) is False
    first.close()
    monkeypatch.undo()
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as second:
        assert len(second.pending_deletes()) == 1
        assert second.list_files() in ((), (SEGMENT,))
        second.retry_pending_deletes(force=True)
        assert _pending_files(root) == ()


def test_a_deferred_file_is_invisible_to_the_namespace(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_device.create(SEGMENT)
    monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
    assert local_device.recycle(SEGMENT) is False
    monkeypatch.undo()
    assert SEGMENT not in local_device.list_files() or not IS_WINDOWS
    with pytest.raises(GrafxUnsupportedOperation):
        # The reserved infix can never be addressed as a logical name either.
        local_device.exists(f"wal/000000000001.wal{PENDING_DELETE_MARKER}1")


def test_the_memory_device_reclaims_without_ever_deferring(memory_device: MemoryStorageDevice) -> None:
    memory_device.create(SEGMENT)
    assert memory_device.recycle(SEGMENT) is True
    assert memory_device.pending_deletes() == ()
    assert memory_device.retry_pending_deletes() == 0
    assert memory_device.retry_pending_deletes(force=True) == 0


def _refusing_remove(path: str) -> None:
    """Stand in for a deletion the platform refuses because a handle still holds the file."""
    raise PermissionError(13, "The file is in use by another process.", os.fspath(path))
