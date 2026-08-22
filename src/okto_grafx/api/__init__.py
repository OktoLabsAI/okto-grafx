"""The public facade of Okto Grafx (CONTRACT.md section 10, C11).

Everything an application needs is here and is re-exported from :mod:`okto_grafx` itself, so the
supported import is the short one::

    from okto_grafx import connect, Database, Transaction

    db = connect("./mydb", partitions_per_table=64)      # or ":memory:"
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    db.close()

This layer is deliberately thin. It turns keyword arguments into a validated
:class:`okto_grafx.runtime.config.DatabaseConfig`, hands that to the composition root, and gets
back an open :class:`okto_grafx.engine.database.Database`. It holds no state of its own, so there
is nothing here for two databases in one process to share (SPEC-M1 FR-13, BR-8).
"""

from __future__ import annotations

import os
from dataclasses import fields

from okto_grafx.api.assembly import assemble_database, database_label
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.engine.database import Database, DatabaseIdentity, Transaction
from okto_grafx.engine.query_engine import QueryResult
from okto_grafx.runtime.bootstrap import build_default_registry, open_database
from okto_grafx.runtime.config import DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry

__all__ = [
    "Database",
    "DatabaseConfig",
    "DatabaseIdentity",
    "PortRegistry",
    "QueryResult",
    "Transaction",
    "assemble_database",
    "build_default_registry",
    "connect",
    "open_database",
]


def connect(
    path: str | os.PathLike[str],
    *,
    registry: PortRegistry | None = None,
    **options: object,
) -> Database:
    """Open the database at ``path``, creating it when it does not exist yet (SPEC-M1 FR-1).

    ``path`` is a directory -- a string or any ``os.PathLike``, because a caller holding a
    ``pathlib.Path`` should not have to spell ``str()`` at the one door of the package -- or
    ``":memory:"`` for an in-memory database with exactly the same transactional semantics.
    Every keyword after it is a field of
    :class:`okto_grafx.runtime.config.DatabaseConfig` -- ``page_size``, ``partitions_per_table``,
    ``buffer_budget_bytes``, ``recovery_policy``, ``metrics``, ``vector_math``, ``read_only`` and
    the rest -- and an unknown one is refused by name rather than ignored, because a mistyped
    option that is silently dropped is a configuration a caller believes it applied.

    ``registry`` composes the database over adapters the caller built. The default is None, which
    builds the adapters the configuration selects; either way every required port slot is
    verified before anything is opened (G5). Ports the caller supplied are NOT closed when the
    database is closed: they belong to whoever built them.

    Reopening an existing database runs recovery before it accepts a transaction (FR-8). The
    returned object is a context manager, and closing it releases the lease, the reader
    registrations and everything else this call opened.
    """
    config = _configure(path, options)
    return open_database(config, registry=registry)


def _configure(path: str | os.PathLike[str], options: dict[str, object]) -> DatabaseConfig:
    """Build the configuration for a connect call, refusing an option that does not exist.

    ``DatabaseConfig`` is a slotted frozen dataclass, so an unknown keyword would raise a bare
    ``TypeError`` naming the constructor rather than the caller's mistake. Checking the field
    names here turns that into a GrafxConfigurationError that names the option and lists the ones
    that exist, which is the difference between a typed refusal and a stack trace (section 2).
    """
    unknown = sorted(name for name in options if name not in _CONFIG_FIELDS)
    if unknown:
        raise _unknown_options(unknown)
    return DatabaseConfig(path=_as_path_string(path), **options)  # type: ignore[arg-type]


def _as_path_string(path: str | os.PathLike[str]) -> str:
    """Return the path as the string the configuration stores, refusing anything else.

    ``os.fspath`` raises a bare ``TypeError`` for an object that is neither, and a bare TypeError
    out of the front door of the package is exactly what section 2 and DoD item 5 forbid.
    """
    if isinstance(path, str):
        return path
    try:
        return os.fspath(path)
    except TypeError as failure:
        raise GrafxConfigurationError(
            f"A database path is a string or an os.PathLike; got {type(path).__name__}.",
            field="path",
            value=type(path).__name__,
        ) from failure


def _unknown_options(unknown: list[str]) -> GrafxConfigurationError:
    """Return the refusal for one or more option names that are not configuration fields."""
    known = ", ".join(sorted(_CONFIG_FIELDS))
    return GrafxConfigurationError(
        f"Unknown connection option(s): {', '.join(unknown)}. The options are: {known}.",
        field="options",
        value=unknown,
    )


def _config_fields() -> frozenset[str]:
    """Return the names a caller may pass to :func:`connect`, taken from the dataclass itself.

    Read from ``DatabaseConfig`` rather than written out again: a second list of field names is a
    second definition of one thing, and it stops agreeing the first time C0 adds a field (A24).
    ``path`` is excluded because :func:`connect` takes it positionally.
    """
    return frozenset(field.name for field in fields(DatabaseConfig) if field.name != "path")


_CONFIG_FIELDS: frozenset[str] = _config_fields()
"""Every keyword :func:`connect` accepts, derived from DatabaseConfig and never re-listed."""
