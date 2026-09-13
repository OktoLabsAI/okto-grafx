"""Bounded, checkpoint-consistent local physical backup and offline replacement restore.

These are composition operations, not an alternate storage/recovery engine. Source reads use
the storage port under the existing checkpoint fence. Destination verification uses connect
and verify; no corruption is repaired. Artifacts contain objects, not a writable database.
"""

from __future__ import annotations

import hashlib
import ctypes
import json
import math
import os
import tempfile
import io
from typing import BinaryIO
from time import monotonic
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from okto_grafx.adapters.coordination_local import (
    decode_lease_record,
    encode_lease_record,
)
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.api import connect
from okto_grafx.domain.control_record import (
    ControlRecordKind,
    TwoSlotControlRecordStore,
)
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxRecoveryRefused,
    GrafxUnsupportedOperation,
)
from okto_grafx.engine.database import Database, DatabaseIdentity

__all__ = ["BackupReport", "create_backup", "restore_backup"]

_DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_CHUNK = 1024 * 1024
_MANIFEST_LIMIT = 4 * 1024 * 1024
_BASE_FILES = frozenset(
    {
        "grafx.meta",
        "heap.dat",
        "catalog.dat",
        "commits.dir",
        "commits.dat",
        "system-history.dat",
        "control/commit.state",
        "control/writer.lease",
        "bootstrap/first-open.complete",
    }
)


@dataclass(frozen=True, slots=True)
class BackupReport:
    """Verified physical cut; bytes count database payload, not manifest or temporary copies."""

    destination: str
    database_uuid: str
    checkpoint_lsn: int
    files: int
    bytes: int


def _refuse(message: str, reason: str) -> GrafxRecoveryRefused:
    """Give callers a stable fail-closed operational reason."""
    return GrafxRecoveryRefused(message, operation="physical_backup", reason=reason)


def _budget(value: int) -> int:
    """Reject unbounded/forged byte budgets before touching a destination."""
    if type(value) is not int or value <= 0:
        raise GrafxConfigurationError(
            "max_bytes must be a positive integer.", field="max_bytes"
        )
    return value


def _logical_file(name: object) -> bool:
    """Only canonical database payloads, never path traversal or live reader/lock state."""
    if type(name) is not str or not name or "\\" in name or ":" in name:
        return False
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or any(p in {".", ".."} for p in path.parts)
    ):
        return False
    return name in _BASE_FILES or (
        len(path.parts) == 2
        and path.parts[0] in {"index", "wal"}
        and not path.parts[1].endswith((".tmp", ".lock", ".staging"))
    )


def _destination(path: str | os.PathLike[str], *, source: Path) -> Path:
    """Require a new sibling/external destination; never replace or nest in the source."""
    raw = Path(path).absolute()
    if raw.exists() or raw.is_symlink():
        raise _refuse(
            "The destination already exists; nothing was overwritten.",
            "destination_exists",
        )
    parent = raw.parent.resolve(strict=True)
    target = parent / raw.name
    if target == source or source in target.parents or target in source.parents:
        raise _refuse(
            "Source and destination must be disjoint directories.", "overlapping_paths"
        )
    return target


def _put(storage: LocalStorageDevice, name: str, payload: bytes) -> None:
    """Create and barrier one new artifact file through the regular storage adapter."""
    storage.create(name)
    storage.append_log(name, payload)
    storage.durable_barrier(name)


def _verify(root: Path, manifest: dict) -> None:
    """Require observational reopen, exact identity/cut and complete semantic verification."""
    with connect(
        root,
        read_only=True,
        page_size=manifest["page_size"],
        partitions_per_table=manifest["partitions_per_table"],
    ) as db:
        if db.identity.database_uuid.hex() != manifest["database_uuid"]:
            raise _refuse(
                "The restored database identity differs from the manifest.",
                "identity_mismatch",
            )
        state = db._transactions.published_state()
        if (
            state.last_committed_lsn != manifest["checkpoint_lsn"]
            or state.checkpoint_lsn != state.last_committed_lsn
        ):
            raise _refuse(
                "The physical image is not the declared complete checkpoint.",
                "checkpoint_mismatch",
            )
        if db.verify("all").findings:
            raise _refuse(
                "The physical image has verification findings.", "verification_failed"
            )


def _promote(staging: Path, destination: Path) -> None:
    """Publish only an already verified directory; never deliberately replace a destination."""
    if destination.exists() or destination.is_symlink():
        raise _refuse(
            "The destination appeared during preparation.", "destination_exists"
        )
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move.restype = ctypes.c_int
        # WRITE_THROUGH, deliberately without REPLACE_EXISTING; staging is on the same volume.
        if not move(str(staging), str(destination), 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise GrafxUnsupportedOperation(
                "This platform has no supported atomic no-replace directory publication.",
                operation="physical_backup",
            )
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(staging), -100, os.fsencode(destination), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
        descriptor = os.open(
            destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def create_backup(
    database: Database,
    destination: str | os.PathLike[str],
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_capture_seconds: float = 5.0,
    capture_mode: str = "disk",
) -> BackupReport:
    """Create a verified consistent cut with bounded chunked capture and read-back.

    disk (default) spools to a private temporary file beside the destination;
    memory retains the legacy RAM-backed capture choice. Writers wait during the
    entire source-to-spool copy. Neither mode is a no-pause hot backup. Temporary
    objects are unpublished and removed on every normal success/failure exit.
    """
    if type(capture_mode) is not str or capture_mode not in ("disk", "memory"):
        raise GrafxConfigurationError("capture_mode must be disk or memory.", field="capture_mode")
    storage = database._storage
    if type(storage) is not LocalStorageDevice or database.read_only:
        raise GrafxUnsupportedOperation("Physical backup requires writable local storage.",
                                       operation="physical_backup")
    target = _destination(destination, source=Path(storage.root).resolve(strict=True))
    with (tempfile.TemporaryFile(dir=target.parent, prefix=".grafx-backup-spool-")
          if capture_mode == "disk" else io.BytesIO()) as capture:
        return _create_backup(database, destination, max_bytes=max_bytes,
                              max_capture_seconds=max_capture_seconds, capture=capture)


def _create_backup(
    database: Database,
    destination: str | os.PathLike[str],
    *,
    max_bytes: int,
    max_capture_seconds: float,
    capture: BinaryIO,
) -> BackupReport:
    """Capture a local checkpoint into a checked, non-database artifact at a new directory.

    The bounded spool capture holds the existing commit/WAL fence: other participants may
    read and stage work, but commit publication waits during capture. Artifact IO and full
    verify/reopen run after releasing that fence. This is not a no-pause hot backup.
    max_bytes bounds captured payload (not total Python RSS); max_capture_seconds is checked
    between source reads, not an OS-level timeout for one blocked device call. No live source
    reader registrations or locks are copied. The source must have no transaction on this handle.
    """
    limit = _budget(max_bytes)
    if (
        type(max_capture_seconds) not in (float, int)
        or not math.isfinite(max_capture_seconds)
        or max_capture_seconds <= 0
    ):
        raise GrafxConfigurationError(
            "max_capture_seconds must be finite and positive.",
            field="max_capture_seconds",
        )
    storage = database._storage
    if type(storage) is not LocalStorageDevice or database.read_only:
        raise GrafxUnsupportedOperation(
            "Physical backup requires a writable default local storage handle.",
            operation="physical_backup",
        )
    source = Path(storage.root).resolve(strict=True)
    target = _destination(destination, source=source)
    identity = database.identity

    def capture_cut(lsn: int) -> tuple[int, dict[str, tuple[int, int, str]]]:
        """Read exactly one already-published checkpoint while recycling/commits are fenced."""
        started = monotonic()
        names = tuple(name for name in storage.list_files() if _logical_file(name))
        if len(names) > 10000:
            raise _refuse(
                "The physical snapshot exceeds max_bytes or 10,000 files.",
                "backup_budget",
            )
        sizes = {}
        total = 0
        for name in names:
            if monotonic() - started > max_capture_seconds:
                raise _refuse(
                    "The bounded checkpoint capture timed out.", "capture_timeout"
                )
            sizes[name] = storage.file_size(name)
            total += sizes[name]
            if total > limit:
                raise _refuse(
                    "The physical snapshot exceeds max_bytes.", "backup_budget"
                )
        payloads: dict[str, tuple[int, int, str]] = {}
        for name in names:
            start = capture.tell()
            digest = hashlib.sha256()
            for offset in range(0, sizes[name], _CHUNK):
                if monotonic() - started > max_capture_seconds:
                    raise _refuse(
                        "The bounded checkpoint capture timed out.", "capture_timeout"
                    )
                wanted = min(_CHUNK, sizes[name] - offset)
                data = storage.read_log(name, offset, wanted)
                if len(data) != wanted:
                    raise _refuse(
                        "A source file changed or returned a short read.", "short_read"
                    )
                if capture.write(data) != len(data):
                    raise _refuse("Short temporary capture write.", "short_write")
                digest.update(data)
            payloads[name] = (start, sizes[name], digest.hexdigest())
        capture.flush()
        if monotonic() - started > max_capture_seconds:
            raise _refuse(
                "The bounded checkpoint capture timed out.", "capture_timeout"
            )
        return lsn, payloads

    with database._public_operation("physical backup"):
        database._require_open()
        with database._transactions._participant_section():
            if database._transactions._open:
                raise _refuse(
                    "Close this handle's transactions before backup.",
                    "active_transaction",
                )
            _, (lsn, payloads) = database._transactions._checkpoint(capture_cut)
    manifest = {
        "format": "okto-grafx-physical-1",
        "database_uuid": identity.database_uuid.hex(),
        "page_size": identity.page_size,
        "partitions_per_table": identity.partitions_per_table,
        "checkpoint_lsn": lsn,
        "files": [
            {
                "name": name,
                "object": f"objects/{i:06d}",
                "size": data[1],
                "sha256": data[2],
            }
            for i, (name, data) in enumerate(sorted(payloads.items()))
        ],
    }
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}.incomplete-", dir=target.parent
    ) as temp:
        staging = Path(temp) / "artifact"
        verify_root = Path(temp) / "verify"
        with (
            LocalStorageDevice(staging, page_size=identity.page_size) as artifact,
            LocalStorageDevice(
                verify_root, page_size=identity.page_size
            ) as verification,
        ):
            for item in manifest["files"]:
                artifact.create(item["object"])
                capture.seek(payloads[item["name"]][0])
                digest = hashlib.sha256()
                for offset in range(0, item["size"], _CHUNK):
                    wanted = min(_CHUNK, item["size"] - offset)
                    data = capture.read(wanted)
                    if len(data) != wanted:
                        raise _refuse("Short temporary capture read.", "short_read")
                    digest.update(data)
                    artifact.append_log(item["object"], data)
                if digest.hexdigest() != item["sha256"]:
                    raise _refuse("Temporary capture checksum mismatch.", "object_mismatch")
                artifact.durable_barrier(item["object"])
                verification.create(item["name"])
                digest = hashlib.sha256()
                for offset in range(0, item["size"], _CHUNK):
                    wanted = min(_CHUNK, item["size"] - offset)
                    copied = artifact.read_log(item["object"], offset, wanted)
                    if len(copied) != wanted:
                        raise _refuse("Short artifact read-back.", "object_mismatch")
                    digest.update(copied)
                    verification.append_log(item["name"], copied)
                if digest.hexdigest() != item["sha256"]:
                    raise _refuse(
                        "The written backup object failed read-back verification.",
                        "object_mismatch",
                    )
                verification.durable_barrier(item["name"])
            raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            if len(raw) > _MANIFEST_LIMIT:
                raise _refuse("The manifest exceeds its bound.", "backup_budget")
            _put(artifact, "manifest.json", raw)
            if _manifest(artifact, limit) != manifest:
                raise _refuse(
                    "The written manifest failed read-back verification.",
                    "manifest_invalid",
                )
        _verify(verify_root, manifest)
        _promote(staging, target)
    return BackupReport(
        str(target),
        manifest["database_uuid"],
        lsn,
        len(payloads),
        sum(item[1] for item in payloads.values()),
    )


def _manifest(storage: LocalStorageDevice, limit: int) -> dict:
    """Validate closed manifest structure and bounded canonical object paths before data IO."""
    size = storage.file_size("manifest.json")
    if not 0 < size <= _MANIFEST_LIMIT:
        raise _refuse("Invalid manifest length.", "manifest_invalid")
    try:

        def require(condition: bool) -> None:
            """Unlike assert, disk validation remains active under python -O."""
            if not condition:
                raise ValueError("invalid manifest field")

        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            """Duplicate JSON keys cannot silently replace one manifest claim with another."""
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        value = json.loads(
            storage.read_log("manifest.json", 0, size), object_pairs_hook=unique
        )
        require(
            type(value) is dict
            and set(value)
            == {
                "format",
                "database_uuid",
                "page_size",
                "partitions_per_table",
                "checkpoint_lsn",
                "files",
            }
        )
        require(value["format"] == "okto-grafx-physical-1")
        require(
            type(value["database_uuid"]) is str and len(value["database_uuid"]) == 32
        )
        require(bytes.fromhex(value["database_uuid"]).hex() == value["database_uuid"])
        for field in ("page_size", "partitions_per_table", "checkpoint_lsn"):
            require(type(value[field]) is int and value[field] >= 0)
        require(type(value["files"]) is list and 3 <= len(value["files"]) <= 10000)
        names = set()
        total = 0
        for i, item in enumerate(value["files"]):
            require(
                type(item) is dict and set(item) == {"name", "object", "size", "sha256"}
            )
            require(_logical_file(item["name"]) and item["name"] not in names)
            names.add(item["name"])
            require(item["object"] == f"objects/{i:06d}")
            require(type(item["size"]) is int and item["size"] >= 0)
            total += item["size"]
            require(type(item["sha256"]) is str and len(item["sha256"]) == 64)
            require(bytes.fromhex(item["sha256"]).hex() == item["sha256"])
        require(
            {
                "grafx.meta",
                "heap.dat",
                "catalog.dat",
                "control/commit.state",
                "control/writer.lease",
            }
            <= names
        )
        require(total <= limit)
        return value
    except (ValueError, TypeError, KeyError, RecursionError) as failure:
        raise _refuse(
            "Invalid or over-budget physical backup manifest.", "manifest_invalid"
        ) from failure


def _release_restored_lease(
    storage: LocalStorageDevice, identity: DatabaseIdentity
) -> None:
    """Release only the private restored copy's lease, preserving its epoch lineage and UUID."""
    file = "control/writer.lease"
    slots = TwoSlotControlRecordStore(
        storage,
        file=file,
        record_kind=ControlRecordKind.LEASE,
        database_uuid=identity.database_uuid,
        file_nonce=0,
        temporary="control/restore.lease.tmp",
    )
    record = slots.read()
    if record is None:
        raise _refuse("The backup has no writer epoch lineage.", "lease_missing")
    lease = decode_lease_record(record.payload, file=file)
    payload = encode_lease_record(replace(lease, held=False))
    if identity.format_version == 2:
        slots.publish(payload)
    else:
        _put(storage, "control/restore.lease.tmp", payload)
        storage.atomic_replace("control/restore.lease.tmp", file)
        storage.durable_barrier(file)


def restore_backup(
    backup: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    confirm_original_offline: bool = False,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> BackupReport:
    """Verify and restore into a NEW directory for offline replacement, never a writable fork.

    confirm_original_offline=True asserts every original participant is stopped and the
    original will not resume alongside this same-UUID replacement. This is an operator
    assertion, not process detection. No original/target data is deleted or overwritten.
    Hashes detect accidental alteration, not a malicious party rewriting manifest and objects.
    The copied lease is released only after verification; its epoch lineage is retained.
    """
    if confirm_original_offline is not True:
        raise GrafxConfigurationError(
            "Physical restore requires confirm_original_offline=True; same-UUID writable forks are unsupported.",
            field="confirm_original_offline",
        )
    limit = _budget(max_bytes)
    source = Path(backup).resolve(strict=True)
    target = _destination(destination, source=source)
    with LocalStorageDevice(source, create_root=False) as artifact:
        manifest = _manifest(artifact, limit)
        with tempfile.TemporaryDirectory(
            prefix=f".{target.name}.incomplete-", dir=target.parent
        ) as temp:
            staging = Path(temp) / "database"
            with LocalStorageDevice(
                staging, page_size=manifest["page_size"]
            ) as restored:
                for item in manifest["files"]:
                    if artifact.file_size(item["object"]) != item["size"]:
                        raise _refuse(
                            "Backup object length differs from the manifest.",
                            "object_mismatch",
                        )
                    digest = hashlib.sha256()
                    restored.create(item["name"])
                    for offset in range(0, item["size"], _CHUNK):
                        wanted = min(_CHUNK, item["size"] - offset)
                        data = artifact.read_log(item["object"], offset, wanted)
                        if len(data) != wanted:
                            raise _refuse(
                                "Backup object returned a short read.", "short_read"
                            )
                        digest.update(data)
                        restored.append_log(item["name"], data)
                    if digest.hexdigest() != item["sha256"]:
                        raise _refuse(
                            "Backup object checksum differs from the manifest.",
                            "object_mismatch",
                        )
                    restored.durable_barrier(item["name"])
            _verify(staging, manifest)
            with connect(
                staging,
                read_only=True,
                page_size=manifest["page_size"],
                partitions_per_table=manifest["partitions_per_table"],
            ) as db:
                identity = db.identity
            with LocalStorageDevice(
                staging, page_size=manifest["page_size"]
            ) as restored:
                _release_restored_lease(restored, identity)
            _verify(staging, manifest)
            _promote(staging, target)
    return BackupReport(
        str(target),
        manifest["database_uuid"],
        manifest["checkpoint_lsn"],
        len(manifest["files"]),
        sum(item["size"] for item in manifest["files"]),
    )
