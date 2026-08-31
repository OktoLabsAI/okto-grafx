"""CE-1 descriptor-cache reservations for the two hot in-place control files."""

from __future__ import annotations

from pathlib import Path

from okto_grafx.adapters.storage_local import LocalStorageDevice

PAGE_SIZE = 512
LEASE = "control/writer.lease"
COMMIT_STATE = "control/commit.state"


def _create_log(device: LocalStorageDevice, name: str, payload: bytes) -> None:
    device.create(name)
    device.append_log(name, payload)


def test_pinned_control_descriptors_survive_eviction_pressure_inside_the_budget(
    tmp_path: Path,
) -> None:
    with LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE, max_open_files=4) as device:
        assert device.pin_descriptor(LEASE)
        assert device.pin_descriptor(COMMIT_STATE)
        _create_log(device, LEASE, b"lease")
        _create_log(device, COMMIT_STATE, b"state")

        for index in range(400):
            name = f"wal/{index:012d}.wal"
            _create_log(device, name, index.to_bytes(4, "little"))
            assert len(device._handles) <= 4

        assert LEASE in device._handles
        assert COMMIT_STATE in device._handles
        assert device.read_log(LEASE, 0, 5) == b"lease"
        assert device.read_log(COMMIT_STATE, 0, 5) == b"state"


def test_a_tiny_cache_declines_pins_instead_of_exceeding_its_bound(
    tmp_path: Path,
) -> None:
    with LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE, max_open_files=1) as device:
        assert not device.pin_descriptor(LEASE)
        _create_log(device, LEASE, b"lease")
        _create_log(device, "wal/000000000001.wal", b"wal")
        assert len(device._handles) == 1


def test_a_pinned_descriptor_is_still_reproved_after_foreign_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with LocalStorageDevice(root, page_size=PAGE_SIZE, max_open_files=4) as reader:
        assert reader.pin_descriptor(LEASE)
        _create_log(reader, LEASE, b"old")
        assert reader.read_log(LEASE, 0, 3) == b"old"

        with LocalStorageDevice(root, page_size=PAGE_SIZE) as publisher:
            _create_log(publisher, "control/new-lease.tmp", b"new")
            publisher.atomic_replace("control/new-lease.tmp", LEASE)
            publisher.durable_barrier(LEASE)

        assert reader.read_log(LEASE, 0, 3) == b"new"
        assert LEASE in reader._handles
        assert LEASE in reader._pinned_handles
