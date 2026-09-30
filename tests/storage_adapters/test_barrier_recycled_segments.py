"""A global durability barrier and a WAL segment another participant recycled (issue #11).

A handle that has read or written a WAL segment keeps its descriptor cached. When a second
process checkpoints and recycles that segment, the name is gone from the directory. A barrier
that names no file flushes every cached descriptor, and used to refuse the whole barrier over a
file this device owed nothing for. The rule pinned here is narrow on purpose: only a CLEAN cached
WAL segment, found missing by a barrier that named no file, is skipped and uncached. A file this
device still owes a flush for, a file the caller named, and any file that is not a WAL segment
still fail the barrier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.errors import GrafxDurabilityBarrierFailed

PAGE_SIZE: int = 512
SEGMENT: str = "wal/000000000001.wal"
KEEP: str = "wal/000000000002.wal"


@pytest.fixture
def device(tmp_path: Path) -> LocalStorageDevice:
    local = LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE)
    yield local
    local.close()


def _flushed_segment(device: LocalStorageDevice, name: str) -> None:
    """Create a segment, write it and flush it, leaving a clean cached descriptor behind."""
    device.create(name)
    device.append_log(name, b"records")
    device.durable_barrier()
    assert name not in device._dirty
    assert name in device._handles


def _vanish(device: LocalStorageDevice, name: str) -> None:
    """Delete the file the way another process's recycle does, behind this device's back."""
    (Path(device.root) / name).unlink()


def test_clean_cached_segment_recycled_elsewhere_is_skipped_and_uncached(
    device: LocalStorageDevice,
) -> None:
    _flushed_segment(device, SEGMENT)
    _flushed_segment(device, KEEP)
    _vanish(device, SEGMENT)
    device.durable_barrier()
    assert SEGMENT not in device._handles
    assert KEEP in device._handles
    device.durable_barrier()


def test_dirty_segment_that_vanished_still_fails_the_barrier(
    device: LocalStorageDevice,
) -> None:
    device.create(SEGMENT)
    device.append_log(SEGMENT, b"records this device still owes to disk")
    assert SEGMENT in device._dirty
    _vanish(device, SEGMENT)
    with pytest.raises(GrafxDurabilityBarrierFailed):
        device.durable_barrier()
    assert SEGMENT in device._dirty


def test_a_segment_named_explicitly_still_fails_when_it_vanished(
    device: LocalStorageDevice,
) -> None:
    _flushed_segment(device, SEGMENT)
    _vanish(device, SEGMENT)
    with pytest.raises(GrafxDurabilityBarrierFailed):
        device.durable_barrier(SEGMENT)


def test_a_clean_file_that_is_not_a_wal_segment_still_fails_when_it_vanished(
    device: LocalStorageDevice,
) -> None:
    name = "heap.dat"
    device.create(name)
    device.append_log(name, b"records")
    device.durable_barrier()
    assert name not in device._dirty
    _vanish(device, name)
    with pytest.raises(GrafxDurabilityBarrierFailed):
        device.durable_barrier()
