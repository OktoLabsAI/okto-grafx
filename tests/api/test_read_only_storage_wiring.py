"""The public read-only composition has no writable storage capability in its data plane."""

from __future__ import annotations

import hashlib
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

import pytest

from okto_grafx import connect
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_read_only import ReadOnlyStorageDevice
from okto_grafx.api import assembly
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
    GrafxUnsupportedOperation,
)
from okto_grafx.engine.database import META_FILE
from okto_grafx.runtime import bootstrap
from okto_grafx.runtime.bootstrap import build_default_registry, open_database
from okto_grafx.runtime.config import DatabaseConfig

PAGE_SIZE: int = 512
RAW_MUTATORS: tuple[str, ...] = (
    "create",
    "remove",
    "atomic_replace",
    "recycle",
    "allocate",
    "write_page",
    "append_log",
    "truncate_log",
    "durable_barrier",
    "retry_pending_deletes",
    "close",
)


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


def _namespace_snapshot(root: Path) -> dict[str, str]:
    """Return every directory name and every file digest, including an empty ``control/``."""
    return {
        (
            f"{path.relative_to(root).as_posix()}/"
            if path.is_dir()
            else path.relative_to(root).as_posix()
        ): (
            "<directory>"
            if path.is_dir()
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in sorted(root.rglob("*"))
    }


def _forbid_raw_mutations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Install bombs on every raw storage mutator and return their recording trail."""
    attempted: list[str] = []

    def forbidden(
        _device: LocalStorageDevice, *_args: object, **_kwargs: object
    ) -> None:
        attempted.append("attempted")
        raise AssertionError("observational preflight reached a raw storage mutator")

    for door in RAW_MUTATORS:
        monkeypatch.setattr(LocalStorageDevice, door, forbidden)
    return attempted


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


def test_read_only_existing_empty_root_is_refused_before_claiming_control(
    tmp_path: Path,
) -> None:
    """The expected empty-path refusal leaves even directory names byte-for-byte absent."""
    root = tmp_path / "empty-existing"
    root.mkdir()
    before = _namespace_snapshot(root)

    with pytest.raises(GrafxUnsupportedOperation) as raised:
        connect(root, page_size=PAGE_SIZE, read_only=True)

    assert raised.value.details["field"] == "read_only"
    assert _namespace_snapshot(root) == before == {}


@pytest.mark.parametrize("existing_first_open_lock", [False, True])
def test_read_only_foreign_tree_is_classified_without_changing_names_or_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_first_open_lock: bool,
) -> None:
    """A lock is only permission to wait; foreign evidence still gets the shared classifier."""
    root = tmp_path / ("foreign-with-lock" if existing_first_open_lock else "foreign")
    victim = root / "owner" / "payload.bin"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"not an Okto Grafx database")
    if existing_first_open_lock:
        lock = root / "control" / "first-open.lock"
        lock.parent.mkdir()
        lock.write_bytes(b"foreign lock payload that must survive")
    before = _namespace_snapshot(root)
    raw_mutations = _forbid_raw_mutations(monkeypatch)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(root, page_size=PAGE_SIZE, read_only=True)

    assert raised.value.details["field"] == "identity_missing"
    assert raw_mutations == []
    assert _namespace_snapshot(root) == before


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("foreign", GrafxCorruptionDetected),
        ("truncated", GrafxSchemaVersionMismatch),
        ("magic", GrafxCorruptionDetected),
        ("checksum", GrafxCorruptionDetected),
    ],
)
def test_read_only_meta_must_be_decodable_before_control_is_claimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
    expected: type[BaseException],
) -> None:
    """A reserved name is not authority until its page and identity validate observationally."""
    seed = tmp_path / f"seed-{damage}"
    with connect(seed, page_size=PAGE_SIZE):
        pass
    valid = bytearray((seed / META_FILE).read_bytes())
    if damage == "foreign":
        raw = (b"SQLite format 3\x00foreign" * PAGE_SIZE)[:PAGE_SIZE]
    elif damage == "truncated":
        raw = bytes(valid[:-1])
    elif damage == "magic":
        valid[0] ^= 0xFF
        raw = bytes(valid)
    else:
        valid[-1] ^= 0xFF
        raw = bytes(valid)

    root = tmp_path / f"invalid-meta-{damage}"
    root.mkdir()
    (root / META_FILE).write_bytes(raw)
    before = _namespace_snapshot(root)
    raw_mutations = _forbid_raw_mutations(monkeypatch)

    with pytest.raises(expected):
        connect(root, page_size=PAGE_SIZE, read_only=True)

    assert raw_mutations == []
    assert _namespace_snapshot(root) == before
    assert not (root / "control").exists()


def test_read_only_valid_database_without_control_materializes_only_liveness(
    tmp_path: Path,
) -> None:
    """Published Grafx authority is sufficient to recreate absent liveness, never data bytes."""
    root = tmp_path / "valid-without-control"
    with connect(root, page_size=PAGE_SIZE) as writer:
        identity = writer.identity
    shutil.rmtree(root / "control")
    shutil.rmtree(root / "bootstrap")
    immutable = _data_snapshot(root)
    assert not (root / "control").exists()
    assert not (root / "bootstrap").exists()

    with connect(root, page_size=PAGE_SIZE, read_only=True) as reader:
        assert reader.identity == identity
        assert reader.verify("all").findings == ()

    assert (root / "control").is_dir()
    assert _data_snapshot(root) == immutable


def test_default_read_only_waits_on_existing_first_open_lock_before_classifying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader arriving before intent publication waits for the creator's exact lock."""
    root = tmp_path / "concurrent-first-open"
    writer_holding = threading.Event()
    allow_writer = threading.Event()
    reader_attempting = threading.Event()
    reader_acquired = threading.Event()
    writer_ready = threading.Event()
    reader_done = threading.Event()
    failures: list[BaseException] = []
    results: dict[str, bytes] = {}
    original_exclusive = LocalProcessCoordinator.exclusive

    def observed_exclusive(
        coordinator: LocalProcessCoordinator,
        name: str,
        *,
        timeout: float,
    ) -> object:
        section = original_exclusive(coordinator, name, timeout=timeout)
        if name != assembly._FIRST_OPEN_SECTION:
            return section

        @contextmanager
        def observed_section() -> object:
            is_writer = threading.current_thread().name == "first-open-writer"
            is_reader = threading.current_thread().name == "first-open-reader"
            if is_reader:
                reader_attempting.set()
            with section:
                if is_writer:
                    writer_holding.set()
                    if not allow_writer.wait(30.0):
                        raise AssertionError("the first-open writer was not released")
                elif is_reader:
                    reader_acquired.set()
                    if not writer_ready.wait(30.0):
                        raise AssertionError(
                            "the writer did not finish database assembly"
                        )
                yield

        return observed_section()

    monkeypatch.setattr(LocalProcessCoordinator, "exclusive", observed_exclusive)

    def create_database() -> None:
        database = None
        try:
            database = connect(root, page_size=PAGE_SIZE)
            results["writer"] = database.identity.database_uuid
            writer_ready.set()
            if not reader_done.wait(30.0):
                raise AssertionError("the read-only participant did not finish")
        except BaseException as failure:  # noqa: BLE001 - carried to the test thread
            failures.append(failure)
            writer_ready.set()
        finally:
            if database is not None:
                database.close()

    def open_reader() -> None:
        try:
            with connect(root, page_size=PAGE_SIZE, read_only=True) as database:
                results["reader"] = database.identity.database_uuid
        except BaseException as failure:  # noqa: BLE001 - carried to the test thread
            failures.append(failure)
        finally:
            reader_done.set()

    writer = threading.Thread(
        target=create_database, name="first-open-writer", daemon=True
    )
    writer.start()
    assert writer_holding.wait(30.0)
    assert (root / "control" / "first-open.lock").is_file()
    assert not (root / META_FILE).exists(), (
        "the writer was paused before publishing identity"
    )

    reader = threading.Thread(target=open_reader, name="first-open-reader", daemon=True)
    reader.start()
    assert reader_attempting.wait(30.0), (
        "preflight refused instead of reaching the existing lock"
    )
    assert not reader_acquired.wait(0.1), (
        "the reader crossed a lock still held by the writer"
    )
    assert not reader_done.is_set()

    allow_writer.set()
    writer.join(30.0)
    reader.join(30.0)
    assert not writer.is_alive()
    assert not reader.is_alive()
    assert not failures
    assert results["reader"] == results["writer"]


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
