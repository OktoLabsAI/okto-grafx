"""Focused regression for recovery's WAL port-shape validation (TXN-2)."""

from __future__ import annotations

from typing import Any

import pytest

from okto_grafx.engine.recovery_manager import RecoveryManager
from okto_grafx.engine.wal_manager import WalManager

from .conftest import Stack


class _ScanBackedDamageWal:
    """An alternative WAL whose damage property rebuilds its view before answering."""

    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped
        self.scans = 0

    def open(self) -> None:
        """Open and count the one authoritative reconstruction requested by the caller."""
        self.scans += 1
        self._wrapped.open()  # type: ignore[attr-defined]

    @property
    def damage(self) -> object:
        """Model a compatible lazy implementation whose observation reconstructs the WAL."""
        self.scans += 1
        self._wrapped.open()  # type: ignore[attr-defined]
        return self._wrapped.damage  # type: ignore[attr-defined,no-any-return]

    def __getattr__(self, name: str) -> Any:
        """Forward the rest of the WAL port, including callable recovery doors."""
        return getattr(self._wrapped, name)


def _recovery(stack: Stack, wal: object) -> RecoveryManager:
    """Construct recovery with only its WAL collaborator replaced."""
    return RecoveryManager(
        stack.storage,  # type: ignore[arg-type]
        wal,
        stack.ledger,
        stack.quarantine,
        stack.pool,
        stack.metrics,  # type: ignore[arg-type]
    )


def test_wal_shape_check_does_not_rescan_a_descriptor_backed_damage_property(
    stack: Stack,
) -> None:
    """One explicit open is one rebuild; validating ``damage`` must perform no second scan."""
    wal = _ScanBackedDamageWal(stack.wal)

    wal.open()
    _recovery(stack, wal)

    assert wal.scans == 1


def test_native_cold_recovery_walks_the_wal_once(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port validation must not add a complete native walk before recovery's own pass."""
    original = WalManager._walk
    walks = 0

    def counted_walk(self: WalManager, *args: Any, **kwargs: Any) -> Any:
        nonlocal walks
        walks += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(WalManager, "_walk", counted_walk)

    stack.recovery().run()

    assert walks == 1


def test_wal_shape_check_keeps_dynamic_forwarders_compatible(stack: Stack) -> None:
    """Attributes available only through ``__getattr__`` still satisfy the WAL port."""

    class DynamicWal:
        def __getattr__(self, name: str) -> Any:
            return getattr(stack.wal, name)

    _recovery(stack, DynamicWal())


def test_wal_shape_check_does_not_hide_dynamic_lookup_failures(stack: Stack) -> None:
    """Only AttributeError means missing; an alternative port's own exception is preserved."""
    failure = RuntimeError("wal capability probe failed")

    class FailingDynamicWal:
        def __getattr__(self, name: str) -> object:
            if name == "truncate_after":
                raise failure
            return getattr(stack.wal, name)

    with pytest.raises(RuntimeError, match="wal capability probe failed") as caught:
        _recovery(stack, FailingDynamicWal())

    assert caught.value is failure
