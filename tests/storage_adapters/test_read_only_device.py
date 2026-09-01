"""Fail-closed contract for the read-only storage capability."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from okto_grafx.adapters import storage_local
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_read_only import ReadOnlyStorageDevice
from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.domain.ports import StorageDevice

PAGE: bytes = bytes(range(256)) * 2
LOG_SLICE: bytes = bytes(range(31))
FILES: tuple[str, ...] = ("heap.dat", "wal/0000000000000001.wal")


class _RecordingDevice:
    """A storage port that records every door and bombs if a mutation reaches it."""

    def __init__(self, failure: BaseException | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.failure = failure

    @property
    def name(self) -> str:
        self.calls.append(("name",))
        return "recording"

    @property
    def page_size(self) -> int:
        self.calls.append(("page_size",))
        return len(PAGE)

    def exists(self, file: str) -> bool:
        self.calls.append(("exists", file))
        return True

    def create(self, file: str, *, exclusive: bool = True) -> None:
        self._mutate("create", file, exclusive)

    def remove(self, file: str) -> None:
        self._mutate("remove", file)

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        self.calls.append(("list_files", prefix))
        return FILES

    def file_size(self, file: str) -> int:
        self.calls.append(("file_size", file))
        return 1_337

    def atomic_replace(self, source: str, target: str) -> None:
        self._mutate("atomic_replace", source, target)

    def recycle(self, file: str) -> bool:
        self._mutate("recycle", file)

    def page_count(self, file: str) -> int:
        self.calls.append(("page_count", file))
        return 7

    def allocate(self, file: str, count: int = 1) -> int:
        self._mutate("allocate", file, count)

    def read_page(self, file: str, page_index: int) -> bytes:
        self.calls.append(("read_page", file, page_index))
        return PAGE

    def write_page(self, file: str, page_index: int, data: bytes) -> None:
        self._mutate("write_page", file, page_index, data)

    def append_log(self, file: str, payload: bytes) -> int:
        self._mutate("append_log", file, payload)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        self.calls.append(("read_log", file, offset, length))
        return LOG_SLICE

    def log_size(self, file: str) -> int:
        self.calls.append(("log_size", file))
        return 2_048

    def truncate_log(self, file: str, size: int) -> None:
        self._mutate("truncate_log", file, size)

    def durable_barrier(self, file: str | None = None) -> None:
        self._mutate("durable_barrier", file)

    def close(self) -> None:
        self.calls.append(("close",))
        if self.failure is not None:
            raise self.failure

    def _mutate(self, operation: str, *arguments: object) -> None:
        self.calls.append((operation, *arguments))
        if self.failure is not None:
            raise self.failure
        raise AssertionError(f"read-only wrapper reached {operation}")


class _RecordingIdentityDevice(_RecordingDevice):
    """A storage port that advertises the optional cache-only invalidation capability."""

    def invalidate_descriptor_identity(self, file: str | None = None) -> None:
        self.calls.append(("invalidate_descriptor_identity", file))


READ_CASES: tuple[
    tuple[str, Callable[[ReadOnlyStorageDevice], object], object, tuple[object, ...]], ...
] = (
    ("name", lambda device: device.name, "recording", ("name",)),
    ("page_size", lambda device: device.page_size, len(PAGE), ("page_size",)),
    ("exists", lambda device: device.exists("heap.dat"), True, ("exists", "heap.dat")),
    ("list_files", lambda device: device.list_files(""), FILES, ("list_files", "")),
    ("file_size", lambda device: device.file_size("heap.dat"), 1_337, ("file_size", "heap.dat")),
    ("page_count", lambda device: device.page_count("heap.dat"), 7, ("page_count", "heap.dat")),
    ("read_page", lambda device: device.read_page("heap.dat", 3), PAGE, ("read_page", "heap.dat", 3)),
    ("read_log", lambda device: device.read_log("wal/1", 2, 31), LOG_SLICE, ("read_log", "wal/1", 2, 31)),
    ("log_size", lambda device: device.log_size("wal/1"), 2_048, ("log_size", "wal/1")),
)


@pytest.mark.parametrize(("_door", "read", "expected", "call"), READ_CASES)
def test_every_observation_is_delegated_once_without_transforming_the_answer(
    _door: str,
    read: Callable[[ReadOnlyStorageDevice], object],
    expected: object,
    call: tuple[object, ...],
) -> None:
    inner = _RecordingDevice()
    device = ReadOnlyStorageDevice(inner)
    answer = read(device)
    assert answer == expected
    if isinstance(expected, bytes):
        assert answer is expected
    assert inner.calls == [call]


MUTATION_CASES: tuple[
    tuple[str, Callable[[ReadOnlyStorageDevice], object]], ...
] = (
    ("create", lambda device: device.create("new", exclusive=False)),
    ("remove", lambda device: device.remove("old")),
    ("atomic_replace", lambda device: device.atomic_replace("new", "old")),
    ("recycle", lambda device: device.recycle("old")),
    ("allocate", lambda device: device.allocate("heap.dat", 2)),
    ("write_page", lambda device: device.write_page("heap.dat", 3, PAGE)),
    ("append_log", lambda device: device.append_log("wal/1", b"record")),
    ("truncate_log", lambda device: device.truncate_log("wal/1", 4)),
    ("durable_barrier", lambda device: device.durable_barrier("heap.dat")),
)


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize(("door", "mutate"), MUTATION_CASES)
def test_every_mutation_is_typed_and_refused_before_the_real_port(
    door: str,
    mutate: Callable[[ReadOnlyStorageDevice], object],
    failure_type: type[BaseException],
) -> None:
    inner = _RecordingDevice(failure_type("must remain unreachable"))
    device = ReadOnlyStorageDevice(inner)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        mutate(device)
    assert raised.value.details == {"operation": door, "read_only": True}
    assert inner.calls == []


def test_the_wrapper_satisfies_the_port_without_a_public_raw_escape() -> None:
    inner = _RecordingDevice()
    device = ReadOnlyStorageDevice(inner)
    assert isinstance(device, StorageDevice)
    assert not hasattr(device, "inner")
    assert not hasattr(device, "device")
    assert not hasattr(device, "__dict__")


@pytest.mark.parametrize(
    "file", (None, "heap.dat"), ids=("whole-generation", "one-file")
)
def test_descriptor_identity_invalidation_is_forwarded_without_becoming_a_mutation(
    file: str | None,
) -> None:
    inner = _RecordingIdentityDevice()
    device = ReadOnlyStorageDevice(inner)

    device.invalidate_descriptor_identity(file)

    assert inner.calls == [("invalidate_descriptor_identity", file)]


def test_missing_descriptor_identity_capability_is_a_no_op() -> None:
    inner = _RecordingDevice()
    device = ReadOnlyStorageDevice(inner)

    device.invalidate_descriptor_identity()

    assert inner.calls == []


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt, SystemExit))
def test_close_and_context_exit_are_non_owning(failure_type: type[BaseException]) -> None:
    inner = _RecordingDevice(failure_type("raw close must remain unreachable"))
    device = ReadOnlyStorageDevice(inner)
    device.close()
    with device:
        pass
    assert inner.calls == []


def test_close_does_not_retry_or_remove_a_real_pending_delete(
    local_device: LocalStorageDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    segment = "wal/read-only-pending.wal"
    local_device.create(segment)
    local_device.append_log(segment, b"must remain")

    def refuse_removal(path: str) -> None:
        raise PermissionError(13, f"held for the non-owning close probe: {path}")

    monkeypatch.setattr(storage_local, "_remove_file", refuse_removal)
    assert local_device.recycle(segment) is False
    queued = local_device.pending_deletes()
    queued_paths = tuple(Path(local_device.root).joinpath(*name.split("/")) for name in queued)
    assert queued and all(path.exists() for path in queued_paths)
    monkeypatch.undo()

    with ReadOnlyStorageDevice(local_device):
        pass

    assert local_device.pending_deletes() == queued
    assert all(path.exists() for path in queued_paths)
    assert local_device.page_size == len(PAGE), "the raw owner remains open for its real closer"
