"""connect / close: what an open database owns, and what it must give back (C11, FR-1).

The sharpest risk in a composition root is a lifecycle that half-succeeds: a connect that leaves a
device, a socket or a descriptor behind, or a close that does not release what it opened. Every
test here is about that, and each one observes the resource by IDENTITY rather than by name (A44).
"""

from __future__ import annotations

import gc
from pathlib import Path

import pytest

from okto_grafx import Database, Transaction, connect
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.engine.public_views import StorageView
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxError,
    GrafxSchemaVersionMismatch,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports
from okto_grafx.runtime.config import DatabaseConfig


def _closed(device: object) -> bool:
    """Return True when a storage device refuses work because it has been closed."""
    try:
        device.exists("probe")  # type: ignore[attr-defined]
    except GrafxUnsupportedOperation as refusal:
        return refusal.details.get("reason") == "device_closed"
    return False


def test_connect_opens_an_in_memory_database_and_closes_it() -> None:
    db = connect(":memory:")
    assert isinstance(db, Database)
    assert db.path == ":memory:"
    assert db.closed is False
    assert isinstance(db.storage, StorageView)
    assert db.storage.name == "memory"
    db.close()
    assert db.closed is True


class _FailingPath:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    def __fspath__(self) -> str:
        raise self.failure


def test_a_pathlike_ordinary_failure_is_a_typed_refusal_before_open() -> None:
    marker = RuntimeError("caller path callback")
    with pytest.raises(GrafxConfigurationError) as raised:
        connect(_FailingPath(marker))  # type: ignore[arg-type]
    assert raised.value.details["field"] == "path"
    assert raised.value.details["cause"] == "RuntimeError"
    assert raised.value.__cause__ is marker


def test_a_pathlike_grafx_failure_keeps_its_identity() -> None:
    marker = GrafxConfigurationError("path owner refusal", field="external_path")
    with pytest.raises(GrafxConfigurationError) as raised:
        connect(_FailingPath(marker))  # type: ignore[arg-type]
    assert raised.value is marker


@pytest.mark.parametrize("signal", [KeyboardInterrupt(), SystemExit(91)])
def test_a_pathlike_process_control_signal_keeps_its_identity(
    signal: BaseException,
) -> None:
    with pytest.raises(type(signal)) as raised:
        connect(_FailingPath(signal))  # type: ignore[arg-type]
    assert raised.value is signal


def test_an_invalid_registry_is_refused_before_a_database_root_is_created(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-not-exist"
    with pytest.raises(GrafxConfigurationError) as raised:
        connect(root, registry=object())  # type: ignore[arg-type]
    assert raised.value.details["field"] == "registry"
    assert not root.exists()


def test_connect_creates_the_directory_and_the_files_a_database_needs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "created"
    with connect(root) as db:
        names = set(db.storage.list_files())
    assert root.is_dir()
    # FR-1: an open database carries its identity page, and the two stores exist.
    assert {"grafx.meta", "catalog.dat", "heap.dat"} <= names


def test_the_database_is_a_context_manager_that_closes_on_the_way_out(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db") as db:
        assert db.closed is False
    assert db.closed is True


def test_leaving_the_block_through_an_exception_still_closes_and_keeps_the_failure(
    tmp_path: Path,
) -> None:
    db = connect(tmp_path / "db")
    marker = RuntimeError("the caller's own failure")
    with pytest.raises(RuntimeError) as raised:
        with db:
            raise marker
    assert raised.value is marker
    assert db.closed is True


def test_closing_twice_is_a_no_op(tmp_path: Path) -> None:
    db = connect(tmp_path / "db")
    db.close()
    db.close()
    assert db.closed is True


def test_close_releases_the_device_the_composition_opened(tmp_path: Path) -> None:
    db = connect(tmp_path / "db")
    # Lifecycle wiring is an internal composition assertion; the public property is now a
    # detached immutable snapshot and intentionally cannot expose a live close state.
    device = db._storage
    assert _closed(device) is False
    db.close()
    assert _closed(device) is True


def test_reopen_adopts_listed_indexes_without_rechecking_each_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "db"
    with connect(root) as database:
        with database.begin("write") as transaction:
            for number in range(8):
                transaction.execute(
                    f"CREATE NODE TABLE T{number}(id INT64, PRIMARY KEY(id))"
                )

    original_exists = LocalStorageDevice.exists
    index_exists_calls: list[str] = []

    def counted_exists(device: LocalStorageDevice, file: str) -> bool:
        if file.startswith("index/"):
            index_exists_calls.append(file)
        return original_exists(device, file)

    monkeypatch.setattr(LocalStorageDevice, "exists", counted_exists)

    with connect(root) as reopened:
        assert len(reopened.indexes.indexes()) >= 8

    # The directory inventory proves names; the final fenced admission still
    # opens/validates their bytes without per-name existence probes.
    assert index_exists_calls == []


def test_close_leaves_a_caller_supplied_registry_alone(tmp_path: Path) -> None:
    # A caller that composed its own adapters may be using them for something else, so the
    # database closes only what the composition root opened.
    config = DatabaseConfig(path=str(tmp_path / "db"))
    registry = build_default_registry(config)
    device = registry.get("storage")
    try:
        db = connect(tmp_path / "db", registry=registry)
        db.close()
        assert db.closed is True
        assert _closed(device) is False
    finally:
        release_ports(registry)
    assert _closed(device) is True


def test_a_closed_database_refuses_every_door(tmp_path: Path) -> None:
    db = connect(tmp_path / "db")
    db.close()
    for call in (
        lambda: db.begin("read"),
        lambda: db.execute("RETURN 1"),
        lambda: db.verify("all"),
        lambda: db.recover(),
        lambda: db.flush(),
        lambda: db.snapshot_metrics(),
    ):
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            call()
        assert "closed" in str(raised.value)


def test_closing_with_an_open_transaction_aborts_it(tmp_path: Path) -> None:
    db = connect(tmp_path / "db")
    txn = db.begin("write")
    assert txn.active is True
    db.close()
    assert txn.active is False
    with pytest.raises(GrafxTransactionStateError):
        txn.commit()


def test_closing_with_an_open_transaction_withdraws_its_reader_registration(
    tmp_path: Path,
) -> None:
    # A pin that outlived its database would hold the recycling horizon down forever, and no
    # caller would be holding anything it could withdraw it with. The registry is supplied by
    # this test so the device survives the close and the horizon can still be READ afterwards --
    # the property is what the coordinator says once the database is gone.
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    try:
        db = connect(tmp_path / "db", registry=registry)
        coordinator = registry.get("coordinator")
        db.begin("read")
        assert coordinator.reader_horizon() is not None
        db.close()
        assert coordinator.reader_horizon() is None
    finally:
        release_ports(registry)


def test_a_transaction_block_commits_on_a_clean_exit(tmp_path: Path) -> None:
    with connect(tmp_path / "db") as db:
        txn = db.begin("write")
        with txn:
            pass
        assert txn.active is False
        assert txn.report is not None
        assert txn.report.durable is True


def test_a_transaction_block_rolls_back_when_the_body_raises(tmp_path: Path) -> None:
    with connect(tmp_path / "db") as db:
        txn = db.begin("write")
        with pytest.raises(ValueError):
            with txn:
                raise ValueError("the caller's own failure")
        assert txn.active is False
        assert txn.report is None


def test_a_finished_transaction_refuses_to_be_used_again(tmp_path: Path) -> None:
    with connect(tmp_path / "db") as db:
        txn = db.begin("read")
        txn.commit()
        with pytest.raises(GrafxTransactionStateError):
            txn.commit()
        with pytest.raises(GrafxTransactionStateError):
            txn.execute("RETURN 1")
        # Rolling back a finished transaction is deliberately harmless: closing a database must
        # be able to abandon whatever is open without first asking what state it is in.
        txn.rollback()


def test_two_databases_in_one_process_do_not_share_anything(tmp_path: Path) -> None:
    # FR-13 and BR-8: a budget, a page cache and a metric label belong to ONE database.
    with connect(tmp_path / "a") as first, connect(tmp_path / "b") as second:
        assert first._pool is not second._pool
        assert first._storage is not second._storage
        assert first.label != second.label
        assert first.identity.database_uuid != second.identity.database_uuid


def test_the_metric_label_is_a_short_hash_and_never_the_path(tmp_path: Path) -> None:
    # TR-7 and A79: a scraped label may not disclose a deployment layout.
    with connect(tmp_path / "some" / "revealing" / "place") as db:
        label = db.label
    assert "some" not in label and "revealing" not in label
    assert len(label) <= 64
    assert all(character.isalnum() or character in "-_." for character in label)


def test_an_unknown_connection_option_is_refused_by_name() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        connect(":memory:", page_sizes=4096)  # type: ignore[call-arg]
    assert "page_sizes" in str(raised.value)
    assert raised.value.details["field"] == "options"


def test_the_removed_recall_knob_names_the_offline_migration() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        connect(":memory:", vector_recall_target=0.95)  # type: ignore[call-arg]

    assert raised.value.details == {
        "field": "vector_recall_target",
        "replacement": "bench.harness.gate --recall-target",
    }
    assert "bench.harness.gate --recall-target" in raised.value.message
    assert "vector_ef_search" in raised.value.message


def test_a_read_only_database_refuses_a_write_transaction(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(root) as db:
        pass
    with connect(root, read_only=True) as db:
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            db.begin("write")
        assert raised.value.details["field"] == "read_only"
        assert raised.value.details["operation"] == "begin a write transaction"
        assert db.begin("read").active is True


def test_read_only_never_creates_a_database(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        connect(root, read_only=True)
    assert raised.value.details["field"] == "read_only"
    assert not (root / "grafx.meta").exists()


def test_a_component_that_is_not_composed_refuses_by_name(tmp_path: Path) -> None:
    # A seam is a typed refusal that NAMES the missing component, never a stub and never a wrong
    # answer. Every engine of the default composition is present, so the seam is exercised by
    # dropping one from a real database rather than by waiting for a component not to exist.
    with connect(tmp_path / "db") as db:
        object.__setattr__(db, "_queries", None)
        object.__setattr__(db, "_verifier_factory", None)
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            db.execute("MATCH (n) RETURN n")
        assert raised.value.details["component"] == "queries"
        with pytest.raises(GrafxUnsupportedOperation) as verifying:
            db.verify("all")
        assert verifying.value.details["component"] == "verifier"


def test_the_contract_example_runs_through_the_public_surface(tmp_path: Path) -> None:
    """CONTRACT.md section 10, as written, against the real composition.

    Row writing is refused by the query engine for a reason it states in full (no component
    allocates a RecordId, and no component applies staged index work at the commit number), so
    the example stops where that build stops -- but the schema statement, the autocommit read and
    the plan all run, and the schema survives a close and a reopen.
    """
    root = tmp_path / "mydb"
    db = connect(root, partitions_per_table=64)
    with db.begin("write") as txn:
        created = txn.execute(
            "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
        )
    assert created.statistics["tables_created"] == 1
    result = db.execute("MATCH (p:Person) RETURN p.name")
    assert result.columns == ("p.name",)
    assert result.rows == ()
    report = db.verify(scope="all")
    assert report.findings == ()
    entries = db.ledger.list(origin_class="forensic")
    assert entries == ()
    db.close()

    with connect(root, partitions_per_table=64) as reopened:
        assert [table.name for table in reopened.catalog.catalog.tables()] == ["Person"]


def test_a_clean_database_verifies_empty_on_every_scope(tmp_path: Path) -> None:
    # FR-11: a clean database produces an empty report. The header pages of a freshly created
    # database live in the cache until something writes them, and a verifier reads the DEVICE --
    # so an open sequence that handed back a database before flushing them reported three
    # checksum failures on a database that had just been created cleanly.
    for path in (":memory:", str(tmp_path / "db")):
        with connect(path) as db:
            for scope in ("pages", "records", "indexes", "all"):
                report = db.verify(scope)
                assert report.findings == (), f"{path} {scope}: {report.findings}"
            assert db.verify("all").pages_checked > 0


def test_verify_refuses_a_scope_that_is_not_one_of_the_four(tmp_path: Path) -> None:
    with connect(tmp_path / "db") as db:
        with pytest.raises(GrafxConfigurationError) as raised:
            db.verify("everything")
        assert raised.value.details["field"] == "scope"


def test_the_composed_components_that_exist_are_reachable(tmp_path: Path) -> None:
    with connect(tmp_path / "db") as db:
        assert db.ledger is not None
        assert db.quarantine is not None
        assert db.indexes is not None
        assert db.vectors is not None
        assert db.queries is not None
        assert db.recovery_report is not None


def test_an_event_sink_that_re_enters_close_does_not_deadlock(tmp_path: Path) -> None:
    # A91 and LESSONS L2: every port is host-supplied, so any of them may re-enter the public
    # API. The database holds no lock at all, which is why a re-entrant close terminates instead
    # of waiting for something it already holds. A hang here is the loudest possible failure and
    # the suite-wide timeout converts it into a red run (A42, A88).
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    reentries: list[int] = []

    class ReentrantEventSink:
        """An event sink that closes the database while the database is closing it."""

        def __init__(self) -> None:
            self.database: Database | None = None

        def emit(self, event: str, payload: object) -> None:
            reentries.append(1)
            if self.database is not None:
                self.database.close()

    sink = ReentrantEventSink()
    registry.bind("events", sink)
    db = connect(tmp_path / "db", registry=registry)
    sink.database = db
    try:
        sink.emit("closing", {})
        assert db.closed is True
        assert reentries == [1]
    finally:
        release_ports(registry)


def test_a_close_that_cannot_release_still_records_the_database_as_closed(
    tmp_path: Path,
) -> None:
    # A database that failed to release something must not be left in a state where a caller can
    # neither use it nor retire it. The failure is reported; the object is closed either way.
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    db = connect(tmp_path / "db", registry=registry)
    refusal = GrafxError("The release could not finish.")

    def refuse() -> None:
        raise refusal

    object.__setattr__(db, "_closers", (refuse,))
    with pytest.raises(GrafxError) as raised:
        db.close()
    assert raised.value is refusal
    assert db.closed is True
    db.close()
    release_ports(registry)


@pytest.mark.parametrize(
    "failure_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_close_finishes_every_step_and_reraises_the_first_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    """A foreign or process-control failure cannot strand later close work."""
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    db = connect(tmp_path / "db", registry=registry)
    first = failure_type("the transaction close failed")
    later = RuntimeError("metrics publication also failed")
    ran: list[str] = []

    def close_transactions(_database: Database) -> None:
        # Failure is AFTER transaction quiescence. A pre-enter failure has a different safety
        # contract: lower dependencies must remain open for retry (terminal-close regression).
        _database._transactions.close()
        ran.append("transactions")
        raise first

    def flush_pages(_database: Database) -> None:
        ran.append("pages")

    def publish_metrics(_database: Database) -> None:
        ran.append("metrics")
        raise later

    def release_closers(_database: Database) -> None:
        ran.append("closers")

    monkeypatch.setattr(Database, "_close_transactions", close_transactions)
    monkeypatch.setattr(Database, "_flush_pages", flush_pages)
    monkeypatch.setattr(Database, "_publish_metrics", publish_metrics)
    monkeypatch.setattr(Database, "_release_closers", release_closers)
    try:
        with pytest.raises(failure_type) as raised:
            db.close()
        assert raised.value is first
        assert ran == ["transactions", "pages", "metrics", "closers"]
        assert db.closed is True
    finally:
        release_ports(registry)


def test_every_closer_runs_after_foreign_and_process_control_failures(
    tmp_path: Path,
) -> None:
    """Closer order is exhausted and the first BaseException keeps its identity."""
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    db = connect(tmp_path / "db", registry=registry)
    ran: list[str] = []
    first = RuntimeError("the inner closer failed")
    later = KeyboardInterrupt("the middle closer was interrupted")

    def outer() -> None:
        ran.append("outer")

    def middle() -> None:
        ran.append("middle")
        raise later

    def inner() -> None:
        ran.append("inner")
        raise first

    object.__setattr__(db, "_closers", (outer, middle, inner))
    try:
        with pytest.raises(RuntimeError) as raised:
            db.close()
        assert raised.value is first
        assert ran == ["inner", "middle", "outer"]
        assert db.closed is True
    finally:
        release_ports(registry)


def test_context_exit_never_masks_an_active_failure_with_a_foreign_close_failure(
    tmp_path: Path,
) -> None:
    """The block's exception remains first while close still exhausts its resources."""
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    db = connect(tmp_path / "db", registry=registry)
    marker = RuntimeError("the caller's own failure")
    close_failure = ValueError("a foreign closer failed")
    ran: list[str] = []

    def outer() -> None:
        ran.append("outer")

    def inner() -> None:
        ran.append("inner")
        raise close_failure

    object.__setattr__(db, "_closers", (outer, inner))
    try:
        with pytest.raises(RuntimeError) as raised:
            with db:
                raise marker
        assert raised.value is marker
        assert ran == ["inner", "outer"]
        assert db.closed is True
    finally:
        release_ports(registry)


def test_nothing_of_a_closed_database_survives_collection(tmp_path: Path) -> None:
    # A live database keeps its own objects reachable; once it is closed and dropped, nothing in
    # this package holds it. A module-level cache would show up here as a survivor (BR-8).
    import weakref

    db = connect(tmp_path / "db")
    reference = weakref.ref(db)
    db.close()
    del db
    gc.collect()
    assert reference() is None


def test_the_public_names_of_the_package_resolve() -> None:
    import okto_grafx

    for name in okto_grafx.__all__:
        assert getattr(okto_grafx, name) is not None
    assert okto_grafx.Database is Database
    assert okto_grafx.Transaction is Transaction
    assert okto_grafx.connect is connect


def test_a_connect_that_fails_mid_assembly_closes_the_device_it_opened(
    tmp_path: Path,
) -> None:
    # The window between "the device is open" and "the database exists". Nothing but the
    # assembly's own guard covers it, so a mutation that removes that guard leaves a device
    # holding descriptors on every failed open -- and on Windows a held descriptor is what stops
    # the next attempt from publishing over the same names.
    root = tmp_path / "db"
    root.mkdir()
    (root / "grafx.meta").write_bytes(b"x" * 100)  # not a whole number of pages
    opened: list[object] = []
    original = LocalStorageDevice.__init__

    def recording(self: object, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(self)

    LocalStorageDevice.__init__ = recording  # type: ignore[method-assign]
    try:
        with pytest.raises(GrafxSchemaVersionMismatch):
            connect(root)
    finally:
        LocalStorageDevice.__init__ = original  # type: ignore[method-assign]
    assert len(opened) == 1
    assert _closed(opened[0]) is True


def test_a_release_that_fails_does_not_stop_the_next_one(tmp_path: Path) -> None:
    # A socket left bound because a device failed to close is the leak close() exists to prevent,
    # so every registered release runs even when an earlier one raised.
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    db = connect(tmp_path / "db", registry=registry)
    ran: list[str] = []
    first = GrafxError("The inner release could not finish.")

    def inner() -> None:
        ran.append("inner")
        raise first

    def outer() -> None:
        ran.append("outer")

    # Acquisition order: outer first, inner second, so the release runs inner then outer.
    object.__setattr__(db, "_closers", (outer, inner))
    with pytest.raises(GrafxError) as raised:
        db.close()
    assert raised.value is first
    assert ran == ["inner", "outer"]
    release_ports(registry)
