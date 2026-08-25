"""Automatic checkpoint policy at the public transaction boundary.

The tests in this module keep threshold arithmetic and races deterministic.  Policy tests feed
an exact published state to the manager seam, concurrency tests coordinate with Events rather
than sleeps, and storage failures are armed only after first-open and schema durability have
finished.  The filesystem scenario is deliberately last-mile: it observes recycled WAL names
and then reopens the database, rather than trusting an in-process maintenance report.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import connect
from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.domain.errors import (
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxError,
)
from okto_grafx.domain.txn.commit_state import CommitState
from okto_grafx.domain.wal.replay import RecycleReport
from okto_grafx.engine.database import Database
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.wal_manager import WalManager
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports
from okto_grafx.runtime.config import DatabaseConfig

pytestmark = pytest.mark.timeout(120, method="thread")

THREAD_DEADLINE_SECONDS = 5.0
WAL_SEGMENT_BYTES = 4 * 1024


class _RecordingEvents:
    """Small EventSink twin local to the public composition being exercised."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, payload: Mapping[str, object]) -> None:
        self.events.append((event, dict(payload)))


class _BrokenEvents:
    """EventSink whose only observable action is a foreign exception."""

    def __init__(self) -> None:
        self.calls = 0

    def emit(self, event: str, payload: Mapping[str, object]) -> None:
        del event, payload
        self.calls += 1
        raise RuntimeError("the event collector is unavailable")


class _ClosingEvents:
    """EventSink that requests terminal database state from inside failure reporting."""

    def __init__(self) -> None:
        self.database: Database | None = None
        self.calls = 0

    def emit(self, event: str, payload: Mapping[str, object]) -> None:
        del event, payload
        self.calls += 1
        database = self.database
        assert database is not None
        database.close()


class _HostileGrafxError(GrafxError):
    """Grafx-shaped maintenance failure whose diagnostic attributes are unsafe."""

    @property
    def code(self) -> str:
        raise RuntimeError("hostile-code")

    @property
    def retryable(self) -> bool:
        raise RuntimeError("hostile-retryable")


class _RefuseNextCheckpointBarrier(FaultInjectingStorageDevice):
    """Refuse one pool-wide barrier while leaving WAL/commit-state barriers honest."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner, seed=1)
        self._refuse_global_barrier = False
        self.refusals = 0

    def arm(self) -> None:
        self._refuse_global_barrier = True

    def durable_barrier(self, file: str | None = None) -> None:
        if self._refuse_global_barrier and file is None:
            self._refuse_global_barrier = False
            self.refusals += 1
            raise GrafxDurabilityBarrierFailed(
                "The injected checkpoint barrier did not complete.",
                file=file,
                operation="checkpoint",
            )
        super().durable_barrier(file)


def _schema(database: Database) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")


def _insert(database: Database, identity: int) -> None:
    with database.begin("write") as txn:
        txn.execute(f"CREATE (:P {{id: {identity}, name: 'n{identity}'}})")


def _identities(database: Database) -> list[int]:
    rows = database.execute("MATCH (p:P) RETURN p.id").rows
    return sorted(identity for (identity,) in rows)


def _fault_composition(
    events: _RecordingEvents | _BrokenEvents | _ClosingEvents,
) -> tuple[Any, _RefuseNextCheckpointBarrier]:
    config = DatabaseConfig(path=":memory:", checkpoint_interval_records=1)
    registry = build_default_registry(config)
    inner = registry.get("storage")
    storage = _RefuseNextCheckpointBarrier(inner)
    registry.bind("storage", storage)
    registry.bind("events", events)
    return registry, storage


def _close_caller_composition(database: Database, registry: Any) -> None:
    try:
        database.close()
    finally:
        release_ports(registry)


def _join(thread: threading.Thread) -> None:
    thread.join(THREAD_DEADLINE_SECONDS)
    assert not thread.is_alive(), f"{thread.name} did not finish"


def _recycle_report(*, reclaimed_bytes: int = 0) -> RecycleReport:
    return RecycleReport(
        horizon_lsn=0,
        recycled=(),
        deferred=(),
        retained=(),
        reclaimed_bytes=reclaimed_bytes,
        lag_segments=0,
        reader_present=False,
    )


@pytest.mark.parametrize(
    ("checkpoint_lsn", "expected_attempts"),
    [(93, 0), (92, 1)],
    ids=("one-record-below", "exact-threshold"),
)
def test_auto_checkpoint_uses_a_greater_than_or_equal_threshold(
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_lsn: int,
    expected_attempts: int,
) -> None:
    database = connect(":memory:", checkpoint_interval_records=8)
    attempts: list[str] = []
    state = CommitState(
        last_committed_lsn=100,
        last_csn=100,
        checkpoint_lsn=checkpoint_lsn,
    )

    monkeypatch.setattr(
        TransactionManager,
        "_published_state_in_section",
        lambda manager: state,
    )
    monkeypatch.setattr(
        WalManager,
        "total_bytes",
        lambda manager: pytest.fail(
            "wal_max_bytes=None must not inspect the WAL size"
        ),
    )
    monkeypatch.setattr(
        Database,
        "checkpoint",
        lambda candidate: attempts.append(candidate.path) or object(),
    )
    try:
        database._maybe_checkpoint()
        assert len(attempts) == expected_attempts
        assert database._checkpoint_retry_pending is False
    finally:
        database.close()


@pytest.mark.parametrize(
    ("checkpoint_lsn", "wal_bytes", "expected_attempts"),
    [
        (93, 1023, 0),
        (93, 1024, 1),
        (92, 0, 1),
    ],
    ids=("below-both", "byte-threshold", "record-threshold"),
)
def test_auto_checkpoint_combines_record_and_wal_byte_thresholds(
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_lsn: int,
    wal_bytes: int,
    expected_attempts: int,
) -> None:
    database = connect(
        ":memory:", checkpoint_interval_records=8, wal_max_bytes=1024
    )
    attempts: list[str] = []
    state = CommitState(
        last_committed_lsn=100,
        last_csn=100,
        checkpoint_lsn=checkpoint_lsn,
    )

    monkeypatch.setattr(
        TransactionManager,
        "_published_state_in_section",
        lambda manager: state,
    )
    monkeypatch.setattr(WalManager, "total_bytes", lambda manager: wal_bytes)
    monkeypatch.setattr(
        Database,
        "checkpoint",
        lambda candidate: attempts.append(candidate.path) or _recycle_report(),
    )
    try:
        database._maybe_checkpoint()
        assert len(attempts) == expected_attempts
        assert database._checkpoint_retry_pending is False
    finally:
        database.close()


def test_wal_byte_threshold_is_rearmed_only_after_wal_falls_below_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = connect(
        ":memory:", checkpoint_interval_records=1_000_000, wal_max_bytes=1024
    )
    attempts: list[str] = []
    observed = iter((1024, 1024, 1023, 1024))
    state = CommitState(last_committed_lsn=100, last_csn=100, checkpoint_lsn=99)

    monkeypatch.setattr(
        TransactionManager,
        "_published_state_in_section",
        lambda manager: state,
    )
    monkeypatch.setattr(WalManager, "total_bytes", lambda manager: next(observed))
    monkeypatch.setattr(
        Database,
        "checkpoint",
        lambda candidate: attempts.append(candidate.path) or _recycle_report(),
    )
    try:
        for _ in range(4):
            database._maybe_checkpoint()
        assert attempts == [":memory:", ":memory:"]
        assert database._checkpoint_retry_pending is False
    finally:
        database.close()


def test_a_failed_wal_byte_checkpoint_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = connect(
        ":memory:", checkpoint_interval_records=1_000_000, wal_max_bytes=1024
    )
    attempts = 0
    state = CommitState(last_committed_lsn=100, last_csn=100, checkpoint_lsn=99)

    monkeypatch.setattr(
        TransactionManager,
        "_published_state_in_section",
        lambda manager: state,
    )
    monkeypatch.setattr(WalManager, "total_bytes", lambda manager: 1024)

    def fail_once(candidate: Database) -> RecycleReport:
        nonlocal attempts
        del candidate
        attempts += 1
        if attempts == 1:
            raise GrafxDeviceFull("The checkpoint has no working space.", free_bytes=0)
        return _recycle_report(reclaimed_bytes=1024)

    monkeypatch.setattr(Database, "checkpoint", fail_once)
    try:
        database._maybe_checkpoint()
        assert attempts == 1
        assert database._checkpoint_retry_pending is True

        database._maybe_checkpoint()
        assert attempts == 2
        assert database._checkpoint_retry_pending is False
    finally:
        database.close()


def test_only_a_write_commit_considers_an_automatic_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = connect(":memory:", checkpoint_interval_records=1)
    attempts: list[str] = []
    monkeypatch.setattr(
        Database,
        "_maybe_checkpoint",
        lambda candidate: attempts.append(candidate.path),
    )
    try:
        read_report = database.begin("read").commit()
        empty_write_report = database.begin("write").commit()
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")

        assert read_report.wrote is False
        assert empty_write_report.wrote is False
        assert attempts == [":memory:"]
    finally:
        database.close()


def test_schema_settlement_finishes_before_auto_checkpoint_is_considered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = connect(":memory:", checkpoint_interval_records=1)
    order: list[str] = []
    original_settle = Database._settle_schema

    def observed_settle(
        candidate: Database, context: object, *, committed: bool
    ) -> None:
        order.append("settlement-entered")
        original_settle(candidate, context, committed=committed)  # type: ignore[arg-type]
        order.append("settlement-finished")

    monkeypatch.setattr(Database, "_settle_schema", observed_settle)
    monkeypatch.setattr(
        Database,
        "_maybe_checkpoint",
        lambda candidate: order.append("checkpoint-considered"),
    )
    try:
        _schema(database)
        assert order == [
            "settlement-entered",
            "settlement-finished",
            "checkpoint-considered",
        ]
    finally:
        database.close()


def test_a_schema_settlement_failure_does_not_start_auto_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = connect(":memory:", checkpoint_interval_records=1)
    transaction = database.begin("write")
    transaction.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
    attempts: list[str] = []
    original_settle = Database._settle_schema

    def settle_then_fail(
        candidate: Database, context: object, *, committed: bool
    ) -> None:
        original_settle(candidate, context, committed=committed)  # type: ignore[arg-type]
        raise GrafxDeviceFull("The schema journal could not settle.", free_bytes=0)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(Database, "_settle_schema", settle_then_fail)
            scoped.setattr(
                Database,
                "_maybe_checkpoint",
                lambda candidate: attempts.append(candidate.path),
            )
            report = transaction.commit()

        assert report.durable is True
        assert report.wrote is True
        assert attempts == []
    finally:
        database.close()


def test_auto_checkpoint_reentry_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = connect(":memory:", checkpoint_interval_records=8)
    state = CommitState(last_committed_lsn=100, last_csn=100, checkpoint_lsn=92)
    attempts: list[str] = []
    failures: list[BaseException] = []

    monkeypatch.setattr(
        TransactionManager,
        "_published_state_in_section",
        lambda manager: state,
    )

    def reenter_once(candidate: Database) -> object:
        attempts.append(candidate.path)
        if len(attempts) == 1:
            candidate._maybe_checkpoint()
        return object()

    monkeypatch.setattr(Database, "checkpoint", reenter_once)

    def invoke() -> None:
        try:
            database._maybe_checkpoint()
        except BaseException as failure:  # noqa: BLE001 - thread evidence is asserted below
            failures.append(failure)

    worker = threading.Thread(
        target=invoke, name="auto-checkpoint-reentry", daemon=True
    )
    worker.start()
    _join(worker)
    try:
        assert failures == []
        assert attempts == [":memory:"]
        assert database._checkpointing is False
    finally:
        database.close()


def test_concurrent_due_checks_start_one_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = connect(":memory:", checkpoint_interval_records=8)
    state = [CommitState(last_committed_lsn=100, last_csn=100, checkpoint_lsn=92)]
    checkpoint_entered = threading.Event()
    release_checkpoint = threading.Event()
    section_attempted = {
        "auto-checkpoint-one": threading.Event(),
        "auto-checkpoint-two": threading.Event(),
    }
    attempts: list[str] = []
    failures: list[BaseException] = []
    original_participant_section = TransactionManager._participant_section

    @contextmanager
    def observed_participant_section(
        manager: TransactionManager,
    ) -> Iterator[None]:
        attempted = section_attempted.get(threading.current_thread().name)
        if attempted is not None:
            attempted.set()
        with original_participant_section(manager):
            yield

    def published_state(manager: TransactionManager) -> CommitState:
        del manager
        return state[0]

    def checkpoint_once(candidate: Database) -> object:
        attempts.append(candidate.path)
        checkpoint_entered.set()
        if not release_checkpoint.wait(THREAD_DEADLINE_SECONDS):
            raise AssertionError("the checkpoint test did not release its barrier")
        state[0] = CommitState(
            last_committed_lsn=100,
            last_csn=100,
            checkpoint_lsn=100,
        )
        return object()

    monkeypatch.setattr(
        TransactionManager, "_participant_section", observed_participant_section
    )
    monkeypatch.setattr(
        TransactionManager, "_published_state_in_section", published_state
    )
    monkeypatch.setattr(Database, "checkpoint", checkpoint_once)

    def invoke() -> None:
        try:
            database._maybe_checkpoint()
        except BaseException as failure:  # noqa: BLE001 - thread evidence is asserted below
            failures.append(failure)

    workers = [
        threading.Thread(target=invoke, name=name, daemon=True)
        for name in section_attempted
    ]
    for worker in workers:
        worker.start()
    try:
        for attempted in section_attempted.values():
            assert attempted.wait(THREAD_DEADLINE_SECONDS)
        assert checkpoint_entered.wait(THREAD_DEADLINE_SECONDS)
    finally:
        release_checkpoint.set()
    for worker in workers:
        _join(worker)
    try:
        assert failures == []
        assert attempts == [":memory:"]
        assert database._checkpointing is False
    finally:
        database.close()


def test_a_refused_auto_checkpoint_is_retried_without_invalidating_the_commit() -> None:
    events = _RecordingEvents()
    registry, storage = _fault_composition(events)
    database = connect(":memory:", registry=registry, checkpoint_interval_records=1)
    try:
        _schema(database)
        storage.arm()

        transaction = database.begin("write")
        transaction.execute("CREATE (:P {id: 1, name: 'first'})")
        committed = transaction.commit()
        state_after_refusal = database.transactions.published_state()

        assert committed.durable is True
        assert committed.wrote is True
        assert _identities(database) == [1]
        assert storage.refusals == 1
        assert (
            state_after_refusal.checkpoint_lsn < state_after_refusal.last_committed_lsn
        )
        assert events.events == [
            (
                "checkpoint.auto_failed",
                {
                    "code": "durability_barrier_failed",
                    "retryable": False,
                    "published_lsn": state_after_refusal.last_committed_lsn,
                    "checkpoint_lsn": state_after_refusal.checkpoint_lsn,
                    "interval_records": 1,
                },
            )
        ]

        _insert(database, 2)
        retried = database.transactions.published_state()
        assert retried.checkpoint_lsn == retried.last_committed_lsn
        assert _identities(database) == [1, 2]
        assert storage.refusals == 1
    finally:
        _close_caller_composition(database, registry)


def test_a_late_checkpoint_failure_stays_pending_after_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = connect(":memory:", checkpoint_interval_records=1_000_000)
    original_open = IndexManager.open
    open_attempts = 0
    try:
        _schema(database)
        database._checkpoint_interval_records = 1

        def fail_first_postlude(
            manager: IndexManager, *args: object, **kwargs: object
        ) -> object:
            nonlocal open_attempts
            open_attempts += 1
            if open_attempts == 1:
                raise GrafxDeviceFull(
                    "The index checkpoint postlude could not finish.", free_bytes=0
                )
            return original_open(manager, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(IndexManager, "open", fail_first_postlude)

        database._maybe_checkpoint()
        published_after_failure = database.transactions.published_state()
        assert published_after_failure.checkpoint_lsn == (
            published_after_failure.last_committed_lsn
        )
        assert database._checkpoint_retry_pending is True
        assert open_attempts == 1

        database._maybe_checkpoint()
        published_after_retry = database.transactions.published_state()
        assert published_after_retry == published_after_failure
        assert database._checkpoint_retry_pending is False
        assert open_attempts == 2
    finally:
        database.close()


def test_a_broken_event_sink_cannot_escape_a_failed_auto_checkpoint() -> None:
    events = _BrokenEvents()
    registry, storage = _fault_composition(events)
    database = connect(":memory:", registry=registry, checkpoint_interval_records=1)
    try:
        _schema(database)
        storage.arm()

        transaction = database.begin("write")
        transaction.execute("CREATE (:P {id: 1, name: 'kept'})")
        report = transaction.commit()

        assert report.durable is True
        assert report.wrote is True
        assert _identities(database) == [1]
        assert storage.refusals == 1
        assert events.calls == 1
    finally:
        _close_caller_composition(database, registry)


def test_a_failure_event_may_request_reentrant_close_without_deadlock() -> None:
    events = _ClosingEvents()
    registry, storage = _fault_composition(events)
    database = connect(":memory:", registry=registry, checkpoint_interval_records=1)
    events.database = database
    try:
        _schema(database)
        storage.arm()

        transaction = database.begin("write")
        transaction.execute("CREATE (:P {id: 1, name: 'kept'})")
        report = transaction.commit()

        assert report.durable is True
        assert report.wrote is True
        assert transaction.report == report
        assert transaction.active is False
        assert events.calls == 1
        assert database.closed is True
        assert database.close_complete is True
        assert database._checkpointing is False
        assert database._checkpoint_retry_pending is True
    finally:
        _close_caller_composition(database, registry)


@pytest.mark.parametrize(
    "signal",
    [KeyboardInterrupt("checkpoint interrupted"), SystemExit("checkpoint stopped")],
    ids=("keyboard-interrupt", "system-exit"),
)
def test_process_control_signals_keep_their_identity_after_a_durable_commit(
    monkeypatch: pytest.MonkeyPatch,
    signal: BaseException,
) -> None:
    database = connect(":memory:", checkpoint_interval_records=1_000_000)
    try:
        _schema(database)
        database._checkpoint_interval_records = 1

        def interrupt(candidate: Database) -> object:
            del candidate
            raise signal

        monkeypatch.setattr(Database, "checkpoint", interrupt)
        transaction = database.begin("write")
        transaction.execute("CREATE (:P {id: 8, name: 'kept'})")

        with pytest.raises(BaseException) as caught:
            transaction.commit()

        assert caught.value is signal
        assert transaction.active is False
        assert transaction.report is not None
        assert transaction.report.durable is True
        assert transaction.report.wrote is True
        assert _identities(database) == [8]
        assert database._checkpoint_retry_pending is True
        assert database._checkpointing is False
    finally:
        database.close()


def test_hostile_failure_diagnostics_cannot_invalidate_a_durable_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _RecordingEvents()
    config = DatabaseConfig(path=":memory:", checkpoint_interval_records=1_000_000)
    registry = build_default_registry(config)
    registry.bind("events", events)
    database = connect(
        ":memory:", registry=registry, checkpoint_interval_records=1_000_000
    )
    try:
        _schema(database)
        before = database.transactions.published_state()
        database._checkpoint_interval_records = 1

        def refuse(candidate: Database) -> object:
            del candidate
            raise _HostileGrafxError("unsafe maintenance diagnostic")

        monkeypatch.setattr(Database, "checkpoint", refuse)
        transaction = database.begin("write")
        transaction.execute("CREATE (:P {id: 7, name: 'kept'})")
        report = transaction.commit()

        assert report.durable is True
        assert report.wrote is True
        assert transaction.active is False
        assert _identities(database) == [7]
        assert events.events == [
            (
                "checkpoint.auto_failed",
                {
                    "code": "foreign_error",
                    "retryable": False,
                    "published_lsn": report.csn,
                    "checkpoint_lsn": before.checkpoint_lsn,
                    "interval_records": 1,
                },
            )
        ]
    finally:
        _close_caller_composition(database, registry)


def test_auto_checkpoint_recycles_wal_and_the_database_reopens(tmp_path: Path) -> None:
    root = tmp_path / "database"
    options = {
        "page_size": 512,
        "wal_segment_bytes": WAL_SEGMENT_BYTES,
        "checkpoint_interval_records": 1,
    }
    database = connect(root, **options)
    try:
        _schema(database)
        for identity in range(1, 21):
            _insert(database, identity)

        state_before_close = database.transactions.published_state()
        segment_numbers = sorted(
            int(segment.stem) for segment in (root / "wal").glob("*.wal")
        )
        assert (
            state_before_close.checkpoint_lsn == state_before_close.last_committed_lsn
        )
        assert segment_numbers
        assert max(segment_numbers) > len(segment_numbers), (
            "the workload rolled the WAL, but no old numbered segment was recycled"
        )
    finally:
        database.close()

    reopened = connect(root, **options)
    try:
        assert _identities(reopened) == list(range(1, 21))
        assert reopened.transactions.published_state() == state_before_close
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_wal_byte_threshold_recycles_wal_and_the_database_reopens(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    wal_max_bytes = 3 * WAL_SEGMENT_BYTES
    options = {
        "page_size": 512,
        "wal_segment_bytes": WAL_SEGMENT_BYTES,
        "wal_max_bytes": wal_max_bytes,
        "checkpoint_interval_records": 1_000_000,
    }
    database = connect(root, **options)
    inserted: list[int] = []
    state_before_close: CommitState | None = None
    try:
        _schema(database)
        for identity in range(1, 61):
            _insert(database, identity)
            inserted.append(identity)
            wal_bytes = sum(file.stat().st_size for file in (root / "wal").glob("*.wal"))
            assert wal_bytes <= wal_max_bytes
            state = database.transactions.published_state()
            segment_numbers = sorted(
                int(segment.stem) for segment in (root / "wal").glob("*.wal")
            )
            if (
                state.checkpoint_lsn == state.last_committed_lsn
                and segment_numbers
                and max(segment_numbers) > len(segment_numbers)
            ):
                state_before_close = state
                break
        assert state_before_close is not None, (
            "the byte threshold never checkpointed and recycled an old WAL segment"
        )
    finally:
        database.close()

    reopened = connect(root, **options)
    try:
        assert _identities(reopened) == inserted
        assert reopened.transactions.published_state() == state_before_close
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_a_pinned_reader_does_not_cause_a_wal_checkpoint_storm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "database"
    wal_max_bytes = 3 * WAL_SEGMENT_BYTES
    options = {
        "page_size": 512,
        "wal_segment_bytes": WAL_SEGMENT_BYTES,
        "wal_max_bytes": wal_max_bytes,
        "checkpoint_interval_records": 1_000_000,
    }
    database = connect(root, **options)
    inserted: list[int] = []
    reader = None
    original_checkpoint = Database.checkpoint
    automatic_attempts = 0

    def observed_checkpoint(candidate: Database) -> RecycleReport:
        nonlocal automatic_attempts
        automatic_attempts += 1
        return original_checkpoint(candidate)

    try:
        _schema(database)
        reader = database.begin("read")
        monkeypatch.setattr(Database, "checkpoint", observed_checkpoint)

        next_identity = 1
        for _ in range(60):
            _insert(database, next_identity)
            inserted.append(next_identity)
            next_identity += 1
            wal_bytes = sum(
                file.stat().st_size for file in (root / "wal").glob("*.wal")
            )
            if automatic_attempts and wal_bytes >= wal_max_bytes:
                break
        else:
            pytest.fail("the pinned-reader workload never latched above the byte threshold")

        attempts_while_latched = automatic_attempts
        for _ in range(4):
            _insert(database, next_identity)
            inserted.append(next_identity)
            next_identity += 1
        assert automatic_attempts == attempts_while_latched
        assert reader.execute("MATCH (p:P) RETURN p.id").rows == ()

        reader.rollback()
        reader = None
        original_checkpoint(database)
        assert sum(
            file.stat().st_size for file in (root / "wal").glob("*.wal")
        ) < wal_max_bytes

        attempts_before_rearm = automatic_attempts
        for _ in range(60):
            _insert(database, next_identity)
            inserted.append(next_identity)
            next_identity += 1
            if automatic_attempts > attempts_before_rearm:
                break
        assert automatic_attempts == attempts_before_rearm + 1
    finally:
        if reader is not None:
            reader.rollback()
        database.close()

    reopened = connect(root, **options)
    try:
        assert _identities(reopened) == inserted
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()
