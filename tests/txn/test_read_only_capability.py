"""The transaction manager's fail-fast read-only capability boundary."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.txn.context import TransactionContext, TransactionMode
from okto_grafx.engine.txn_manager import TransactionManager
from txn_support import Stack, build_stack


class _UnreachableWal:
    """Record and fail any WAL access; a read-only gate must leave this object untouched."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.calls.append(name)
        raise self.failure


class _UnreachableCoordinator:
    """Supply construction identity and fail every operational coordination door."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.calls: list[str] = []

    def owner_id(self) -> str:
        self.calls.append("owner_id")
        return "read-only-probe"

    def __getattr__(self, name: str) -> object:
        self.calls.append(name)
        raise self.failure


def _manager(
    stack: Stack,
    *,
    writable: object,
    wal: object | None = None,
    coordinator: object | None = None,
) -> TransactionManager:
    """Return another manager over ``stack`` with the requested capability collaborators."""
    return TransactionManager(
        stack.wal if wal is None else wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        stack.coordinator if coordinator is None else coordinator,  # type: ignore[arg-type]
        stack.clock,
        stack.metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
        writable=writable,  # type: ignore[arg-type]
    )


READ_ONLY_DOORS: tuple[tuple[str, Callable[[TransactionManager], object]], ...] = (
    ("begin", lambda manager: manager.begin("write")),
    ("checkpoint", lambda manager: manager.checkpoint()),
)


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize(("_door", "operation"), READ_ONLY_DOORS)
def test_persistent_work_is_refused_before_coordination_wal_or_storage(
    _door: str,
    operation: Callable[[TransactionManager], object],
    failure_type: type[BaseException],
) -> None:
    storage = FaultInjectingStorageDevice(MemoryStorageDevice(page_size=512), seed=71)
    stack = build_stack(storage=storage)
    wal = _UnreachableWal(failure_type("WAL must remain unreachable"))
    coordinator = _UnreachableCoordinator(failure_type("coordination must remain unreachable"))
    manager = _manager(stack, writable=False, wal=wal, coordinator=coordinator)
    storage.clear_trail()
    coordinator.calls.clear()

    with pytest.raises(GrafxUnsupportedOperation) as raised:
        operation(manager)

    assert raised.value.details["read_only"] is True
    assert manager.open_transactions == 0
    assert coordinator.calls == []
    assert wal.calls == []
    assert storage.trail() == ()


def _stored_bytes(storage: FaultInjectingStorageDevice) -> tuple[tuple[str, bytes], ...]:
    """Return every stored file and its exact bytes without crossing the recording wrapper."""
    inner = storage.inner
    return tuple(
        (file, inner.read_log(file, 0, inner.file_size(file)))
        for file in inner.list_files()
    )


def _forged(
    manager: TransactionManager,
    genuine: TransactionContext,
    mode: TransactionMode,
) -> TransactionContext:
    """Return an owner-spoofed context carrying a live transaction's numeric identity."""
    transaction = TransactionContext(
        txn_id=genuine.txn_id,
        mode=mode,
        snapshot=genuine.snapshot,
        epoch=genuine.epoch,
        owner=manager,
        page_staging_capability=object(),
    )
    if mode is TransactionMode.WRITE:
        transaction.note_write(17)
    return transaction


FORGED_DOORS: tuple[
    tuple[str, Callable[[TransactionManager, TransactionContext], object]], ...
] = (
    ("commit", lambda manager, transaction: manager.commit(transaction)),
    ("rollback", lambda manager, transaction: manager.rollback(transaction)),
    ("retry", lambda manager, transaction: manager.retry(transaction)),
)


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize("writable", (True, False))
@pytest.mark.parametrize("mode", (TransactionMode.READ, TransactionMode.WRITE))
@pytest.mark.parametrize(("_door", "operation"), FORGED_DOORS)
def test_a_forged_same_id_context_cannot_consume_the_real_transaction(
    _door: str,
    operation: Callable[[TransactionManager, TransactionContext], object],
    mode: TransactionMode,
    writable: bool,
    failure_type: type[BaseException],
) -> None:
    storage = FaultInjectingStorageDevice(MemoryStorageDevice(page_size=512), seed=79)
    stack = build_stack(storage=storage)
    manager = _manager(stack, writable=writable)
    genuine = manager.begin("read")
    forged = _forged(manager, genuine, mode)
    if _door == "retry":
        forged.mark_conflicted()
    horizon = stack.coordinator.reader_horizon()
    bytes_before = _stored_bytes(storage)
    storage.clear_trail()
    wal = _UnreachableWal(failure_type("forged context reached WAL"))
    coordinator = _UnreachableCoordinator(failure_type("forged context reached coordination"))
    real_wal, real_coordinator = manager._wal, manager._coordinator
    manager._wal, manager._coordinator = wal, coordinator
    try:
        with pytest.raises(GrafxTransactionStateError) as raised:
            operation(manager, forged)
    finally:
        manager._wal, manager._coordinator = real_wal, real_coordinator

    assert raised.value.details["reason"] == "transaction_identity_mismatch"
    assert genuine.active
    assert manager.open_transactions == 1
    assert wal.calls == []
    assert coordinator.calls == []
    assert storage.trail() == ()
    assert _stored_bytes(storage) == bytes_before
    assert stack.coordinator.reader_horizon() == horizon
    manager.rollback(genuine)


WRITE_OUTCOME_DOORS: tuple[
    tuple[str, Callable[[TransactionManager, TransactionContext], object]], ...
] = (
    ("commit", lambda manager, transaction: manager.commit(transaction)),
    ("retry", lambda manager, transaction: manager.retry(transaction)),
)


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize(("door", "operation"), WRITE_OUTCOME_DOORS)
def test_write_commit_and_retry_recheck_capability_before_any_side_effect(
    door: str,
    operation: Callable[[TransactionManager, TransactionContext], object],
    failure_type: type[BaseException],
) -> None:
    storage = FaultInjectingStorageDevice(MemoryStorageDevice(page_size=512), seed=83)
    stack = build_stack(storage=storage)
    manager = _manager(stack, writable=True)
    transaction = manager.begin("write")
    transaction.note_write(23)
    if door == "retry":
        transaction.mark_conflicted()
    horizon = stack.coordinator.reader_horizon()
    bytes_before = _stored_bytes(storage)
    storage.clear_trail()
    wal = _UnreachableWal(failure_type("read-only write reached WAL"))
    coordinator = _UnreachableCoordinator(failure_type("read-only write reached coordination"))
    real_wal, real_coordinator = manager._wal, manager._coordinator
    manager._writable = False
    manager._wal, manager._coordinator = wal, coordinator
    try:
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            operation(manager, transaction)
    finally:
        manager._wal, manager._coordinator = real_wal, real_coordinator
        manager._writable = True

    assert raised.value.details["read_only"] is True
    assert transaction.active
    assert manager.open_transactions == 1
    assert wal.calls == []
    assert coordinator.calls == []
    assert storage.trail() == ()
    assert _stored_bytes(storage) == bytes_before
    assert stack.coordinator.reader_horizon() == horizon
    manager.rollback(transaction)


@pytest.mark.parametrize("settled", ("aborted", "committed"))
def test_a_settled_fake_is_not_mistaken_for_an_idempotent_real_rollback(
    stack: Stack, settled: str
) -> None:
    manager = stack.manager
    genuine = manager.begin("read")
    manager.rollback(genuine)
    manager.rollback(genuine)
    forged = _forged(manager, genuine, TransactionMode.READ)
    if settled == "aborted":
        forged.mark_aborted()
    else:
        forged.mark_committed(genuine.snapshot.read_lsn)

    with pytest.raises(GrafxTransactionStateError) as raised:
        manager.rollback(forged)

    assert raised.value.details["reason"] == "transaction_capability_mismatch"
    assert manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None


def test_read_transactions_and_their_reader_pins_are_unchanged(stack: Stack) -> None:
    manager = _manager(stack, writable=False)
    transaction = manager.begin("read")
    assert manager.writable is False
    assert manager.open_transactions == 1
    assert stack.coordinator.reader_horizon() == transaction.snapshot.read_lsn

    report = manager.commit(transaction)

    assert report.csn == transaction.snapshot.read_lsn
    assert manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None


def test_writable_defaults_to_the_compatible_enabled_capability(stack: Stack) -> None:
    assert stack.manager.writable is True
    transaction = stack.manager.begin("write")
    stack.manager.rollback(transaction)


@pytest.mark.parametrize("invalid", (None, 0, 1, "false", object()))
def test_a_non_boolean_writable_capability_is_refused_at_construction(
    stack: Stack, invalid: object
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        _manager(stack, writable=invalid)
    assert raised.value.details == {"field": "writable", "value": type(invalid).__name__}
