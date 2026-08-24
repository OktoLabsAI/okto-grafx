"""The transaction manager's fail-fast read-only capability boundary."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxUnsupportedOperation
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


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt))
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
