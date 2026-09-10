"""Explicit operator controls for native system history, never autonomous retention."""

from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from okto_grafx.engine.database import Database
    from okto_grafx.engine.commit_catalog_store import CommitCatalogStore

__all__ = ["compact", "control", "pins"]

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.temporal import TemporalCompactionReport, TemporalPin, TemporalPruneReport
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


def compact(database: Database, *, confirm_quiescent: bool,
            max_bytes: int) -> TemporalCompactionReport:
    """Publish complete native redo before reclaiming an explicitly unreferenced tail.

    Crash before COMMIT keeps the old picture. Crash after COMMIT keeps the new
    qualified logical extent even if the old physical tail remains. Truncation is
    strictly after checkpoint and never determines which history is authoritative.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= 2**31:
        raise GrafxConfigurationError("Invalid compaction capture budget.", field="max_bytes")
    with database._public_operation("compact_system_history"):
        with database._transactions.quiescent_maintenance_section(
                confirm_quiescent=confirm_quiescent, operation="compact system history"):
            from okto_grafx.engine.system_history_store import SystemHistoryStore, _COMPACT_HEAD_MAGIC
            before = database._storage.file_size("system-history.dat") if database._storage.exists("system-history.dat") else 0
            with database.begin("write") as tx:
                plan = database._transactions._history_publication.stage_compaction(tx._context, max_bytes=max_bytes)
            identity = None if plan is None else CommitId(database.identity.database_uuid, tx.report.csn)
            database.checkpoint()
            # Quiescence is still held, no user transaction remains, and the
            # checkpoint has proved/applied the full native history replacement.
            pool = database._pool
            store = SystemHistoryStore(database._storage.read_page,
                database_uuid=database.identity.database_uuid, page_size=pool.page_size)
            head = store._page(0)
            fields = store._head(head.page_lsn, database._storage.page_count("system-history.dat"))
            size = fields[3] * pool.page_size
            actual = database._storage.file_size("system-history.dat")
            if head.read_slot(0)[:8] == _COMPACT_HEAD_MAGIC and size < actual:
                pool.invalidate("system-history.dat")
                database._storage.truncate_log("system-history.dat", size)
                database._storage.durable_barrier("system-history.dat")
            after = database._storage.file_size("system-history.dat")
            return TemporalCompactionReport(identity, before, after, max(0, before - after))


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
