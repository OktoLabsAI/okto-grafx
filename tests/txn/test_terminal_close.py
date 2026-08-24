"""Terminal close and lifecycle quiescence regressions for ``TransactionManager``."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.metrics_contained import ContainedMetricsSink
from okto_grafx.domain.errors import (
    GrafxLeaseTimeout,
    GrafxTransactionStateError,
)
from okto_grafx.domain.txn import TransactionContext, TransactionState, WalRecordType
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.coordination import LeaseGuard
from okto_grafx.engine.database import Database, DatabaseIdentity, Transaction
from okto_grafx.engine.txn_manager import ACTIVE_TRANSACTIONS, TransactionManager
from txn_support import RecordingMetricsSink, Stack, build_stack, make_page_image

HEAP: str = "heap.dat"
WAIT_SECONDS: float = 10.0


def _stack(
    *,
    metrics: RecordingMetricsSink | None = None,
    retain_lease: bool = False,
    commit_lock_timeout: float = 5.0,
) -> tuple[Stack, FaultInjectingStorageDevice]:
    """Return a real in-memory stack with a complete storage call trail."""
    storage = FaultInjectingStorageDevice(MemoryStorageDevice(page_size=512), seed=211)
    return (
        build_stack(
            storage=storage,
            metrics=metrics,
            retain_lease=retain_lease,
            commit_lock_timeout=commit_lock_timeout,
        ),
        storage,
    )


def _write(stack: Stack, page: int = 3) -> TransactionContext:
    """Open one write transaction with a proved physical page image."""
    txn = stack.manager.begin("write")
    payload = f"terminal-{page}".encode()
    txn.owner._stage_page_image(
        txn,
        HEAP,
        page,
        make_page_image(stack.codec, [payload], page_index=page),
    )
    txn.note_write(stack.manager.partition_of(1, payload))
    return txn


def _database(stack: Stack, closer: Callable[[], None]) -> Database:
    """Wrap a transaction stack in the real facade with one observable owned closer."""
    metrics = stack.manager._metrics
    if not isinstance(metrics, ContainedMetricsSink):
        metrics = ContainedMetricsSink(metrics)
        stack.manager._metrics = metrics
    return Database(
        storage=stack.storage,
        clock=stack.clock,
        codec=stack.codec,
        metrics=metrics,
        events=object(),  # type: ignore[arg-type] - unused by lifecycle probes
        vector_math=object(),  # type: ignore[arg-type] - unused by lifecycle probes
        coordinator=stack.coordinator,
        pool=stack.pool,
        catalog=stack.catalog,
        heap=stack.heap,
        wal=stack.wal,  # type: ignore[arg-type] - LogWal implements the exercised doors
        transactions=stack.manager,
        identity=DatabaseIdentity(
            database_uuid=b"terminal-close!!",
            page_size=stack.storage.page_size,
            partitions_per_table=stack.manager.partitions_per_table,
            created_at_wall=stack.clock.wall(),
            granularity_descriptor="hash-v1;partitions_per_table=8",
        ),
        path=":memory:",
        label="terminal-close",
        checkpoint_interval_records=2**63 - 1,
        closers=(closer,),
    )


def _worker(
    operation: Callable[[], object],
    outcomes: list[object],
    failures: list[BaseException],
) -> None:
    """Retain the exact outcome or BaseException from a lifecycle worker."""
    try:
        outcomes.append(operation())
    except BaseException as failure:  # noqa: BLE001 - exact KI/SystemExit identity matters
        failures.append(failure)


def _join(worker: threading.Thread) -> None:
    """Join under a deterministic deadline and report a lifecycle deadlock."""
    worker.join(timeout=WAIT_SECONDS)
    assert not worker.is_alive(), f"terminal-close worker {worker.name!r} deadlocked"


def _commit_records(stack: Stack) -> tuple[object, ...]:
    """Return durable commit records in the test WAL."""
    return tuple(
        record
        for record in stack.wal.records()
        if record.record_type == WalRecordType.COMMIT
    )


def test_successful_retry_replaces_the_context_without_gauge_churn() -> None:
    """Old and successor are one externally indivisible pin/count transition."""
    metrics = RecordingMetricsSink()
    stack, _storage = _stack(metrics=metrics)
    old = _write(stack)
    old.mark_conflicted()
    before = list(metrics.gauges)

    successor = stack.manager.retry(old)

    assert metrics.gauges == before
    assert old.state is TransactionState.ABORTED
    assert successor.active
    assert stack.manager.open_transactions == 1
    assert stack.coordinator.reader_horizon() == successor.snapshot.read_lsn
    stack.manager.rollback(successor)


def test_retry_open_failure_leaves_the_old_context_aborted_and_no_partial_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successor-opening bomb cannot resurrect old work or leak its new registration."""
    metrics = RecordingMetricsSink()
    stack, _storage = _stack(metrics=metrics)
    old = _write(stack)
    old.mark_conflicted()
    opening_bomb = SystemExit("successor read view bomb")
    original = BufferPool.begin_read_view

    def fail_successor_open(pool: BufferPool, read_lsn: int) -> None:
        if pool is stack.pool:
            raise opening_bomb
        original(pool, read_lsn)

    monkeypatch.setattr(BufferPool, "begin_read_view", fail_successor_open)

    with pytest.raises(SystemExit) as raised:
        stack.manager.retry(old)

    assert raised.value is opening_bomb
    assert old.state is TransactionState.ABORTED
    assert stack.manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None
    assert metrics.gauge_values(ACTIVE_TRANSACTIONS, "mode", "write")[-1] == 0.0


def test_retry_winner_and_close_are_one_serial_lifecycle_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close waits while retry withdraws old and registers its successor in one section."""
    stack, _storage = _stack()
    old = _write(stack)
    old.mark_conflicted()
    successor_registered = threading.Event()
    release_retry = threading.Event()
    registered_successors: list[TransactionContext] = []
    original = TransactionManager._begin_in_section

    def pause_registered_successor(self: TransactionManager, mode):  # noqa: ANN001, ANN202
        answer = original(self, mode)
        if self is stack.manager and old.state is TransactionState.ABORTED:
            registered_successors.append(answer[0])
            successor_registered.set()
            assert release_retry.wait(WAIT_SECONDS), "close never contended with retry"
        return answer

    monkeypatch.setattr(
        TransactionManager, "_begin_in_section", pause_registered_successor
    )
    retry_outcomes: list[object] = []
    retry_failures: list[BaseException] = []
    close_outcomes: list[object] = []
    close_failures: list[BaseException] = []
    retry_worker = threading.Thread(
        target=_worker,
        args=(lambda: stack.manager.retry(old), retry_outcomes, retry_failures),
        name="retry-winner",
    )
    retry_worker.start()
    assert successor_registered.wait(WAIT_SECONDS), (
        "retry never registered its successor"
    )
    close_worker = threading.Thread(
        target=_worker,
        args=(stack.manager.close, close_outcomes, close_failures),
        name="close-after-retry",
    )
    close_worker.start()
    assert stack.manager.closed
    assert close_worker.is_alive(), "close crossed retry's participant section"

    release_retry.set()
    _join(retry_worker)
    _join(close_worker)

    assert retry_outcomes == []
    assert len(retry_failures) == 1
    assert isinstance(retry_failures[0], GrafxTransactionStateError)
    assert len(registered_successors) == 1
    successor = registered_successors[0]
    assert successor.state is TransactionState.ABORTED
    assert close_failures == [] and close_outcomes == [None]
    assert stack.manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None


def test_begin_winner_is_registered_then_close_withdraws_the_external_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A begin that owns the section is visible externally before close can settle it."""
    stack, _storage = _stack()
    registered = threading.Event()
    release_begin = threading.Event()
    registered_contexts: list[TransactionContext] = []
    original = TransactionManager._begin_in_section

    def pause_registered_begin(self: TransactionManager, mode):  # noqa: ANN001, ANN202
        answer = original(self, mode)
        if self is stack.manager:
            registered_contexts.append(answer[0])
            registered.set()
            assert release_begin.wait(WAIT_SECONDS), "close never contended with begin"
        return answer

    monkeypatch.setattr(TransactionManager, "_begin_in_section", pause_registered_begin)
    begin_outcomes: list[object] = []
    begin_failures: list[BaseException] = []
    close_outcomes: list[object] = []
    close_failures: list[BaseException] = []
    begin_worker = threading.Thread(
        target=_worker,
        args=(lambda: stack.manager.begin("read"), begin_outcomes, begin_failures),
        name="registered-begin",
    )
    begin_worker.start()
    assert registered.wait(WAIT_SECONDS), (
        "begin never published its reader registration"
    )
    assert stack.coordinator.reader_horizon() == 0
    close_worker = threading.Thread(
        target=_worker,
        args=(stack.manager.close, close_outcomes, close_failures),
        name="close-after-begin",
    )
    close_worker.start()
    assert stack.manager.closed
    assert close_worker.is_alive(), "close crossed begin's participant section"

    release_begin.set()
    _join(begin_worker)
    _join(close_worker)

    assert begin_outcomes == []
    assert len(begin_failures) == 1
    assert isinstance(begin_failures[0], GrafxTransactionStateError)
    assert len(registered_contexts) == 1
    assert registered_contexts[0].state is TransactionState.ABORTED
    assert close_failures == [] and close_outcomes == [None]
    assert stack.manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None


def test_close_retries_participant_timeouts_until_the_commit_winner_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live section holder cannot make Database.close release resources underneath it."""
    stack, _storage = _stack(commit_lock_timeout=0.001)
    txn = _write(stack)
    commit_inside = threading.Event()
    release_commit = threading.Event()
    timeout_seen = threading.Event()
    original_validate = TransactionManager._validate_staged_inputs
    original_section = TransactionManager._participant_section

    def pause_commit(self: TransactionManager, candidate: TransactionContext) -> None:
        if self is stack.manager:
            commit_inside.set()
            assert release_commit.wait(WAIT_SECONDS), (
                "close never timed out behind commit"
            )
        original_validate(self, candidate)

    def observe_timeouts(self: TransactionManager):  # noqa: ANN202
        inner = original_section(self)

        @contextmanager
        def observed() -> Iterator[None]:
            try:
                with inner:
                    yield
            except GrafxLeaseTimeout:
                if (
                    self is stack.manager
                    and threading.current_thread().name == "quiescent-close"
                ):
                    timeout_seen.set()
                raise

        return observed()

    monkeypatch.setattr(TransactionManager, "_validate_staged_inputs", pause_commit)
    monkeypatch.setattr(TransactionManager, "_participant_section", observe_timeouts)
    commit_outcomes: list[object] = []
    commit_failures: list[BaseException] = []
    close_outcomes: list[object] = []
    close_failures: list[BaseException] = []
    commit_worker = threading.Thread(
        target=_worker,
        args=(lambda: stack.manager.commit(txn), commit_outcomes, commit_failures),
        name="commit-before-close",
    )
    commit_worker.start()
    assert commit_inside.wait(WAIT_SECONDS), "commit never won the participant section"
    close_worker = threading.Thread(
        target=_worker,
        args=(stack.manager.close, close_outcomes, close_failures),
        name="quiescent-close",
    )
    close_worker.start()
    assert timeout_seen.wait(WAIT_SECONDS), (
        "close did not expose the forced section timeout"
    )
    assert stack.manager.closed
    assert close_worker.is_alive(), (
        "close escaped while a commit still owned the section"
    )

    release_commit.set()
    _join(commit_worker)
    _join(close_worker)

    assert commit_failures == [] and len(commit_outcomes) == 1
    assert txn.state is TransactionState.COMMITTED
    assert close_failures == [] and close_outcomes == [None]
    assert len(_commit_records(stack)) == 1
    assert stack.wal.barriers == 1


def test_close_winner_prevents_a_contending_commit_before_wal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once close publishes its latch, a late commit cannot touch lease, WAL or pool."""
    stack, storage = _stack()
    txn = _write(stack)
    close_inside = threading.Event()
    release_close = threading.Event()
    original = TransactionManager._abort_for_close_in_section

    def pause_close(self: TransactionManager, candidate: TransactionContext):  # noqa: ANN202
        if self is stack.manager:
            close_inside.set()
            assert release_close.wait(WAIT_SECONDS), "commit never contended with close"
        return original(self, candidate)

    monkeypatch.setattr(TransactionManager, "_abort_for_close_in_section", pause_close)
    close_outcomes: list[object] = []
    close_failures: list[BaseException] = []
    close_worker = threading.Thread(
        target=_worker,
        args=(stack.manager.close, close_outcomes, close_failures),
        name="close-before-commit",
    )
    close_worker.start()
    assert close_inside.wait(WAIT_SECONDS), "close never started terminal settlement"
    storage.clear_trail()

    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager.commit(txn)

    assert raised.value.details["closed"] is True
    assert not [
        record
        for record in storage.trail()
        if record.method in {"append_log", "durable_barrier"}
    ]
    release_close.set()
    _join(close_worker)
    assert close_failures == [] and close_outcomes == [None]
    assert txn.state is TransactionState.ABORTED
    assert _commit_records(stack) == ()
    assert stack.wal.barriers == 0


class _PreEnterFailure(AbstractContextManager[None]):
    """Raise the exact audit bomb before a participant section can be entered."""

    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    def __enter__(self) -> None:
        raise self._failure

    def __exit__(self, kind: object, value: object, trace: object) -> None:
        return None


@pytest.mark.parametrize(
    "failure_type",
    (GrafxLeaseTimeout, RuntimeError, KeyboardInterrupt, SystemExit),
)
def test_public_rollback_preenter_failure_keeps_wrapper_and_pin_retryable(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    """A rollback that never entered changes no wrapper, context, count or reader horizon."""
    stack, _storage = _stack()
    database = _database(stack, lambda: None)
    context = stack.manager.begin("read")
    transaction = Transaction(database, context)
    bomb = failure_type("rollback participant pre-enter bomb")
    original_section = TransactionManager._participant_section
    armed = True

    def fail_once(self: TransactionManager):  # noqa: ANN202
        nonlocal armed
        if self is stack.manager and armed:
            armed = False
            return _PreEnterFailure(bomb)
        return original_section(self)

    monkeypatch.setattr(TransactionManager, "_participant_section", fail_once)

    with pytest.raises(failure_type) as raised:
        transaction.rollback()

    assert raised.value is bomb
    assert transaction.active
    assert context.state is TransactionState.ACTIVE
    assert stack.manager.open_transactions == 1
    assert stack.coordinator.reader_horizon() == context.snapshot.read_lsn

    transaction.rollback()

    assert not transaction.active
    assert context.state is TransactionState.ABORTED
    assert stack.manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None
    database.close()


@pytest.mark.parametrize("cleanup_type", (RuntimeError, KeyboardInterrupt, SystemExit))
def test_transaction_exit_preserves_primary_when_rollback_never_enters(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_type: type[BaseException],
) -> None:
    """Unwind retains the block exception identity and records every rollback BaseException."""
    stack, _storage = _stack()
    database = _database(stack, lambda: None)
    context = stack.manager.begin("read")
    transaction = Transaction(database, context)
    primary = ValueError("primary block failure")
    cleanup = cleanup_type("rollback cleanup failure")
    original_section = TransactionManager._participant_section
    armed = True

    def fail_once(self: TransactionManager):  # noqa: ANN202
        nonlocal armed
        if self is stack.manager and armed:
            armed = False
            return _PreEnterFailure(cleanup)
        return original_section(self)

    monkeypatch.setattr(TransactionManager, "_participant_section", fail_once)

    with pytest.raises(ValueError) as raised:
        with transaction:
            raise primary

    assert raised.value is primary
    assert transaction.active
    assert any(
        type(cleanup).__name__ in note and str(cleanup) in note
        for note in getattr(primary, "__notes__", ())
    )

    transaction.rollback()
    assert stack.manager.open_transactions == 0
    database.close()


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt, SystemExit))
def test_database_close_preenter_failure_leaks_safely_then_retry_releases(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    """No pool/device closer runs until a later close proves the commit winner quiescent."""
    stack, storage = _stack()
    released: list[str] = []
    database = _database(stack, lambda: released.append("closed"))
    context = _write(stack)
    transaction = Transaction(database, context)
    commit_inside = threading.Event()
    release_commit = threading.Event()
    failed_once = threading.Event()
    bomb = failure_type("participant pre-enter bomb")
    original_validate = TransactionManager._validate_staged_inputs
    original_section = TransactionManager._participant_section

    def pause_commit(self: TransactionManager, candidate: TransactionContext) -> None:
        if self is stack.manager:
            commit_inside.set()
            assert release_commit.wait(WAIT_SECONDS), "close never attempted pre-enter"
        original_validate(self, candidate)

    def fail_close_preenter(self: TransactionManager):  # noqa: ANN202
        if (
            self is stack.manager
            and threading.current_thread().name == "pre-enter-close"
            and not failed_once.is_set()
        ):
            failed_once.set()
            return _PreEnterFailure(bomb)
        return original_section(self)

    monkeypatch.setattr(TransactionManager, "_validate_staged_inputs", pause_commit)
    monkeypatch.setattr(TransactionManager, "_participant_section", fail_close_preenter)
    commit_outcomes: list[object] = []
    commit_failures: list[BaseException] = []
    close_outcomes: list[object] = []
    close_failures: list[BaseException] = []
    commit_worker = threading.Thread(
        target=_worker,
        args=(transaction.commit, commit_outcomes, commit_failures),
        name="pre-enter-commit-winner",
    )
    commit_worker.start()
    assert commit_inside.wait(WAIT_SECONDS), (
        "commit never entered its participant section"
    )
    storage.clear_trail()
    close_worker = threading.Thread(
        target=_worker,
        args=(database.close, close_outcomes, close_failures),
        name="pre-enter-close",
    )
    close_worker.start()
    _join(close_worker)

    assert close_outcomes == []
    assert close_failures == [bomb]
    assert database.closed and not database.close_complete
    assert stack.manager.closed and not stack.manager.close_complete
    assert released == []
    assert storage.trail() == ()
    assert commit_worker.is_alive(), "failed close interrupted the in-flight winner"

    release_commit.set()
    _join(commit_worker)
    assert commit_failures == [] and len(commit_outcomes) == 1
    assert context.state is TransactionState.COMMITTED
    assert len(_commit_records(stack)) == 1
    # Leaving the winner's outer public transition automatically retries the close request;
    # the earlier pre-enter failure remains diagnostic and cannot replace the durable report.
    assert stack.manager.close_quiesced and stack.manager.close_complete
    assert database.close_complete
    assert database._close_failure is bomb
    assert released == ["closed"]
    database.close()
    assert released == ["closed"]


def test_reentrant_close_inside_schema_settlement_defers_dependency_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-thread callback cannot close the pool/device under facade settlement."""
    stack, _storage = _stack()
    released: list[str] = []
    database = _database(stack, lambda: released.append("closed"))
    context = _write(stack)
    transaction = Transaction(database, context)
    observed_inside: list[tuple[bool, bool, bool]] = []
    original_settle = Database._settle_schema

    def close_during_settlement(
        self: Database,
        candidate: TransactionContext,
        *,
        committed: bool,
    ) -> None:
        if self is database:
            assert committed and candidate is context
            assert candidate.state is TransactionState.COMMITTED
            self.close()
            observed_inside.append(
                (self.closed, self.close_complete, stack.manager.close_complete)
            )
            assert released == []
        original_settle(self, candidate, committed=committed)

    monkeypatch.setattr(Database, "_settle_schema", close_during_settlement)

    report = transaction.commit()

    assert report.durable and report.wrote
    assert observed_inside == [(True, False, False)]
    assert context.state is TransactionState.COMMITTED
    assert database.close_complete and stack.manager.close_complete
    assert released == ["closed"]


def test_concurrent_close_quiesces_manager_but_waits_for_schema_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another thread may close the manager, never lower dependencies, during settlement."""
    stack, _storage = _stack()
    released: list[str] = []
    database = _database(stack, lambda: released.append("closed"))
    context = _write(stack)
    transaction = Transaction(database, context)
    settlement_inside = threading.Event()
    release_settlement = threading.Event()
    original_settle = Database._settle_schema

    def pause_settlement(
        self: Database,
        candidate: TransactionContext,
        *,
        committed: bool,
    ) -> None:
        if self is database:
            assert committed and candidate is context
            settlement_inside.set()
            assert release_settlement.wait(WAIT_SECONDS), (
                "concurrent close never returned from manager quiescence"
            )
        original_settle(self, candidate, committed=committed)

    monkeypatch.setattr(Database, "_settle_schema", pause_settlement)
    commit_outcomes: list[object] = []
    commit_failures: list[BaseException] = []
    close_outcomes: list[object] = []
    close_failures: list[BaseException] = []
    commit_worker = threading.Thread(
        target=_worker,
        args=(transaction.commit, commit_outcomes, commit_failures),
        name="settling-commit",
    )
    commit_worker.start()
    assert settlement_inside.wait(WAIT_SECONDS), (
        "commit never reached schema settlement"
    )
    close_worker = threading.Thread(
        target=_worker,
        args=(database.close, close_outcomes, close_failures),
        name="settlement-close",
    )
    close_worker.start()
    _join(close_worker)

    assert close_failures == [] and close_outcomes == [None]
    assert database.closed and not database.close_complete
    assert stack.manager.close_complete
    assert context.state is TransactionState.COMMITTED
    assert released == []

    release_settlement.set()
    _join(commit_worker)
    assert commit_failures == [] and len(commit_outcomes) == 1
    report = commit_outcomes[0]
    assert getattr(report, "durable", False) is True
    assert database.close_complete
    assert released == ["closed"]


def test_two_close_callers_elect_exactly_one_dependency_releaser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loser does not join a host hook and cannot run a non-idempotent closer twice."""
    stack, _storage = _stack()
    closer_calls = 0
    closer_lock = threading.Lock()
    closer_inside = threading.Event()
    release_closer = threading.Event()
    one_close_returned = threading.Event()

    def non_idempotent_closer() -> None:
        nonlocal closer_calls
        with closer_lock:
            closer_calls += 1
            assert closer_calls == 1, "the owned dependency was released twice"
        closer_inside.set()
        assert release_closer.wait(WAIT_SECONDS), "the losing close never returned"

    database = _database(stack, non_idempotent_closer)
    both_after_manager = threading.Barrier(2, timeout=WAIT_SECONDS)
    original_close_transactions = Database._close_transactions

    def pause_after_manager(self: Database) -> None:
        original_close_transactions(self)
        if self is database:
            both_after_manager.wait()

    monkeypatch.setattr(Database, "_close_transactions", pause_after_manager)
    outcomes: list[object] = []
    failures: list[BaseException] = []

    def close_and_signal() -> None:
        try:
            database.close()
        finally:
            one_close_returned.set()

    workers = [
        threading.Thread(
            target=_worker,
            args=(close_and_signal, outcomes, failures),
            name=f"concurrent-close-{index}",
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    assert closer_inside.wait(WAIT_SECONDS), (
        "neither close caller reached the owned closer"
    )
    assert one_close_returned.wait(WAIT_SECONDS), (
        "the losing close joined the host closer"
    )
    assert not database.close_complete
    assert closer_calls == 1
    release_closer.set()
    for worker in workers:
        _join(worker)

    assert failures == []
    assert outcomes == [None, None]
    assert database.close_complete and stack.manager.close_complete
    assert closer_calls == 1
    database.close()
    assert closer_calls == 1


class _CloseFromClock:
    """Request facade close once from the manager's post-registration monotonic reading."""

    def __init__(self, stack: Stack) -> None:
        self._inner = stack.clock
        self.database: Database | None = None
        self.armed = False
        self.fired = False

    def monotonic(self) -> float:
        if self.armed and not self.fired:
            self.fired = True
            assert self.database is not None
            self.database.close()
        return self._inner.monotonic()

    def wall(self) -> float:
        return self._inner.wall()


def test_clock_reentrant_close_never_publishes_an_active_begin() -> None:
    """The begin registration is withdrawn before auto-resumed close releases dependencies."""
    stack, storage = _stack()
    released: list[str] = []
    database = _database(stack, lambda: released.append("closed"))
    clock = _CloseFromClock(stack)
    clock.database = database
    stack.manager._clock = clock
    database._clock = clock
    clock.armed = True

    with pytest.raises(GrafxTransactionStateError) as raised:
        database.begin("read")

    assert raised.value.details["closed"] is True
    assert clock.fired
    assert database.closed and database.close_complete
    assert stack.manager.close_complete
    assert stack.manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None
    assert released == ["closed"]
    storage.clear_trail()
    with pytest.raises(GrafxTransactionStateError):
        stack.manager.begin("read")
    assert storage.trail() == ()


class _CloseOnIncrement(RecordingMetricsSink):
    """Close the facade when a deferred transition metric reaches its host FIFO."""

    def __init__(self, database: Database) -> None:
        super().__init__()
        self._database = database
        self.fired = False

    def increment(
        self,
        name: str,
        value: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        super().increment(name, value, labels)
        if name == "deferred_terminal_probe" and not self.fired:
            self.fired = True
            self._database.close()


@pytest.mark.parametrize("operation", ("begin", "retry"))
def test_deferred_close_at_begin_or_retry_exit_returns_no_active_context_and_no_trail(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """FIFO drain may close dependencies; terminal revalidation never reacquires afterwards."""
    stack, storage = _stack()
    released: list[str] = []
    database = _database(stack, lambda: released.append("closed"))
    old: TransactionContext | None = None
    wrapper: Transaction | None = None
    if operation == "retry":
        old = _write(stack)
        old.mark_conflicted()
        wrapper = Transaction(database, old)
    host = _CloseOnIncrement(database)
    contained = ContainedMetricsSink(host)
    stack.manager._metrics = contained
    database._metrics = contained
    created: list[TransactionContext] = []
    original = TransactionManager._begin_in_section

    def record_then_defer_close(self: TransactionManager, mode):  # noqa: ANN001, ANN202
        answer = original(self, mode)
        if self is stack.manager and (
            old is None or old.state is TransactionState.ABORTED
        ):
            created.append(answer[0])
            self._metrics.increment("deferred_terminal_probe")
        return answer

    monkeypatch.setattr(
        TransactionManager, "_begin_in_section", record_then_defer_close
    )

    with pytest.raises(GrafxTransactionStateError) as raised:
        if wrapper is None:
            database.begin("read")
        else:
            database.retry(wrapper)

    assert raised.value.details["closed"] is True
    assert host.fired
    assert len(created) == 1 and created[0].state is TransactionState.ABORTED
    assert stack.manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None
    assert stack.manager.close_complete
    assert database.close_complete
    assert released == ["closed"]
    storage.clear_trail()
    with pytest.raises(GrafxTransactionStateError):
        stack.manager.begin("read")
    assert storage.trail() == ()


def test_close_is_fail_complete_across_two_txns_three_baseexceptions_and_a_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RuntimeError, KI and SystemExit are evidence only after every owned object is retired."""
    stack, _storage = _stack(retain_lease=True)
    first = stack.manager.begin("read")
    second = stack.manager.begin("write")
    with stack.manager._participant_section():
        guard = stack.manager._hold_lease()
    refresh_bomb = RuntimeError("reader refresh bomb")
    index_bomb = KeyboardInterrupt("index cleanup bomb")
    lease_bomb = SystemExit("lease release bomb")
    original_refresh = TransactionManager._refresh_due_readers
    original_drop = TransactionManager._drop_index_changes
    original_release = LeaseGuard.release
    refresh_calls = 0

    def fail_first_refresh(
        self: TransactionManager,
        now_monotonic: float | None = None,
        *,
        skip: int | None = None,
    ) -> int:
        nonlocal refresh_calls
        if self is stack.manager and refresh_calls == 0:
            refresh_calls += 1
            raise refresh_bomb
        refresh_calls += 1
        return original_refresh(self, now_monotonic, skip=skip)

    def fail_second_index(
        self: TransactionManager, candidate: TransactionContext
    ) -> None:
        if self is stack.manager and candidate is second:
            raise index_bomb
        original_drop(self, candidate)

    def release_then_fail(self: LeaseGuard) -> None:
        original_release(self)
        if self is guard:
            raise lease_bomb

    monkeypatch.setattr(TransactionManager, "_refresh_due_readers", fail_first_refresh)
    monkeypatch.setattr(TransactionManager, "_drop_index_changes", fail_second_index)
    monkeypatch.setattr(LeaseGuard, "release", release_then_fail)

    with pytest.raises(RuntimeError) as raised:
        stack.manager.close()

    assert raised.value is refresh_bomb
    assert any(
        "KeyboardInterrupt" in note for note in getattr(refresh_bomb, "__notes__", ())
    )
    assert any("SystemExit" in note for note in getattr(refresh_bomb, "__notes__", ()))
    assert first.state is TransactionState.ABORTED
    assert second.state is TransactionState.ABORTED
    assert stack.manager.closed
    assert stack.manager.open_transactions == 0
    assert stack.manager._pins == {}
    assert set(stack.manager._mode_counts.values()) == {0}
    assert stack.manager._lease_guard is None
    assert guard.released
    assert stack.coordinator.reader_horizon() is None
    stack.manager.close()


class _HostileCloseMetrics(RecordingMetricsSink):
    """Re-enter the closed manager and throw a distinct BaseException per final gauge."""

    def __init__(
        self,
        manager: TransactionManager,
        section_depth: list[int],
        failures: tuple[BaseException, BaseException],
    ) -> None:
        super().__init__()
        self._manager = manager
        self._section_depth = section_depth
        self._failures = failures
        self.depths: list[int] = []
        self.reentries: list[BaseException] = []

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.depths.append(self._section_depth[0])
        try:
            self._manager.begin("read")
        except BaseException as failure:  # noqa: BLE001 - typed re-entry is asserted below
            self.reentries.append(failure)
        failure = self._failures[len(self.depths) - 1]
        raise failure


def test_close_metrics_are_reentrant_fail_complete_and_outside_the_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both final gauges run after quiescence, and neither hostile callback replaces the first."""
    stack, _storage = _stack()
    depth = [0]
    first_bomb = RuntimeError("first metric bomb")
    second_bomb = SystemExit("second metric bomb")
    metrics = _HostileCloseMetrics(stack.manager, depth, (first_bomb, second_bomb))
    stack.manager._metrics = metrics
    original = TransactionManager._participant_section

    def track_section(self: TransactionManager):  # noqa: ANN202
        inner = original(self)

        @contextmanager
        def tracked() -> Iterator[None]:
            with inner:
                if self is stack.manager:
                    depth[0] += 1
                try:
                    yield
                finally:
                    if self is stack.manager:
                        depth[0] -= 1

        return tracked()

    monkeypatch.setattr(TransactionManager, "_participant_section", track_section)

    stack.manager.close()

    assert metrics.depths == [0, 0]
    assert len(metrics.reentries) == 2
    assert all(
        isinstance(item, GrafxTransactionStateError) for item in metrics.reentries
    )
    assert stack.manager.close_complete
    stack.manager.close()
    assert metrics.depths == [0, 0]


class _RawGaugeBomb(RecordingMetricsSink):
    """Raise a process-control class from one raw MetricsSink gauge door."""

    def __init__(self, door: str, failure: BaseException) -> None:
        super().__init__()
        self._door = door
        self._failure = failure

    @property
    def enabled(self) -> bool:
        if self._door == "enabled":
            raise self._failure
        return True

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        raise self._failure


@pytest.mark.parametrize("door", ("enabled", "set_gauge"))
@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt, SystemExit))
def test_raw_gauge_baseexceptions_never_hide_begin_rollback_or_durable_commit(
    door: str,
    failure_type: type[BaseException],
) -> None:
    """Containment is a manager invariant even without the composition-root wrapper."""
    stack, _storage = _stack()
    bomb = failure_type(f"raw {door} bomb")
    stack.manager._metrics = _RawGaugeBomb(door, bomb)

    reader = stack.manager.begin("read")
    assert reader.active
    stack.manager.rollback(reader)
    assert reader.state is TransactionState.ABORTED

    writer = _write(stack, page=7)
    report = stack.manager.commit(writer)
    assert report.durable and report.wrote
    assert writer.state is TransactionState.COMMITTED
    assert len(_commit_records(stack)) == 1
    assert stack.manager.open_transactions == 0


def test_every_terminal_door_refuses_before_storage_or_coordination() -> None:
    """After the latch, no public work path reaches device, registry, clock, WAL or pool."""
    stack, storage = _stack()
    read = stack.manager.begin("read")
    conflicted = stack.manager.begin("write")
    conflicted.mark_conflicted()
    stack.manager.close()
    storage.clear_trail()

    operations: tuple[tuple[str, Callable[[], object]], ...] = (
        ("begin", lambda: stack.manager.begin("read")),
        ("commit", lambda: stack.manager.commit(read)),
        ("retry", lambda: stack.manager.retry(conflicted)),
        ("checkpoint", stack.manager.checkpoint),
        ("refresh", stack.manager.refresh_due_readers),
        ("published_state", stack.manager.published_state),
        ("published_lsn", stack.manager.published_lsn),
        ("recyclable_horizon", stack.manager.recyclable_horizon),
        ("assert_recovery_complete", stack.manager.assert_recovery_complete),
        ("require_recovery", stack.manager.require_recovery),
        ("recovery_completed", stack.manager.recovery_completed),
        ("recovery_section", lambda: stack.manager.recovery_section().__enter__()),
        (
            "page_access_section",
            lambda: stack.manager.page_access_section().__enter__(),
        ),
    )
    for name, operation in operations:
        try:
            operation()
        except GrafxTransactionStateError as failure:
            assert failure.details["closed"] is True, name
        else:
            pytest.fail(f"terminal operation {name!r} did not refuse")
        assert storage.trail() == ()

    stack.manager.rollback(read)
    assert storage.trail() == ()
    stack.manager.close()
    assert storage.trail() == ()
