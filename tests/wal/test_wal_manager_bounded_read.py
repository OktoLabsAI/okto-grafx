"""Exact, work-bounded WAL reads used by optional cache invalidation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxTransactionStateError
from okto_grafx.engine.wal_manager import WalManager

from .conftest import make_record


class ReadRecordingDevice:
    """Forward storage calls while recording physical WAL reads."""

    def __init__(self, inner: MemoryStorageDevice) -> None:
        self._inner = inner
        self.reads: list[tuple[str, int, int]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        self.reads.append((file, offset, length))
        return self._inner.read_log(file, offset, length)


def test_an_exact_retained_range_is_returned_across_segments(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The bounded door returns every requested LSN, and no neighbouring record."""
    manager = make_wal(memory_device, segment_bytes=300)
    for txn_id in range(8):
        manager.append(make_record(txn_id + 1, payload=bytes(96)))
    segments = manager.segments()
    assert len(segments) >= 3

    first = segments[0].last_lsn
    through = segments[-1].first_lsn
    records = manager.read_bounded(
        first,
        through,
        max_records=through - first + 1,
        max_bytes=manager.total_bytes(),
    )

    assert records is not None
    assert [record.lsn for record in records] == list(range(first, through + 1))


def test_budgets_are_checked_before_physical_read_and_accept_the_exact_boundary(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Both limits are inclusive, and an exceeded limit performs no segment read."""
    device = ReadRecordingDevice(memory_device)
    manager = make_wal(device, segment_bytes=300)
    for txn_id in range(6):
        manager.append(make_record(txn_id + 1, payload=bytes(96)))
    first = manager.segments()[0].first_lsn
    through = manager.last_lsn
    record_count = through - first + 1
    byte_count = manager.total_bytes()

    device.reads.clear()
    assert (
        manager.read_bounded(
            first,
            through,
            max_records=record_count - 1,
            max_bytes=byte_count,
        )
        is None
    )
    assert device.reads == []

    assert (
        manager.read_bounded(
            first,
            through,
            max_records=record_count,
            max_bytes=byte_count - 1,
        )
        is None
    )
    assert device.reads == []

    records = manager.read_bounded(
        first,
        through,
        max_records=record_count,
        max_bytes=byte_count,
    )
    assert records is not None
    assert [record.lsn for record in records] == list(range(first, through + 1))
    assert sum(length for _file, _offset, length in device.reads) == byte_count


def test_foreign_tail_refresh_and_interval_share_one_physical_byte_budget(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A large foreign append declines before read; a fitting refresh is charged once."""
    first = make_wal(memory_device, segment_bytes=4096)
    first.append(make_record(1, payload=bytes(32)))
    first.barrier()
    device = ReadRecordingDevice(memory_device)
    second = make_wal(device, segment_bytes=4096)
    assert second.last_lsn == first.last_lsn

    tail = first.segments()[-1].name
    before = memory_device.log_size(tail)
    foreign_lsn = first.append(make_record(2, payload=bytes(256)))
    first.barrier()
    appended_bytes = memory_device.log_size(tail) - before
    interval_bytes = memory_device.log_size(tail)
    assert appended_bytes > 1
    assert interval_bytes > appended_bytes

    device.reads.clear()
    assert (
        second.read_bounded(
            foreign_lsn,
            foreign_lsn,
            max_records=1,
            max_bytes=appended_bytes - 1,
        )
        is None
    )
    assert device.reads == []

    device.reads.clear()
    assert (
        second.read_bounded(
            foreign_lsn,
            foreign_lsn,
            max_records=1,
            # Each phase fits this budget by itself; only their sum exceeds it. A mutant that
            # restarts the counter after refreshing the foreign tail would incorrectly succeed.
            max_bytes=interval_bytes,
        )
        is None
    )
    assert sum(length for _file, _offset, length in device.reads) == appended_bytes

    device.reads.clear()
    records = second.read_bounded(
        foreign_lsn,
        foreign_lsn,
        max_records=1,
        max_bytes=interval_bytes,
    )
    assert records is not None
    assert [record.lsn for record in records] == [foreign_lsn]
    assert sum(length for _file, _offset, length in device.reads) == interval_bytes


def test_a_recycled_prefix_or_missing_terminal_returns_none_without_reading(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """A shorter retained stream is never mistaken for the complete requested interval."""
    device = ReadRecordingDevice(memory_device)
    manager = make_wal(device, segment_bytes=300)
    for txn_id in range(8):
        manager.append(make_record(txn_id + 1, payload=bytes(96)))
    before = manager.segments()
    assert len(before) >= 3
    recycled_first = before[0].first_lsn
    report = manager.recycle(before[1].first_lsn, reader_present=False)
    assert before[0].name in report.recycled

    device.reads.clear()
    through = manager.last_lsn
    assert (
        manager.read_bounded(
            recycled_first,
            through,
            max_records=through - recycled_first + 1,
            max_bytes=manager.total_bytes(),
        )
        is None
    )
    assert (
        manager.read_bounded(
            manager.segments()[0].first_lsn,
            through + 1,
            max_records=through + 1,
            max_bytes=manager.total_bytes(),
        )
        is None
    )
    assert device.reads == []


def test_known_damage_or_an_uncertain_append_returns_none(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """Neither unsafe WAL state can be used as a cache-coherence certificate."""
    damaged = make_wal(memory_device, directory="damaged")
    damaged.append(make_record())
    first = damaged.segments()[0].first_lsn
    through = damaged.last_lsn
    memory_device.append_log(damaged.segments()[-1].name, bytes(64))
    assert (
        damaged.read_bounded(
            first, through, max_records=through - first + 1, max_bytes=4096
        )
        is None
    )
    assert damaged.damage is not None

    uncertain = make_wal(memory_device, directory="uncertain")
    uncertain.append(make_record())
    uncertain._append_uncertain = True  # noqa: SLF001 - plant the recovery latch itself
    assert (
        uncertain.read_bounded(
            uncertain.segments()[0].first_lsn,
            uncertain.last_lsn,
            max_records=2,
            max_bytes=4096,
        )
        is None
    )


@pytest.mark.parametrize(
    ("arguments", "field"),
    (
        (
            {"first_lsn": 2, "through_lsn": 1, "max_records": 1, "max_bytes": 1},
            "through_lsn",
        ),
        (
            {"first_lsn": 1, "through_lsn": 1, "max_records": 0, "max_bytes": 1},
            "max_records",
        ),
        (
            {"first_lsn": 1, "through_lsn": 1, "max_records": 1, "max_bytes": 0},
            "max_bytes",
        ),
    ),
)
def test_invalid_bounded_read_arguments_are_typed_refusals(
    wal: WalManager, arguments: dict[str, int], field: str
) -> None:
    """Programming errors are distinct from a valid range that cannot be proved."""
    with pytest.raises(GrafxConfigurationError) as caught:
        wal.read_bounded(**arguments)
    assert caught.value.details["field"] == field


def test_the_bounded_read_door_refuses_before_open(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice
) -> None:
    """The new public read door obeys the manager lifecycle."""
    manager = make_wal(memory_device, open_now=False)
    with pytest.raises(GrafxTransactionStateError) as caught:
        manager.read_bounded(1, 1, max_records=1, max_bytes=1)
    assert caught.value.details["state"] == "closed"
