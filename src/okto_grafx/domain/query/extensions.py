"""Explicit trusted scalar extension values; no discovery or persisted executable code."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from math import isfinite

from okto_grafx.domain.errors import GrafxError, GrafxConfigurationError, GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.domain.model.schema import is_identifier
from okto_grafx.domain.model.value import Timestamp, Uuid, ValueType, encode_value
from okto_grafx.domain.model.decimal_values import DecimalValue
from okto_grafx.domain.query.procedure_writer import ProcedureWriter, ProcedureReader
from okto_grafx.domain.query.procedure_values import (
    EXTENDED_PROCEDURE_TYPES, ENTITY_PROCEDURE_TYPES, owned_procedure_value, procedure_result_size,
)

__all__ = ["ScalarFunction", "TabularProcedure", "ExtensionRegistry"]

_TYPES = {"BOOL": bool, "INT64": int, "DOUBLE": float, "STRING": str,
          "BYTES": bytes, "TIMESTAMP": Timestamp, "UUID": Uuid}
_PROCEDURE_TYPES = frozenset(_TYPES) | {"NUMBER"} | EXTENDED_PROCEDURE_TYPES


def _procedure_argument_accepts(actual: ValueType | None, expected: str) -> bool:
    if expected == "ANY":
        return True
    if actual in (None, ValueType.NULL):
        return True
    if expected in ENTITY_PROCEDURE_TYPES:
        return actual is ValueType.LIST if expected.startswith("LIST<") else False
    if expected == "NUMBER":
        return actual in (ValueType.INT64, ValueType.DOUBLE, ValueType.DECIMAL)
    if expected == "DOUBLE":
        return actual in (ValueType.INT64, ValueType.DOUBLE)
    return actual is ValueType[expected]


def _procedure_result_type(kind: str) -> ValueType | None:
    # NUMBER is a query signature union, never a fictitious persisted type tag.
    return (ValueType.LIST if kind.startswith("LIST<") else None) if kind in ENTITY_PROCEDURE_TYPES else (
        None if kind in ("NUMBER", "ANY") else ValueType[kind])


def _procedure_checker(procedure: TabularProcedure) -> ScalarFunction:
    kinds = procedure.argument_types
    if (type(kinds) is not tuple or len(kinds) > 32
            or any(type(kind) is not str or kind not in _PROCEDURE_TYPES for kind in kinds)):
        raise GrafxConfigurationError("Invalid procedure type signature.", field="argument_types")
    return ScalarFunction(procedure.name, tuple("BOOL" if kind in EXTENDED_PROCEDURE_TYPES else
                                               "DOUBLE" if kind == "NUMBER" else kind for kind in kinds),
                          "BOOL", procedure.implementation, procedure.max_value_bytes)


def _procedure_value(checker: ScalarFunction, value: object, kind: str, entity_resolver: Callable[[object], object] | None = None) -> object:
    if kind in EXTENDED_PROCEDURE_TYPES:
        return owned_procedure_value(value, kind, max_bytes=checker.max_value_bytes, procedure=checker.name,
                                     entity_resolver=entity_resolver)
    if kind == "NUMBER":
        if type(value) is DecimalValue:
            return owned_procedure_value(value, "DECIMAL", max_bytes=checker.max_value_bytes, procedure=checker.name)
        checker._check(value, "INT64" if type(value) is int else "DOUBLE")
    elif kind == "DOUBLE" and type(value) is int:
        # Validate native integer range before any potentially lossy binary64 conversion.
        checker._check(value, "INT64")
        value = float(value)
        checker._check(value, "DOUBLE")
    else:
        checker._check(value, kind)
    return value


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
    """Trusted typed callback with explicit read/write mode and named permissions.

    Read callbacks are pure unless graph_read opts into a permissioned reader. CALL never
    imports code, grants filesystem/network access, or makes callback side effects transactional.
    NULL cells are allowed. NUMBER preserves native int/float/DECIMAL cells; DOUBLE widens
    native INT64 arguments/results to binary64. Other scalar types remain exact.
    Native temporal/DECIMAL/vector and LIST/MAP/ANY values are validated and detached recursively.
    An empty columns tuple declares a unit callback, which must return exactly None.
    schema_write=True additionally requires write mode and the literal schema permission;
    it grants ProcedureWriter.schema() and implicit CREATE/MERGE schema, never COMMIT.
    max_schema_statements counts explicit/implicit schema operations (default 32, 1..1024)
    across same-name invocations in one outer statement, also charging max_write_statements.
    Every ancestor must hold schema_write authority; descendants cannot escalate it.
    """

    name: str
    argument_types: tuple[str, ...]
    columns: tuple[tuple[str, str], ...]
    implementation: Callable[..., Iterable[tuple[object, ...]] | None]
    required_permissions: frozenset[str] = frozenset()
    max_rows: int = 10_000
    max_result_bytes: int = 8 * 1024 * 1024
    max_value_bytes: int = 1024 * 1024
    argument_names: tuple[str, ...] | None = None
    mode: str = "read"
    max_write_statements: int = 128
    graph_read: bool = False
    max_query_statements: int = 128
    max_query_rows: int = 10_000
    max_query_bytes: int = 8 * 1024 * 1024
    max_call_depth: int = 8
    deterministic: bool = False
    schema_write: bool = False
    max_schema_statements: int = 32

    def __post_init__(self) -> None:
        """Validate the complete immutable signature and budgets before registration."""
        _procedure_checker(self)
        if (type(self.required_permissions) is not frozenset
                or any(type(item) is not str or not is_identifier(item) for item in self.required_permissions)):
            raise GrafxConfigurationError("Permissions must be an immutable set of names.", field="required_permissions")
        if type(self.mode) is not str or self.mode not in ("read", "write"):
            raise GrafxConfigurationError("Procedure mode must be read or write.", field="mode")
        if type(self.deterministic) is not bool or (self.deterministic and self.mode == "write"):
            raise GrafxConfigurationError("Only read procedures may declare deterministic=True.", field="deterministic")
        if (type(self.schema_write) is not bool or (self.schema_write and
                (self.mode != "write" or "schema" not in self.required_permissions))):
            raise GrafxConfigurationError("Schema authority requires write mode and explicit schema permission.", field="schema_write")
        if (type(self.max_write_statements) is not int
                or not 1 <= self.max_write_statements <= 1024):
            raise GrafxConfigurationError("Procedure write statement budget must be 1..1024.", field="max_write_statements")
        if self.mode == "write" and not self.required_permissions:
            raise GrafxConfigurationError("Writing procedures require explicit named permissions.", field="required_permissions")
        if type(self.graph_read) is not bool or (self.graph_read and self.mode != "read"):
            raise GrafxConfigurationError("graph_read is a read-mode boolean opt-in.", field="graph_read")
        if self.graph_read and not self.required_permissions:
            raise GrafxConfigurationError("Graph-reading procedures require explicit named permissions.", field="required_permissions")
        for name, maximum in (("max_query_statements", 1024), ("max_query_rows", 2**31), ("max_query_bytes", 2**31), ("max_call_depth", 16), ("max_schema_statements", 1024)):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= maximum:
                raise GrafxConfigurationError("Invalid procedure query budget.", field=name)
        if self.argument_names is not None and (
            type(self.argument_names) is not tuple
            or len(self.argument_names) != len(self.argument_types)
            or any(type(name) is not str or not is_identifier(name) for name in self.argument_names)
            or len(set(self.argument_names)) != len(self.argument_names)
        ):
            raise GrafxConfigurationError("Argument names must uniquely name each positional argument.", field="argument_names")
        if (type(self.columns) is not tuple or not 0 <= len(self.columns) <= 64
                or any(type(item) is not tuple or len(item) != 2
                       or type(item[0]) is not str or not is_identifier(item[0])
                       or type(item[1]) is not str or item[1] not in _PROCEDURE_TYPES for item in self.columns)
                or len({item[0] for item in self.columns}) != len(self.columns)):
            raise GrafxConfigurationError("Invalid tabular output schema.", field="columns")
        for field_name in ("max_rows", "max_result_bytes"):
            if type(getattr(self, field_name)) is not int or not 1 <= getattr(self, field_name) <= 2**31:
                raise GrafxConfigurationError("Invalid procedure budget.", field=field_name)

    def invoke(self, arguments: tuple[object, ...], *, writer: ProcedureWriter | None = None,
               entity_resolver: Callable[[object], object] | None = None,
               reader: ProcedureReader | None = None) -> Iterator[tuple[object, ...]]:
        """Validate inputs/results and close the callback stream on exhaustion or cancellation."""
        checker = _procedure_checker(self)
        if ((self.mode == "write" and type(writer) is not ProcedureWriter)
                or (self.mode == "read" and writer is not None)
                or (self.graph_read and type(reader) is not ProcedureReader)
                or (not self.graph_read and reader is not None)):
            raise GrafxPlanError("Procedure invocation needs exactly its declared write authority.", field="procedure_authority")
        if type(arguments) is not tuple or len(arguments) != len(self.argument_types):
            raise GrafxPlanError("Procedure argument count mismatch.", field="procedure_arity", procedure=self.name)
        arguments = tuple(_procedure_value(checker, value, kind, entity_resolver)
                          for value, kind in zip(arguments, self.argument_types, strict=True))
        stream = None
        primary_failure: BaseException | None = None
        try:
            authority = writer if writer is not None else reader
            result = (self.implementation(authority, *arguments) if authority is not None
                      else self.implementation(*arguments))
            if not self.columns:
                if result is not None:
                    raise GrafxPlanError("Unit procedure must return None.", field="procedure_result", procedure=self.name)
                return
            stream = iter(result)
            used = 0
            for count, row in enumerate(stream, start=1):
                if count > self.max_rows:
                    raise GrafxQueryBudgetExceeded("Procedure row budget exceeded.", resource="procedure_rows")
                if type(row) is not tuple or len(row) != len(self.columns):
                    raise GrafxPlanError("Procedure returned the wrong row shape.", field="procedure_result", procedure=self.name)
                row = tuple(_procedure_value(checker, value, kind, entity_resolver)
                            for value, (_column, kind) in zip(row, self.columns, strict=True))
                for value in row:
                    used += procedure_result_size(value)
                if used > self.max_result_bytes:
                    raise GrafxQueryBudgetExceeded("Procedure result byte budget exceeded.", resource="procedure_bytes")
                yield row
        except GrafxError as failure:
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
