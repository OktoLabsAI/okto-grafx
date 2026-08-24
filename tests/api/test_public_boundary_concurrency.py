"""Concurrency and side-effect probes for the sealed public observation boundary."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from okto_grafx import connect
from okto_grafx.adapters.metrics_contained import ContainedMetricsSink
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
)
from okto_grafx.domain.txn import WalRecordType
from okto_grafx.domain.txn.context import TransactionContext
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.engine.database import Database, Transaction
from okto_grafx.engine.index_manager import IndexManager
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

    def increment(self, name: str, value: float = 1.0, labels: object = None) -> None:
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


def _instrumented_database() -> tuple[
    Any, Any, FaultInjectingStorageDevice, _RecordingMetrics
]:
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
                raise AssertionError(
                    "the WAL-view probe did not release the paused commit"
                )
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
        assert append_registered.wait(_WAIT_SECONDS), (
            "commit never registered its WAL append"
        )
        assert database._wal._unflushed, "the append did not leave a segment pending"

        view_thread = threading.Thread(target=observe, name="wal-view")
        view_thread.start()
        assert view_started.wait(_WAIT_SECONDS)
        assert not view_finished.wait(0.2), (
            "WAL observation crossed an in-flight commit"
        )
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
                    raise AssertionError(
                        "the catalog-view probe did not release the DDL commit"
                    )
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
        assert first_chain_applied.wait(_WAIT_SECONDS), (
            "DDL never applied its first chain page"
        )
        view_thread.start()
        assert view_started.wait(_WAIT_SECONDS)
        assert not view_finished.wait(0.2), (
            "catalog view escaped between DDL page images"
        )
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
        committed_index_names = frozenset(
            index.name for index in database.indexes.indexes()
        )
        committed_vector_indexes = tuple(database.vectors.indexes())
        assert tuple(table.name for table in database.catalog.catalog.tables()) == (
            "P",
        )
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


def test_public_vector_filter_is_exact_immutable_data_and_never_a_reentrant_predicate() -> (
    None
):
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


def test_invalid_retry_keeps_speculative_ddl_intact_until_real_rollback() -> None:
    """Manager validation refuses before public retry can unwind a live schema journal."""
    database, registry, storage, _metrics = _instrumented_database()
    transaction = database.begin("write")
    transaction.execute("CREATE NODE TABLE Pending(id INT64, PRIMARY KEY(id))")

    try:
        with pytest.raises(GrafxTransactionStateError) as raised:
            database.retry(transaction)

        assert raised.value.details["conflicts"] == 0
        assert transaction.active
        assert transaction.txn_id in database._public_contexts
        assert transaction.txn_id in database._queries._working
        assert transaction.txn_id in database._queries._txn_effects
        assert "pk_pending" in database._indexes._indexes
        assert storage.exists("index/pk_Pending.idx")

        transaction.rollback()
        assert not transaction.active
        assert transaction.txn_id not in database._public_contexts
        assert transaction.txn_id not in database._queries._working
        assert transaction.txn_id not in database._queries._txn_effects
        assert "pk_pending" not in database._indexes._indexes
        assert not storage.exists("index/pk_Pending.idx")
    finally:
        database.close()
        release_ports(registry)


def test_retry_cleanup_preenter_failure_seals_and_retires_unpublished_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable ACTIVE successor forces terminal close without masking settlement."""
    database, registry, _storage, _metrics = _instrumented_database()
    loser = _conflicted_writer(database)
    settlement_failure = RuntimeError("predecessor schema settlement sentinel")
    cleanup_failure = SystemExit("successor rollback pre-enter sentinel")
    original_settle = Database._settle_schema
    original_section = TransactionManager._participant_section
    original_begin = TransactionManager._begin_in_section
    created: list[TransactionContext] = []
    cleanup_armed = [False]

    def capture_successor(self: TransactionManager, mode: object):  # noqa: ANN202
        answer = original_begin(self, mode)  # type: ignore[arg-type]
        if self is database._transactions:
            created.append(answer[0])
            cleanup_armed[0] = True
        return answer

    def refuse_predecessor(
        self: Database, context: TransactionContext, *, committed: bool
    ) -> None:
        if self is database and context is loser._context:
            raise settlement_failure
        original_settle(self, context, committed=committed)

    def fail_successor_rollback_preenter(self: TransactionManager):  # noqa: ANN202
        if self is database._transactions and cleanup_armed[0]:
            cleanup_armed[0] = False

            @contextmanager
            def refused() -> Iterator[None]:
                raise cleanup_failure
                yield  # pragma: no cover - context entry always raises

            return refused()
        return original_section(self)

    with monkeypatch.context() as boundary:
        boundary.setattr(TransactionManager, "_begin_in_section", capture_successor)
        boundary.setattr(
            TransactionManager, "_participant_section", fail_successor_rollback_preenter
        )
        boundary.setattr(Database, "_settle_schema", refuse_predecessor)
        with pytest.raises(RuntimeError) as raised:
            database.retry(loser)

    assert raised.value is settlement_failure
    assert len(created) == 1 and created[0].state.value == "aborted"
    assert any(
        "SystemExit" in note and "successor rollback pre-enter sentinel" in note
        for note in getattr(settlement_failure, "__notes__", ())
    )
    assert database.closed and database.close_complete
    assert database._transactions.open_transactions == 0
    assert database._coordinator.reader_horizon() is None
    assert database._public_contexts == {}
    release_ports(registry)


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
        assert commit_checked.wait(_WAIT_SECONDS), (
            "old commit never crossed its wrapper check"
        )
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


def test_committed_schema_settlement_failure_is_diagnostic_and_close_retries_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup bomb after manager success cannot turn a durable commit into a retry signal."""
    database = connect(tmp_path / "settlement-retry")
    transaction = database.begin("write")
    transaction.execute("CREATE NODE TABLE Durable(id INT64, PRIMARY KEY(id))")
    settlement_failure = RuntimeError("committed settlement sentinel")
    original_settle = QueryEngine.settle_schema
    armed = [True]

    def fail_once(self: QueryEngine, txn_id: int, *, committed: bool) -> None:
        if self is database._queries and txn_id == transaction.txn_id and armed[0]:
            armed[0] = False
            assert committed is True
            raise settlement_failure
        original_settle(self, txn_id, committed=committed)

    with monkeypatch.context() as boundary:
        boundary.setattr(QueryEngine, "settle_schema", fail_once)
        report = transaction.commit()

    assert report.durable and report.wrote
    assert transaction.report == report
    assert transaction.report is not report
    assert not transaction.active
    assert database._close_failure is settlement_failure
    assert database._public_contexts == {transaction.txn_id: transaction._context}

    database.close()

    assert database.close_complete
    assert database._public_contexts == {}
    with connect(tmp_path / "settlement-retry") as reopened:
        assert reopened.catalog.catalog.has_table("Durable")
        assert tuple(index.name for index in reopened.indexes.indexes()) == (
            "pk_Durable",
        )


def test_public_wrapper_reports_durable_outcome_when_foreign_apply_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-barrier typed failure leaves the public wrapper terminal with an honest report."""
    database, registry, _storage, _metrics = _instrumented_database()
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
    writer = database.begin("write")
    writer.execute("CREATE (:P {id: 1})")
    apply_failure = RuntimeError("foreign public apply sentinel")
    commits_before = tuple(
        record
        for record in database._wal.read_from(1)
        if record.record_type == int(WalRecordType.COMMIT)
    )
    original_apply = TransactionManager._apply_images

    def fail_apply(
        self: TransactionManager,
        images: object,
    ) -> None:
        if self is database._transactions:
            raise apply_failure
        original_apply(self, images)  # type: ignore[arg-type]

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(TransactionManager, "_apply_images", fail_apply)
            with pytest.raises(GrafxTransactionStateError) as raised:
                writer.commit()

        assert raised.value.__cause__ is apply_failure
        assert raised.value.details["committed"] is True
        assert raised.value.details["durable"] is True
        assert raised.value.details["recovery_required"] is True
        assert raised.value.retryable is False
        assert not writer.active
        assert writer.report is not None
        assert writer.report.durable and writer.report.wrote
        assert writer.report.csn == writer._context.commit_csn
        assert writer.txn_id not in database._public_contexts
        commits_after = tuple(
            record
            for record in database._wal.read_from(1)
            if record.record_type == int(WalRecordType.COMMIT)
        )
        assert len(commits_after) == len(commits_before) + 1
        assert commits_after[-1].txn_id == writer.txn_id
        assert commits_after[-1].lsn == writer.report.csn
    finally:
        database.close()
        release_ports(registry)


def test_autocommit_execute_preserves_primary_when_rollback_cleanup_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The convenience read door annotates cleanup and never masks its statement failure."""
    database = connect(":memory:")
    statement_failure = ValueError("autocommit statement sentinel")
    rollback_failure = SystemExit("autocommit rollback sentinel")

    def fail_statement(
        _transaction: Transaction,
        _text: str,
        _parameters: object = None,
    ) -> object:
        raise statement_failure

    def fail_rollback(
        _manager: TransactionManager,
        _context: TransactionContext,
    ) -> None:
        raise rollback_failure

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(Transaction, "execute", fail_statement)
            boundary.setattr(TransactionManager, "rollback", fail_rollback)
            with pytest.raises(ValueError) as raised:
                database.execute("RETURN 1")

        assert raised.value is statement_failure
        assert any(
            "SystemExit" in note and "autocommit rollback sentinel" in note
            for note in getattr(statement_failure, "__notes__", ())
        )
        assert database._transactions.open_transactions == 1
    finally:
        database.close()


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
            boundary.setattr(
                TransactionManager, "_participant_section", tracked_section
            )
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


def test_rollback_cleanup_failure_after_abort_still_unwinds_schema_and_finishes_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ABORTED manager outcome owns schema unwind even when cleanup reports a sentinel."""
    database, registry, storage, _metrics = _instrumented_database()
    transaction = database.begin("write")
    transaction.execute("CREATE NODE TABLE Ghost(id INT64, PRIMARY KEY(id))")
    cleanup_failure = RuntimeError("manager cleanup after abort sentinel")
    original_rollback = TransactionManager.rollback

    def abort_then_fail(self: TransactionManager, context: TransactionContext) -> None:
        original_rollback(self, context)
        if self is database._transactions and context is transaction._context:
            raise cleanup_failure

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(TransactionManager, "rollback", abort_then_fail)
            with pytest.raises(RuntimeError) as raised:
                transaction.rollback()

        assert raised.value is cleanup_failure
        assert not transaction.active
        assert transaction._context.state.value == "aborted"
        assert database._transactions.open_transactions == 0
        assert transaction.txn_id not in database._public_contexts
        assert transaction.txn_id not in database._queries._working
        assert transaction.txn_id not in database._queries._txn_effects
        assert "pk_ghost" not in database._indexes._indexes
        assert not storage.exists("index/pk_Ghost.idx")
    finally:
        database.close()
        release_ports(registry)


def test_close_drains_an_active_ddl_before_releasing_storage_and_late_rollback_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal close removes every speculative registration/file/journal before lower release."""
    database, registry, storage, _metrics = _instrumented_database()
    transaction = database.begin("write")
    transaction.execute("CREATE NODE TABLE Ghost(id INT64, PRIMARY KEY(id))")
    txn_id = transaction.txn_id

    try:
        assert txn_id in database._public_contexts
        assert txn_id in database._queries._working
        assert txn_id in database._queries._txn_effects
        assert "pk_ghost" in database._indexes._indexes
        assert storage.exists("index/pk_Ghost.idx")

        database.close()

        assert database.close_complete
        assert transaction._context.state.value == "aborted"
        assert database._transactions.open_transactions == 0
        assert database._public_contexts == {}
        assert txn_id not in database._queries._working
        assert txn_id not in database._queries._txn_effects
        assert "pk_ghost" not in database._indexes._indexes
        assert not storage.exists("index/pk_Ghost.idx")

        # Close already owns and removed this journal. A tardy wrapper observes ABORTED and its
        # absent-id settlement must not touch QueryEngine or storage after lower release.
        storage.clear_trail()

        def forbidden_settle(*_args: object, **_kwargs: object) -> None:
            raise AssertionError(
                "late rollback reached an already-drained schema journal"
            )

        with monkeypatch.context() as boundary:
            boundary.setattr(QueryEngine, "settle_schema", forbidden_settle)
            transaction.rollback()
        assert storage.trail() == ()
        assert not transaction.active
    finally:
        database.close()
        release_ports(registry)


def test_close_drain_failure_safe_leaks_lower_storage_and_retry_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed schema unwind leaves dependencies owned until a later close drains it."""
    storage_closed = threading.Event()
    target_storage: list[LocalStorageDevice | None] = [None]
    original_storage_close = LocalStorageDevice.close
    original_settle = QueryEngine.settle_schema
    settlement_failure = RuntimeError("close schema drain sentinel")
    armed = [True]

    def observe_storage_close(self: LocalStorageDevice) -> None:
        if self is target_storage[0]:
            storage_closed.set()
        original_storage_close(self)

    def fail_once(self: QueryEngine, txn_id: int, *, committed: bool) -> None:
        if self is database._queries and txn_id == transaction.txn_id and armed[0]:
            armed[0] = False
            assert committed is False
            raise settlement_failure
        original_settle(self, txn_id, committed=committed)

    monkeypatch.setattr(LocalStorageDevice, "close", observe_storage_close)
    database = connect(tmp_path / "failed-close-drain")
    target_storage[0] = database._storage  # type: ignore[assignment]
    transaction = database.begin("write")
    transaction.execute("CREATE NODE TABLE Pending(id INT64, PRIMARY KEY(id))")
    monkeypatch.setattr(QueryEngine, "settle_schema", fail_once)

    with pytest.raises(RuntimeError) as raised:
        database.close()

    assert raised.value is settlement_failure
    assert database.closed and not database.close_complete
    assert database._transactions.close_complete
    assert transaction.txn_id in database._public_contexts
    assert not storage_closed.is_set()

    database.close()

    assert database.close_complete
    assert database._public_contexts == {}
    assert transaction.txn_id not in database._queries._working
    assert transaction.txn_id not in database._queries._txn_effects
    assert "pk_pending" not in database._indexes._indexes
    assert storage_closed.is_set()


def test_close_between_precheck_and_transition_refuses_late_begin_without_new_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal seal closes the false-active-check to late facade-transition race."""
    database, registry, storage, _metrics = _instrumented_database()
    before_transition = threading.Event()
    release_begin = threading.Event()
    failures: list[BaseException] = []
    original_transition = Database._public_transition

    def pause_before_transition(self: Database):  # noqa: ANN202
        inner = original_transition(self)

        @contextmanager
        def paused() -> Iterator[None]:
            if (
                self is database
                and threading.current_thread().name == "late-public-begin"
            ):
                before_transition.set()
                assert release_begin.wait(_WAIT_SECONDS), (
                    "close did not seal the late begin"
                )
            with inner:
                yield

        return paused()

    monkeypatch.setattr(Database, "_public_transition", pause_before_transition)

    def begin_late() -> None:
        try:
            database.begin("read")
        except BaseException as failure:  # noqa: BLE001 - asserted as exact lifecycle evidence
            failures.append(failure)

    worker = threading.Thread(target=begin_late, name="late-public-begin")
    try:
        worker.start()
        assert before_transition.wait(_WAIT_SECONDS), (
            "begin never crossed its open precheck"
        )
        database.close()
        assert database.close_complete
        storage.clear_trail()
    finally:
        release_begin.set()
        worker.join(_WAIT_SECONDS)

    try:
        assert not worker.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], GrafxUnsupportedOperation)
        assert failures[0].details["path"] == ":memory:"
        assert database._transactions.open_transactions == 0
        assert database._public_contexts == {}
        assert storage.trail() == ()
    finally:
        database.close()
        release_ports(registry)


def test_checkpoint_defers_metric_close_until_inventory_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One outer section keeps checkpoint callbacks behind its complete facade postlude."""
    database, registry, _storage, metrics = _instrumented_database()
    manager_finished = [False]
    inventory_finished = [False]
    callback_observations: list[bool] = []
    original_checkpoint = TransactionManager.checkpoint
    original_indexes = IndexManager.indexes

    def checkpoint_then_record(self: TransactionManager):  # noqa: ANN202
        report = original_checkpoint(self)
        if self is database._transactions:
            manager_finished[0] = True
            self._metrics.set_gauge("checkpoint_outer_section_probe", 1.0)
        return report

    def observe_inventory(self: IndexManager):  # noqa: ANN202
        result = original_indexes(self)
        if self is database._indexes and manager_finished[0]:
            inventory_finished[0] = True
        return result

    def close_from_callback() -> None:
        metrics.callback = None
        callback_observations.append(inventory_finished[0])
        database.close()

    try:
        monkeypatch.setattr(TransactionManager, "checkpoint", checkpoint_then_record)
        monkeypatch.setattr(IndexManager, "indexes", observe_inventory)
        metrics.calls.clear()
        metrics.callback = close_from_callback
        report = database.checkpoint()

        assert report.horizon_lsn >= 0
        assert callback_observations == [True]
        assert database.close_complete
    finally:
        metrics.callback = None
        database.close()
        release_ports(registry)


def test_checkpoint_outer_section_blocks_commit_until_inventory_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No committing thread can enter the manager-to-inventory checkpoint gap."""
    database = connect(":memory:")
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
    writer = database.begin("write")
    writer.execute("CREATE (:P {id: 1})")

    manager_finished = threading.Event()
    release_postlude = threading.Event()
    inventory_finished = threading.Event()
    commit_attempted = threading.Event()
    commit_finished = threading.Event()
    checkpoint_failures: list[BaseException] = []
    commit_failures: list[BaseException] = []
    commit_before_inventory: list[bool] = []
    original_checkpoint = TransactionManager.checkpoint
    original_commit = TransactionManager.commit
    original_indexes = IndexManager.indexes

    def pause_after_manager(self: TransactionManager):  # noqa: ANN202
        report = original_checkpoint(self)
        if self is database._transactions:
            manager_finished.set()
            if not release_postlude.wait(_WAIT_SECONDS):
                raise AssertionError("test did not release checkpoint postlude")
        return report

    def mark_inventory(self: IndexManager):  # noqa: ANN202
        result = original_indexes(self)
        if self is database._indexes and manager_finished.is_set():
            inventory_finished.set()
        return result

    def observe_commit(self: TransactionManager, context: TransactionContext) -> object:
        if self is database._transactions and context is writer._context:
            commit_attempted.set()
        result = original_commit(self, context)
        if self is database._transactions and context is writer._context:
            commit_before_inventory.append(not inventory_finished.is_set())
            commit_finished.set()
        return result

    monkeypatch.setattr(TransactionManager, "checkpoint", pause_after_manager)
    monkeypatch.setattr(TransactionManager, "commit", observe_commit)
    monkeypatch.setattr(IndexManager, "indexes", mark_inventory)

    def checkpoint() -> None:
        try:
            database.checkpoint()
        except BaseException as failure:  # noqa: BLE001 - exact evidence below
            checkpoint_failures.append(failure)

    def commit() -> None:
        try:
            writer.commit()
        except BaseException as failure:  # noqa: BLE001 - exact evidence below
            commit_failures.append(failure)
            commit_finished.set()

    checkpoint_worker = threading.Thread(target=checkpoint, name="paused-checkpoint")
    commit_worker = threading.Thread(target=commit, name="checkpoint-racing-commit")
    try:
        checkpoint_worker.start()
        assert manager_finished.wait(_WAIT_SECONDS)
        commit_worker.start()
        assert commit_attempted.wait(_WAIT_SECONDS)
        assert not commit_finished.wait(0.25), (
            "commit crossed the manager-to-inventory checkpoint gap"
        )
        release_postlude.set()
        checkpoint_worker.join(_WAIT_SECONDS)
        commit_worker.join(_WAIT_SECONDS)
    finally:
        release_postlude.set()
        checkpoint_worker.join(_WAIT_SECONDS)
        commit_worker.join(_WAIT_SECONDS)
        database.close()

    assert not checkpoint_worker.is_alive()
    assert not commit_worker.is_alive()
    assert checkpoint_failures == []
    assert commit_failures == []
    assert commit_before_inventory == [False]


def test_vector_math_can_wait_for_cross_thread_close_without_deadlock() -> None:
    """A host VectorMath may synchronously wait for close while search owns page access."""
    database = connect(":memory:")
    with database.begin("write") as schema:
        schema.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
        schema.execute("CREATE NODE TABLE V(id INT64, e VECTOR(s), PRIMARY KEY(id))")
    with database.begin("write") as writer:
        writer.execute("CREATE (:V {id: 1, e: [1.0, 0.0]})")

    inner = PureVectorMath()
    close_joined: list[bool] = []
    close_failures: list[BaseException] = []

    class ClosingMath:
        @property
        def name(self) -> str:
            return "closing"

        def dot(self, a: Sequence[float], b: Sequence[float]) -> float:
            return inner.dot(a, b)

        def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
            return inner.cosine(a, b)

        def euclidean(self, a: Sequence[float], b: Sequence[float]) -> float:
            return inner.euclidean(a, b)

        def norm(self, a: Sequence[float]) -> float:
            return inner.norm(a)

        def normalize(self, a: Sequence[float]) -> tuple[float, ...]:
            return inner.normalize(a)

        def score(
            self,
            a: Sequence[float],
            b: Sequence[float],
            metric: DistanceMetric,
        ) -> float:
            return inner.score(a, b, metric)

        def top_k(
            self,
            query: Sequence[float],
            candidates: Sequence[tuple[int, Sequence[float]]],
            k: int,
            metric: DistanceMetric,
        ) -> list[tuple[int, float]]:
            def close() -> None:
                try:
                    database.close()
                except BaseException as failure:  # noqa: BLE001 - exact evidence below
                    close_failures.append(failure)

            worker = threading.Thread(target=close, name="vector-math-close")
            worker.start()
            worker.join(_WAIT_SECONDS)
            close_joined.append(not worker.is_alive())
            return inner.top_k(query, candidates, k, metric)

    database._vectors._math = ClosingMath()
    reader = database.begin("read")
    result = database.search_vectors(reader, space="s", query=(1.0, 0.0), k=1)

    assert tuple(hit.record_id for hit in result.hits) == (1,)
    assert close_joined == [True]
    assert close_failures == []
    assert not reader.active
    assert database.close_complete


def test_clock_can_wait_for_cross_thread_close_without_deadlock() -> None:
    """A host Clock callback under the participant section sees terminal close return."""
    database = connect(":memory:")
    manager = database._transactions
    inner = manager._clock
    close_joined: list[bool] = []
    close_failures: list[BaseException] = []
    close_workers: list[threading.Thread] = []

    class ClosingClock:
        def __init__(self) -> None:
            self.fired = False

        def monotonic(self) -> float:
            if not self.fired:
                self.fired = True

                def close() -> None:
                    try:
                        database.close()
                    except BaseException as failure:  # noqa: BLE001 - exact evidence below
                        close_failures.append(failure)

                worker = threading.Thread(target=close, name="clock-close")
                close_workers.append(worker)
                worker.start()
                worker.join(_WAIT_SECONDS)
                close_joined.append(not worker.is_alive())
            return inner.monotonic()

        def wall(self) -> float:
            return inner.wall()

    clock = ClosingClock()
    manager._clock = clock
    try:
        with pytest.raises(GrafxTransactionStateError):
            database.begin("read")
    finally:
        database.close()
        for worker in close_workers:
            worker.join(_WAIT_SECONDS)

    assert clock.fired
    assert close_joined == [True]
    assert close_failures == []
    assert all(not worker.is_alive() for worker in close_workers)
    assert manager.open_transactions == 0
    assert database.close_complete


def test_storage_append_can_wait_for_cross_thread_close_without_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supported StorageDevice callback under commit may synchronously wait for close."""
    database, registry, storage, _metrics = _instrumented_database()
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
    writer = database.begin("write")
    writer.execute("CREATE (:P {id: 1})")
    close_joined: list[bool] = []
    close_failures: list[BaseException] = []
    close_workers: list[threading.Thread] = []
    original_append = FaultInjectingStorageDevice.append_log
    fired = [False]

    def append_log(self: FaultInjectingStorageDevice, file: str, payload: bytes) -> int:
        if self is storage and not fired[0] and file.startswith("wal/"):
            fired[0] = True

            def close() -> None:
                try:
                    database.close()
                except BaseException as failure:  # noqa: BLE001 - exact evidence below
                    close_failures.append(failure)

            worker = threading.Thread(target=close, name="storage-append-close")
            close_workers.append(worker)
            worker.start()
            worker.join(_WAIT_SECONDS)
            close_joined.append(not worker.is_alive())
        return original_append(self, file, payload)

    monkeypatch.setattr(FaultInjectingStorageDevice, "append_log", append_log)
    try:
        report = writer.commit()
    finally:
        database.close()
        for worker in close_workers:
            worker.join(_WAIT_SECONDS)
        release_ports(registry)

    assert report.durable and report.wrote
    assert fired == [True]
    assert close_joined == [True]
    assert close_failures == []
    assert all(not worker.is_alive() for worker in close_workers)
    assert database.close_complete


def test_index_unwind_storage_can_wait_for_cross_thread_close_without_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback marks QueryEngine's index/file unwind, not all schema settlement."""
    database, registry, storage, _metrics = _instrumented_database()
    close_joined: list[bool] = []
    close_failures: list[BaseException] = []
    close_workers: list[threading.Thread] = []
    original_remove = FaultInjectingStorageDevice.remove
    fired = [False]

    def remove(self: FaultInjectingStorageDevice, file: str) -> None:
        if self is storage and not fired[0] and file.startswith("index/"):
            fired[0] = True

            def close() -> None:
                try:
                    database.close()
                except BaseException as failure:  # noqa: BLE001 - exact evidence below
                    close_failures.append(failure)

            worker = threading.Thread(target=close, name="index-unwind-close")
            close_workers.append(worker)
            worker.start()
            worker.join(_WAIT_SECONDS)
            close_joined.append(not worker.is_alive())
        original_remove(self, file)

    monkeypatch.setattr(FaultInjectingStorageDevice, "remove", remove)
    transaction = database.begin("write")
    transaction.execute(
        "CREATE VECTOR SPACE transient {dimension: 2, metric: 'cosine'}"
    )
    transaction.execute(
        "CREATE NODE TABLE V(id INT64, e VECTOR(transient), PRIMARY KEY(id))"
    )
    try:
        transaction.rollback()
    finally:
        database.close()
        for worker in close_workers:
            worker.join(_WAIT_SECONDS)
        release_ports(registry)

    assert fired == [True]
    assert close_joined == [True]
    assert close_failures == []
    assert all(not worker.is_alive() for worker in close_workers)
    assert not transaction.active
    assert database.close_complete


def test_recovery_mechanism_can_wait_for_cross_thread_close_without_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery's storage/quarantine/index pass remains resumable as one host phase."""
    database = connect(":memory:")
    manager = database._recovery
    original_run = type(manager).run
    close_joined: list[bool] = []
    close_failures: list[BaseException] = []
    close_workers: list[threading.Thread] = []
    fired = [False]

    def run(self: object) -> object:
        if self is manager and not fired[0]:
            fired[0] = True

            def close() -> None:
                try:
                    database.close()
                except BaseException as failure:  # noqa: BLE001 - exact evidence below
                    close_failures.append(failure)

            worker = threading.Thread(target=close, name="recovery-mechanism-close")
            close_workers.append(worker)
            worker.start()
            worker.join(_WAIT_SECONDS)
            close_joined.append(not worker.is_alive())
        return original_run(self)

    monkeypatch.setattr(type(manager), "run", run)
    try:
        with pytest.raises(GrafxTransactionStateError):
            database.recover()
    finally:
        database.close()
        for worker in close_workers:
            worker.join(_WAIT_SECONDS)

    assert fired == [True]
    assert close_joined == [True]
    assert close_failures == []
    assert all(not worker.is_alive() for worker in close_workers)
    assert database.close_complete


@pytest.mark.parametrize("phase", ("factory", "enter", "exit"))
def test_coordinator_context_phases_can_wait_for_cross_thread_close(
    phase: str,
) -> None:
    """Coordinator factory/enter/exit callbacks are hazards, never the yielded body."""
    database = connect(":memory:")
    manager = database._transactions
    inner = manager._coordinator
    close_joined: list[bool] = []
    close_failures: list[BaseException] = []
    close_workers: list[threading.Thread] = []
    fired = [False]

    def fire_close() -> None:
        fired[0] = True

        def close() -> None:
            try:
                database.close()
            except BaseException as failure:  # noqa: BLE001 - exact evidence below
                close_failures.append(failure)

        worker = threading.Thread(target=close, name=f"coordinator-{phase}-close")
        close_workers.append(worker)
        worker.start()
        worker.join(_WAIT_SECONDS)
        close_joined.append(not worker.is_alive())

    class ClosingSection:
        def __init__(
            self,
            section: object,
            *,
            entered: bool = False,
            value: object = None,
        ) -> None:
            self.section = section
            self.entered = entered
            self.value = value

        def __enter__(self) -> object:
            if self.entered:
                return self.value
            self.value = self.section.__enter__()  # type: ignore[attr-defined]
            self.entered = True
            if phase == "enter" and not fired[0]:
                fire_close()
            return self.value

        def __exit__(self, kind: object, value: object, trace: object) -> object:
            if phase == "exit" and not fired[0]:
                fire_close()
            return self.section.__exit__(kind, value, trace)  # type: ignore[attr-defined]

    class ClosingCoordinator:
        def __getattr__(self, name: str) -> object:
            return getattr(inner, name)

        def exclusive(self, name: str, *, timeout: float) -> object:
            section = inner.exclusive(name, timeout=timeout)
            if fired[0] or name != manager._participant_section_name:
                return section
            if phase == "factory":
                value = section.__enter__()
                fire_close()
                return ClosingSection(section, entered=True, value=value)
            return ClosingSection(section)

    manager._coordinator = ClosingCoordinator()  # type: ignore[assignment]
    try:
        with pytest.raises(GrafxTransactionStateError):
            database.begin("read")
    finally:
        database.close()
        for worker in close_workers:
            worker.join(_WAIT_SECONDS)

    assert fired == [True]
    assert close_joined == [True]
    assert close_failures == []
    assert all(not worker.is_alive() for worker in close_workers)
    assert manager.open_transactions == 0
    assert database.close_complete


def test_close_cannot_enter_the_page_access_acquire_to_body_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global page marker closes the last window before the in-section recheck."""
    database = connect(":memory:")
    reader = database.begin("read")
    manager = database._transactions
    section_acquired = threading.Event()
    release_body = threading.Event()
    search_failures: list[BaseException] = []
    close_failures: list[BaseException] = []
    original_section = TransactionManager._participant_section

    def pause_after_acquire(self: TransactionManager):  # noqa: ANN202
        inner = original_section(self)

        @contextmanager
        def paused() -> Iterator[None]:
            with inner:
                if (
                    self is manager
                    and threading.current_thread().name == "page-window-search"
                ):
                    section_acquired.set()
                    if not release_body.wait(_WAIT_SECONDS):
                        raise AssertionError("test did not release page-access body")
                yield

        return paused()

    def search() -> None:
        try:
            database.search_vectors(reader, space="s", query=(1.0,), k=1)
        except BaseException as failure:  # noqa: BLE001 - exact evidence below
            search_failures.append(failure)

    def close() -> None:
        try:
            database.close()
        except BaseException as failure:  # noqa: BLE001 - exact evidence below
            close_failures.append(failure)

    monkeypatch.setattr(TransactionManager, "_participant_section", pause_after_acquire)
    search_worker = threading.Thread(target=search, name="page-window-search")
    close_worker = threading.Thread(target=close, name="page-window-close")
    try:
        search_worker.start()
        assert section_acquired.wait(_WAIT_SECONDS)
        close_worker.start()
        close_worker.join(_WAIT_SECONDS)
        assert not close_worker.is_alive(), "close waited in the acquire-to-body window"
        assert close_failures == []
        assert database.closed and not database.close_complete
        release_body.set()
        search_worker.join(_WAIT_SECONDS)
    finally:
        release_body.set()
        search_worker.join(_WAIT_SECONDS)
        close_worker.join(_WAIT_SECONDS)
        database.close()

    assert not search_worker.is_alive()
    assert len(search_failures) == 1
    assert isinstance(search_failures[0], GrafxTransactionStateError)
    assert database.close_complete


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt, SystemExit))
def test_automatic_close_release_failure_has_one_concurrent_explicit_claimant(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    """Two explicit closes race for one exact swallowed RuntimeError or process signal."""
    database = connect(":memory:")
    snapshot_inside = threading.Event()
    release_snapshot = threading.Event()
    snapshot_failures: list[BaseException] = []
    early_close_failures: list[BaseException] = []
    closer_calls = [0]
    bomb = failure_type("automatic release sentinel")
    original_snapshot = ContainedMetricsSink.snapshot

    def pause_snapshot(self: ContainedMetricsSink) -> object:
        if self is database._metrics:
            snapshot_inside.set()
            if not release_snapshot.wait(_WAIT_SECONDS):
                raise AssertionError("test did not release metrics snapshot")
        return original_snapshot(self)

    def failing_closer() -> None:
        closer_calls[0] += 1
        raise bomb

    def snapshot() -> None:
        try:
            database.snapshot_metrics()
        except BaseException as failure:  # noqa: BLE001 - exact evidence below
            snapshot_failures.append(failure)

    def close() -> None:
        try:
            database.close()
        except BaseException as failure:  # noqa: BLE001 - exact evidence below
            early_close_failures.append(failure)

    monkeypatch.setattr(ContainedMetricsSink, "snapshot", pause_snapshot)
    object.__setattr__(database, "_closers", (*database._closers, failing_closer))
    snapshot_worker = threading.Thread(target=snapshot, name="paused-metrics-snapshot")
    close_worker = threading.Thread(target=close, name="snapshot-concurrent-close")
    try:
        snapshot_worker.start()
        assert snapshot_inside.wait(_WAIT_SECONDS)
        close_worker.start()
        close_worker.join(_WAIT_SECONDS)
        assert not close_worker.is_alive()
        assert early_close_failures == []
        assert database.closed and not database.close_complete
        release_snapshot.set()
        snapshot_worker.join(_WAIT_SECONDS)
    finally:
        release_snapshot.set()
        snapshot_worker.join(_WAIT_SECONDS)
        close_worker.join(_WAIT_SECONDS)

    assert snapshot_failures == []
    assert database.close_complete
    assert database._close_failure is bomb
    assert closer_calls == [1]
    claim_barrier = threading.Barrier(3, timeout=_WAIT_SECONDS)
    claim_outcomes: list[None] = []
    claim_failures: list[BaseException] = []

    def claim() -> None:
        try:
            claim_barrier.wait()
            claim_outcomes.append(database.close())
        except BaseException as failure:  # noqa: BLE001 - identity is the assertion
            claim_failures.append(failure)

    claim_workers = [
        threading.Thread(target=claim, name=f"release-failure-claim-{index}")
        for index in range(2)
    ]
    for worker in claim_workers:
        worker.start()
    claim_barrier.wait()
    for worker in claim_workers:
        worker.join(_WAIT_SECONDS)

    assert all(not worker.is_alive() for worker in claim_workers)
    assert claim_outcomes == [None]
    assert claim_failures == [bomb]
    database.close()
    assert closer_calls == [1]


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
    assert manager_committed.wait(_WAIT_SECONDS), (
        "DDL never committed inside its manager"
    )
    close_worker.start()
    close_worker.join(_WAIT_SECONDS)
    assert not close_worker.is_alive(), (
        "close waited for a wrapper after manager quiescence"
    )
    assert close_failures == []
    assert settle_outcomes == [True]
    assert database._public_contexts == {}
    assert not database.close_complete

    # The wrapper's absent-id settlement is a QueryEngine no-op; leaving its facade transition
    # then resumes the pending close and releases lower dependencies.
    release_wrapper.set()
    commit_worker.join(_WAIT_SECONDS)

    assert not commit_worker.is_alive()
    assert commit_failures == []
    assert len(commit_results) == 1 and commit_results[0].durable is True
    assert settle_outcomes == [True]
    assert database.close_complete

    with connect(tmp_path / "committed-schema") as reopened:
        assert reopened.catalog.catalog.has_table("Durable")
        assert tuple(index.name for index in reopened.indexes.indexes()) == (
            "pk_Durable",
        )
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

    rollback_worker = threading.Thread(
        target=rollback, name="manager-rolled-back-wrapper"
    )
    close_worker = threading.Thread(target=close, name="close-rolled-back-schema")
    try:
        rollback_worker.start()
        assert manager_rolled_back.wait(_WAIT_SECONDS), (
            "manager rollback never released its pin"
        )
        close_worker.start()
        assert close_drain_started.wait(_WAIT_SECONDS), (
            "close did not adopt schema unwind"
        )
        assert not storage_closed.is_set(), (
            "storage closed while speculative files were live"
        )
        assert settle_outcomes == [False]
        assert close_worker.is_alive()

        release_close_drain.set()
        close_worker.join(_WAIT_SECONDS)
        assert not close_worker.is_alive()
        assert close_failures == []
        assert not storage_closed.is_set()
        assert not database.close_complete
        assert database._public_contexts == {}

        # The original wrapper now returns from manager.rollback. Its settlement is absent-id
        # and cannot reach QueryEngine again; transition exit resumes the safe pending close.
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
    assert storage_closed.is_set()
    assert database.close_complete

    with connect(tmp_path / "rolled-back-schema") as reopened:
        assert not reopened.catalog.catalog.has_table("Ghost")
        assert reopened.indexes.indexes() == ()
        assert reopened.verify("all").findings == ()
