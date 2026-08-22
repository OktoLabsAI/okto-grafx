"""Diagnosing a bad port binding never runs adapter code (CONTRACT.md section 5, BR-8)."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports import Clock, StorageDevice
from okto_grafx.runtime import registry as registry_module
from okto_grafx.runtime.registry import (
    PortRegistry,
    _declared_as_method,
    _member_defects,
    _missing_members,
)


class ExplodingStorage:
    """A device whose page_size property raises, to prove diagnosis stays static."""

    evaluations = 0

    @property
    def name(self) -> str:
        return "exploding"

    @property
    def page_size(self) -> int:
        type(self).evaluations += 1
        raise RuntimeError("This property must never be evaluated by the registry.")


def test_missing_members_reports_what_the_instance_lacks() -> None:
    missing = _missing_members(Clock, object())
    assert set(missing) == {"monotonic", "wall"}
    assert missing == tuple(sorted(missing))


def test_missing_members_sees_an_attribute_that_exists() -> None:
    class OnlyMonotonic:
        def monotonic(self) -> float:
            return 0.0

    assert _missing_members(Clock, OnlyMonotonic()) == ("wall",)


def test_diagnosing_a_bad_binding_does_not_evaluate_properties() -> None:
    ExplodingStorage.evaluations = 0
    registry = PortRegistry()

    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("storage", ExplodingStorage())

    assert ExplodingStorage.evaluations == 0
    missing = raised.value.details["missing"]
    assert isinstance(missing, list)
    assert "durable_barrier" in missing
    assert "page_size" not in missing


def test_an_instance_attribute_satisfies_a_protocol_member() -> None:
    class AttributeClock:
        def __init__(self) -> None:
            self.monotonic = lambda: 1.0
            self.wall = lambda: 2.0

    assert _missing_members(Clock, AttributeClock()) == ()
    registry = PortRegistry()
    registry.bind("clock", AttributeClock())
    assert isinstance(registry.get("clock"), Clock)


def test_every_required_slot_is_backed_by_a_port_protocol(
    complete_registry: PortRegistry,
) -> None:
    for slot in PortRegistry.REQUIRED:
        assert complete_registry.get(slot) is not None
    assert isinstance(complete_registry.get("storage"), StorageDevice)


# --- no adapter code runs during a bind, on any interpreter ------------------------------------


def test_a_complete_adapter_whose_property_raises_binds_without_running_it(
    fake_ports: dict[str, object]
) -> None:
    # The decisive case for Python 3.11, where isinstance against a runtime checkable Protocol
    # still used hasattr and therefore evaluated properties: a complete-shape adapter whose
    # page_size raises used to escape as a bare RuntimeError from bind().
    class ExplodingCompleteStorage(type(fake_ports["storage"])):  # type: ignore[misc]
        """A device with the full StorageDevice shape whose page_size raises on access."""

        @property
        def page_size(self) -> int:
            raise RuntimeError("This property must never be evaluated by the registry.")

    adapter = ExplodingCompleteStorage()
    registry = PortRegistry()
    registry.bind("storage", adapter)
    assert registry.get("storage") is adapter
    assert _missing_members(StorageDevice, adapter) == ()

    # The adapter really is hostile: the test would be vacuous if the property were harmless.
    with pytest.raises(RuntimeError):
        adapter.page_size


def test_an_incomplete_adapter_whose_property_raises_fails_with_a_grafx_error(
    fake_ports: dict[str, object]
) -> None:
    class HalfExplodingStorage:
        """Missing most of the port, and hostile on the members it does declare."""

        @property
        def name(self) -> str:
            raise RuntimeError("This property must never be evaluated by the registry.")

        @property
        def page_size(self) -> int:
            raise RuntimeError("This property must never be evaluated by the registry.")

    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("storage", HalfExplodingStorage())
    assert raised.value.code == "configuration_error"
    assert "durable_barrier" in raised.value.message


def test_an_object_that_raises_on_every_attribute_lookup_yields_a_grafx_error() -> None:
    class Hostile:
        """A proxy whose __getattr__ raises instead of answering."""

        def __getattr__(self, name: str) -> object:
            raise RuntimeError(f"Refusing to answer for {name!r}.")

    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("clock", Hostile())
    assert set(raised.value.details["missing"]) == {"monotonic", "wall"}  # type: ignore[arg-type]


def test_an_inspection_failure_is_still_reported_as_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The last line of defence for DoD item 5: whatever the inspection does, only a Grafx error
    # leaves bind(). Reaching it needs a forced failure, which is the point of the guard.
    def explode(protocol: type, instance: object) -> dict[str, str]:
        raise RuntimeError("Inspection blew up.")

    monkeypatch.setattr(registry_module, "_member_defects", explode)
    with pytest.raises(GrafxConfigurationError) as raised:
        PortRegistry().bind("clock", object())
    assert raised.value.details["slot"] == "clock"
    assert raised.value.details["protocol"] == "Clock"
    assert isinstance(raised.value.__cause__, RuntimeError)


@pytest.mark.parametrize("slot", sorted(PortRegistry.REQUIRED))
def test_no_slot_lets_a_foreign_exception_escape_a_bind(slot: str) -> None:
    class Hostile:
        def __getattr__(self, name: str) -> object:
            raise RuntimeError(f"Refusing to answer for {name!r}.")

    registry = PortRegistry()
    try:
        registry.bind(slot, Hostile())
    except GrafxConfigurationError:
        pass
    except Exception as failure:  # noqa: BLE001 - the point of the test is what escapes
        pytest.fail(f"bind({slot!r}) leaked {type(failure).__name__}: {failure}")
    else:
        pytest.fail(f"bind({slot!r}) accepted an object with no members")


# --- a present member is not automatically a usable one ---------------------------------------


def test_a_member_bound_to_none_is_refused() -> None:
    class HollowClock:
        """The shape is there, but monotonic is None and would fail at the first call."""

        monotonic = None

        def wall(self) -> float:
            return 0.0

    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("clock", HollowClock())
    assert raised.value.details["reasons"] == {"monotonic": "bound to None"}
    assert "monotonic" in raised.value.message


def test_a_declared_method_bound_to_a_non_callable_is_refused() -> None:
    class NumberClock:
        """monotonic holds a number instead of a method: isinstance accepts this, we do not."""

        monotonic = 42

        def wall(self) -> float:
            return 0.0

    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("clock", NumberClock())
    assert "monotonic" in raised.value.details["reasons"]
    assert "not callable" in raised.value.message or "bound to int" in raised.value.message


def test_a_property_where_the_protocol_declares_a_method_is_refused_without_evaluation() -> None:
    class PropertyClock:
        """monotonic is a property, so calling it would return a float, not a method."""

        @property
        def monotonic(self) -> float:
            raise RuntimeError("This property must never be evaluated by the registry.")

        def wall(self) -> float:
            return 0.0

    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("clock", PropertyClock())
    assert "monotonic" in raised.value.details["reasons"]


def test_a_property_backed_member_the_protocol_declares_as_a_property_still_binds(
    fake_ports: dict[str, object]
) -> None:
    # The deliberate exception: StorageDevice.name and page_size ARE properties, so a property
    # on the adapter is correct and must bind without being evaluated.
    class ExplodingCompleteStorage(type(fake_ports["storage"])):  # type: ignore[misc]
        """A device whose page_size property raises, which the registry must never call."""

        @property
        def page_size(self) -> int:
            raise RuntimeError("This property must never be evaluated by the registry.")

    registry = PortRegistry()
    registry.bind("storage", ExplodingCompleteStorage())
    assert _member_defects(StorageDevice, ExplodingCompleteStorage()) == {}


def test_static_and_class_methods_satisfy_a_declared_method() -> None:
    class StaticClock:
        """A perfectly legitimate adapter that happens to use staticmethod and classmethod."""

        @staticmethod
        def monotonic() -> float:
            return 0.0

        @classmethod
        def wall(cls) -> float:
            return 0.0

    registry = PortRegistry()
    registry.bind("clock", StaticClock())
    assert isinstance(registry.get("clock"), Clock)


def test_a_lambda_attribute_still_satisfies_a_declared_method() -> None:
    class LambdaClock:
        def __init__(self) -> None:
            self.monotonic = lambda: 1.0
            self.wall = lambda: 2.0

    registry = PortRegistry()
    registry.bind("clock", LambdaClock())
    assert _member_defects(Clock, LambdaClock()) == {}


def test_the_declared_kind_comes_from_the_protocol_not_from_the_candidate() -> None:
    assert _declared_as_method(Clock, "monotonic") is True
    assert _declared_as_method(Clock, "wall") is True
    assert _declared_as_method(StorageDevice, "name") is False
    assert _declared_as_method(StorageDevice, "page_size") is False
    assert _declared_as_method(StorageDevice, "durable_barrier") is True
    assert _declared_as_method(Clock, "unknown_member") is False


def test_none_is_refused_for_a_property_backed_member_too(
    fake_ports: dict[str, object]
) -> None:
    class NamelessStorage(type(fake_ports["storage"])):  # type: ignore[misc]
        """name is None, which would read as a null metric label instead of a device name."""

        name = None

    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("storage", NamelessStorage())
    assert raised.value.details["reasons"] == {"name": "bound to None"}


def test_a_class_object_is_refused_where_an_instance_is_required() -> None:
    # The methods of a class are unbound functions, so the shape check passes and every later
    # call arrives without its instance. The slot takes an object, not the type of one.
    class TinyClock:
        def monotonic(self) -> float:
            return 1.0

        def wall(self) -> float:
            return 2.0

    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError) as raised:
        registry.bind("clock", TinyClock)
    assert raised.value.details["slot"] == "clock"
    assert "instance" in raised.value.message

    registry.bind("clock", TinyClock())
    assert isinstance(registry.get("clock"), Clock)


@pytest.mark.parametrize("slot", sorted(PortRegistry.REQUIRED))
def test_no_slot_accepts_a_class_object(slot: str, fake_ports: dict[str, object]) -> None:
    registry = PortRegistry()
    with pytest.raises(GrafxConfigurationError):
        registry.bind(slot, type(fake_ports[slot]))
