"""The fail-closed port registry (CONTRACT.md section 5, SPEC-M1 BR-8, guideline G5)."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxPortNotConfigured
from okto_grafx.domain.ports import Clock, StorageDevice
from okto_grafx.runtime.registry import PortRegistry, _protocol_members


class TinyClock:
    """The smallest object that satisfies the Clock port."""

    def monotonic(self) -> float:
        return 1.0

    def wall(self) -> float:
        return 1_700_000_000.0


def test_required_slots_match_the_contract() -> None:
    assert PortRegistry.REQUIRED == (
        "storage",
        "clock",
        "coordinator",
        "codec",
        "metrics",
        "vector_math",
        "events",
    )


def test_every_slot_that_can_be_bound_is_a_required_slot(fake_ports: dict[str, object]) -> None:
    # The slot table and the required list cannot drift apart: a slot nobody requires would be
    # a port that startup never checks.
    assert set(fake_ports) == set(PortRegistry.REQUIRED)
    registry = PortRegistry()
    for slot, instance in fake_ports.items():
        registry.bind(slot, instance)
    registry.require_complete()


def test_a_fresh_registry_is_empty_and_refuses_to_pass() -> None:
    registry = PortRegistry()
    with pytest.raises(GrafxPortNotConfigured) as raised:
        registry.require_complete()
    assert raised.value.details["missing"] == list(PortRegistry.REQUIRED)


def test_require_complete_lists_every_missing_slot_in_one_error(
    fake_ports: dict[str, object]
) -> None:
    registry = PortRegistry()
    registry.bind("storage", fake_ports["storage"])
    registry.bind("metrics", fake_ports["metrics"])

    with pytest.raises(GrafxPortNotConfigured) as raised:
        registry.require_complete()

    error = raised.value
    missing = ["clock", "coordinator", "codec", "vector_math", "events"]
    assert error.details["missing"] == missing
    listed = error.message.split(":", 1)[1].split(".", 1)[0]
    assert [slot.strip() for slot in listed.split(",")] == missing
    assert error.code == "port_not_configured"
    assert error.retryable is False


def test_require_complete_passes_when_every_slot_is_bound(
    complete_registry: PortRegistry,
) -> None:
    assert complete_registry.require_complete() is None


def test_get_returns_the_bound_instance(fake_ports: dict[str, object]) -> None:
    registry = PortRegistry()
    registry.bind("clock", fake_ports["clock"])
    assert registry.get("clock") is fake_ports["clock"]


def test_get_on_an_empty_slot_fails_closed() -> None:
    registry = PortRegistry()
    with pytest.raises(GrafxPortNotConfigured) as raised:
        registry.get("storage")
    assert raised.value.details["missing"] == ["storage"]
    assert "StorageDevice" in raised.value.message


def test_get_on_an_unknown_slot_is_a_configuration_error() -> None:
    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.get("wal")
    assert raised.value.details["slot"] == "wal"


def test_bind_on_an_unknown_slot_is_a_configuration_error(
    fake_ports: dict[str, object]
) -> None:
    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("storage_device", fake_ports["storage"])
    assert "Unknown port slot" in raised.value.message
    for slot in PortRegistry.REQUIRED:
        assert slot in raised.value.message


def test_bind_on_an_unhashable_slot_name_is_a_configuration_error(
    fake_ports: dict[str, object]
) -> None:
    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError):
        registry.bind(["storage"], fake_ports["storage"])  # type: ignore[arg-type]


def test_binding_a_wrong_shape_names_the_slot_and_the_missing_members() -> None:
    class HalfStorage:
        """A device that forgot the durability barrier and the log primitives."""

        @property
        def name(self) -> str:
            return "half"

        @property
        def page_size(self) -> int:
            return 8192

        def exists(self, file: str) -> bool:
            return False

    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("storage", HalfStorage())

    error = raised.value
    assert error.details["slot"] == "storage"
    assert error.details["protocol"] == "StorageDevice"
    missing = error.details["missing"]
    assert isinstance(missing, list)
    assert "durable_barrier" in missing
    assert "append_log" in missing
    assert "exists" not in missing
    assert "storage" in error.message
    assert "durable_barrier" in error.message


def test_binding_an_object_with_no_members_at_all_is_rejected() -> None:
    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("clock", object())
    assert set(raised.value.details["missing"]) == {"monotonic", "wall"}  # type: ignore[arg-type]


def test_a_rejected_bind_leaves_the_slot_empty() -> None:
    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError):
        registry.bind("clock", object())
    with pytest.raises(GrafxPortNotConfigured):
        registry.get("clock")


def test_binding_the_right_shape_to_the_wrong_slot_is_rejected(
    fake_ports: dict[str, object]
) -> None:
    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("clock", fake_ports["storage"])
    assert raised.value.details["protocol"] == "Clock"


def test_a_later_bind_replaces_the_previous_one() -> None:
    registry = PortRegistry()
    first = TinyClock()
    second = TinyClock()
    registry.bind("clock", first)
    registry.bind("clock", second)
    assert registry.get("clock") is second


def test_registries_do_not_share_state() -> None:
    # FR-13 and BR-8: nothing is global, so one database never sees the ports of another.
    first = PortRegistry()
    second = PortRegistry()
    first.bind("clock", TinyClock())
    with pytest.raises(GrafxPortNotConfigured):
        second.get("clock")


def test_protocol_members_reads_the_declared_protocol_attributes() -> None:
    assert set(_protocol_members(Clock)) == {"monotonic", "wall"}
    assert "durable_barrier" in _protocol_members(StorageDevice)
    assert _protocol_members(Clock) == tuple(sorted(_protocol_members(Clock)))


def test_protocol_members_falls_back_to_the_class_dictionary() -> None:
    # Older interpreters do not publish __protocol_attrs__; the fallback must find the same
    # members by walking the class hierarchy.
    class Shim:
        def monotonic(self) -> float:
            return 0.0

        def wall(self) -> float:
            return 0.0

    assert not hasattr(Shim, "__protocol_attrs__")
    assert set(_protocol_members(Shim)) == {"monotonic", "wall"}


def test_protocol_members_fallback_ignores_inherited_object_members() -> None:
    class Base:
        def emit(self, event: str, payload: dict[str, object]) -> None:
            return None

    class Derived(Base):
        def flush(self) -> None:
            return None

    assert set(_protocol_members(Derived)) == {"emit", "flush"}
