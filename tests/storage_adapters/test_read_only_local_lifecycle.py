"""Observational local-storage lifecycle and namespace-scoped reclamation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.errors import GrafxUnsupportedOperation

PAGE_SIZE: int = 512
CONTROL_TARGET: str = "control/readers/touch.reader"
PENDING_PAYLOADS: dict[str, bytes] = {
    "control/stale.reader.pending-delete-9101": b"control pending",
    "wal/stale.wal.pending-delete-9102": b"wal pending",
    "index/stale.idx.pending-delete-9103": b"index pending",
    "heap.dat.pending-delete-9104": b"root data pending",
}


def _plant(root: Path, *, target: bool = False) -> None:
    root.mkdir(parents=True)
    for name, payload in PENDING_PAYLOADS.items():
        path = root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if target:
        target_path = root.joinpath(*CONTROL_TARGET.split("/"))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"live control entry")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path(root: Path, name: str) -> Path:
    return root.joinpath(*name.split("/"))


@pytest.mark.parametrize("door", ["create", "remove", "recycle"])
def test_control_automatic_doors_retry_only_the_control_namespace(
    tmp_path: Path,
    door: str,
) -> None:
    """Coordinator traffic cannot drain retained WAL, index or root-data evidence."""
    root = tmp_path / door
    _plant(root, target=door != "create")
    immutable = {
        name: _digest(_path(root, name))
        for name in PENDING_PAYLOADS
        if not name.startswith("control/")
    }
    device = LocalStorageDevice(root, page_size=PAGE_SIZE, create_root=False)
    try:
        assert set(device.pending_deletes()) == set(PENDING_PAYLOADS)
        if door == "create":
            device.create(CONTROL_TARGET)
        elif door == "remove":
            device.remove(CONTROL_TARGET)
        else:
            assert device.recycle(CONTROL_TARGET) is True

        assert not _path(root, "control/stale.reader.pending-delete-9101").exists()
        assert {name: _digest(_path(root, name)) for name in immutable} == immutable
        assert set(device.pending_deletes()) == set(immutable)
    finally:
        device.close_read_only()


def test_observational_close_preserves_pending_but_explicit_retry_is_global(
    tmp_path: Path,
) -> None:
    """RO close is observational; the explicit maintenance door still drains every namespace."""
    root = tmp_path / "manual"
    _plant(root)
    observer = LocalStorageDevice(root, page_size=PAGE_SIZE, create_root=False)
    queued = observer.pending_deletes()
    observer.close_read_only()
    assert observer.pending_deletes() == queued
    assert all(_path(root, name).exists() for name in PENDING_PAYLOADS)

    maintainer = LocalStorageDevice(root, page_size=PAGE_SIZE, create_root=False)
    try:
        assert maintainer.retry_pending_deletes() == len(PENDING_PAYLOADS)
        assert maintainer.pending_deletes() == ()
        assert not any(_path(root, name).exists() for name in PENDING_PAYLOADS)
    finally:
        maintainer.close_read_only()


def test_writable_close_still_retries_every_pending_namespace(tmp_path: Path) -> None:
    """The normal owned writable lifecycle keeps its historical global reclamation behavior."""
    root = tmp_path / "writable-close"
    _plant(root)
    device = LocalStorageDevice(root, page_size=PAGE_SIZE, create_root=False)
    device.close()
    assert device.pending_deletes() == ()
    assert not any(_path(root, name).exists() for name in PENDING_PAYLOADS)


def test_create_root_false_refuses_without_creating_any_parent(tmp_path: Path) -> None:
    """An observational adapter cannot materialize an absent database path."""
    root = tmp_path / "absent" / "database"
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        LocalStorageDevice(root, page_size=PAGE_SIZE, create_root=False)
    assert raised.value.details["field"] == "read_only"
    assert raised.value.details["create_root"] is False
    assert not root.exists()
    assert not root.parent.exists()
