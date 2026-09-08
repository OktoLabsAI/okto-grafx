"""Focused regression for recovery's WAL port-shape validation (TXN-2)."""

from __future__ import annotations

from okto_grafx.runtime.capability_probe import port_has_attribute

from typing import Any

import pytest

from okto_grafx.engine.recovery_manager import RecoveryManager
from okto_grafx.engine.wal_manager import WalManager
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxPortNotConfigured

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
        attribute_probe=port_has_attribute,
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


def test_host_probe_observes_inherited_descriptors_without_evaluating_them() -> None:
    """An inherited failing descriptor is declared, not an absent dynamic door."""

    class Parent:
        @property
        def damage(self) -> object:
            raise AssertionError("shape observation evaluated a descriptor")

    class Child(Parent):
        __slots__ = ("member",)

    instance = Child()
    assert port_has_attribute(instance, "damage") is True
    assert port_has_attribute(instance, "member") is True
    assert port_has_attribute(instance, "missing") is False


def test_host_probe_keeps_instance_attributes_and_dynamic_absence() -> None:
    """Static instance values count as shape; a missing dynamic member does not."""

    class Dynamic:
        def __init__(self) -> None:
            self.damage = None

        def __getattr__(self, name: str) -> object:
            raise AttributeError(name)

    instance = Dynamic()
    assert port_has_attribute(instance, "damage") is True
    assert port_has_attribute(instance, "missing") is False


@pytest.mark.parametrize("probe", [None, False, object()])
def test_recovery_requires_an_explicit_callable_probe(stack: Stack, probe: object) -> None:
    """Manual composition must not silently fall back to eager descriptor lookup."""
    with pytest.raises(GrafxConfigurationError) as refused:
        stack.recovery(attribute_probe=probe)
    assert refused.value.details["field"] == "attribute_probe"


@pytest.mark.parametrize("answer", [None, 0, 1, "present", object()])
def test_recovery_rejects_non_boolean_probe_answers(stack: Stack, answer: object) -> None:
    """Truthiness from a malformed collaborator cannot grant port admission."""
    with pytest.raises(GrafxConfigurationError) as refused:
        stack.recovery(attribute_probe=lambda instance, name: answer)
    assert refused.value.details["field"] == "attribute_probe"
    assert refused.value.details["slot"] == "storage"
    assert refused.value.details["member"] == "exists"


def test_engine_keeps_missing_member_policy_with_an_injected_probe(stack: Stack) -> None:
    """The host supplies observation; recovery still owns the mandatory WAL doors."""
    observed: list[tuple[object, str]] = []

    def probe(instance: object, name: str) -> bool:
        observed.append((instance, name))
        if instance is stack.wal and name == "truncate_after":
            return False
        return port_has_attribute(instance, name)

    with pytest.raises(GrafxPortNotConfigured) as refused:
        stack.recovery(attribute_probe=probe)
    assert refused.value.details["slot"] == "wal"
    assert refused.value.details["missing"] == ("truncate_after",)
    assert (stack.wal, "damage") in observed


def test_recovery_does_not_swallow_injected_probe_failures(stack: Stack) -> None:
    """The engine does not translate a host failure into an absent or valid member."""
    failure = RuntimeError("injected shape probe failure")

    def probe(instance: object, name: str) -> bool:
        raise failure

    with pytest.raises(RuntimeError) as caught:
        stack.recovery(attribute_probe=probe)
    assert caught.value is failure
