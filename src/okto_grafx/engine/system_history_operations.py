"""Explicit operator controls for native system history, never autonomous retention."""

from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from okto_grafx.engine.database import Database
    from okto_grafx.engine.commit_catalog_store import CommitCatalogStore

__all__ = ["control", "pins"]

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.temporal import TemporalPin, TemporalPruneReport
from okto_grafx.domain.txn.commit_identity import CommitId


def control(database: Database, *, operation: str, before: CommitId | None = None,
            tables: tuple[str, ...] = (), name: str | None = None,
            max_bytes: int = 16 * 1024 * 1024) -> TemporalPruneReport | None:
    """Validate inputs before admission and delegate publication to the native coordinator."""
    if operation != "unpin system history":
        before = database._history_identity(before)
        if (type(tables) is not tuple or not tables or any(type(table) is not str or not table for table in tables)
                or len(set(tables)) != len(tables)):
            raise GrafxConfigurationError("Specify distinct history tables.", field="tables")
    if operation != "prune system history":
        try:
            valid_name = type(name) is str and bool(name) and len(name.encode("utf-8")) <= 128
        except UnicodeEncodeError:
            valid_name = False
        if not valid_name:
            raise GrafxConfigurationError("History pin names require 1..128 UTF-8 bytes.", field="name")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 2**31:
        raise GrafxConfigurationError("Invalid retention byte budget.", field="max_bytes")
    with database._public_operation(operation):
        database._require_writable(operation)
        with database.begin("write") as tx:
            plan = database._transactions._history_publication.stage_control(
                tx._context, operation=operation, names=tables, before=before, pin_name=name, max_bytes=max_bytes)
        if operation == "prune system history":
            return TemporalPruneReport(before, tables,
                None if plan is None else CommitId(database.identity.database_uuid, tx.report.csn),
                0 if plan is None else plan.redacted_versions, 0 if plan is None else plan.redacted_bytes)
    return None


def pins(database: Database) -> tuple[TemporalPin, ...]:
    """Read durable pin inventory under the same publication observation as the journal."""
    with database._public_operation("system history pins"):
        with database.begin("read") as tx:
            def collect(_journal: CommitCatalogStore) -> tuple[TemporalPin, ...]:
                """Capture detached pin values inside the journal's qualified observation."""
                catalog = database._catalog.catalog
                return tuple(TemporalPin(name, CommitId(database.identity.database_uuid, sequence),
                             tuple(catalog.table_by_id(key).name for key in tables))
                             for name, sequence, tables in catalog.system_history_pins())
            return database._observe_history(tx._context, collect, ())
