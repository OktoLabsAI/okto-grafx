"""The port registry (CONTRACT.md section 5, SPEC-M1 BR-8, guideline G5).

The registry is fail-closed on purpose. An empty slot is not filled with a silent default and
never degrades into a no-op: opening a database with an incomplete registry raises
GrafxPortNotConfigured naming every slot that is missing, in one error, so an operator fixes
the composition once instead of once per attempt.
"""

from __future__ import annotations

from collections.abc import Mapping
from inspect import getattr_static
from types import MappingProxyType
from typing import ClassVar

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxError,
    GrafxPortNotConfigured,
)
from okto_grafx.domain.ports import (
    Clock,
    EventSink,
    MetricsSink,
    PageCodec,
    ProcessCoordinator,
    StorageDevice,
    VectorMath,
)

__all__ = ["PortRegistry"]

_SLOT_PROTOCOLS: Mapping[str, type] = MappingProxyType(
    {
        "storage": StorageDevice,
        "clock": Clock,
        "coordinator": ProcessCoordinator,
        "codec": PageCodec,
        "metrics": MetricsSink,
        "vector_math": VectorMath,
        "events": EventSink,
    }
)

_NON_MEMBER_ATTRIBUTES: frozenset[str] = frozenset(
    {"_is_protocol", "_is_runtime_protocol", "_abc_impl"}
)


def _protocol_members(protocol: type) -> tuple[str, ...]:
    """Return the attribute names a class must expose to satisfy the protocol, sorted.

    Interpreters from 3.12 on publish the set as ``__protocol_attrs__``; older ones do not, so
    the fallback walks the class hierarchy and keeps every name that is neither a dunder nor
    protocol bookkeeping.
    """
    declared = getattr(protocol, "__protocol_attrs__", None)
    if declared is not None:
        return tuple(sorted(declared))
    names: set[str] = set()
    for base in protocol.__mro__:
        if base.__name__ in {"Protocol", "Generic", "object"}:
            continue
        for name in vars(base):
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in _NON_MEMBER_ATTRIBUTES:
                continue
            names.add(name)
    return tuple(sorted(names))


_ABSENT: object = object()


def _declared_as_method(protocol: type, name: str) -> bool:
    """Return True when the protocol declares this member as a method rather than a property.

    The answer comes from the protocol's own declaration, never from probing the candidate: a
    property on the candidate must stay unevaluated during a bind.
    """
    for base in protocol.__mro__:
        declaration = vars(base).get(name, _ABSENT)
        if declaration is _ABSENT:
            continue
        if isinstance(declaration, property):
            return False
        return callable(declaration) or isinstance(
            declaration, (staticmethod, classmethod)
        )
    return False


def _is_usable_method(value: object) -> bool:
    """Return True when the statically found value could be called as a method."""
    if isinstance(value, (staticmethod, classmethod)):
        return True
    return callable(value)


def _member_defects(protocol: type, instance: object) -> dict[str, str]:
    """Return every protocol member the instance does not usably provide, mapped to the reason.

    The lookup is static on every interpreter: no descriptor is executed, so binding an adapter
    can never run adapter code. ``isinstance`` against a runtime checkable Protocol is not used
    for this decision, because it evaluates properties on Python 3.11 and only became static in
    3.12 -- and this package supports both, so the verdict has to be the same on both.

    A member present but bound to None, or a declared method bound to something that cannot be
    called, is a defect too: it passes a shape check and then fails as an untyped TypeError at
    the first call, which is exactly what fail-closed composition exists to prevent.
    """
    defects: dict[str, str] = {}
    for name in _protocol_members(protocol):
        try:
            value = getattr_static(instance, name)
        except AttributeError:
            defects[name] = "missing"
            continue
        if value is None:
            defects[name] = "bound to None"
            continue
        if _declared_as_method(protocol, name) and not _is_usable_method(value):
            defects[name] = (
                f"declared as a method but bound to {_builtin_type_name(value)}"
            )
    return defects


def _missing_members(protocol: type, instance: object) -> tuple[str, ...]:
    """Return the names of the protocol members the instance does not usably provide."""
    return tuple(_member_defects(protocol, instance))


class PortRegistry:
    """Fail-closed (G5). Every required slot must be bound before open_database returns."""

    REQUIRED: ClassVar[tuple[str, ...]] = (
        "storage",
        "clock",
        "coordinator",
        "codec",
        "metrics",
        "vector_math",
        "events",
    )

    def __init__(self) -> None:
        """Create an empty registry: no slot is bound and no default is implied."""
        self._bindings: dict[str, object] = {}

    def bind(self, slot: str, instance: object) -> None:
        """Bind an adapter to a required slot, refusing an unknown slot or a wrong shape.

        The instance is checked against the members of the Protocol that owns the slot, by
        static lookup: no adapter code runs during a bind, so a property that raises cannot
        escape as an untyped failure (DoD item 5, SPEC-M1 TR-6). The consequence is that an
        adapter which synthesises its members through ``__getattr__`` must declare them, on
        every interpreter version.

        A class object is refused outright: its methods are unbound functions, so the shape
        check would pass and every later call would be missing its instance.

        A later bind on the same slot replaces the previous one, so a caller can override a
        default adapter it does not want.
        """
        slot = _require_slot(slot)
        protocol = self._protocol_for(slot)
        if isinstance(instance, type):
            observed = _builtin_class_name(instance)
            raise GrafxConfigurationError(
                f"Port slot {slot!r} takes an instance, not the class itself; "
                f"instantiate {observed!r} before binding it.",
                slot=slot,
                protocol=protocol.__name__,
            )
        try:
            defects = _member_defects(protocol, instance)
        except (
            Exception
        ) as failure:  # an exotic object must never escape as a foreign error
            cause = _builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"Port slot {slot!r} could not be inspected against {protocol.__name__}: "
                f"{cause}.",
                slot=slot,
                protocol=protocol.__name__,
                cause=cause,
            ) from failure
        if defects:
            detail = ", ".join(f"{name} ({reason})" for name, reason in defects.items())
            observed = _builtin_type_name(instance)
            raise GrafxConfigurationError(
                f"Port slot {slot!r} needs an implementation of {protocol.__name__}; "
                f"{observed} does not provide: {detail}.",
                slot=slot,
                protocol=protocol.__name__,
                missing=list(defects),
                reasons=dict(defects),
            )
        bindings = _bindings_of(self)
        try:
            dict.__setitem__(bindings, slot, instance)
        except Exception as failure:
            cause = _builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"Registry slot {slot!r} could not be stored safely; got {cause}.",
                field="registry",
                slot=slot,
                cause=cause,
            ) from failure

    def get(self, slot: str) -> object:
        """Return the adapter bound to the slot, raising GrafxPortNotConfigured when it is empty."""
        slot = _require_slot(slot)
        self._protocol_for(slot)
        bindings = _bindings_of(self)
        try:
            return dict.__getitem__(bindings, slot)
        except KeyError:
            raise GrafxPortNotConfigured(
                f"Port slot {slot!r} is not configured. Bind an implementation of "
                f"{_SLOT_PROTOCOLS[slot].__name__} before opening a database.",
                missing=[slot],
            ) from None
        except Exception as failure:
            cause = _builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"Registry slot {slot!r} could not be read safely; got {cause}.",
                field="registry",
                slot=slot,
                cause=cause,
            ) from failure

    def require_complete(self) -> None:
        """Raise GrafxPortNotConfigured listing every empty slot, or return when all are bound."""
        bindings = _bindings_of(self)
        try:
            missing = tuple(
                slot for slot in self.REQUIRED if not dict.__contains__(bindings, slot)
            )
        except Exception as failure:
            cause = _builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"Registry completeness could not be checked safely; got {cause}.",
                field="registry",
                cause=cause,
            ) from failure
        if not missing:
            return
        raise GrafxPortNotConfigured(
            "Port slots are not configured: "
            + ", ".join(missing)
            + ". Bind every required slot before opening a database.",
            missing=list(missing),
        )

    def _protocol_for(self, slot: str) -> type:
        """Return the protocol that owns the slot, rejecting an unknown slot name."""
        slot = _require_slot(slot)
        try:
            return _SLOT_PROTOCOLS[slot]
        except KeyError:
            raise GrafxConfigurationError(
                f"Unknown port slot {slot!r}. Known slots: {', '.join(self.REQUIRED)}.",
                slot=slot,
            ) from None


def _builtin_type_name(value: object) -> str:
    """Name a registry value without executing a hostile metaclass descriptor."""
    value_type = type(value)
    declared = type.__dict__["__name__"].__get__(value_type, type(value_type))
    return str.__str__(declared)


def _builtin_class_name(value: type) -> str:
    """Name a class without consulting descriptors supplied by its metaclass."""
    declared = type.__dict__["__name__"].__get__(value, type(value))
    return str.__str__(declared)


def _require_slot(value: object) -> str:
    """Return an exact slot name without executing string-subclass hooks."""
    if not issubclass(type(value), str):
        observed = _builtin_type_name(value)
        raise GrafxConfigurationError(
            f"A port slot must be a string; got {observed}.",
            field="slot",
            value=observed,
        )
    return str.__str__(value)


def _bindings_of(value: PortRegistry) -> dict[str, object]:
    """Return the exact internal map or classify forged registry state."""
    try:
        bindings = object.__getattribute__(value, "_bindings")
    except Exception as failure:
        cause = _builtin_type_name(failure)
        raise GrafxConfigurationError(
            f"registry has no validated binding map; got {cause}.",
            field="registry",
            cause=cause,
        ) from failure
    if type(bindings) is not dict:
        observed = _builtin_type_name(bindings)
        raise GrafxConfigurationError(
            f"registry has an invalid binding map of type {observed}.",
            field="registry",
            value=observed,
        )
    return bindings


def _snapshot_port_registry(value: object) -> PortRegistry:
    """Return a validated registry snapshot, refusing forged internal state before open.

    An exact instance may still have been made with ``object.__new__`` or altered through
    ``object.__setattr__``. Copying only the declared slots into a fresh registry re-runs every
    static port-shape check and prevents concurrent caller mutation from changing an in-flight
    assembly.
    """
    if type(value) is not PortRegistry:
        observed = _builtin_type_name(value)
        raise GrafxConfigurationError(
            f"registry must be an exact PortRegistry; got {observed}.",
            field="registry",
            value=observed,
        )
    bindings = dict.copy(_bindings_of(value))
    snapshot = PortRegistry()
    for slot in PortRegistry.REQUIRED:
        try:
            instance = dict.__getitem__(bindings, slot)
        except KeyError:
            continue
        except Exception as failure:
            cause = _builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"registry bindings could not be read safely; got {cause}.",
                field="registry",
                cause=cause,
            ) from failure
        try:
            snapshot.bind(slot, instance)
        except GrafxConfigurationError as failure:
            raise GrafxConfigurationError(
                f"registry binding {slot!r} does not satisfy its port contract.",
                field="registry",
                slot=slot,
            ) from failure
        except GrafxError:
            raise
        except Exception as failure:
            cause = _builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"registry binding {slot!r} could not be validated safely; got {cause}.",
                field="registry",
                slot=slot,
                cause=cause,
            ) from failure
    snapshot.require_complete()
    return snapshot
