"""CAT-3: configurable descriptor capacity beyond the former 64-file knee."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.adapters.storage_local import (
    MAX_OPEN_FILES,
    DescriptorCacheStats,
    LocalStorageDevice,
)
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.runtime.config import DEFAULT_MAX_OPEN_FILES, DatabaseConfig

PAGE_SIZE = 512
TABLES = 64
ARTIFACTS_PER_TABLE = 3


def _physical(root: Path, logical: str) -> Path:
    """Resolve a test-owned logical name without relying on adapter internals."""
    return root.joinpath(*logical.split("/"))


def _disk_identity(
    root: Path, names: tuple[str, ...]
) -> tuple[tuple[str, int, int, int, bytes], ...]:
    """Capture namespace identity and bytes so cache policy cannot change persistence."""
    captured: list[tuple[str, int, int, int, bytes]] = []
    for name in names:
        path = _physical(root, name)
        information = path.stat()
        captured.append(
            (
                name,
                information.st_dev,
                information.st_ino,
                information.st_size,
                path.read_bytes(),
            )
        )
    return tuple(captured)


def test_default_capacity_is_aligned_for_direct_and_composed_devices(
    tmp_path: Path,
) -> None:
    assert MAX_OPEN_FILES == 256
    assert DEFAULT_MAX_OPEN_FILES == MAX_OPEN_FILES
    assert DatabaseConfig(path=":memory:").max_open_files == MAX_OPEN_FILES

    with LocalStorageDevice(tmp_path / "direct", page_size=PAGE_SIZE) as direct:
        assert direct._max_open_files == MAX_OPEN_FILES

    with connect(tmp_path / "composed", page_size=PAGE_SIZE) as database:
        assert type(database._storage) is LocalStorageDevice
        assert database._storage._max_open_files == MAX_OPEN_FILES


@pytest.mark.parametrize("invalid", (0, -1, True, 1.5, "256", None))
def test_direct_capacity_refuses_invalid_values_before_creating_the_root(
    tmp_path: Path, invalid: object
) -> None:
    root = tmp_path / "invalid"

    with pytest.raises(GrafxConfigurationError) as raised:
        LocalStorageDevice(root, page_size=PAGE_SIZE, max_open_files=invalid)  # type: ignore[arg-type]

    assert raised.value.details["field"] == "max_open_files"
    assert not root.exists()


def test_a_small_override_preserves_lru_eviction_and_close(tmp_path: Path) -> None:
    device = LocalStorageDevice(
        tmp_path / "small", page_size=PAGE_SIZE, max_open_files=2
    )
    try:
        device.create("a.bin")
        device.create("b.bin")
        a_descriptor = device._handles["a.bin"]
        b_descriptor = device._handles["b.bin"]

        assert device.read_log("a.bin", 0, 0) == b""
        device.create("c.bin")
        c_descriptor = device._handles["c.bin"]

        assert tuple(device._handles) == ("a.bin", "c.bin")
        assert device.descriptor_cache_stats().evictions == 1
        with pytest.raises(OSError):
            os.fstat(b_descriptor)
        assert os.fstat(a_descriptor)
        assert os.fstat(c_descriptor)
    finally:
        device.close()

    with pytest.raises(OSError):
        os.fstat(a_descriptor)
    with pytest.raises(OSError):
        os.fstat(c_descriptor)


def test_descriptor_capacity_and_counters_are_isolated_per_device(tmp_path: Path) -> None:
    with (
        LocalStorageDevice(
            tmp_path / "roomy", page_size=PAGE_SIZE, max_open_files=4
        ) as roomy,
        LocalStorageDevice(
            tmp_path / "tight", page_size=PAGE_SIZE, max_open_files=1
        ) as tight,
    ):
        roomy.create("a.bin")
        roomy.create("b.bin")
        tight.create("x.bin")
        tight.create("y.bin")

        assert roomy._max_open_files == 4
        assert tuple(roomy._handles) == ("a.bin", "b.bin")
        assert roomy.descriptor_cache_stats() == DescriptorCacheStats(0, 0, 0)
        assert tight._max_open_files == 1
        assert tuple(tight._handles) == ("y.bin",)
        assert tight.descriptor_cache_stats() == DescriptorCacheStats(0, 0, 1)


def test_256_capacity_removes_64_table_cache_thrash_without_changing_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "working-set"
    names = tuple(
        f"index/table_{table:02d}_{artifact}.idx"
        for table in range(TABLES)
        for artifact in range(ARTIFACTS_PER_TABLE)
    )
    payloads = {
        name: f"payload:{name}".encode("ascii")
        for name in names
    }
    with LocalStorageDevice(
        root, page_size=PAGE_SIZE, max_open_files=MAX_OPEN_FILES
    ) as setup:
        for name in names:
            setup.create(name)
            setup.append_log(name, payloads[name])
        setup.durable_barrier()
    before = _disk_identity(root, names)

    observed: dict[int, tuple[bytes, ...]] = {}
    stats: dict[int, DescriptorCacheStats] = {}
    for capacity in (64, MAX_OPEN_FILES):
        with LocalStorageDevice(
            root, page_size=PAGE_SIZE, max_open_files=capacity
        ) as device:
            first = tuple(
                device.read_log(name, 0, len(payloads[name])) for name in names
            )
            second = tuple(
                device.read_log(name, 0, len(payloads[name])) for name in names
            )
            observed[capacity] = first + second
            stats[capacity] = device.descriptor_cache_stats()

    expected = tuple(payloads[name] for name in names) * 2
    assert observed[64] == expected
    assert observed[MAX_OPEN_FILES] == expected
    assert stats[64].misses == len(names) * 2
    assert stats[64].evictions == len(names) * 2 - 64
    assert stats[MAX_OPEN_FILES].misses == len(names)
    assert stats[MAX_OPEN_FILES].hits == len(names)
    assert stats[MAX_OPEN_FILES].evictions == 0
    assert _disk_identity(root, names) == before
