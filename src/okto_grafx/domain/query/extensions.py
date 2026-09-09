"""Explicit trusted scalar extension values; no discovery or persisted executable code."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.domain.model.schema import is_identifier
from okto_grafx.domain.model.value import Timestamp, Uuid, encode_value

__all__ = ["ScalarFunction", "ExtensionRegistry"]

_TYPES = {"BOOL": bool, "INT64": int, "DOUBLE": float, "STRING": str,
          "BYTES": bytes, "TIMESTAMP": Timestamp, "UUID": Uuid}


@dataclass(frozen=True, slots=True)
class ScalarFunction:
    """Trusted deterministic scalar callback with exact positional types and NULL propagation."""

    name: str
    argument_types: tuple[str, ...]
    return_type: str
    implementation: Callable[..., object]
    max_value_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        """Validate a namespaced, bounded scalar signature before any database opens."""
        if (type(self.name) is not str or len(self.name) > 128 or "." not in self.name
                or not all(is_identifier(part) for part in self.name.split("."))):
            raise GrafxConfigurationError("Scalar names need an explicit namespace (app.function).", field="name")
        if (type(self.argument_types) is not tuple or len(self.argument_types) > 32
                or any(type(t) is not str or t not in _TYPES for t in self.argument_types)
                or type(self.return_type) is not str or self.return_type not in _TYPES):
            raise GrafxConfigurationError("Invalid scalar type signature.", field="argument_types")
        if not callable(self.implementation):
            raise GrafxConfigurationError("Scalar implementation must be callable.", field="implementation")
        if type(self.max_value_bytes) is not int or not 1 <= self.max_value_bytes <= 2**31:
            raise GrafxConfigurationError("Invalid scalar value budget.", field="max_value_bytes")

    def _check(self, value: object, kind: str) -> None:
        """Validate exact immutable scalar types, native encodability and logical size."""
        if value is None:
            return
        if type(value) is not _TYPES[kind]:
            raise GrafxPlanError("Scalar value does not match its declared type.", field="udf_type", function=self.name)
        if type(value) is float and not isfinite(value):
            raise GrafxPlanError("Scalar doubles must be finite.", field="udf_type", function=self.name)
        # Upper-bound UTF-8 before allocating its encoded image.
        size = 4 * len(value) if type(value) is str else len(value) if type(value) is bytes else 16
        if size > self.max_value_bytes:
            raise GrafxQueryBudgetExceeded("Scalar value budget exceeded.", resource="udf_value", function=self.name)
        try:
            encode_value(value)
        except Exception as failure:
            raise GrafxPlanError("Scalar value is outside the native value contract.", field="udf_type", function=self.name) from failure

    def invoke(self, arguments: tuple[object, ...]) -> object:
        """Call with scalar values only; NULL propagates and callback failures are typed."""
        if type(arguments) is not tuple or len(arguments) != len(self.argument_types):
            raise GrafxPlanError("Scalar argument count mismatch.", field="udf_arity", function=self.name)
        for value, kind in zip(arguments, self.argument_types):
            self._check(value, kind)
        if any(value is None for value in arguments):
            return None
        try:
            result = self.implementation(*arguments)
        except Exception as failure:
            raise GrafxPlanError("Trusted scalar callback failed.", field="udf_callback", function=self.name) from failure
        self._check(result, self.return_type)
        return result


@dataclass(frozen=True, slots=True)
class ExtensionRegistry:
    """Per-handle immutable trusted allowlist; not a sandbox or a plugin loader."""

    scalars: tuple[ScalarFunction, ...] = ()
    trusted: bool = False

    def __post_init__(self) -> None:
        """Require explicit host trust and unique exact names; reject mutable registrations."""
        if type(self.trusted) is not bool or not self.trusted:
            raise GrafxConfigurationError("Extension callbacks require trusted=True.", field="trusted")
        if (type(self.scalars) is not tuple or len(self.scalars) > 128
                or any(type(fn) is not ScalarFunction for fn in self.scalars)):
            raise GrafxConfigurationError("Invalid scalar registry.", field="scalars")
        if len({fn.name for fn in self.scalars}) != len(self.scalars):
            raise GrafxConfigurationError("Duplicate scalar name.", field="scalars")

    def call_scalar(self, name: str, arguments: tuple[object, ...]) -> object:
        """Invoke only an exact registered name; no module/path or builtin resolution."""
        if type(name) is not str:
            raise GrafxPlanError("Scalar name must be text.", field="udf_name")
        for function in self.scalars:
            if function.name == name:
                return function.invoke(arguments)
        raise GrafxPlanError("Scalar function is not registered on this handle.", field="udf_name")
