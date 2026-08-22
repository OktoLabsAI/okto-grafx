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
from okto_grafx.domain.errors import GrafxError, GrafxUnsupportedOperation

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
        # Every holder here shares delete access, so the space really is back at once.
        assert local_device.recycle(SEGMENT) is True
        assert local_device.pending_deletes() == ()
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


def _refuse_to_free_the_name(
    device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shut both doors out of recycling, on every family, and prove the device queues nothing.

    This is the state A27 is about: the platform refuses to delete the file and refuses to
    rename it out of the way, so the name cannot be freed. The device must forget the intent
    entirely rather than carry it across a window in which somebody could re-claim the name.
    """
    real_replace = storage_local.os.replace

    def _refusing_replace(source: str, target: str) -> None:
        if PENDING_DELETE_MARKER in str(target):
            raise PermissionError(13, "The file is in use by another process.")
        real_replace(source, target)

    monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
    monkeypatch.setattr(storage_local.os, "replace", _refusing_replace)
    assert device.recycle(SEGMENT) is False
    assert device.pending_deletes() == (), "a name that could not be freed must queue nothing"


def test_a_recycle_that_cannot_free_the_name_queues_nothing(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A27: the device has no reliable way to tell later that the name was re-claimed, so it
    # never carries the intent. The file stays live, readable and writable.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"old-records")
    _refuse_to_free_the_name(local_device, monkeypatch)
    assert local_device.exists(SEGMENT) is True
    assert local_device.list_files() == (SEGMENT,)
    assert local_device.read_log(SEGMENT, 0, 11) == b"old-records"
    monkeypatch.undo()
    # A27 step 3: reclaiming the space is the business of the caller, which asks again.
    assert local_device.recycle(SEGMENT) is True
    assert local_device.exists(SEGMENT) is False
    assert local_device.pending_deletes() == ()


@pytest.mark.parametrize("door", ["retry", "recycle", "create", "remove", "close"])
def test_a_cross_instance_page_write_survives_a_refused_recycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    # The measurement that killed the identity stamp: a size preserving page write leaves
    # device, index, size and modification time unchanged, so no stamp could ever have seen it.
    # A27 removes the need to see it, and this asserts the outcome from every deletion door.
    root = tmp_path / "db"
    owner = LocalStorageDevice(root, page_size=PAGE_SIZE)
    writer = LocalStorageDevice(root, page_size=PAGE_SIZE)
    try:
        owner.create(SEGMENT)
        owner.allocate(SEGMENT, 1)
        owner.write_page(SEGMENT, 0, bytes([0x11]) * PAGE_SIZE)
        owner.durable_barrier(SEGMENT)
        stamp_before = os.stat(_segment_path(owner))
        _refuse_to_free_the_name(owner, monkeypatch)
        monkeypatch.undo()

        writer.write_page(SEGMENT, 0, bytes([0x22]) * PAGE_SIZE)
        writer.durable_barrier(SEGMENT)
        stamp_after = os.stat(_segment_path(owner))
        # Three of the four components of the A26.2 stamp are unchanged for certain. The fourth
        # is st_mtime_ns, which only advances once per file system tick and is therefore the
        # component the measurement behind A27 found unreliable: it is deliberately NOT asserted
        # here, because a test that depended on it would be a coin toss. What is asserted is the
        # outcome, from every door, which no timestamp can make flaky.
        assert (stamp_after.st_dev, stamp_after.st_ino, stamp_after.st_size) == (
            stamp_before.st_dev,
            stamp_before.st_ino,
            stamp_before.st_size,
        ), "a page write moves nothing a stamp could key on except a timestamp that may not move"

        if door == "retry":
            owner.retry_pending_deletes(force=True)
        elif door == "recycle":
            owner.recycle(OTHER)
        elif door == "create":
            owner.create("wal/000000000003.wal")
        elif door == "remove":
            owner.create("wal/000000000003.wal")
            owner.remove("wal/000000000003.wal")
        else:
            owner.close()
        assert _segment_path(owner).read_bytes() == bytes([0x22]) * PAGE_SIZE
    finally:
        owner.close()
        writer.close()


def test_every_queued_deletion_carries_a_name_no_file_can_take_over(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The A27 invariant, asserted at the door that enforces it: a live name is not queueable.
    local_device.create(SEGMENT)
    monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
    assert local_device.recycle(SEGMENT) is False
    monkeypatch.undo()
    queued = local_device.pending_deletes()
    assert queued != ()
    assert all(PENDING_DELETE_MARKER in name for name in queued)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        local_device._defer(str(_segment_path(local_device)))
    assert raised.value.details["reason"] == "unqueueable_deletion"


@pytest.mark.parametrize("operation", ["remove", "recycle", "atomic_replace"])
def test_an_operation_that_lets_a_name_go_stops_owing_it_a_barrier(
    local_device: LocalStorageDevice, operation: str
) -> None:
    # A29, removal side: a name that left the device must leave the unflushed set with it.
    # Otherwise the next global barrier looks for a file that is gone, and under FR-5 no commit
    # on this device can ever succeed again.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"records")
    if operation == "remove":
        local_device.remove(SEGMENT)
    elif operation == "recycle":
        assert local_device.recycle(SEGMENT) is True
    else:
        local_device.create("wal/staging.tmp")
        local_device.append_log("wal/staging.tmp", b"published")
        local_device.atomic_replace("wal/staging.tmp", SEGMENT)
    local_device.durable_barrier()
    # The device still commits, which is the property FR-5 hangs on.
    local_device.create(OTHER)
    local_device.append_log(OTHER, b"next")
    local_device.durable_barrier()
    assert local_device.read_log(OTHER, 0, 4) == b"next"


def test_a_write_method_cannot_be_added_without_stating_its_intent(
    local_device: LocalStorageDevice,
) -> None:
    # The choke point of A26.1 is a required argument, so the rule cannot be forgotten by a new
    # method: taking a descriptor without stating why is refused outright.
    local_device.create(SEGMENT)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        local_device._descriptor(SEGMENT, "peek")
    assert raised.value.details["reason"] == "invalid_intent"
    with pytest.raises(TypeError):
        local_device._descriptor(SEGMENT)


def test_a_refused_write_leaves_no_residue_in_the_unflushed_set(
    local_device: LocalStorageDevice,
) -> None:
    # A29: one contract-defined refusal used to poison the device, because the name joined the
    # unflushed set before it was validated. Under FR-5 that means no commit could ever succeed.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"record")
    for probe in (
        lambda: local_device.durable_barrier("wal/ghost.wal"),
        lambda: local_device.append_log("wal/ghost.wal", b"x"),
        lambda: local_device.durable_barrier("WAL/000000000001.WAL"),
        lambda: local_device.write_page("wal/ghost.wal", 0, bytes(PAGE_SIZE)),
    ):
        with pytest.raises(GrafxError):
            probe()
        local_device.durable_barrier()
    local_device.append_log(SEGMENT, b"-more")
    local_device.durable_barrier()
    assert local_device.read_log(SEGMENT, 0, 32) == b"record-more"


def test_a_name_reported_as_taken_cannot_be_created_by_the_same_caller(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # create() used to drain the queue first, so a name exists() had just reported as taken
    # could be created, which contradicts its own docstring.
    local_device.create(SEGMENT)
    _refuse_to_free_the_name(local_device, monkeypatch)
    monkeypatch.undo()
    assert local_device.exists(SEGMENT) is True
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        local_device.create(SEGMENT)
    assert raised.value.details["reason"] == "file_exists"


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
        # NTFS lets the delete through because the holder shares delete access (A16), so
        # the name is gone and the space is already accounted for.
        assert local_device.recycle(SEGMENT) is True
        assert local_device.exists(SEGMENT) is False
        assert local_device.list_files() == ()
        assert local_device.pending_deletes() == ()
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
        # A27: the name could not be freed, so nothing is queued and the file stays visible.
        assert local_device.recycle(SEGMENT) is False
        assert local_device.pending_deletes() == ()
        assert local_device.exists(SEGMENT) is True
        assert local_device.read_log(SEGMENT, 0, 7) == b"records"
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


def test_a_second_grafx_process_never_blocks_recycling(
    local_device: LocalStorageDevice, holder_process: Any
) -> None:
    # A16 and AC-9: the slow reader of the scenario is another Okto Grafx process. Because this
    # adapter opens through CreateFileW with FILE_SHARE_DELETE on Windows, its handle does not
    # stand in the way of the owner recycling the segment.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"records")
    local_device.durable_barrier(SEGMENT)
    holder = holder_process(_segment_path(local_device), mode="grafx")
    try:
        assert local_device.recycle(SEGMENT) is True
        assert local_device.exists(SEGMENT) is False
        assert local_device.list_files() == ()
        assert local_device.pending_deletes() == ()
        assert _pending_files(Path(local_device.root)) == ()
    finally:
        holder.release()


@pytest.mark.platform_specific
@pytest.mark.skipif(not IS_WINDOWS, reason="Only Windows lets a handle block a deletion.")
def test_a_holder_opened_without_delete_sharing_is_what_used_to_block_recycling(
    local_device: LocalStorageDevice, holder_process: Any
) -> None:
    # Counterpart: test_a_holder_opened_without_delete_sharing_is_harmless_on_posix.
    # The same reader opened the pre-A16 way blocks the deletion, which is exactly the
    # difference the share delete bit makes. The contract still holds: no exception, and the
    # space comes back once the holder is gone.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"records")
    holder = holder_process(_segment_path(local_device), mode="legacy")
    try:
        assert local_device.recycle(SEGMENT) is False
    finally:
        holder.release()
    assert local_device.recycle(SEGMENT) is True
    assert local_device.exists(SEGMENT) is False
    assert _pending_files(Path(local_device.root)) == ()


@pytest.mark.platform_specific
@pytest.mark.skipif(IS_WINDOWS, reason="POSIX unlinks whatever share mode a holder chose.")
def test_a_holder_opened_without_delete_sharing_is_harmless_on_posix(
    local_device: LocalStorageDevice, holder_process: Any
) -> None:
    # Counterpart: test_a_holder_opened_without_delete_sharing_is_what_used_to_block_recycling.
    local_device.create(SEGMENT)
    local_device.append_log(SEGMENT, b"records")
    holder = holder_process(_segment_path(local_device), mode="legacy")
    try:
        assert local_device.recycle(SEGMENT) is True
        assert local_device.exists(SEGMENT) is False
        assert local_device.pending_deletes() == ()
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
    # The real bound is 256 passes; walking it would make this test cost a few hundred file
    # system calls and turn a busy machine into a false failure. The rule is the same at three.
    assert MAX_PENDING_DELETE_ATTEMPTS > 3
    monkeypatch.setattr(storage_local, "MAX_PENDING_DELETE_ATTEMPTS", 3)
    monkeypatch.setattr(storage_local, "_remove_file", _counting_remove)
    assert local_device.recycle(SEGMENT) is False
    attempts.clear()
    for _ in range(3 + 4):
        local_device.retry_pending_deletes()
    assert len(attempts) == 3
    # The file is never forgotten: an operator can still see it and ask for another pass.
    assert len(local_device.pending_deletes()) == 1
    local_device.retry_pending_deletes(force=True)
    assert len(attempts) == 4


def test_a_deletion_pass_runs_from_more_doors_than_recycle(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A caller that recycles once and never again still gets its space back.
    for door in ("create", "remove", "recycle", "close"):
        device = LocalStorageDevice(Path(local_device.root) / door, page_size=PAGE_SIZE)
        device.create(SEGMENT)
        # Everything the door needs exists BEFORE the queue is armed, so the door itself is the
        # only thing that can drain it.
        device.create(OTHER)
        monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
        assert device.recycle(SEGMENT) is False
        assert device.pending_deletes() != ()
        monkeypatch.undo()
        if door == "create":
            device.create("wal/000000000003.wal")
        elif door == "remove":
            device.remove(OTHER)
        elif door == "recycle":
            assert device.recycle(OTHER) is True
        else:
            device.close()
        assert device.pending_deletes() == (), f"the {door} door did not drive a deletion pass"
        assert _pending_files(Path(device.root)) == (), f"the {door} door left the file behind"
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
        queued = second.pending_deletes()
        assert len(queued) == 1
        assert PENDING_DELETE_MARKER in queued[0]
        assert second.list_files() == ()
        second.retry_pending_deletes(force=True)
        assert _pending_files(root) == ()


def test_a_renamed_deferral_leaves_the_namespace_and_a_refused_one_says_so(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_device.create(SEGMENT)
    monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
    assert local_device.recycle(SEGMENT) is False
    monkeypatch.undo()
    queued = local_device.pending_deletes()
    # The rename succeeded on both families, so the file left the namespace under a reserved
    # name; the case where it cannot be freed is covered by the A27 tests above, on both
    # families, because the refusal is injected rather than left to the platform.
    assert len(queued) == 1
    assert PENDING_DELETE_MARKER in queued[0]
    assert local_device.list_files() == ()
    assert local_device.exists(SEGMENT) is False
    with pytest.raises(GrafxUnsupportedOperation):
        # The reserved infix can never be addressed as a logical name either.
        local_device.exists(f"wal/000000000001.wal{PENDING_DELETE_MARKER}1")


def test_a_capped_deletion_whose_file_vanished_is_retired_from_the_queue(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A queue entry that outlives its file is a phantom for every operator and metric reading
    # pending_deletes(), so the vanished check has to come before the attempt cap.
    real_remove = storage_local._remove_file
    refusing = {"on": True}

    def _sometimes_refusing_remove(path: str) -> None:
        if refusing["on"]:
            raise PermissionError(13, "The file is in use by another process.")
        real_remove(path)

    local_device.create(SEGMENT)
    # The cap stays patched to the end: undoing it here would put the entry back below the real
    # bound of 256 and the capped state this test is about would quietly stop existing.
    monkeypatch.setattr(storage_local, "MAX_PENDING_DELETE_ATTEMPTS", 3)
    monkeypatch.setattr(storage_local, "_remove_file", _sometimes_refusing_remove)
    assert local_device.recycle(SEGMENT) is False
    for _ in range(4):
        local_device.retry_pending_deletes()
    queued = local_device.pending_deletes()
    assert len(queued) == 1
    # The entry is now capped, and something outside this device removes the file.
    refusing["on"] = False
    (Path(local_device.root) / queued[0]).unlink()
    assert local_device.retry_pending_deletes() == 1
    assert local_device.pending_deletes() == ()


def test_a_pass_that_cannot_read_the_directory_keeps_the_entry(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pass that cannot see the directory knows nothing, and knowing nothing is not the same as
    # having reclaimed the space: dropping the entry there would make pending_deletes() lie
    # while the file goes on occupying the disk.
    local_device.create(SEGMENT)
    monkeypatch.setattr(storage_local, "_remove_file", _refusing_remove)
    assert local_device.recycle(SEGMENT) is False
    queued = local_device.pending_deletes()
    assert len(queued) == 1

    real_listdir = os.listdir

    def _blind_listdir(path: Any) -> list[str]:
        if str(path).endswith("wal"):
            raise OSError(5, "The directory could not be read.")
        return real_listdir(path)

    monkeypatch.setattr(storage_local.os, "listdir", _blind_listdir)
    assert local_device.retry_pending_deletes() == 0, "a blind pass must not claim a reclamation"
    assert local_device.pending_deletes() == queued
    monkeypatch.undo()
    # The directory is readable again and the deletion works, so the space really comes back.
    assert local_device.retry_pending_deletes() == 1
    assert local_device.pending_deletes() == ()
    assert _pending_files(Path(local_device.root)) == ()


def test_the_memory_device_reclaims_without_ever_deferring(memory_device: MemoryStorageDevice) -> None:
    # Observable meaning of "never defers": the space and the name are both back at once, so the
    # very next create succeeds and the device holds nothing else.
    memory_device.create(SEGMENT)
    memory_device.append_log(SEGMENT, b"records")
    before = memory_device.used_bytes()
    assert memory_device.recycle(SEGMENT) is True
    assert memory_device.used_bytes() == before - 7
    assert memory_device.list_files() == ()
    memory_device.create(SEGMENT)
    assert memory_device.log_size(SEGMENT) == 0


def _refusing_remove(path: str) -> None:
    """Stand in for a deletion the platform refuses because a handle still holds the file."""
    raise PermissionError(13, "The file is in use by another process.", os.fspath(path))
