"""Explicit trusted scalar extension values; no discovery or persisted executable code."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from math import isfinite

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.domain.model.schema import is_identifier
from okto_grafx.domain.model.value import Timestamp, Uuid, encode_value

__all__ = ["ScalarFunction", "TabularProcedure", "ExtensionRegistry"]

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
class TabularProcedure:
    """Trusted pure tabular callback: no database handle or implicit write capability.

    The host is responsible for callback purity, just as for ScalarFunction. CALL never
    imports code, grants filesystem/network access, or makes callback side effects transactional.
    NULL cells are allowed; every non-NULL cell must have its exact declared scalar type.
    """

    name: str
    argument_types: tuple[str, ...]
    columns: tuple[tuple[str, str], ...]
    implementation: Callable[..., Iterable[tuple[object, ...]]]
    required_permissions: frozenset[str] = frozenset()
    max_rows: int = 10_000
    max_result_bytes: int = 8 * 1024 * 1024
    max_value_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        """Validate the complete immutable signature and budgets before registration."""
        ScalarFunction(self.name, self.argument_types, "BOOL", self.implementation, self.max_value_bytes)
        if (type(self.columns) is not tuple or not 1 <= len(self.columns) <= 64
                or any(type(item) is not tuple or len(item) != 2
                       or type(item[0]) is not str or not is_identifier(item[0])
                       or type(item[1]) is not str or item[1] not in _TYPES for item in self.columns)
                or len({item[0] for item in self.columns}) != len(self.columns)):
            raise GrafxConfigurationError("Invalid tabular output schema.", field="columns")
        if (type(self.required_permissions) is not frozenset
                or any(type(item) is not str or not is_identifier(item) for item in self.required_permissions)):
            raise GrafxConfigurationError("Permissions must be an immutable set of names.", field="required_permissions")
        for field_name in ("max_rows", "max_result_bytes"):
            if type(getattr(self, field_name)) is not int or not 1 <= getattr(self, field_name) <= 2**31:
                raise GrafxConfigurationError("Invalid procedure budget.", field=field_name)

    def invoke(self, arguments: tuple[object, ...]) -> Iterator[tuple[object, ...]]:
        """Validate inputs/results and close the callback stream on exhaustion or cancellation."""
        checker = ScalarFunction(self.name, self.argument_types, "BOOL", self.implementation, self.max_value_bytes)
        if type(arguments) is not tuple or len(arguments) != len(self.argument_types):
            raise GrafxPlanError("Procedure argument count mismatch.", field="procedure_arity", procedure=self.name)
        for value, kind in zip(arguments, self.argument_types, strict=True):
            checker._check(value, kind)
        stream = None
        primary_failure: BaseException | None = None
        try:
            stream = iter(self.implementation(*arguments))
            used = 0
            for count, row in enumerate(stream, start=1):
                if count > self.max_rows:
                    raise GrafxQueryBudgetExceeded("Procedure row budget exceeded.", resource="procedure_rows")
                if type(row) is not tuple or len(row) != len(self.columns):
                    raise GrafxPlanError("Procedure returned the wrong row shape.", field="procedure_result", procedure=self.name)
                for value, (_column, kind) in zip(row, self.columns, strict=True):
                    checker._check(value, kind)
                    used += len(encode_value(value))
                if used > self.max_result_bytes:
                    raise GrafxQueryBudgetExceeded("Procedure result byte budget exceeded.", resource="procedure_bytes")
                yield row
        except (GrafxPlanError, GrafxQueryBudgetExceeded) as failure:
            primary_failure = failure
            raise
        except Exception as failure:
            primary_failure = GrafxPlanError("Trusted procedure callback failed.", field="procedure_callback", procedure=self.name)
            raise primary_failure from failure
        except GeneratorExit:
            raise
        except BaseException as failure:
            primary_failure = failure
            raise
        finally:
            try:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            except Exception as failure:
                if primary_failure is not None:
                    primary_failure.add_note(f"Procedure stream cleanup also failed: {type(failure).__name__}.")
                else:
                    raise GrafxPlanError("Trusted procedure stream cleanup failed.", field="procedure_cleanup", procedure=self.name) from failure


@dataclass(frozen=True, slots=True)
class ExtensionRegistry:
    """Per-handle immutable trusted allowlist; not a sandbox or a plugin loader."""

    scalars: tuple[ScalarFunction, ...] = ()
    trusted: bool = False
    procedures: tuple[TabularProcedure, ...] = ()
    procedure_permissions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Require explicit host trust and unique exact names; reject mutable registrations."""
        if type(self.trusted) is not bool or not self.trusted:
            raise GrafxConfigurationError("Extension callbacks require trusted=True.", field="trusted")
        if (type(self.scalars) is not tuple or len(self.scalars) > 128
                or any(type(fn) is not ScalarFunction for fn in self.scalars)):
            raise GrafxConfigurationError("Invalid scalar registry.", field="scalars")
        if len({fn.name for fn in self.scalars}) != len(self.scalars):
            raise GrafxConfigurationError("Duplicate scalar name.", field="scalars")
        if (type(self.procedures) is not tuple or len(self.procedures) > 128
                or any(type(item) is not TabularProcedure for item in self.procedures)
                or len({item.name for item in self.procedures}) != len(self.procedures)):
            raise GrafxConfigurationError("Invalid procedure registry.", field="procedures")
        if (type(self.procedure_permissions) is not frozenset
                or any(type(item) is not str or not is_identifier(item) for item in self.procedure_permissions)):
            raise GrafxConfigurationError("Invalid procedure permissions.", field="procedure_permissions")

    def call_scalar(self, name: str, arguments: tuple[object, ...]) -> object:
        """Invoke only an exact registered name; no module/path or builtin resolution."""
        if type(name) is not str:
            raise GrafxPlanError("Scalar name must be text.", field="udf_name")
        for function in self.scalars:
            if function.name == name:
                return function.invoke(arguments)
        raise GrafxPlanError("Scalar function is not registered on this handle.", field="udf_name")
