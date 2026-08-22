"""A crash at every write point of the commit path (SPEC-M1 AC-4, AC-10, AC-11; BR-4).

Recovery belongs to C6 and does not exist yet, so this suite asserts the invariants a recovery
would need rather than running one. They are the invariants the commit protocol exists to
provide, and each one is checked against the bytes actually on the device after the process was
cut off:

* **published implies logged** -- if ``control/commit.state`` names a commit, the COMMIT record
  of that commit is in the log and intact. Otherwise a reader would take a snapshot at a number
  no replay could reconstruct.
* **applied implies logged** -- if a heap page carries the commit's number, the WRITE_PAGE record
  that produced it is in the log. Otherwise a page would exist that recovery could not redo, and
  a crash one moment earlier would have lost it for good.
* **acknowledged implies durable** -- when the commit returned, its COMMIT record survived and
  the barrier had returned before the acknowledgement (BR-4).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from okto_grafx.adapters.storage_fault import (
    FaultInjectingStorageDevice,
    SimulatedCrash,
    WritePoint,
)
from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.ids import NO_LSN
from okto_grafx.domain.txn import (
    WalRecordType,
    decode_page_write,
)
from shared_device import SharedDirectoryDevice
from txn_support import DEFAULT_PAGE_SIZE, LogWal, Stack, build_stack, make_page_image

HEAP = "heap.dat"
PAGES: tuple[int, ...] = (3, 4)


def _commit_workload(stack: Stack, payload: bytes = b"crashy") -> object:
    """Run one commit that touches two pages, so a crash can land between them."""
    txn = stack.manager.begin("write")
    for page in PAGES:
        txn.stage_page_image(
            HEAP, page, make_page_image(stack.codec, [payload], page_index=page)
        )
    txn.note_write(stack.manager.partition_of(1, payload))
    return stack.manager.commit(txn)


def _write_points(tmp_path: Path) -> tuple[WritePoint, ...]:
    """Learn every write point of one commit by running it once with nothing armed."""
    root = tmp_path / "survey"
    device = FaultInjectingStorageDevice(
        SharedDirectoryDevice(root, page_size=DEFAULT_PAGE_SIZE), seed=11
    )
    stack = build_stack(root, storage=device)
    return device.enumerate_write_points(lambda _device: _commit_workload(stack))


def _inspect(root: Path, stack: Stack) -> tuple[int, dict[int, int], tuple[int, ...]]:
    """Return the published commit number, the readable log, and the page numbers on the device."""
    published = stack.manager.published_state().last_committed_lsn
    logged: dict[int, int] = {}
    for record in LogWal(stack.storage).records():
        logged[record.lsn] = record.record_type
    numbers: list[int] = []
    for page in PAGES:
        if stack.storage.exists(HEAP) and stack.storage.page_count(HEAP) > page:
            raw = stack.storage.read_page(HEAP, page)
            try:
                numbers.append(stack.codec.decode_page(raw, verify=True).page_lsn)
            except GrafxError:
                numbers.append(NO_LSN)
        else:
            numbers.append(NO_LSN)
    return published, logged, tuple(numbers)


def _logged_page_numbers(stack: Stack) -> set[int]:
    """Return every commit number a readable WRITE_PAGE record in the log would restore."""
    restored: set[int] = set()
    for record in LogWal(stack.storage).records():
        if record.record_type != WalRecordType.WRITE_PAGE:
            continue
        written = decode_page_write(record.payload)
        if written.file != HEAP:
            continue
        restored.add(stack.codec.decode_page(written.image, verify=True).page_lsn)
    return restored


def test_the_survey_finds_the_write_points_of_a_commit(tmp_path: Path) -> None:
    """A crash battery whose enumeration is empty measures nothing (amendment A75.2)."""
    points = _write_points(tmp_path)
    assert len(points) >= 8
    methods = {point.method for point in points}
    assert "append_log" in methods
    assert "write_page" in methods
    assert "atomic_replace" in methods


@pytest.mark.slow
def test_a_crash_at_every_write_point_leaves_a_recoverable_database(tmp_path: Path) -> None:
    """AC-4: at every point, what is published is logged and what is applied is logged."""
    points = _write_points(tmp_path)
    assert points, "no write point was found, so nothing was tested"
    crashed = 0
    survived = 0
    for point in points:
        root = tmp_path / f"crash-{point.call_index:04d}"
        device = FaultInjectingStorageDevice(
            SharedDirectoryDevice(root, page_size=DEFAULT_PAGE_SIZE), seed=11
        )
        stack = build_stack(root, storage=device)
        device.crash_at(point.call_index, moment="after")
        acknowledged = False
        try:
            _commit_workload(stack)
            acknowledged = True
            survived += 1
        except SimulatedCrash:
            crashed += 1
        except GrafxError:
            # A typed refusal is a legal outcome of a commit; the invariants below still hold.
            pass
        device.disarm()
        reopened = build_stack(root, storage=device, owner_id="reopened")
        published, logged, numbers = _inspect(root, reopened)
        if published != NO_LSN:
            assert logged.get(published) == WalRecordType.COMMIT, (
                f"point {point.call_index} published {published} with no COMMIT record for it"
            )
        restorable = _logged_page_numbers(reopened)
        for number in numbers:
            if number == NO_LSN:
                continue
            assert number in restorable, (
                f"point {point.call_index} left page number {number} that no log record restores"
            )
        if acknowledged:
            assert published != NO_LSN
            assert logged.get(published) == WalRecordType.COMMIT
    assert crashed > 0, "no crash was ever injected, so this battery proved nothing"
    assert survived < len(points), "every run completed, so no crash point was real"


@pytest.mark.slow
def test_a_full_device_at_every_write_point_never_publishes_what_it_did_not_log(
    tmp_path: Path,
) -> None:
    """AC-10: disk-full in the middle of a commit leaves nothing partial behind."""
    points = _write_points(tmp_path)
    refused = 0
    for point in points:
        root = tmp_path / f"full-{point.call_index:04d}"
        device = FaultInjectingStorageDevice(
            SharedDirectoryDevice(root, page_size=DEFAULT_PAGE_SIZE), seed=13
        )
        stack = build_stack(root, storage=device)
        device.fill_device_at(point.call_index)
        try:
            _commit_workload(stack)
        except GrafxError:
            refused += 1
        except SimulatedCrash:  # pragma: no cover - no crash is armed in this battery
            pytest.fail("no crash was armed")
        device.disarm()
        reopened = build_stack(root, storage=device, owner_id="reopened")
        published, logged, numbers = _inspect(root, reopened)
        if published != NO_LSN:
            assert logged.get(published) == WalRecordType.COMMIT
        restorable = _logged_page_numbers(reopened)
        assert {number for number in numbers if number != NO_LSN} <= restorable
    assert refused > 0, "the device never refused anything, so this battery proved nothing"


def test_a_lying_barrier_is_the_device_lying_and_not_the_order_being_wrong(
    database_root: Path,
) -> None:
    """AC-11 and TS-11: the trail proves the ordering; a dishonest fsync is the device's fault."""
    device = FaultInjectingStorageDevice(
        SharedDirectoryDevice(database_root, page_size=DEFAULT_PAGE_SIZE), seed=17
    )
    stack = build_stack(database_root, storage=device)
    device.lie_on_barrier(enabled=True)
    device.clear_trail()
    report = _commit_workload(stack)
    device.lie_on_barrier(enabled=False)
    trail = [(record.method, record.file) for record in device.trail()]
    barrier = _first(trail, "durable_barrier", stack.wal.file)
    publish = _last_replace(trail)
    appends = [index for index, entry in enumerate(trail) if entry == ("append_log", stack.wal.file)]
    assert appends and max(appends) < barrier < publish
    assert report.durable is True


def _first(trail: Sequence[tuple[str, str | None]], method: str, file: str) -> int:
    """Return the index of the first call of one method on one file."""
    for index, entry in enumerate(trail):
        if entry == (method, file):
            return index
    raise AssertionError(f"{method} on {file!r} never happened")


def _last_replace(trail: Sequence[tuple[str, str | None]]) -> int:
    """Return the index of the last atomic replace in the trail."""
    for index in range(len(trail) - 1, -1, -1):
        if trail[index][0] == "atomic_replace":
            return index
    raise AssertionError("nothing was ever published")
