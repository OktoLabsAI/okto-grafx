"""Terminal close and lifecycle quiescence regressions for ``TransactionManager``."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import (
    GrafxLeaseTimeout,
    GrafxTransactionStateError,
)
from okto_grafx.domain.txn import TransactionContext, TransactionState, WalRecordType
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.coordination import LeaseGuard
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


def _worker(
    operation: Callable[[], object], outcomes: list[object], failures: list[BaseException]
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
    original = TransactionManager._begin_in_section

    def pause_registered_successor(self: TransactionManager, mode):  # noqa: ANN001, ANN202
        answer = original(self, mode)
        if self is stack.manager and old.state is TransactionState.ABORTED:
            successor_registered.set()
            assert release_retry.wait(WAIT_SECONDS), "close never contended with retry"
        return answer

    monkeypatch.setattr(TransactionManager, "_begin_in_section", pause_registered_successor)
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
    assert successor_registered.wait(WAIT_SECONDS), "retry never registered its successor"
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

    assert retry_failures == [] and len(retry_outcomes) == 1
    successor = retry_outcomes[0]
    assert isinstance(successor, TransactionContext)
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
    original = TransactionManager._begin_in_section

    def pause_registered_begin(self: TransactionManager, mode):  # noqa: ANN001, ANN202
        answer = original(self, mode)
        if self is stack.manager:
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
    assert registered.wait(WAIT_SECONDS), "begin never published its reader registration"
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

    assert begin_failures == [] and len(begin_outcomes) == 1
    assert isinstance(begin_outcomes[0], TransactionContext)
    assert begin_outcomes[0].state is TransactionState.ABORTED
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
            assert release_commit.wait(WAIT_SECONDS), "close never timed out behind commit"
        original_validate(self, candidate)

    def observe_timeouts(self: TransactionManager):  # noqa: ANN202
        inner = original_section(self)

        @contextmanager
        def observed() -> Iterator[None]:
            try:
                with inner:
                    yield
            except GrafxLeaseTimeout:
                if self is stack.manager and threading.current_thread().name == "quiescent-close":
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
    assert timeout_seen.wait(WAIT_SECONDS), "close did not expose the forced section timeout"
    assert stack.manager.closed
    assert close_worker.is_alive(), "close escaped while a commit still owned the section"

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

    def fail_second_index(self: TransactionManager, candidate: TransactionContext) -> None:
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
    assert any("KeyboardInterrupt" in note for note in getattr(refresh_bomb, "__notes__", ()))
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

    with pytest.raises(RuntimeError) as raised:
        stack.manager.close()

    assert raised.value is first_bomb
    assert metrics.depths == [0, 0]
    assert len(metrics.reentries) == 2
    assert all(isinstance(item, GrafxTransactionStateError) for item in metrics.reentries)
    assert any("SystemExit" in note for note in getattr(first_bomb, "__notes__", ()))
    stack.manager.close()
    assert metrics.depths == [0, 0]


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
        ("page_access_section", lambda: stack.manager.page_access_section().__enter__()),
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
