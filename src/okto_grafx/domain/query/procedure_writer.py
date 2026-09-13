"""Short-lived native query/write authority, never a database or transaction handle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxTransactionStateError

__all__ = ["ProcedureResult", "ProcedureReader", "ProcedureWriter"]


@dataclass(frozen=True, slots=True)
class ProcedureResult:
    """Bounded columns and owned result rows from a transaction-scoped native query."""

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


class ProcedureReader:
    """Query the caller's snapshot without commit, store switching or write authority."""

    __slots__ = ("_query_operation", "_identity", "_owner", "_active", "_failure", "_busy")

    def __init__(self, operation: Callable[[str, Mapping[str, object] | None], ProcedureResult] | None,
                 identity: Callable[[], object]) -> None:
        """Bind an engine-issued query operation to the synchronous callback owner."""
        self._query_operation = operation
        self._identity = identity
        self._owner = identity()
        self._active = True
        self._failure: BaseException | None = None
        self._busy = False

    def query(self, query: str, parameters: Mapping[str, object] | None = None) -> ProcedureResult:
        """Return bounded native query results; a refused operation poisons the invocation."""
        return self._perform(self._query_operation, query, parameters)

    def _perform(self, operation: Callable[[str, Mapping[str, object] | None], object] | None,
                 query: str, parameters: Mapping[str, object] | None) -> object:
        """Run one owned operation with shared expiry, thread and poison checks."""
        if not self._active or self._identity() != self._owner:
            raise GrafxTransactionStateError("Procedure authority is expired or belongs to another execution owner.",
                                             field="procedure_authority")
        self._check()
        if self._busy or operation is None:
            self._failure = GrafxTransactionStateError("Procedure operation is unavailable or not reentrant.",
                                                        field="procedure_authority")
            raise self._failure
        self._busy = True
        try:
            return operation(query, parameters)
        except BaseException as failure:
            self._failure = failure
            raise
        finally:
            self._busy = False

    def _check(self) -> None:
        """Re-raise a failed operation even if trusted host code caught it."""
        if self._failure is not None:
            raise self._failure

    def _expire(self) -> None:
        """Revoke query authority and release its engine closure on every exit."""
        self._active = False
        self._query_operation = None
        self._failure = None
        self._identity = None
        self._owner = None


class ProcedureWriter(ProcedureReader):
    """Execute native graph mutations in the calling statement; never commit independently.

    Instances are supplied as the first argument of a registered writing callback.
    They expire after its complete output stream closes. Host code is trusted, not
    sandboxed; this object only restricts the authority supplied by Grafx itself.
    """

    __slots__ = ("_operation", "_schema_operation")

    def __init__(self, operation: Callable[[str, Mapping[str, object] | None], None],
                 identity: Callable[[], object], *,
                 query_operation: Callable[[str, Mapping[str, object] | None], ProcedureResult] | None = None,
                 schema_operation: Callable[[str, Mapping[str, object] | None], None] | None = None) -> None:
        """Bind an engine-issued operation to its current synchronous execution owner."""
        self._operation = operation
        self._schema_operation = schema_operation
        super().__init__(query_operation, identity)

    def execute(self, query: str, parameters: Mapping[str, object] | None = None) -> None:
        """Run one result-free native mutation; any refused operation poisons this invocation."""
        self._perform(self._operation, query, parameters)

    def schema(self, query: str) -> None:
        """Stage authorized native DDL under the caller's schema journal and rollback."""
        self._perform(self._schema_operation, query, None)

    def _expire(self) -> None:
        """Revoke authority and release the engine closure on every stream exit."""
        super()._expire()
        self._operation = None
        self._schema_operation = None
