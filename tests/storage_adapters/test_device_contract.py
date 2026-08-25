"""The storage port contract, proved once against every device family (C2, TR-3).

Every test in this module runs twice: against LocalStorageDevice over a real directory and
against MemoryStorageDevice over byte buffers. A behaviour that only holds on one medium is a
defect, not a platform difference, so nothing here is marked platform specific.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from okto_grafx.adapters import storage_local
from okto_grafx.adapters.storage_local import (
    MAX_ALLOCATION_PAGES,
    PENDING_DELETE_MARKER,
    LocalStorageDevice,
    find_case_conflict,
    normalize_logical_name,
)
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain import page
from okto_grafx.domain.ports import StorageDevice

PAGE_SIZE: int = 512
"""Kept equal to the page size of the fixtures; a drift fails the device shape test."""

HEAP: str = "heap.dat"
SEGMENT: str = "wal/000000000001.wal"
READER: str = "control/readers/reader-1.reader"


def _page(filler: int) -> bytes:
    """Return one page of a recognisable byte."""
    return bytes([filler]) * PAGE_SIZE


# --- shape ------------------------------------------------------------------------------


def test_the_device_satisfies_the_port(device: Any) -> None:
    assert isinstance(device, StorageDevice)


def test_the_device_name_is_a_short_bounded_label(device: Any) -> None:
    # G7 and TR-7: this value may end up as a metric label, so it can never be a path.
    assert device.name in {"local", "memory"}
    assert len(device.name) <= 16
    assert device.page_size == PAGE_SIZE


def test_the_page_size_vocabulary_has_exactly_one_owner(tmp_path: Any) -> None:
    # A24: one definition, owned by C1. A second validator of the same name with wider bounds
    # is the integration failure A20 exists to prevent.
    assert storage_local.validate_page_size is page.validate_page_size
    assert storage_local.MIN_PAGE_SIZE is page.MIN_PAGE_SIZE
    assert storage_local.MAX_PAGE_SIZE is page.MAX_PAGE_SIZE
    assert page.MAX_PAGE_SIZE == 32768
    assert "MAX_PAGE_SIZE" not in storage_local.__all__
    assert "validate_page_size" not in storage_local.__all__
    # A20: the size the old bound accepted is refused by every door of the system.
    for builder in (
        lambda size: LocalStorageDevice(tmp_path / f"db{size}", page_size=size),
        lambda size: MemoryStorageDevice(page_size=size),
        page.validate_page_size,
    ):
        with pytest.raises(GrafxConfigurationError):
            builder(65536)


def test_a_page_size_outside_the_format_is_refused(tmp_path: Any) -> None:
    for page_size in (0, 7, 500, 65536, 1 << 20, True, "8192"):
        with pytest.raises(GrafxConfigurationError):
            LocalStorageDevice(tmp_path / "a", page_size=page_size)
        with pytest.raises(GrafxConfigurationError):
            MemoryStorageDevice(page_size=page_size)


# --- namespace --------------------------------------------------------------------------


def test_create_exists_list_and_remove_round_trip(device: Any) -> None:
    assert device.list_files() == ()
    assert device.exists(HEAP) is False
    device.create(HEAP)
    device.create(SEGMENT)
    device.create(READER)
    assert device.exists(HEAP) is True
    assert device.file_size(HEAP) == 0
    assert device.list_files() == (READER, HEAP, SEGMENT)
    assert device.list_files("wal/") == (SEGMENT,)
    assert device.list_files("control/readers/") == (READER,)
    assert device.list_files("nothing") == ()
    device.remove(SEGMENT)
    assert device.exists(SEGMENT) is False
    assert device.list_files() == (READER, HEAP)


def test_an_exclusive_create_refuses_an_existing_file(device: Any) -> None:
    device.create(HEAP)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.create(HEAP)
    assert raised.value.details["reason"] == "file_exists"


def test_a_shared_create_keeps_the_bytes_already_there(device: Any) -> None:
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"records")
    device.create(SEGMENT, exclusive=False)
    assert device.log_size(SEGMENT) == 7
    assert device.read_log(SEGMENT, 0, 7) == b"records"


def test_removing_a_file_that_is_not_there_is_a_typed_failure(device: Any) -> None:
    with pytest.raises(GrafxCorruptionDetected) as raised:
        device.remove(HEAP)
    assert raised.value.details["reason"] == "missing_file"


def test_a_prefix_that_is_not_a_string_is_refused(device: Any) -> None:
    with pytest.raises(GrafxUnsupportedOperation):
        device.list_files(1)


# --- paged space ------------------------------------------------------------------------


def test_allocate_grows_by_whole_zero_filled_pages(device: Any) -> None:
    device.create(HEAP)
    assert device.page_count(HEAP) == 0
    assert device.allocate(HEAP) == 0
    assert device.page_count(HEAP) == 1
    assert device.allocate(HEAP, 3) == 1
    assert device.page_count(HEAP) == 4
    assert device.file_size(HEAP) == 4 * PAGE_SIZE
    for index in range(4):
        assert device.read_page(HEAP, index) == bytes(PAGE_SIZE)


def test_write_page_then_read_page_returns_the_same_bytes(device: Any) -> None:
    device.create(HEAP)
    device.allocate(HEAP, 2)
    device.write_page(HEAP, 1, _page(0xAB))
    assert device.read_page(HEAP, 1) == _page(0xAB)
    assert device.read_page(HEAP, 0) == bytes(PAGE_SIZE)


def test_writing_a_page_that_was_never_allocated_is_refused(device: Any) -> None:
    device.create(HEAP)
    device.allocate(HEAP)
    for index in (1, 7, -1):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            device.write_page(HEAP, index, _page(0x01))
        assert raised.value.details["reason"] == "page_not_allocated"
    assert device.file_size(HEAP) == PAGE_SIZE


def test_reading_a_page_that_was_never_allocated_is_refused(device: Any) -> None:
    device.create(HEAP)
    with pytest.raises(GrafxCorruptionDetected):
        device.read_page(HEAP, 0)


def test_a_page_write_must_carry_exactly_one_page(device: Any) -> None:
    device.create(HEAP)
    device.allocate(HEAP)
    for payload in (b"", b"short", bytes(PAGE_SIZE + 1)):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            device.write_page(HEAP, 0, payload)
        assert raised.value.details["reason"] == "page_size_mismatch"
    assert device.read_page(HEAP, 0) == bytes(PAGE_SIZE)


def test_a_boolean_is_refused_where_its_number_would_be_a_valid_index(device: Any) -> None:
    # A34: the range check masks the type check whenever the file is too short for True to be
    # in range. On a multi-page file True is a perfectly valid index arithmetically, so only the
    # bool guard can refuse it, and accepting it would write the wrong page in silence.
    device.create(HEAP)
    device.allocate(HEAP, 3)
    device.write_page(HEAP, 1, _page(0x11))
    device.write_page(HEAP, 0, _page(0x22))
    for index in (True, False):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            device.write_page(HEAP, index, _page(0xEE))
        assert raised.value.details["reason"] == "page_not_allocated"
        with pytest.raises(GrafxCorruptionDetected):
            device.read_page(HEAP, index)
    assert device.read_page(HEAP, 1) == _page(0x11)
    assert device.read_page(HEAP, 0) == _page(0x22)


def test_a_boolean_size_is_refused_where_its_number_would_be_a_valid_size(device: Any) -> None:
    # The same masking on the shrink door: True is 1, which is a legal size for a file of six
    # bytes, so accepting it would silently throw five bytes away.
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"abcdef")
    for size in (True, False):
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.truncate_log(SEGMENT, size)
        assert raised.value.details["reason"] == "invalid_size"
        assert device.log_size(SEGMENT) == 6
    assert device.read_log(SEGMENT, 0, 6) == b"abcdef"


def test_allocate_refuses_a_count_that_is_not_a_positive_number(device: Any) -> None:
    device.create(HEAP)
    for count in (0, -1, True, "2"):
        with pytest.raises(GrafxUnsupportedOperation):
            device.allocate(HEAP, count)


def test_allocate_refuses_a_request_no_device_could_serve(device: Any) -> None:
    # A wrong number must fail fast instead of asking the platform for an endless file.
    device.create(HEAP)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.allocate(HEAP, MAX_ALLOCATION_PAGES + 1)
    assert raised.value.details["reason"] == "invalid_page_count"
    assert device.file_size(HEAP) == 0


def test_a_root_that_is_not_a_path_is_refused() -> None:
    for root in (7, None, object()):
        with pytest.raises(GrafxConfigurationError):
            LocalStorageDevice(root)


def test_a_paged_view_of_an_unaligned_file_is_refused(device: Any) -> None:
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"not a page")
    with pytest.raises(GrafxCorruptionDetected) as raised:
        device.page_count(SEGMENT)
    assert raised.value.details["reason"] == "unaligned_paged_file"


# --- append-only log space ---------------------------------------------------------------


def test_append_log_returns_the_new_total_size(device: Any) -> None:
    device.create(SEGMENT)
    assert device.log_size(SEGMENT) == 0
    assert device.append_log(SEGMENT, b"alpha") == 5
    assert device.append_log(SEGMENT, b"beta") == 9
    assert device.append_log(SEGMENT, b"") == 9
    assert device.read_log(SEGMENT, 0, 9) == b"alphabeta"
    assert device.read_log(SEGMENT, 5, 4) == b"beta"
    assert device.file_size(SEGMENT) == 9


def test_reading_past_the_end_returns_what_is_there(device: Any) -> None:
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"alpha")
    assert device.read_log(SEGMENT, 0, 100) == b"alpha"
    assert device.read_log(SEGMENT, 5, 10) == b""
    assert device.read_log(SEGMENT, 0, 0) == b""


def test_a_read_range_that_is_not_a_natural_number_is_refused(device: Any) -> None:
    device.create(SEGMENT)
    for offset, length in ((-1, 1), (0, -1), (True, 1), (0, "4")):
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.read_log(SEGMENT, offset, length)
        assert raised.value.details["reason"] == "invalid_range"


def test_truncate_log_shrinks_the_file(device: Any) -> None:
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"alphabeta")
    device.truncate_log(SEGMENT, 5)
    assert device.log_size(SEGMENT) == 5
    assert device.read_log(SEGMENT, 0, 9) == b"alpha"
    device.truncate_log(SEGMENT, 0)
    assert device.log_size(SEGMENT) == 0


def test_truncate_log_refuses_to_grow(device: Any) -> None:
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"alpha")
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.truncate_log(SEGMENT, 6)
    assert raised.value.details["reason"] == "truncate_would_grow"
    assert device.log_size(SEGMENT) == 5
    with pytest.raises(GrafxUnsupportedOperation):
        device.truncate_log(SEGMENT, -1)


def test_a_payload_that_is_not_bytes_is_refused(device: Any) -> None:
    device.create(SEGMENT)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.append_log(SEGMENT, "text")
    assert raised.value.details["reason"] == "not_a_payload"
    assert device.log_size(SEGMENT) == 0


def test_bytearray_and_memoryview_payloads_are_accepted(device: Any) -> None:
    device.create(SEGMENT)
    device.append_log(SEGMENT, bytearray(b"one"))
    device.append_log(SEGMENT, memoryview(b"two"))
    assert device.read_log(SEGMENT, 0, 6) == b"onetwo"


# --- atomic replace ----------------------------------------------------------------------


def test_atomic_replace_moves_the_bytes_over_an_existing_target(device: Any) -> None:
    device.create("control/commit.state")
    device.append_log("control/commit.state", b"old")
    device.create("control/commit.state.new")
    device.append_log("control/commit.state.new", b"published")
    device.atomic_replace("control/commit.state.new", "control/commit.state")
    assert device.exists("control/commit.state.new") is False
    assert device.read_log("control/commit.state", 0, 9) == b"published"
    assert device.list_files("control/") == ("control/commit.state",)


def test_atomic_replace_creates_a_target_that_is_not_there_yet(device: Any) -> None:
    device.create("control/commit.state.new")
    device.append_log("control/commit.state.new", b"first")
    device.atomic_replace("control/commit.state.new", "control/commit.state")
    assert device.read_log("control/commit.state", 0, 5) == b"first"


def test_atomic_replace_refuses_a_missing_source_and_a_single_name(device: Any) -> None:
    with pytest.raises(GrafxCorruptionDetected):
        device.atomic_replace("control/commit.state.new", "control/commit.state")
    device.create(HEAP)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.atomic_replace(HEAP, HEAP)
    assert raised.value.details["reason"] == "same_file"


# --- durability --------------------------------------------------------------------------


def test_a_durability_barrier_over_a_written_file_succeeds(device: Any) -> None:
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"record")
    device.durable_barrier(SEGMENT)
    device.durable_barrier()
    assert device.read_log(SEGMENT, 0, 6) == b"record"


def test_a_durability_barrier_over_a_missing_file_is_a_typed_failure(device: Any) -> None:
    with pytest.raises(GrafxDurabilityBarrierFailed) as raised:
        device.durable_barrier(SEGMENT)
    assert raised.value.details["reason"] == "missing_file"
    assert raised.value.retryable is False


def test_a_capacity_bound_device_refuses_to_pass_it_on_every_write_door(
    memory_device: MemoryStorageDevice,
) -> None:
    # The in-memory device is the one that can be given a size, which is how a test reaches the
    # device full path without filling a real disk.
    assert memory_device.capacity_bytes is None
    bounded = MemoryStorageDevice(page_size=PAGE_SIZE, capacity_bytes=2 * PAGE_SIZE)
    assert bounded.capacity_bytes == 2 * PAGE_SIZE
    bounded.create(SEGMENT)
    bounded.append_log(SEGMENT, bytes(2 * PAGE_SIZE))
    assert bounded.used_bytes() == 2 * PAGE_SIZE
    for probe in (
        lambda: bounded.append_log(SEGMENT, b"x"),
        lambda: bounded.allocate(SEGMENT, 1),
    ):
        with pytest.raises(GrafxDeviceFull) as raised:
            probe()
        assert raised.value.retryable is True
    assert bounded.used_bytes() == 2 * PAGE_SIZE
    bounded.truncate_log(SEGMENT, PAGE_SIZE)
    assert bounded.append_log(SEGMENT, bytes(PAGE_SIZE)) == 2 * PAGE_SIZE
    with pytest.raises(GrafxUnsupportedOperation):
        MemoryStorageDevice(page_size=PAGE_SIZE, capacity_bytes=0)


def test_the_recycled_name_never_stays_in_the_deletion_queue(device: Any) -> None:
    # A27 stated per family through the operator surface both devices expose: whatever the
    # platform did, the queue never carries the logical name that was recycled.
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"records")
    device.recycle(SEGMENT)
    assert SEGMENT not in device.pending_deletes()
    device.retry_pending_deletes()
    assert SEGMENT not in device.pending_deletes()
    device.create(SEGMENT)
    assert device.log_size(SEGMENT) == 0


# --- names -------------------------------------------------------------------------------

INADMISSIBLE_NAMES: tuple[tuple[str, object], ...] = (
    ("absolute_name", "/etc/passwd"),
    ("absolute_name", "C:/db/heap.dat"),
    ("parent_traversal", "../heap.dat"),
    ("parent_traversal", "wal/../../heap.dat"),
    ("relative_segment", "./heap.dat"),
    ("backslash_separator", "wal\\1.wal"),
    ("empty_name", ""),
    ("empty_segment", "wal//1.wal"),
    ("forbidden_character", "wal/1*.wal"),
    ("forbidden_character", "wal/1\n.wal"),
    ("reserved_device_name", "control/NUL"),
    ("reserved_device_name", "com1.dat"),
    ("reserved_infix", "wal/1.wal.pending-delete-3"),
    ("padded_segment", "wal/1.wal "),
    ("padded_segment", "wal/1."),
    ("not_a_string", 7),
    ("name_too_long", "x" * 256),
    ("segment_too_long", "wal/" + "x" * 129),
)


@pytest.mark.parametrize(("reason", "name"), INADMISSIBLE_NAMES, ids=[f"{row[0]}:{row[1]!r}" for row in INADMISSIBLE_NAMES])
def test_an_inadmissible_name_is_refused_with_its_reason(reason: str, name: object) -> None:
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        normalize_logical_name(name)
    assert raised.value.details["reason"] == reason


@pytest.mark.parametrize(("reason", "name"), INADMISSIBLE_NAMES, ids=[f"{row[0]}:{row[1]!r}" for row in INADMISSIBLE_NAMES])
def test_every_device_refuses_an_inadmissible_name(device: Any, reason: str, name: object) -> None:
    for call in (
        lambda: device.exists(name),
        lambda: device.create(name),
        lambda: device.remove(name),
        lambda: device.file_size(name),
        lambda: device.append_log(name, b"x"),
        lambda: device.read_log(name, 0, 1),
        lambda: device.recycle(name),
    ):
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            call()
        assert raised.value.details["reason"] == reason
    # A28: the barrier door raises one type whatever went wrong, and carries the reason.
    with pytest.raises(GrafxDurabilityBarrierFailed) as barrier:
        device.durable_barrier(name)
    assert barrier.value.details["reason"] == reason


def test_a_name_that_differs_only_by_case_is_refused_not_overwritten(device: Any) -> None:
    device.create(HEAP)
    device.allocate(HEAP)
    device.write_page(HEAP, 0, _page(0x7A))
    for name in ("HEAP.DAT", "Heap.dat"):
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.create(name)
        assert raised.value.details["reason"] == "case_collision"
        assert raised.value.details["stored"] == HEAP
        with pytest.raises(GrafxUnsupportedOperation):
            device.exists(name)
    assert device.list_files() == (HEAP,)
    assert device.read_page(HEAP, 0) == _page(0x7A)


def test_a_directory_that_differs_only_by_case_is_refused(device: Any) -> None:
    device.create(SEGMENT)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.create("WAL/000000000002.wal")
    assert raised.value.details["reason"] == "case_collision"
    assert device.list_files() == (SEGMENT,)


def test_a_directory_outlives_the_files_that_made_it(device: Any) -> None:
    # A real directory stays behind when its last file goes, so the twin keeps it too: without
    # that, the two devices answer differently about a name that differs only by case.
    device.create(SEGMENT)
    device.remove(SEGMENT)
    assert device.list_files() == ()
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.create("WAL/000000000002.wal")
    assert raised.value.details["reason"] == "case_collision"
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.create("wal")
    assert raised.value.details["reason"] == "not_a_file"
    device.create("wal/000000000002.wal")
    assert device.list_files() == ("wal/000000000002.wal",)


def test_a_directory_can_never_become_the_target_of_a_replace(device: Any) -> None:
    device.create(SEGMENT)
    device.create("staging.tmp")
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.atomic_replace("staging.tmp", "wal")
    assert raised.value.details["reason"] == "not_a_file"
    assert device.list_files() == ("staging.tmp", SEGMENT)


def test_the_reserved_infix_is_refused_whatever_its_case() -> None:
    # A case insensitive volume would otherwise let a logical name shadow a queued deletion.
    for spelling in ("pending-delete-1", "PENDING-DELETE-1", "Pending-Delete-9"):
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            normalize_logical_name(f"wal/000000000001.wal.{spelling}")
        assert raised.value.details["reason"] == "reserved_infix"
    assert PENDING_DELETE_MARKER == ".pending-delete-"


def test_an_argument_is_validated_before_the_state_of_the_device(device: Any) -> None:
    # Both families answer the same question first, so a caller cannot tell them apart by the
    # error it gets for a wrong argument on a closed device.
    device.close()
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.list_files(5)
    assert raised.value.details["reason"] == "not_a_string"
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.exists("../escape")
    assert raised.value.details["reason"] == "parent_traversal"


def test_the_case_conflict_finder_ignores_an_exact_match() -> None:
    assert find_case_conflict("heap.dat", ("heap.dat", "wal")) is None
    assert find_case_conflict("HEAP.dat", ("heap.dat",)) == "heap.dat"
    assert find_case_conflict("other.dat", ("heap.dat",)) is None


def test_a_name_that_already_holds_other_files_cannot_become_a_file(device: Any) -> None:
    # The only divergence a seeded differential run found between the two families: a file must
    # not be allowed to shadow a directory of the namespace on one medium and not on the other.
    device.create("a/b/c.dat")
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.create("a")
    assert raised.value.details["reason"] == "not_a_file"
    with pytest.raises(GrafxUnsupportedOperation):
        device.create("a/b")
    assert device.list_files() == ("a/b/c.dat",)


def test_a_name_whose_parent_is_a_file_is_refused(device: Any) -> None:
    device.create(HEAP)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.create("heap.dat/inner.dat")
    assert raised.value.details["reason"] == "not_a_directory"


# --- lifecycle ---------------------------------------------------------------------------


def test_a_closed_device_refuses_every_call(device: Any) -> None:
    device.create(HEAP)
    device.close()
    for call in (
        lambda: device.exists(HEAP),
        lambda: device.create("other.dat"),
        lambda: device.remove(HEAP),
        lambda: device.list_files(),
        lambda: device.file_size(HEAP),
        lambda: device.append_log(HEAP, b"x"),
        lambda: device.log_size(HEAP),
        lambda: device.recycle(HEAP),
    ):
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            call()
        assert raised.value.details["reason"] == "device_closed"
    with pytest.raises(GrafxDurabilityBarrierFailed) as barrier:
        device.durable_barrier()
    assert barrier.value.details["reason"] == "device_closed"
    device.close()


def test_the_device_is_a_context_manager(tmp_path: Any) -> None:
    with LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE) as opened:
        opened.create(HEAP)
    with pytest.raises(GrafxUnsupportedOperation):
        opened.exists(HEAP)
    with MemoryStorageDevice(page_size=PAGE_SIZE) as buffered:
        buffered.create(HEAP)
    with pytest.raises(GrafxUnsupportedOperation):
        buffered.exists(HEAP)


def test_a_local_device_reopens_over_the_bytes_it_left_behind(tmp_path: Any) -> None:
    root = tmp_path / "db"
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as first:
        first.create(SEGMENT)
        first.append_log(SEGMENT, b"durable")
        first.durable_barrier(SEGMENT)
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as second:
        assert second.list_files() == (SEGMENT,)
        assert second.read_log(SEGMENT, 0, 7) == b"durable"


def test_the_memory_device_reopens_over_the_bytes_it_kept(memory_device: MemoryStorageDevice) -> None:
    memory_device.create(SEGMENT)
    memory_device.append_log(SEGMENT, b"durable")
    memory_device.close()
    memory_device.reopen()
    assert memory_device.read_log(SEGMENT, 0, 7) == b"durable"


# --- no failure escapes the taxonomy ------------------------------------------------------


def test_every_failure_of_the_device_is_a_grafx_error(device: Any) -> None:
    device.create(HEAP)
    device.allocate(HEAP)
    device.create(SEGMENT)
    probes = (
        lambda: device.create(HEAP),
        lambda: device.remove("missing.dat"),
        lambda: device.read_page(HEAP, 9),
        lambda: device.write_page(HEAP, 9, _page(1)),
        lambda: device.write_page(HEAP, 0, b"short"),
        lambda: device.allocate(HEAP, 0),
        lambda: device.append_log("missing.dat", b"x"),
        lambda: device.append_log(SEGMENT, None),
        lambda: device.read_log(SEGMENT, -1, 1),
        lambda: device.log_size("missing.dat"),
        lambda: device.truncate_log(SEGMENT, 10),
        lambda: device.page_count("missing.dat"),
        lambda: device.durable_barrier("missing.dat"),
        lambda: device.atomic_replace("missing.dat", HEAP),
        lambda: device.exists("../escape"),
        lambda: device.list_files(None),
        lambda: device.file_size("missing.dat"),
    )
    for probe in probes:
        with pytest.raises(GrafxError) as raised:
            probe()
        # A concrete class of the taxonomy, never the base class and never a bare Exception.
        assert type(raised.value) is not GrafxError
        assert raised.value.message
        assert raised.value.message.isascii()
        assert raised.value.code


def test_the_two_families_answer_a_failure_with_the_same_type(tmp_path: Any) -> None:
    # TR-3: one contract, two mechanisms. The twin is only useful when the failure it produces
    # is indistinguishable from the failure of the real device.
    with LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE) as local:
        with MemoryStorageDevice(page_size=PAGE_SIZE) as memory:
            for subject in (local, memory):
                subject.create("heap.dat")
            for probe in (
                lambda subject: subject.durable_barrier("missing.dat"),
                lambda subject: subject.durable_barrier("../escape"),
                lambda subject: subject.durable_barrier("HEAP.DAT"),
                lambda subject: subject.durable_barrier("con"),
                lambda subject: subject.remove("missing.dat"),
                lambda subject: subject.read_page("missing.dat", 0),
                lambda subject: subject.append_log("missing.dat", b"x"),
                lambda subject: subject.durable_barrier("missing.dat"),
                lambda subject: subject.exists("../escape"),
                lambda subject: subject.create("con"),
                lambda subject: subject.list_files(3),
                lambda subject: subject.truncate_log("missing.dat", 0),
                lambda subject: subject.allocate("missing.dat", 1),
                lambda subject: subject.write_page("missing.dat", 0, bytes(PAGE_SIZE)),
            ):
                with pytest.raises(GrafxError) as local_failure:
                    probe(local)
                with pytest.raises(GrafxError) as memory_failure:
                    probe(memory)
                assert type(local_failure.value) is type(memory_failure.value)
                assert local_failure.value.details.get("reason") == memory_failure.value.details.get("reason")
                assert local_failure.value.retryable == memory_failure.value.retryable
                assert local_failure.value.details.get("retryable") == memory_failure.value.details.get(
                    "retryable"
                )


def test_a_handle_of_this_device_never_blocks_a_deletion(tmp_path: Any) -> None:
    # A16, stated as the behaviour rather than as a flag: whoever holds a file through this
    # adapter must not stop anybody else from deleting it. True on POSIX by unlink semantics,
    # and on Windows only because the descriptor is opened with FILE_SHARE_DELETE.
    with LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE) as device:
        device.create(SEGMENT)
        device.append_log(SEGMENT, b"records")
        path = Path(device.root) / "wal" / "000000000001.wal"
        assert device.read_log(SEGMENT, 0, 7) == b"records"
        os.remove(path)  # not blocked by the descriptor this device holds: that is the property
        # The name no longer names a file, and the device says so rather than serving the
        # unlinked bytes through the descriptor it still holds: a name that reads as present
        # after its deletion is the same lie as a descriptor that reads a replaced file.
        with pytest.raises(GrafxCorruptionDetected):
            device.read_log(SEGMENT, 0, 7)
