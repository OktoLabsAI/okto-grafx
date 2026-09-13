"""Bounded single SQLite SELECT snapshot, closed before atomic Grafx staging."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import os
import sqlite3

from okto_grafx.domain.model.value import encode_value
from okto_grafx.domain.model.stored_types import StoredType
from okto_grafx.domain.query.control import CancellationToken
from okto_grafx.engine.database import Transaction, ExecuteManyReport
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxQueryBudgetExceeded,
    GrafxUnsupportedOperation,
)
from okto_grafx.parquet import _path, _unchanged_root
from okto_grafx.projections import _Work
from okto_grafx.text_import import TextImportLimits, _declarations, _value

__all__ = ["SQLiteImportLimits", "read_sqlite_rows", "import_sqlite"]


@dataclass(frozen=True, slots=True)
class SQLiteImportLimits:
    """Logical input bounds, including SQLite VM instructions; not a process RSS promise."""

    max_rows: int = 10000
    max_bytes: int = 16 * 1024 * 1024
    max_field_bytes: int = 65536
    max_work: int = 1_000_000

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if (
                type(getattr(self, name)) is not int
                or not 0 < getattr(self, name) <= 2**31
            ):
                raise GrafxConfigurationError(
                    "Invalid SQLite import bound.", field=name
                )
        if self.max_field_bytes > 65536:
            raise GrafxConfigurationError(
                "max_field_bytes must be <=65536.", field="max_field_bytes"
            )


def read_sqlite_rows(
    path: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str],
    query: str,
    columns: tuple[str, ...],
    types: tuple[str | StoredType, ...],
    parameters: tuple = (),
    limits: SQLiteImportLimits = SQLiteImportLimits(),
    cancellation: CancellationToken | None = None,
) -> tuple[dict[str, object], ...]:
    """Read one bounded SELECT; SQL NULL stays None; source closes before return.

    Explicit temporal/DECIMAL types decode canonical tagged JSON from TEXT,
    preserving coordinates and p/s without inference, float casts or zone lookup.
    StoredType collections require TEXT containing exact collection JSON or SQL NULL.
    """
    if type(limits) is not SQLiteImportLimits:
        raise GrafxConfigurationError("Expected SQLiteImportLimits.", field="limits")
    conversion = TextImportLimits(max_field_bytes=limits.max_field_bytes)
    types = _declarations(columns, types, conversion)
    if type(query) is not str or not query or len(query) > 65536:
        raise GrafxConfigurationError("Declare bounded SQL query.", field="query")
    if (
        type(parameters) is not tuple
        or len(parameters) > 256
        or any(type(p) not in (str, bytes, int, float, type(None)) for p in parameters)
    ):
        raise GrafxConfigurationError(
            "Expected tuple of SQLite scalar parameters.", field="parameters"
        )
    work = _Work(limits.max_work, cancellation)
    for value in parameters:
        if (
            type(value) in (str, bytes)
            and len(value.encode("utf-8") if type(value) is str else value)
            > limits.max_field_bytes
        ):
            raise GrafxQueryBudgetExceeded(
                "SQLite parameter bound exceeded.", resource="sqlite_parameter"
            )
    target, root, identity = _path(path, allowed_root)
    if not target.is_file():
        raise GrafxConfigurationError("SQLite source does not exist.", field="path")
    _unchanged_root(root, identity)
    connection = None
    interrupted = None

    def progress() -> int:
        """Translate cooperative cancellation or budget exhaustion to SQLite interruption."""
        nonlocal interrupted
        try:
            work.step(100)
        except Exception as exc:
            interrupted = exc
            return 1
        return 0

    def authorize(action: int, arg1: str | None, arg2: str | None,
                  database: str | None, trigger: str | None) -> int:
        """Allow only the bounded read-only SQLite query surface."""
        allowed = (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE)
        if action == sqlite3.SQLITE_FUNCTION:
            # No extension, file, randomblob/zeroblob, or host callbacks.
            return (
                sqlite3.SQLITE_OK
                if (arg2 or "").lower()
                in (
                    "count",
                    "min",
                    "max",
                    "sum",
                    "avg",
                    "coalesce",
                    "ifnull",
                    "lower",
                    "upper",
                    "trim",
                    "abs",
                    "length",
                )
                else sqlite3.SQLITE_DENY
            )
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    try:
        connection = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=1)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(limits.max_bytes, 2**30))
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65536)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 256)
        connection.set_authorizer(authorize)
        connection.set_progress_handler(progress, 100)
        cursor = connection.execute(query, parameters)
        if (
            cursor.description is None
            or tuple(c[0] for c in cursor.description) != columns
        ):
            raise GrafxConfigurationError(
                "SELECT columns must exactly match declarations.", field="columns"
            )
        rows, size = [], 0
        for number, values in enumerate(cursor, 1):
            work.step()
            if number > limits.max_rows:
                raise GrafxQueryBudgetExceeded(
                    "SQLite row bound exceeded.", resource="sqlite_rows"
                )
            row = {}
            for name, kind, value in zip(columns, types, values, strict=True):
                work.step()
                if (
                    type(value) in (str, bytes)
                    and len(value.encode("utf-8") if type(value) is str else value)
                    > limits.max_field_bytes
                ):
                    raise GrafxQueryBudgetExceeded(
                        "SQLite field bound exceeded.", resource="sqlite_field"
                    )
                if kind == "BYTES" and value is not None:
                    if type(value) is not bytes:
                        raise GrafxUnsupportedOperation(
                            "SQLite BYTES requires BLOB.", field="types"
                        )
                else:
                    if type(kind) is StoredType and value is not None and type(value) is not str:
                        raise GrafxUnsupportedOperation("SQLite collections require JSON TEXT.", field="types", row=number, column=name)
                    if kind == "BOOL" and type(value) is int and value in (0, 1):
                        value = bool(value)
                    value = _value(value, kind, False, number, name, conversion)
                encoded_size = len(encode_value(value))
                if type(kind) is StoredType:
                    work.step(encoded_size)
                size += encoded_size + len(name.encode("utf-8")) + 64
                if size > limits.max_bytes:
                    raise GrafxQueryBudgetExceeded(
                        "SQLite byte bound exceeded.", resource="sqlite_bytes"
                    )
                row[name] = value
            rows.append(row)
        work.step(0)
        _unchanged_root(root, identity)
        return tuple(rows)
    except sqlite3.Error as exc:
        if interrupted is not None:
            raise interrupted from exc
        raise GrafxUnsupportedOperation(
            "SQLite read refused or failed.", field="sqlite_query"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def import_sqlite(
    transaction: Transaction,
    statement: str,
    path: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str],
    query: str,
    columns: tuple[str, ...],
    types: tuple[str | StoredType, ...],
    parameters: tuple = (),
    limits: SQLiteImportLimits = SQLiteImportLimits(),
    cancellation: CancellationToken | None = None,
) -> ExecuteManyReport:
    """Atomically stage the complete bounded selection; caller owns Grafx commit/rollback."""
    if type(transaction) is not Transaction:
        raise GrafxConfigurationError(
            "Expected native transaction.", field="transaction"
        )
    rows = read_sqlite_rows(
        path,
        allowed_root=allowed_root,
        query=query,
        columns=columns,
        types=types,
        parameters=parameters,
        limits=limits,
        cancellation=cancellation,
    )
    work = _Work(limits.max_work, cancellation)

    def controlled_rows() -> Iterator[dict[str, object]]:
        """Charge each source row before passing it to native atomic staging."""
        for row in rows:
            work.step()
            yield row

    return transaction.executemany(statement, controlled_rows())
