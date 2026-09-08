"""Offline, crash-safe migrations of the small control-record envelope."""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.adapters.control_record_io import read_control_if_exists
from okto_grafx.adapters.coordination_local import (
    LEASE_SECTION,
    decode_lease_record,
)
from okto_grafx.api import assembly
from okto_grafx.domain.control_record import (
    ControlRecordKind,
    TwoSlotControlRecordStore,
)
from okto_grafx.domain.errors import (
    GrafxSchemaVersionMismatch,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.database import META_FILE, DatabaseIdentity, MetaStore
from okto_grafx.engine.txn_manager import COMMIT_SECTION
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports
from okto_grafx.runtime.config import DatabaseConfig

__all__ = ["ControlDowngradeResult", "downgrade_control_format"]

_LEASE_FILE = "control/writer.lease"
_READERS_PREFIX = "control/readers/"


@dataclass(frozen=True, slots=True)
class ControlDowngradeResult:
    """The exact state transition completed by an offline downgrade."""

    previous_format: int
    current_format: int
    converted_files: tuple[str, ...]
    already_current: bool = False


def _legacy_publish(storage: object, name: str, payload: bytes) -> None:
    """Publish one legacy payload with the original whole-file durability protocol."""
    temporary = f"{name}.control-downgrade.tmp"
    if storage.exists(temporary):  # type: ignore[attr-defined]
        storage.remove(temporary)  # type: ignore[attr-defined]
    storage.create(temporary, exclusive=True)  # type: ignore[attr-defined]
    terminal = storage.append_log(temporary, payload)  # type: ignore[attr-defined]
    if terminal != len(payload):
        raise GrafxUnsupportedOperation(
            "The legacy control payload was not staged in full; the downgrade stopped.",
            file=temporary,
            field="terminal_offset",
            value=terminal,
            expected=len(payload),
        )
    storage.durable_barrier(temporary)  # type: ignore[attr-defined]
    storage.atomic_replace(temporary, name)  # type: ignore[attr-defined]
    storage.durable_barrier(name)  # type: ignore[attr-defined]


def _slot_payload(
    storage: object,
    *,
    identity: DatabaseIdentity,
    file: str,
    kind: int,
) -> bytes | None:
    """Return the newest logical payload regardless of whether the current file is v1 or v2."""
    record = TwoSlotControlRecordStore(
        storage,  # type: ignore[arg-type]
        read_if_exists=read_control_if_exists,
        file=file,
        record_kind=kind,
        database_uuid=identity.database_uuid,
        # Existing v2 files carry their own nonce in page zero.  This value is used only if a
        # missing/legacy file were published, which this read-only helper never does.
        file_nonce=0,
        temporary=f"{file}.control-downgrade-read.tmp",
    ).read()
    return None if record is None else record.payload


def _downgraded_identity(identity: DatabaseIdentity) -> DatabaseIdentity:
    return DatabaseIdentity(
        database_uuid=identity.database_uuid,
        page_size=identity.page_size,
        partitions_per_table=identity.partitions_per_table,
        created_at_wall=identity.created_at_wall,
        granularity_descriptor=identity.granularity_descriptor,
        format_version=1,
    )


def downgrade_control_format(config: DatabaseConfig) -> ControlDowngradeResult:
    """Convert format-2 lease/commit records back to format 1 under both writer sections.

    This is deliberately an offline operator door.  It refuses every active lease and every
    reader registration; callers must also ensure no idle process retains the database and can
    begin new work after these checks.  The v2 meta fence remains durable until all logical
    payloads and the completion marker are v1, so an old build never observes mixed control
    files as a format-1 database.
    """
    if config.read_only:
        raise GrafxUnsupportedOperation(
            "A control-format downgrade writes durable metadata and cannot run read-only.",
            field="read_only",
        )
    registry = build_default_registry(config)
    try:
        storage = registry.get("storage")
        coordinator = registry.get("coordinator")
        pool = BufferPool(
            storage,  # type: ignore[arg-type]
            registry.get("codec"),  # type: ignore[arg-type]
            registry.get("metrics"),  # type: ignore[arg-type]
            budget_bytes=config.buffer_budget_bytes,
            db_label=assembly.database_label(config.path),
        )
        with coordinator.exclusive(  # type: ignore[attr-defined]
            COMMIT_SECTION, timeout=config.commit_lock_timeout_seconds
        ):
            with coordinator.exclusive(  # type: ignore[attr-defined]
                LEASE_SECTION, timeout=config.commit_lock_timeout_seconds
            ):
                identity = MetaStore(pool).read()
                if identity.format_version == 1:
                    return ControlDowngradeResult(1, 1, (), already_current=True)
                if identity.format_version != 2:
                    raise GrafxSchemaVersionMismatch(
                        "This build can downgrade only control format 2 to format 1.",
                        field="format_version",
                        value=identity.format_version,
                        supported=2,
                    )

                lease = _slot_payload(
                    storage,
                    identity=identity,
                    file=_LEASE_FILE,
                    kind=ControlRecordKind.LEASE,
                )
                if lease is not None and decode_lease_record(
                    lease, file=_LEASE_FILE
                ).held:
                    raise GrafxUnsupportedOperation(
                        "The writer lease is active; the offline control downgrade changed nothing.",
                        field="writer_lease",
                        state="active",
                    )
                readers = tuple(storage.list_files(_READERS_PREFIX))  # type: ignore[attr-defined]
                if readers:
                    raise GrafxUnsupportedOperation(
                        "Reader registrations still exist; the offline control downgrade changed nothing.",
                        field="reader_registrations",
                        state="present",
                        files=readers,
                    )

                complete = assembly._read_first_open_complete(storage)
                if complete != identity:
                    raise GrafxUnsupportedOperation(
                        "The first-open completion identity does not match grafx.meta; the "
                        "downgrade changed nothing.",
                        file=assembly._FIRST_OPEN_COMPLETE,
                        field="identity",
                        state="mismatch",
                    )

                payloads = (
                    (_LEASE_FILE, lease),
                    (
                        COMMIT_STATE_FILE,
                        _slot_payload(
                            storage,
                            identity=identity,
                            file=COMMIT_STATE_FILE,
                            kind=ControlRecordKind.COMMIT_STATE,
                        ),
                    ),
                )
                converted: list[str] = []
                for name, payload in payloads:
                    if payload is not None:
                        _legacy_publish(storage, name, payload)
                        converted.append(name)

                downgraded = _downgraded_identity(identity)
                # Completion moves first.  A crash here leaves meta=v2/complete=v1, which the
                # current build already treats as a resumable upgrade; old builds still refuse
                # on meta v2.  Publishing meta v1 last commits the downgrade.
                assembly._replace_first_open_complete(storage, downgraded)
                assembly._stage_meta(storage, pool, downgraded)
                storage.atomic_replace(assembly._FIRST_OPEN_META_STAGING, META_FILE)  # type: ignore[attr-defined]
                storage.durable_barrier(META_FILE)  # type: ignore[attr-defined]
                pool.invalidate(META_FILE)
                pool.invalidate(assembly._FIRST_OPEN_META_STAGING)
                return ControlDowngradeResult(
                    previous_format=2,
                    current_format=1,
                    converted_files=tuple(converted),
                )
    finally:
        release_ports(registry)
