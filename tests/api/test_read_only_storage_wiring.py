"""The public read-only composition has no writable storage capability in its data plane."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import MappingProxyType

import pytest

from okto_grafx import connect
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_read_only import ReadOnlyStorageDevice
from okto_grafx.api import assembly
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxSchemaVersionMismatch,
    GrafxUnsupportedOperation,
)
from okto_grafx.engine.database import META_FILE
from okto_grafx.runtime import bootstrap
from okto_grafx.runtime.bootstrap import build_default_registry, open_database
from okto_grafx.runtime.config import DatabaseConfig

PAGE_SIZE: int = 512


def _snapshot(root: Path) -> dict[str, str]:
    """Return every physical file name and SHA-256, including reserved pending-delete names."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _data_snapshot(root: Path) -> dict[str, str]:
    """Return the immutable plane while deliberately allowing the coordinator's control plane."""
    return {
        name: digest
        for name, digest in _snapshot(root).items()
        if not name.startswith("control/")
    }


def _seed(root: Path) -> object:
    """Publish a database with authoritative rows, an index, WAL, COMPLETE and matching PENDING."""
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
            transaction.execute("CREATE (:Person {id: 1, name: 'Ada'})")
        database.checkpoint()
        identity = database.identity

    writable = LocalStorageDevice(root, page_size=PAGE_SIZE, create_root=False)
    try:
        assembly._write_first_open_intent(writable, identity)
    finally:
        writable.close()
    return identity


def _plant_pending_evidence(root: Path) -> tuple[str, ...]:
    """Plant queue entries directly; logical storage correctly reserves these physical names."""
    evidence = {
        "heap.dat.pending-delete-8101": b"root data pending",
        "wal/retained.wal.pending-delete-8102": b"wal pending",
        "index/retained.idx.pending-delete-8103": b"index pending",
    }
    for name, payload in evidence.items():
        path = root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return tuple(evidence)


def _assert_only_control_changed(
    before: dict[str, str],
    after: dict[str, str],
) -> None:
    """Name every difference and prove each belongs to the mutable reader control plane."""
    changed = {
        name
        for name in before.keys() | after.keys()
        if before.get(name) != after.get(name)
    }
    assert all(name.startswith("control/") for name in changed), sorted(changed)


def test_read_only_open_query_verify_and_close_preserve_complete_pending_and_wal(
    tmp_path: Path,
) -> None:
    """Every public observational phase preserves physical names and bytes outside control/."""
    root = tmp_path / "database"
    _seed(root)
    pending = _plant_pending_evidence(root)
    before = _snapshot(root)
    immutable = _data_snapshot(root)
    assert assembly._FIRST_OPEN_COMPLETE in immutable
    assert assembly._FIRST_OPEN_INTENT in immutable
    assert set(pending) <= immutable.keys()

    reader = connect(root, page_size=PAGE_SIZE, read_only=True)
    after_open = _snapshot(root)
    assert _data_snapshot(root) == immutable
    _assert_only_control_changed(before, after_open)

    rows = tuple(reader.execute("MATCH (p:Person) RETURN p.id, p.name"))
    assert rows == ((1, "Ada"),)
    after_query = _snapshot(root)
    assert _data_snapshot(root) == immutable
    _assert_only_control_changed(before, after_query)

    assert reader.verify("all").findings == ()
    after_verify = _snapshot(root)
    assert _data_snapshot(root) == immutable
    _assert_only_control_changed(before, after_verify)

    reader.close()
    after_close = _snapshot(root)
    assert _data_snapshot(root) == immutable
    _assert_only_control_changed(before, after_close)
    assert {name: after_close[name] for name in pending} == {
        name: before[name] for name in pending
    }


def test_custom_registry_keeps_raw_storage_caller_owned_and_wraps_every_data_component(
    tmp_path: Path,
) -> None:
    """Raw storage stays in registry/coordinator; every engine data path shares one RO view."""
    root = tmp_path / "database"
    _seed(root)
    pending = _plant_pending_evidence(root)
    before = _data_snapshot(root)
    config = DatabaseConfig(path=str(root), page_size=PAGE_SIZE, read_only=True)
    registry = build_default_registry(config)
    raw = registry.get("storage")
    coordinator = registry.get("coordinator")
    assert isinstance(raw, LocalStorageDevice)
    queued = raw.pending_deletes()
    assert set(pending) <= set(queued)

    database = open_database(config, registry=registry)
    view = database._storage
    try:
        assert isinstance(view, ReadOnlyStorageDevice)
        assert registry.get("storage") is raw
        assert database._coordinator is coordinator
        assert coordinator._storage is raw
        assert database.storage is not raw

        assert database._pool.storage is view
        assert database._wal._storage is view
        assert database._catalog._pool.storage is view
        assert database._heap._pool.storage is view
        assert database._indexes._pool.storage is view
        assert database._vectors._pool.storage is view
        assert database._quarantine._storage is view
        assert database._ledger._storage is view
        assert database._recovery._storage is view
        assert database._transactions._pool.storage is view
        assert database._transactions._writable is False

        assert tuple(database.execute("MATCH (p:Person) RETURN p.name")) == (("Ada",),)
        assert database.verify("all").findings == ()
    finally:
        database.close()

    # A custom registry remains caller-owned. Database.close neither closes the raw port nor
    # drives the pending queue that the caller may be preserving for inspection.
    assert raw.exists(META_FILE)
    assert raw.pending_deletes() == queued
    assert _data_snapshot(root) == before
    raw.close_read_only()
    assert raw.pending_deletes() == queued
    assert _data_snapshot(root) == before


def test_real_coordinator_control_traffic_never_drains_other_namespaces(
    tmp_path: Path,
) -> None:
    """Reader registration uses raw control storage without reclaiming data-plane evidence."""
    root = tmp_path / "database"
    _seed(root)
    pending = _plant_pending_evidence(root)
    before = _data_snapshot(root)
    config = DatabaseConfig(path=str(root), page_size=PAGE_SIZE, read_only=True)
    registry = build_default_registry(config)
    raw = registry.get("storage")
    coordinator = registry.get("coordinator")
    assert isinstance(raw, LocalStorageDevice)
    queued = raw.pending_deletes()
    try:
        handle = coordinator.register_reader(0)
        coordinator.refresh_reader(handle)
        coordinator.unregister_reader(handle)
        assert _data_snapshot(root) == before
        assert raw.pending_deletes() == queued
        assert set(pending) <= set(queued)
    finally:
        raw.close_read_only()
    assert _data_snapshot(root) == before


def test_default_read_only_close_surfaces_observational_closer_failure_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed RO descriptor close is terminal and never escalates to writable housekeeping."""
    root = tmp_path / "database"
    _seed(root)
    _plant_pending_evidence(root)
    before = _data_snapshot(root)
    observed: list[LocalStorageDevice] = []
    original_observational = LocalStorageDevice.close_read_only
    bomb = RuntimeError("observational close bomb")

    def failing_observational_close(device: LocalStorageDevice) -> None:
        observed.append(device)
        raise bomb

    def forbidden_writable_close(device: LocalStorageDevice) -> None:
        raise AssertionError(
            f"read-only close reached writable close for {device.root}"
        )

    monkeypatch.setattr(
        LocalStorageDevice, "close_read_only", failing_observational_close
    )
    monkeypatch.setattr(LocalStorageDevice, "close", forbidden_writable_close)
    database = connect(root, page_size=PAGE_SIZE, read_only=True)

    with pytest.raises(RuntimeError) as raised:
        database.close()
    assert raised.value is bomb
    assert database.close_complete is True
    assert database._close_failure is bomb
    assert len(observed) == 1
    assert observed[0].pending_deletes()
    assert _data_snapshot(root) == before

    # The injected failure deliberately happened before descriptor release. Finish the test's
    # own cleanup through the captured raw capability, still without driving pending deletion.
    original_observational(observed[0])
    assert _data_snapshot(root) == before


def test_default_read_only_assembly_failure_uses_observational_unwind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page-size refusal closes descriptors without draining any physical pending evidence."""
    root = tmp_path / "database"
    _seed(root)
    pending = _plant_pending_evidence(root)
    before = _snapshot(root)
    observed: list[LocalStorageDevice] = []
    original_observational = LocalStorageDevice.close_read_only

    def observational_close(device: LocalStorageDevice) -> None:
        observed.append(device)
        original_observational(device)

    def forbidden_writable_close(device: LocalStorageDevice) -> None:
        raise AssertionError(
            f"read-only unwind reached writable close for {device.root}"
        )

    monkeypatch.setattr(LocalStorageDevice, "close_read_only", observational_close)
    monkeypatch.setattr(LocalStorageDevice, "close", forbidden_writable_close)

    with pytest.raises(GrafxSchemaVersionMismatch):
        connect(root, page_size=1024, read_only=True)

    assert len(observed) == 1
    assert observed[0].pending_deletes()
    assert _snapshot(root) == before
    assert set(pending) <= _snapshot(root).keys()


@pytest.mark.parametrize(
    "closer_failure", [RuntimeError, KeyboardInterrupt, SystemExit]
)
def test_default_read_only_port_build_failure_preserves_the_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closer_failure: type[BaseException],
) -> None:
    """Even a process-control close failure cannot replace the late factory failure."""
    root = tmp_path / "database"
    _seed(root)
    pending = _plant_pending_evidence(root)
    before = _snapshot(root)
    observed: list[LocalStorageDevice] = []
    original_observational = LocalStorageDevice.close_read_only

    def observational_close(device: LocalStorageDevice) -> None:
        observed.append(device)
        original_observational(device)
        raise closer_failure("observational build-unwind bomb")

    def forbidden_writable_close(device: LocalStorageDevice) -> None:
        raise AssertionError(
            f"read-only build unwind reached writable close for {device.root}"
        )

    def fail_after_storage(_context: object) -> object:
        raise RuntimeError("clock factory bomb")

    factories = dict(bootstrap._DEFAULT_PORT_FACTORIES)
    factories["clock"] = fail_after_storage
    monkeypatch.setattr(
        bootstrap,
        "_DEFAULT_PORT_FACTORIES",
        MappingProxyType(factories),
    )
    monkeypatch.setattr(LocalStorageDevice, "close_read_only", observational_close)
    monkeypatch.setattr(LocalStorageDevice, "close", forbidden_writable_close)

    config = DatabaseConfig(path=str(root), page_size=PAGE_SIZE, read_only=True)
    with pytest.raises(GrafxConfigurationError) as raised:
        bootstrap.build_default_registry(config)
    assert raised.value.details["cause"] == "RuntimeError"
    assert len(observed) == 1
    assert observed[0].pending_deletes()
    assert _snapshot(root) == before
    assert set(pending) <= _snapshot(root).keys()


def test_read_only_missing_root_is_refused_before_the_directory_exists(
    tmp_path: Path,
) -> None:
    """The default storage factory does not create even the root of an absent RO database."""
    root = tmp_path / "absent" / "nested"
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        connect(root, page_size=PAGE_SIZE, read_only=True)
    assert getattr(raised.value, "details", {}).get("field") == "read_only"
    assert not root.exists()
    assert not root.parent.exists()
