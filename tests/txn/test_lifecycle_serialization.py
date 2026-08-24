"""Deterministic races between lifecycle doors of one transaction context."""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import GrafxTransactionStateError
from okto_grafx.domain.txn import TransactionContext, TransactionState, WalRecordType
from okto_grafx.engine.txn_manager import TransactionManager
from txn_support import WAL_FILE, Stack, build_stack, make_page_image

HEAP: str = "heap.dat"
WAIT_SECONDS: float = 10.0


def _stack() -> tuple[Stack, FaultInjectingStorageDevice]:
    """Return a fast real stack whose storage records every WAL mutation."""
    storage = FaultInjectingStorageDevice(MemoryStorageDevice(page_size=512), seed=97)
    return build_stack(storage=storage), storage


def _transaction(stack: Stack, mode: str) -> TransactionContext:
    """Open a transaction, staging one physical write when ``mode`` is write."""
    transaction = stack.manager.begin(mode)
    if mode == "write":
        transaction.owner._stage_page_image(
            transaction,
            HEAP,
            3,
            make_page_image(stack.codec, [b"serialized"], page_index=3),
        )
        transaction.note_write(stack.manager.partition_of(1, b"serialized"))
    return transaction


def _worker(
    operation: Callable[[], object], outcomes: list[object], failures: list[BaseException]
) -> None:
    """Run one lifecycle door and retain either its answer or the exact escaping failure."""
    try:
        outcomes.append(operation())
    except BaseException as failure:  # noqa: BLE001 - the assertion needs the exact loser
        failures.append(failure)


def _join(thread: threading.Thread) -> None:
    """Join a worker under a deterministic bound and fail rather than hang on deadlock."""
    thread.join(timeout=WAIT_SECONDS)
    assert not thread.is_alive(), f"lifecycle worker {thread.name!r} deadlocked"


def _wal_mutations(device: FaultInjectingStorageDevice) -> tuple[tuple[str, str | None], ...]:
    """Return only appends and barriers that reached the transaction WAL file."""
    return tuple(
        (record.method, record.file)
        for record in device.trail()
        if record.file == WAL_FILE
        and record.method in {"append_log", "durable_barrier"}
    )


@pytest.mark.parametrize(
    ("mode", "helper_name"),
    (("read", "_commit_without_writing"), ("write", "_commit_with_writing")),
)
def test_rollback_winning_after_commit_selection_prevents_all_commit_effects(
    monkeypatch: pytest.MonkeyPatch, mode: str, helper_name: str
) -> None:
    stack, storage = _stack()
    transaction = _transaction(stack, mode)
    manager = stack.manager
    selected = threading.Event()
    resume = threading.Event()
    original = getattr(TransactionManager, helper_name)

    def pause_after_selection(self: TransactionManager, candidate: TransactionContext) -> object:
        if self is manager:
            selected.set()
            assert resume.wait(WAIT_SECONDS), "rollback never released the selected commit"
        return original(self, candidate)

    monkeypatch.setattr(TransactionManager, helper_name, pause_after_selection)
    outcomes: list[object] = []
    failures: list[BaseException] = []
    storage.clear_trail()
    worker = threading.Thread(
        target=_worker,
        args=(lambda: manager.commit(transaction), outcomes, failures),
        name=f"selected-{mode}-commit",
    )
    worker.start()
    assert selected.wait(WAIT_SECONDS), "commit never reached its selected lifecycle path"

    manager.rollback(transaction)
    resume.set()
    _join(worker)

    assert outcomes == []
    assert len(failures) == 1 and isinstance(failures[0], GrafxTransactionStateError)
    assert transaction.state is TransactionState.ABORTED
    assert manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None
    assert stack.wal.barriers == 0
    assert not [record for record in stack.wal.records() if record.record_type == WalRecordType.COMMIT]
    assert _wal_mutations(storage) == ()


@pytest.mark.parametrize(
    ("mode", "helper_name"),
    (("read", "_commit_without_writing"), ("write", "_commit_with_writing")),
)
def test_two_selected_commits_produce_exactly_one_outcome(
    monkeypatch: pytest.MonkeyPatch, mode: str, helper_name: str
) -> None:
    stack, _storage = _stack()
    transaction = _transaction(stack, mode)
    manager = stack.manager
    first_selected = threading.Event()
    resume_first = threading.Event()
    original = getattr(TransactionManager, helper_name)

    def pause_first(self: TransactionManager, candidate: TransactionContext) -> object:
        if self is manager and threading.current_thread().name == "first-selected-commit":
            first_selected.set()
            assert resume_first.wait(WAIT_SECONDS), "second commit never released the first"
        return original(self, candidate)

    monkeypatch.setattr(TransactionManager, helper_name, pause_first)
    first_outcomes: list[object] = []
    first_failures: list[BaseException] = []
    first = threading.Thread(
        target=_worker,
        args=(lambda: manager.commit(transaction), first_outcomes, first_failures),
        name="first-selected-commit",
    )
    first.start()
    assert first_selected.wait(WAIT_SECONDS), "first commit never reached the selected path"

    winning_report = manager.commit(transaction)
    resume_first.set()
    _join(first)

    assert winning_report.durable is True
    assert first_outcomes == []
    assert len(first_failures) == 1
    assert isinstance(first_failures[0], GrafxTransactionStateError)
    assert transaction.state is TransactionState.COMMITTED
    assert manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None
    commits = [
        record for record in stack.wal.records() if record.record_type == WalRecordType.COMMIT
    ]
    assert len(commits) == (1 if mode == "write" else 0)
    assert stack.wal.barriers == (1 if mode == "write" else 0)


def test_retry_settlement_serializes_against_a_commit_of_the_same_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack, storage = _stack()
    transaction = _transaction(stack, "write")
    transaction.mark_conflicted()
    manager = stack.manager
    retry_inside_section = threading.Event()
    release_retry = threading.Event()
    commit_selected = threading.Event()
    original_rollback = TransactionManager._rollback_active_in_section
    original_commit = TransactionManager._commit_with_writing

    def pause_retry(self: TransactionManager, candidate: TransactionContext):  # noqa: ANN202
        if self is manager and not retry_inside_section.is_set():
            retry_inside_section.set()
            assert release_retry.wait(WAIT_SECONDS), "commit never contended with retry"
        return original_rollback(self, candidate)

    def observe_commit(self: TransactionManager, candidate: TransactionContext):  # noqa: ANN202
        if self is manager:
            commit_selected.set()
        return original_commit(self, candidate)

    monkeypatch.setattr(TransactionManager, "_rollback_active_in_section", pause_retry)
    monkeypatch.setattr(TransactionManager, "_commit_with_writing", observe_commit)
    retry_outcomes: list[object] = []
    retry_failures: list[BaseException] = []
    commit_outcomes: list[object] = []
    commit_failures: list[BaseException] = []
    storage.clear_trail()
    retry_worker = threading.Thread(
        target=_worker,
        args=(lambda: manager.retry(transaction), retry_outcomes, retry_failures),
        name="serialized-retry",
    )
    retry_worker.start()
    assert retry_inside_section.wait(WAIT_SECONDS), "retry never acquired the participant section"
    commit_worker = threading.Thread(
        target=_worker,
        args=(lambda: manager.commit(transaction), commit_outcomes, commit_failures),
        name="commit-contending-with-retry",
    )
    commit_worker.start()
    assert commit_selected.wait(WAIT_SECONDS), "commit never selected its write path"
    assert commit_worker.is_alive(), "commit did not wait for retry's lifecycle settlement"

    release_retry.set()
    _join(retry_worker)
    _join(commit_worker)

    assert retry_failures == [] and len(retry_outcomes) == 1
    successor = retry_outcomes[0]
    assert isinstance(successor, TransactionContext) and successor.active
    assert commit_outcomes == []
    assert len(commit_failures) == 1
    assert isinstance(commit_failures[0], GrafxTransactionStateError)
    assert transaction.state is TransactionState.ABORTED
    assert manager.open_transactions == 1
    assert stack.coordinator.reader_horizon() == successor.snapshot.read_lsn
    assert stack.wal.barriers == 0
    assert not [record for record in stack.wal.records() if record.record_type == WalRecordType.COMMIT]
    assert _wal_mutations(storage) == ()
    manager.rollback(successor)
    assert manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None
