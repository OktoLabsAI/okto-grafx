"""Conservative orphan-file reclamation under explicit whole-store quiescence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxUnsupportedOperation
from okto_grafx.domain.index.definition import index_file
from okto_grafx.domain.index.records import IndexChange
from okto_grafx.domain.txn.records import decode_page_write_location
from okto_grafx.domain.wal.record import WalRecordType
from okto_grafx.engine.index_manager import IndexManager

if TYPE_CHECKING:
    from okto_grafx.engine.database import Database

__all__ = ["IndexCleanupFile", "IndexCleanupReport"]


@dataclass(frozen=True, slots=True)
class IndexCleanupFile:
    """One inventoried index artifact and the reason it is retained or removable."""

    file: str
    bytes: int
    reason: str


@dataclass(frozen=True, slots=True)
class IndexCleanupReport:
    """A bounded census; removed/deferred names describe only this cleanup call."""

    dry_run: bool
    files: tuple[IndexCleanupFile, ...]
    candidate_bytes: int
    removed: tuple[str, ...]
    deferred: tuple[str, ...]
    wal_records_examined: int


def _known_orphan_name(file: str) -> bool:
    """Admit only native immutable-generation or displaced-artifact spellings."""
    if file.startswith("index/g_") and file.endswith(".idx"):
        nonce = file[len("index/g_") : -4]
        return (
            len(nonce) == 16
            and all(c in "0123456789abcdef" for c in nonce)
            and int(nonce, 16) != 0
        )
    if file.startswith("index_orphan/") and file.endswith(".idx"):
        pieces = file[len("index_orphan/") : -4].split(".")
        return (
            len(pieces) in (1, 2)
            and len(pieces[0]) == 32
            and all(c in "0123456789abcdef" for c in pieces[0])
            and (
                len(pieces) == 1
                or (
                    len(pieces[1]) == 4
                    and all(c in "0123456789abcdef" for c in pieces[1])
                )
            )
        )
    return False


def _cleanup_indexes(
    database: Database,
    *,
    dry_run: bool,
    confirm_quiescent: bool,
    max_files: int,
    max_wal_records: int,
) -> IndexCleanupReport:
    """Prove the whole candidate set before any deletion; never publish catalog changes."""
    if type(dry_run) is not bool:
        raise GrafxConfigurationError("dry_run must be a boolean.", field="dry_run")
    for name, value in (("max_files", max_files), ("max_wal_records", max_wal_records)):
        if type(value) is not int or not 0 < value <= 1_000_000:
            raise GrafxConfigurationError(
                "Cleanup limits must be positive integers at most 1000000.", field=name
            )
    with database._public_operation("index cleanup"):
        with database._transactions.quiescent_maintenance_section(
            confirm_quiescent=confirm_quiescent, operation="cleanup_indexes"
        ):
            with database._transactions.schema_artifact_section():
                storage = database._storage
                files = tuple(
                    sorted(
                        (
                            *storage.list_files("index/"),
                            *storage.list_files("index_orphan/"),
                        )
                    )
                )
                if len(files) > max_files:
                    raise GrafxUnsupportedOperation(
                        "Index inventory exceeds max_files; no artifact was removed.",
                        field="max_files",
                        observed=len(files),
                        limit=max_files,
                    )
                catalog = database._catalog.catalog
                protected = {
                    definition.file.casefold()
                    for definition in catalog.active_index_definitions()
                }
                # STALE and BUILDING are ownership, not garbage. Removing these requires a
                # future catalog-retirement protocol, not an orphan-file heuristic.
                for logical in catalog.index_definitions():
                    protected.update(
                        generation.file.casefold() for generation in logical.generations
                    )
                manager = database._indexes
                if manager is not None:
                    if not isinstance(manager, IndexManager):
                        raise GrafxUnsupportedOperation(
                            "Cleanup requires the native index manager.",
                            field="indexes",
                        )
                    protected.update(
                        index.file.casefold() for index in manager.indexes()
                    )
                wal_files: set[str] = set()
                logical_files = {
                    definition.registry_key: {
                        generation.file.casefold()
                        for generation in definition.generations
                    }
                    for definition in catalog.index_definitions()
                }
                for definition in catalog.active_index_definitions():
                    logical_files.setdefault(definition.registry_key, set()).add(
                        definition.file.casefold()
                    )
                unresolved_logical_wal = False
                retained_catalog_wal = False
                examined = 0
                # Include uncommitted effects and records <= checkpoint too. A retained record
                # is never dismissed just because the current replay would skip its effect.
                for record in database._wal.read_from(1):
                    examined += 1
                    if examined > max_wal_records:
                        raise GrafxUnsupportedOperation(
                            "Retained WAL exceeds max_wal_records; no artifact was removed.",
                            field="max_wal_records",
                            limit=max_wal_records,
                        )
                    if record.record_type == int(WalRecordType.WRITE_PAGE):
                        file = decode_page_write_location(
                            record.payload
                        ).file.casefold()
                        wal_files.add(file)
                        # Older complete catalog images can own generations absent from today's
                        # catalog. Do not guess their dependency set from a partial WAL tail.
                        retained_catalog_wal = (
                            retained_catalog_wal
                            or file == database._catalog.file.casefold()
                        )
                    elif record.record_type in (
                        int(WalRecordType.INDEX_WRITE),
                        int(WalRecordType.INDEX_RECONCILE),
                    ):
                        name = IndexChange.decode(record.payload).index.casefold()
                        wal_files.add(index_file(name).casefold())
                        if name in logical_files:
                            wal_files.update(logical_files[name])
                        else:
                            # A logical name alone cannot identify a detached physical nonce.
                            # Without its catalog mapping, conservatively keep all generations.
                            unresolved_logical_wal = True
                    elif record.record_type not in {
                        int(WalRecordType.BEGIN),
                        int(WalRecordType.COMMIT),
                        int(WalRecordType.ABORT),
                        int(WalRecordType.CHECKPOINT),
                        int(WalRecordType.SEGMENT_HEADER),
                    }:
                        raise GrafxUnsupportedOperation(
                            "Cleanup cannot prove file independence for this retained WAL record type.",
                            field="record_type",
                            value=record.record_type,
                        )
                inventory = tuple(
                    IndexCleanupFile(
                        file,
                        storage.file_size(file),
                        "catalog_or_registered"
                        if file.casefold() in protected
                        else "retained_wal"
                        if file.casefold() in wal_files
                        else "retained_catalog_wal"
                        if retained_catalog_wal and file.startswith("index/g_")
                        else "unresolved_logical_wal"
                        if unresolved_logical_wal and file.startswith("index/g_")
                        else "orphan"
                        if _known_orphan_name(file)
                        else "unrecognized_preserved",
                    )
                    for file in files
                )
                candidates = tuple(
                    item for item in inventory if item.reason == "orphan"
                )
                # Prove no cache holder before discarding even the first speculative frame.
                for item in candidates:
                    database._pool._require_file_unpinned(item.file)
                removed: list[str] = []
                deferred: list[str] = []
                if not dry_run:
                    for item in candidates:
                        database._pool._discard_unowned_file(item.file)
                        (removed if storage.recycle(item.file) else deferred).append(
                            item.file
                        )
                    if candidates:
                        storage.durable_barrier()
                return IndexCleanupReport(
                    dry_run,
                    inventory,
                    sum(item.bytes for item in candidates),
                    tuple(removed),
                    tuple(deferred),
                    examined,
                )
