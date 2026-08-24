"""Concurrency and side-effect probes for the sealed public observation boundary."""

from __future__ import annotations

import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from okto_grafx import connect
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxTransactionStateError,
    GrafxWriteConflict,
)
from okto_grafx.domain.txn.context import TransactionContext
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.engine.database import Database, Transaction
from okto_grafx.engine.catalog_store import CATALOG_FILE
from okto_grafx.engine import txn_manager as txn_module
from okto_grafx.engine.query_engine import QueryEngine
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.wal_manager import WalManager
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports
from okto_grafx.runtime.config import DatabaseConfig

pytestmark = pytest.mark.timeout(60, method="thread")

_WAIT_SECONDS = 5.0


class _RecordingMetrics:
    """Small enabled sink whose calls can prove an observation stayed callback-free."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.callback: Callable[[], None] | None = None

    def _record(self, kind: str, name: str) -> None:
        """Record one call and run the currently armed hostile callback at most as configured."""
        self.calls.append((kind, name))
        callback = self.callback
        if callback is not None:
            callback()

    @property
    def enabled(self) -> bool:
        return True

    def register(self, descriptor: object) -> None:
        self._record("register", str(getattr(descriptor, "name", "")))

    def increment(
        self, name: str, value: float = 1.0, labels: object = None
    ) -> None:
        del value, labels
        self._record("increment", name)

    def set_gauge(self, name: str, value: float, labels: object = None) -> None:
        del value, labels
        self._record("set_gauge", name)

    def observe(self, name: str, value: float, labels: object = None) -> None:
        del value, labels
        self._record("observe", name)

    def time(self, name: str, labels: object = None) -> object:
        del labels
        self._record("time", name)
        return nullcontext()

    def snapshot(self) -> dict[str, object]:
        self._record("snapshot", "")
        return {}


def _instrumented_database() -> tuple[Any, Any, FaultInjectingStorageDevice, _RecordingMetrics]:
    """Compose one database over a recording storage and metrics sink."""
    registry = build_default_registry(DatabaseConfig(path=":memory:"))
    storage = FaultInjectingStorageDevice(registry.get("storage"), seed=17)
    metrics = _RecordingMetrics()
    registry.bind("storage", storage)
    registry.bind("metrics", metrics)
    return connect(":memory:", registry=registry), registry, storage, metrics


def _conflicted_writer(database: Database) -> Transaction:
    """Return one still-active writer after a real optimistic refusal."""
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
    with database.begin("write") as seed:
        seed.execute("CREATE (:P {id: 1, name: 'seed'})")
    winner = database.begin("write")
    loser = database.begin("write")
    winner.execute("MATCH (p:P {id: 1}) SET p.name = 'winner'")
    loser.execute("MATCH (p:P {id: 1}) SET p.name = 'loser'")
    winner.commit()
    with pytest.raises(GrafxWriteConflict):
        loser.commit()
    assert loser.active
    return loser


def test_wal_observation_waits_for_commit_and_cannot_erase_its_pending_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent ``database.wal`` cannot clear ``_unflushed`` before commit barriers it."""
    database, registry, storage, _metrics = _instrumented_database()
    release_commit = threading.Event()
    append_registered = threading.Event()
    view_started = threading.Event()
    view_finished = threading.Event()
    armed = threading.Event()
    commit_results: list[object] = []
    views: list[object] = []
    failures: list[BaseException] = []
    refresh_calls: list[str] = []
    original_append_many = WalManager.append_many
    original_refresh = WalManager.refresh

    def pause_after_append(
        manager: WalManager,
        records: object,
        *,
        expected_terminal_lsn: int | None = None,
    ) -> int:
        result = original_append_many(
            manager,
            records,  # type: ignore[arg-type]
            expected_terminal_lsn=expected_terminal_lsn,
        )
        if manager is database._wal and armed.is_set():
            append_registered.set()
            if not release_commit.wait(_WAIT_SECONDS):
                raise AssertionError("the WAL-view probe did not release the paused commit")
        return result

    def destructive_refresh(manager: WalManager) -> None:
        # This models the exact old failure: refresh could replace the pending cache while the
        # commit sat between append and barrier.  The corrected view never reaches this method.
        if manager is database._wal and threading.current_thread().name == "wal-view":
            refresh_calls.append(threading.current_thread().name)
            manager._unflushed.clear()
            return
        original_refresh(manager)

    monkeypatch.setattr(WalManager, "append_many", pause_after_append)
    monkeypatch.setattr(WalManager, "refresh", destructive_refresh)
    writer = None
    commit_thread = None
    view_thread = None
    try:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        writer = database.begin("write")
        writer.execute("CREATE (:P {id: 1})")
        storage.clear_trail()
        armed.set()

        def commit() -> None:
            try:
                commit_results.append(writer.commit())
            except BaseException as failure:  # noqa: BLE001 - evidence belongs to the assertion
                failures.append(failure)

        def observe() -> None:
            view_started.set()
            try:
                views.append(database.wal)
            except BaseException as failure:  # noqa: BLE001 - evidence belongs to the assertion
                failures.append(failure)
            finally:
                view_finished.set()

        commit_thread = threading.Thread(target=commit, name="wal-commit")
        commit_thread.start()
        assert append_registered.wait(_WAIT_SECONDS), "commit never registered its WAL append"
        assert database._wal._unflushed, "the append did not leave a segment pending"

        view_thread = threading.Thread(target=observe, name="wal-view")
        view_thread.start()
        assert view_started.wait(_WAIT_SECONDS)
        assert not view_finished.wait(0.2), "WAL observation crossed an in-flight commit"
    finally:
        release_commit.set()
        for thread in (commit_thread, view_thread):
            if thread is not None:
                thread.join(_WAIT_SECONDS)

    try:
        assert failures == []
        assert commit_thread is not None and not commit_thread.is_alive()
        assert view_thread is not None and not view_thread.is_alive()
        assert len(commit_results) == 1 and commit_results[0].durable is True
        assert len(views) == 1 and views[0].last_lsn == commit_results[0].csn
        assert refresh_calls == []
        assert database._wal._unflushed == []
        assert any(
            call.method == "durable_barrier"
            and call.file is not None
            and call.file.startswith("wal/")
            for call in storage.trail()
        ), "the commit acknowledged without a WAL durability barrier"
    finally:
        if writer is not None and writer.active:
            writer.rollback()
        database.close()
        release_ports(registry)


def test_pool_and_wal_views_do_no_io_or_telemetry_when_pages_are_cold() -> None:
    """The two cache-only inventories never fault, evict or invoke host telemetry."""
    database, registry, storage, metrics = _instrumented_database()
    try:
        with database.begin("write") as schema:
            schema.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
            schema.execute(
                "CREATE NODE TABLE P(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )
        database.checkpoint()
        database._pool.invalidate()
        storage.clear_trail()
        metrics.calls.clear()

        pool = database.pool
        wal = database.wal

        assert pool.used_bytes() == 0
        assert wal.last_lsn >= 0
        assert storage.trail() == ()
        assert metrics.calls == []

        # Schema accuracy requires a cold catalog read.  It happens optimistically outside the
        # participant section; index headers themselves stay optional rather than causing an
        # eviction just to fill a diagnostic field.
        catalog = database.catalog
        indexes = database.indexes
        vectors = database.vectors
        assert [table.name for table in catalog.catalog.tables()] == ["P"]
        assert indexes.indexes()
        assert all(index.built_through_lsn is None for index in indexes.indexes())
        assert all(index.reconciled_through_lsn is None for index in indexes.indexes())
        assert vectors.indexes()
        assert all(index.built_through_lsn is None for index in vectors.indexes())
    finally:
        database.close()
        release_ports(registry)


def test_catalog_view_waits_out_a_multi_page_ddl_and_never_returns_a_partial_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Epoch validation linearizes a cold view after the whole catalog commit, not mid-apply."""
    database = connect(":memory:", page_size=512)
    writer = database.begin("write")
    names = tuple(f"T{position}" for position in range(18))
    for name in names:
        writer.execute(f"CREATE NODE TABLE {name}(value STRING)")
    catalog_images = tuple(
        key for key in writer._context.page_images if key[0] == CATALOG_FILE
    )
    assert len(catalog_images) >= 3, "the probe needs a genuinely multi-page catalog"

    first_chain_applied = threading.Event()
    release_commit = threading.Event()
    view_started = threading.Event()
    view_finished = threading.Event()
    commit_failures: list[BaseException] = []
    view_failures: list[BaseException] = []
    views: list[object] = []
    applied = 0
    original_apply = txn_module.apply_page_image

    def pause_between_catalog_images(
        pool: object, file: str, page_index: int, image: bytes
    ) -> bool:
        nonlocal applied
        result = original_apply(pool, file, page_index, image)  # type: ignore[arg-type]
        if (
            file == CATALOG_FILE
            and page_index != 0
            and threading.current_thread().name == "ddl-commit"
        ):
            applied += 1
            if applied == 1:
                first_chain_applied.set()
                if not release_commit.wait(_WAIT_SECONDS):
                    raise AssertionError("the catalog-view probe did not release the DDL commit")
        return result

    monkeypatch.setattr(txn_module, "apply_page_image", pause_between_catalog_images)

    def commit() -> None:
        try:
            writer.commit()
        except BaseException as failure:  # noqa: BLE001 - evidence belongs to the assertion
            commit_failures.append(failure)

    def observe() -> None:
        view_started.set()
        try:
            views.append(database.catalog)
        except BaseException as failure:  # noqa: BLE001 - evidence belongs to the assertion
            view_failures.append(failure)
        finally:
            view_finished.set()

    commit_thread = threading.Thread(target=commit, name="ddl-commit")
    view_thread = threading.Thread(target=observe, name="catalog-view")
    try:
        commit_thread.start()
        assert first_chain_applied.wait(_WAIT_SECONDS), "DDL never applied its first chain page"
        view_thread.start()
        assert view_started.wait(_WAIT_SECONDS)
        assert not view_finished.wait(0.2), "catalog view escaped between DDL page images"
    finally:
        release_commit.set()
        commit_thread.join(_WAIT_SECONDS)
        view_thread.join(_WAIT_SECONDS)

    try:
        assert not commit_thread.is_alive() and not view_thread.is_alive()
        assert commit_failures == []
        assert view_failures == []
        assert len(views) == 1
        assert tuple(table.name for table in views[0].catalog.tables()) == names
    finally:
        if writer.active:
            writer.rollback()
        database.close()


def test_schema_and_index_views_publish_only_committed_ddl() -> None:
    """Pre-commit registrations stay hidden; commit publishes them and rollback never does."""
    with connect(":memory:") as database:
        speculative = database.begin("write")
        speculative.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
        speculative.execute(
            "CREATE NODE TABLE P(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
        )

        assert database.catalog.catalog.tables() == ()
        assert database.catalog.catalog.spaces() == ()
        assert database.indexes.indexes() == ()
        assert database.vectors.spaces() == ()
        assert database.vectors.indexes() == ()

        speculative.commit()
        committed_index_names = frozenset(index.name for index in database.indexes.indexes())
        committed_vector_indexes = tuple(database.vectors.indexes())
        assert tuple(table.name for table in database.catalog.catalog.tables()) == ("P",)
        assert tuple(space.name for space in database.vectors.spaces()) == ("s",)
        assert "pk_P" in committed_index_names
        assert len(committed_vector_indexes) == 1
        assert committed_vector_indexes[0].space_name == "s"

        rolled_back = database.begin("write")
        rolled_back.execute("CREATE VECTOR SPACE t {dimension: 2, metric: 'cosine'}")
        rolled_back.execute(
            "CREATE NODE TABLE Q(id INT64, embedding VECTOR(t), PRIMARY KEY(id))"
        )
        assert frozenset(index.name for index in database.indexes.indexes()) == (
            committed_index_names
        )
        assert tuple(index.name for index in database.vectors.indexes()) == tuple(
            index.name for index in committed_vector_indexes
        )
        assert tuple(space.name for space in database.vectors.spaces()) == ("s",)

        rolled_back.rollback()
        assert frozenset(index.name for index in database.indexes.indexes()) == (
            committed_index_names
        )
        assert tuple(index.name for index in database.vectors.indexes()) == tuple(
            index.name for index in committed_vector_indexes
        )
        assert tuple(space.name for space in database.vectors.spaces()) == ("s",)


def test_public_vector_filter_is_exact_immutable_data_and_never_a_reentrant_predicate() -> None:
    """A predicate that would roll back its reader is rejected before the vector engine."""
    with connect(":memory:") as database:
        with database.begin("write") as schema:
            schema.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
            schema.execute(
                "CREATE NODE TABLE P(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )
        with database.begin("write") as writer:
            writer.execute("CREATE (:P {id: 1, embedding: [1.0, 0.0]})")
            writer.execute("CREATE (:P {id: 2, embedding: [0.0, 1.0]})")

        reader = database.begin("read")
        invoked: list[int] = []

        class _RollbackFilter:
            @property
            def cardinality(self) -> int:
                return 1

            def admits(self, record_id: int) -> bool:
                invoked.append(record_id)
                reader.rollback()
                return True

        try:
            with pytest.raises(GrafxConfigurationError) as refused:
                database.search_vectors(
                    reader,
                    space="s",
                    query=(1.0, 0.0),
                    k=2,
                    candidate_filter=_RollbackFilter(),  # type: ignore[arg-type]
                )
            assert refused.value.details["field"] == "candidate_filter"
            assert invoked == []
            assert reader.active is True

            result = database.search_vectors(
                reader,
                space="s",
                query=(1.0, 0.0),
                k=2,
                candidate_filter=RecordIdFilter.of((2,)),
            )
            assert [hit.record_id for hit in result.hits] == [2]
        finally:
            if reader.active:
                reader.rollback()


def test_retry_settlement_failure_retires_the_unpublished_successor_and_keeps_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A predecessor unwind failure cannot leak its successor or be masked by cleanup."""
    database = connect(":memory:")
    loser = _conflicted_writer(database)
    settlement_failure = RuntimeError("schema settlement sentinel")
    cleanup_failure = SystemExit("successor cleanup sentinel")
    original_settle = Database._settle_schema
    original_rollback = TransactionManager.rollback
    cleaned: list[TransactionContext] = []

    def refuse_predecessor(
        self: Database, context: TransactionContext, *, committed: bool
    ) -> None:
        if self is database and context is loser._context:
            raise settlement_failure
        original_settle(self, context, committed=committed)

    def retire_then_report(
        self: TransactionManager, context: TransactionContext
    ) -> None:
        original_rollback(self, context)
        if self is database._transactions and context is not loser._context:
            cleaned.append(context)
            raise cleanup_failure

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(Database, "_settle_schema", refuse_predecessor)
            boundary.setattr(TransactionManager, "rollback", retire_then_report)
            with pytest.raises(RuntimeError) as raised:
                database.retry(loser)

        assert raised.value is settlement_failure
        assert loser.active is False
        assert len(cleaned) == 1 and cleaned[0].active is False
        assert database._transactions.open_transactions == 0
        assert any(
            "SystemExit" in note and "successor cleanup sentinel" in note
            for note in getattr(settlement_failure, "__notes__", ())
        )
    finally:
        database.close()


def test_public_retry_and_a_checked_commit_have_one_atomic_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit cannot use an old wrapper after retry atomically replaced its context."""
    database, registry, _storage, metrics = _instrumented_database()
    loser = _conflicted_writer(database)
    commit_checked = threading.Event()
    release_commit = threading.Event()
    commit_failures: list[BaseException] = []
    original_require_active = Transaction._require_active
    successor: Transaction | None = None

    def pause_checked_commit(self: Transaction) -> None:
        original_require_active(self)
        if self is loser and threading.current_thread().name == "checked-old-commit":
            commit_checked.set()
            if not release_commit.wait(_WAIT_SECONDS):
                raise AssertionError("retry did not release the checked old commit")

    monkeypatch.setattr(Transaction, "_require_active", pause_checked_commit)

    def commit_old() -> None:
        try:
            loser.commit()
        except BaseException as failure:  # noqa: BLE001 - asserted as lifecycle evidence
            commit_failures.append(failure)

    worker = threading.Thread(target=commit_old, name="checked-old-commit")
    try:
        worker.start()
        assert commit_checked.wait(_WAIT_SECONDS), "old commit never crossed its wrapper check"
        metrics.calls.clear()

        successor = database.retry(loser)

        # A successful atomic replacement does not publish a fictitious zero/one gauge pair and
        # therefore invokes no host metric callback inside or outside a facade section.
        assert metrics.calls == []
        assert successor.active and not loser.active
        assert database._transactions.open_transactions == 1
    finally:
        release_commit.set()
        worker.join(_WAIT_SECONDS)

    try:
        assert not worker.is_alive()
        assert len(commit_failures) == 1
        assert isinstance(commit_failures[0], GrafxTransactionStateError)
        assert successor is not None
        successor.rollback()
        assert database._transactions.open_transactions == 0
    finally:
        database.close()
        release_ports(registry)


def test_failed_retry_metric_reenters_database_only_after_participant_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manager's failed-retry gauge is never captured by an outer Database section."""
    database, registry, _storage, metrics = _instrumented_database()
    loser = _conflicted_writer(database)
    manager = database._transactions
    successor_failure = RuntimeError("successor open sentinel")
    depth = [0]
    metric_depths: list[int] = []
    nested_transactions: list[Transaction] = []
    fail_successor = [True]
    original_section = TransactionManager._participant_section
    original_begin_in_section = TransactionManager._begin_in_section

    def tracked_section(self: TransactionManager):  # noqa: ANN202
        inner = original_section(self)

        @contextmanager
        def tracked() -> Iterator[None]:
            with inner:
                if self is manager:
                    depth[0] += 1
                try:
                    yield
                finally:
                    if self is manager:
                        depth[0] -= 1

        return tracked()

    def refuse_successor(self: TransactionManager, mode: object):  # noqa: ANN202
        if self is manager and fail_successor[0]:
            fail_successor[0] = False
            raise successor_failure
        return original_begin_in_section(self, mode)  # type: ignore[arg-type]

    def reenter_once() -> None:
        metric_depths.append(depth[0])
        metrics.callback = None
        nested = database.begin("read")
        nested_transactions.append(nested)
        nested.rollback()

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(TransactionManager, "_participant_section", tracked_section)
            boundary.setattr(TransactionManager, "_begin_in_section", refuse_successor)
            metrics.calls.clear()
            metrics.callback = reenter_once
            with pytest.raises(RuntimeError) as raised:
                database.retry(loser)

        assert raised.value is successor_failure
        assert metric_depths == [0]
        assert len(nested_transactions) == 1 and not nested_transactions[0].active
        assert not loser.active
        assert database._transactions.open_transactions == 0
    finally:
        metrics.callback = None
        database.close()
        release_ports(registry)


def test_close_drains_committed_schema_with_its_outcome_before_late_wrapper_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DDL commit that won before close is settled as committed and survives reopen."""
    database = connect(tmp_path / "committed-schema")
    transaction = database.begin("write")
    transaction.execute("CREATE NODE TABLE Durable(id INT64, PRIMARY KEY(id))")
    manager_committed = threading.Event()
    release_wrapper = threading.Event()
    settle_outcomes: list[bool] = []
    commit_results: list[object] = []
    commit_failures: list[BaseException] = []
    close_failures: list[BaseException] = []
    original_commit = TransactionManager.commit
    original_settle = QueryEngine.settle_schema

    def pause_after_manager_commit(
        self: TransactionManager, context: TransactionContext
    ) -> object:
        result = original_commit(self, context)
        if self is database._transactions and context is transaction._context:
            manager_committed.set()
            if not release_wrapper.wait(_WAIT_SECONDS):
                raise AssertionError("close did not release the committed wrapper")
        return result

    def observe_settlement(self: QueryEngine, txn_id: int, *, committed: bool) -> None:
        if self is database._queries and txn_id == transaction.txn_id:
            settle_outcomes.append(committed)
        original_settle(self, txn_id, committed=committed)

    monkeypatch.setattr(TransactionManager, "commit", pause_after_manager_commit)
    monkeypatch.setattr(QueryEngine, "settle_schema", observe_settlement)

    def commit() -> None:
        try:
            commit_results.append(transaction.commit())
        except BaseException as failure:  # noqa: BLE001 - asserted below
            commit_failures.append(failure)

    def close() -> None:
        try:
            database.close()
        except BaseException as failure:  # noqa: BLE001 - asserted below
            close_failures.append(failure)

    commit_worker = threading.Thread(target=commit, name="manager-committed-wrapper")
    close_worker = threading.Thread(target=close, name="close-committed-schema")
    commit_worker.start()
    assert manager_committed.wait(_WAIT_SECONDS), "DDL never committed inside its manager"
    close_worker.start()
    close_worker.join(_WAIT_SECONDS)
    assert not close_worker.is_alive(), "close waited for a wrapper after manager quiescence"
    assert close_failures == []
    assert settle_outcomes == [True]
    assert database._public_contexts == {}

    # The wrapper returns after every owned closer. Its absent-id fast path must not attempt to
    # enter coordination (or touch the already-closed storage) a second time.
    with monkeypatch.context() as late:
        late.setattr(
            type(database._coordinator),
            "exclusive",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("late commit settlement entered coordination after close")
            ),
        )
        release_wrapper.set()
        commit_worker.join(_WAIT_SECONDS)

    assert not commit_worker.is_alive()
    assert commit_failures == []
    assert len(commit_results) == 1 and commit_results[0].durable is True

    with connect(tmp_path / "committed-schema") as reopened:
        assert reopened.catalog.catalog.has_table("Durable")
        assert tuple(index.name for index in reopened.indexes.indexes()) == ("pk_Durable",)
        assert reopened.verify("all").findings == ()


def test_close_owns_rollback_schema_unwind_in_the_manager_to_wrapper_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storage stays open until close drains DDL after manager rollback released its pin."""
    storage_closed = threading.Event()
    target_storage: list[LocalStorageDevice | None] = [None]
    original_storage_close = LocalStorageDevice.close

    def observe_storage_close(self: LocalStorageDevice) -> None:
        if self is target_storage[0]:
            storage_closed.set()
        original_storage_close(self)

    monkeypatch.setattr(LocalStorageDevice, "close", observe_storage_close)
    database = connect(tmp_path / "rolled-back-schema")
    target_storage[0] = database._storage  # type: ignore[assignment]
    transaction = database.begin("write")
    transaction.execute("CREATE NODE TABLE Ghost(id INT64, PRIMARY KEY(id))")
    manager_rolled_back = threading.Event()
    release_wrapper = threading.Event()
    close_drain_started = threading.Event()
    release_close_drain = threading.Event()
    settle_outcomes: list[bool] = []
    rollback_failures: list[BaseException] = []
    close_failures: list[BaseException] = []
    original_rollback = TransactionManager.rollback
    original_settle = QueryEngine.settle_schema

    def pause_after_manager_rollback(
        self: TransactionManager, context: TransactionContext
    ) -> None:
        original_rollback(self, context)
        if self is database._transactions and context is transaction._context:
            manager_rolled_back.set()
            if not release_wrapper.wait(_WAIT_SECONDS):
                raise AssertionError("close did not release the rolled-back wrapper")

    def pause_close_drain(self: QueryEngine, txn_id: int, *, committed: bool) -> None:
        if self is database._queries and txn_id == transaction.txn_id:
            settle_outcomes.append(committed)
            if threading.current_thread().name == "close-rolled-back-schema":
                close_drain_started.set()
                if not release_close_drain.wait(_WAIT_SECONDS):
                    raise AssertionError("test did not release close's schema drain")
        original_settle(self, txn_id, committed=committed)

    monkeypatch.setattr(TransactionManager, "rollback", pause_after_manager_rollback)
    monkeypatch.setattr(QueryEngine, "settle_schema", pause_close_drain)

    def rollback() -> None:
        try:
            transaction.rollback()
        except BaseException as failure:  # noqa: BLE001 - asserted below
            rollback_failures.append(failure)

    def close() -> None:
        try:
            database.close()
        except BaseException as failure:  # noqa: BLE001 - asserted below
            close_failures.append(failure)

    rollback_worker = threading.Thread(target=rollback, name="manager-rolled-back-wrapper")
    close_worker = threading.Thread(target=close, name="close-rolled-back-schema")
    try:
        rollback_worker.start()
        assert manager_rolled_back.wait(_WAIT_SECONDS), "manager rollback never released its pin"
        close_worker.start()
        assert close_drain_started.wait(_WAIT_SECONDS), "close did not adopt schema unwind"
        assert not storage_closed.is_set(), "storage closed while speculative files were live"
        assert settle_outcomes == [False]
        assert close_worker.is_alive()

        release_close_drain.set()
        close_worker.join(_WAIT_SECONDS)
        assert not close_worker.is_alive()
        assert close_failures == []
        assert storage_closed.is_set()
        assert database._public_contexts == {}

        # Only after storage is gone does the original wrapper return from manager.rollback. Its
        # settlement must be an absent-id no-op that does not re-enter coordinator or QueryEngine.
        with monkeypatch.context() as late:
            late.setattr(
                type(database._coordinator),
                "exclusive",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("late rollback entered coordination after close")
                ),
            )
            release_wrapper.set()
            rollback_worker.join(_WAIT_SECONDS)
    finally:
        release_close_drain.set()
        release_wrapper.set()
        rollback_worker.join(_WAIT_SECONDS)
        close_worker.join(_WAIT_SECONDS)

    assert not rollback_worker.is_alive()
    assert rollback_failures == []
    assert settle_outcomes == [False]

    with connect(tmp_path / "rolled-back-schema") as reopened:
        assert not reopened.catalog.catalog.has_table("Ghost")
        assert reopened.indexes.indexes() == ()
        assert reopened.verify("all").findings == ()
