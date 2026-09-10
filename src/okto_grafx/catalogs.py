"""Named single-store transaction routing, without distributed commit or global writer locks."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from okto_grafx.catalog_copy import CopyPackage, CopyLimits, CopyReceipt

from okto_grafx.api import connect
from okto_grafx.domain.txn.commit_metadata import CommitMetadata
from okto_grafx.engine.database import Database, Transaction, META_FILE
from okto_grafx.errors import GrafxConfigurationError, GrafxTransactionStateError

__all__ = ["CatalogPathPolicy", "CatalogInfo", "CatalogSession"]


def _refuse(message, field):
    raise GrafxConfigurationError(message, field=field)


def _absolute(path):
    if not isinstance(path, (str, os.PathLike)):
        _refuse("Expected explicit local path.", "path")
    raw = os.fspath(path)
    if (
        type(raw) is not str
        or not raw
        or "\x00" in raw
        or raw.startswith(("//", "\\\\"))
        or "://" in raw
    ):
        _refuse("Expected explicit local path; remote paths are refused.", "path")
    if os.name == "nt" and any(
        part not in (".", "..") and part.endswith((".", " "))
        for part in raw.replace("\\", "/").split("/")
    ):
        _refuse("Windows trailing-dot/space path aliases are refused.", "path")
    result = Path(os.path.abspath(raw))
    if os.name == "nt" and (
        ":" in result.as_posix()[2:]
        or (len(raw) >= 2 and raw[1] == ":" and not Path(raw).is_absolute())
    ):
        _refuse("Drive-relative paths and alternate data streams are refused.", "path")
    # Refuse links/junctions, including those above an otherwise allowed directory.
    for part in (*reversed(result.parents), result):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GrafxConfigurationError(
                "Local path cannot be inspected.", field="path"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _refuse("Links and reparse points are not permitted.", "path")
    return result


@dataclass(frozen=True, slots=True)
class CatalogPathPolicy:
    """Existing allowed roots, no links/UNC, no implicit home expansion or directory creation."""

    allowed_roots: tuple[str, ...]
    max_catalogs: int = 16
    max_active_transactions: int = 64

    def __post_init__(self):
        if (
            type(self.allowed_roots) is not tuple
            or not 1 <= len(self.allowed_roots) <= 64
        ):
            _refuse("Declare 1..64 allowed roots.", "allowed_roots")
        roots = tuple(str(_absolute(root)) for root in self.allowed_roots)
        if any(not Path(root).is_dir() for root in roots):
            _refuse("Allowed roots must be existing directories.", "allowed_roots")
        if len(set(os.path.normcase(root) for root in roots)) != len(roots):
            _refuse("Duplicate allowed root.", "allowed_roots")
        for name in ("max_catalogs", "max_active_transactions"):
            if (
                type(getattr(self, name)) is not int
                or not 1 <= getattr(self, name) <= 4096
            ):
                _refuse("Invalid session bound.", name)
        object.__setattr__(self, "allowed_roots", roots)

    def resolve(self, path: str | os.PathLike[str], *, existing: bool = True) -> str:
        """Validate an explicit path against roots; this never creates or opens a store."""
        if type(existing) is not bool:
            _refuse("existing must be bool.", "existing")
        target = _absolute(path)
        if not any(
            target.is_relative_to(_absolute(root)) for root in self.allowed_roots
        ):
            _refuse("Path is outside allowed roots.", "allowed_roots")
        if existing and not target.is_dir():
            _refuse("Expected existing directory.", "path")
        return str(target)


@dataclass(frozen=True, slots=True)
class CatalogInfo:
    """Detached catalog inventory; store identity is not filesystem authority."""

    alias: str
    database_uuid: bytes
    read_only: bool
    owned: bool
    active_transactions: int


@dataclass(slots=True)
class _Attached:
    database: Database
    database_uuid: bytes
    read_only: bool
    owned: bool
    path: str | None
    directory_identity: tuple[int, int] | None
    transactions: list[Transaction] = field(default_factory=list)
    pending: int = 0

    def active(self):
        """Count live and pending transactions without retaining settled handles."""
        self.transactions[:] = [tx for tx in self.transactions if tx.active]
        return len(self.transactions) + self.pending


def _alias(alias, *, main=False):
    if type(alias) is not str or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", alias):
        _refuse("Catalog alias must be 1..64 ASCII identifier characters.", "alias")
    normalized = alias.lower()
    if normalized == "main" and not main:
        _refuse("main is reserved.", "alias")
    return normalized


def _directory_identity(path):
    try:
        info = Path(path).stat()
        return info.st_dev, info.st_ino
    except OSError as exc:
        raise GrafxConfigurationError(
            "Catalog directory cannot be inspected.", field="path"
        ) from exc


class CatalogSession:
    """Bounded aliases and pinned native transactions. Python permissions are not an OS sandbox."""

    def __init__(
        self,
        main: Database,
        *,
        owned: bool,
        policy: CatalogPathPolicy,
        read_only: bool = True,
    ):
        if type(policy) is not CatalogPathPolicy:
            _refuse("Expected CatalogPathPolicy.", "policy")
        self._policy = policy
        self._lock = RLock()
        self._closed = False
        self._default = "main"
        self._attachments = {}
        self._acquisitions: dict[str, str] = {}
        self._retiring: dict[str, _Attached] = {}
        self._attachments["main"] = self._capture(
            main, owned=owned, read_only=read_only
        )

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        policy: CatalogPathPolicy,
        read_only: bool = True,
    ) -> CatalogSession:
        """Open an existing main store; no creation or workspace lookup. Writable opens may recover."""
        if type(policy) is not CatalogPathPolicy or type(read_only) is not bool:
            _refuse("Invalid session policy or read_only flag.", "policy")
        resolved = policy.resolve(path)
        if not (Path(resolved) / META_FILE).is_file():
            _refuse("No existing Grafx store at path.", "path")
        db = connect(resolved, read_only=read_only)
        try:
            return cls(db, owned=True, policy=policy, read_only=read_only)
        except BaseException as failure:
            try:
                db.close()
            except BaseException as cleanup:
                failure.add_note(
                    f"Acquired main handle cleanup failed: {type(cleanup).__name__}"
                )
            raise

    def _capture(self, database, *, owned, read_only):
        if (
            type(database) is not Database
            or type(owned) is not bool
            or type(read_only) is not bool
        ):
            _refuse(
                "Expected native Database and explicit boolean ownership/permissions.",
                "database",
            )
        if database.closed:
            raise GrafxTransactionStateError(
                "Cannot attach a closed database.", field="catalog"
            )
        if not read_only and database.read_only:
            _refuse("Read-only handle cannot grant writes.", "read_only")
        path = (
            None if database.path == ":memory:" else self._policy.resolve(database.path)
        )
        return _Attached(
            database,
            database.identity.database_uuid,
            read_only,
            owned,
            path,
            None if path is None else _directory_identity(path),
        )

    def _open(self):
        if self._closed:
            raise GrafxTransactionStateError(
                "Catalog session is closed.", field="session"
            )

    def _available(self, alias):
        self._open()
        if (
            alias in self._attachments
            or alias in self._acquisitions
            or alias in self._retiring
        ):
            _refuse("Catalog alias already attached.", "alias")
        if (
            len(self._attachments) + len(self._acquisitions) + len(self._retiring)
            >= self._policy.max_catalogs
        ):
            _refuse("Catalog count bound exceeded.", "max_catalogs")

    def _publish(self, alias, attached, *, reserved=False):
        if not reserved:
            self._available(alias)
        else:
            self._open()
        if attached.path is not None and any(
            name != alias and os.path.normcase(path) == os.path.normcase(attached.path)
            for name, path in self._acquisitions.items()
        ):
            _refuse("Store path is being attached.", "catalog_identity")
        for existing in (*self._attachments.values(), *self._retiring.values()):
            if existing.database_uuid == attached.database_uuid or (
                attached.path is not None
                and existing.path is not None
                and os.path.normcase(attached.path) == os.path.normcase(existing.path)
            ):
                _refuse(
                    "Store identity or path is already attached.", "catalog_identity"
                )
        self._attachments[alias] = attached
        if reserved:
            del self._acquisitions[alias]

    def attach_handle(
        self, database: Database, *, alias: str, owned: bool, read_only: bool = True
    ) -> None:
        """Attach caller-supplied handle; ownership transfers only after successful publication."""
        name = _alias(alias)
        with self._lock:
            self._available(name)
            self._publish(
                name, self._capture(database, owned=owned, read_only=read_only)
            )

    def attach(
        self, path: str | os.PathLike[str], *, alias: str, read_only: bool = True
    ) -> None:
        """Open an existing local catalog with read-only default; release failed acquisitions."""
        name = _alias(alias)
        if type(read_only) is not bool:
            _refuse("read_only must be bool.", "read_only")
        with self._lock:
            self._available(name)
            resolved = self._policy.resolve(path)
            if any(
                a.path is not None
                and os.path.normcase(a.path) == os.path.normcase(resolved)
                for a in (*self._attachments.values(), *self._retiring.values())
            ) or any(
                os.path.normcase(p) == os.path.normcase(resolved)
                for p in self._acquisitions.values()
            ):
                _refuse("Store path already attached.", "catalog_identity")
            if not (Path(resolved) / META_FILE).is_file():
                _refuse("No existing Grafx store at path.", "path")
            self._acquisitions[name] = resolved
        database = None
        published = False
        try:
            database = connect(resolved, read_only=read_only)
            attached = self._capture(database, owned=True, read_only=read_only)
            with self._lock:
                self._publish(name, attached, reserved=True)
                published = True
        except BaseException as failure:
            if database is not None:
                try:
                    database.close()
                except BaseException as cleanup:
                    failure.add_note(
                        f"Acquired handle cleanup failed: {type(cleanup).__name__}"
                    )
            raise
        finally:
            if not published:
                with self._lock:
                    self._acquisitions.pop(name, None)

    def catalogs(self) -> tuple[CatalogInfo, ...]:
        """Return bounded sorted alias/permission/ownership inventory; omit private paths."""
        with self._lock:
            self._open()
            return tuple(
                CatalogInfo(name, a.database_uuid, a.read_only, a.owned, a.active())
                for name, a in sorted(self._attachments.items())
            )

    def use(self, alias: str) -> None:
        """Change the default for future begins only."""
        name = _alias(alias, main=True)
        with self._lock:
            self._open()
            if name not in self._attachments:
                _refuse("Catalog is not attached.", "alias")
            self._default = name

    def begin(
        self,
        mode: str = "read",
        *,
        catalog: str | None = None,
        metadata: CommitMetadata | None = None,
    ) -> Transaction:
        """Return a native transaction permanently owned by the selected store, not the session default."""
        if type(mode) is not str or mode not in ("read", "write"):
            _refuse("Mode must be read or write.", "mode")
        with self._lock:
            self._open()
            name = self._default if catalog is None else _alias(catalog, main=True)
            attached = self._attachments.get(name)
            if attached is None:
                _refuse("Catalog is not attached.", "alias")
            if mode == "write" and attached.read_only:
                raise GrafxTransactionStateError(
                    "Catalog permission is read-only.", field="catalog_mode"
                )
            if (
                sum(a.active() for a in self._attachments.values())
                >= self._policy.max_active_transactions
            ):
                _refuse("Active transaction bound exceeded.", "max_active_transactions")
            if attached.path is not None:
                self._policy.resolve(attached.path)
                if _directory_identity(attached.path) != attached.directory_identity:
                    _refuse("Catalog directory identity changed.", "catalog_identity")
            if (
                attached.database.closed
                or attached.database.identity.database_uuid != attached.database_uuid
            ):
                raise GrafxTransactionStateError(
                    "Attached handle is no longer valid.", field="catalog_identity"
                )
            attached.pending += 1
        # A reservation protects lifetime, but native begin never holds the session
        # metadata lock: an unrelated catalog can begin even if this store is busy.
        try:
            transaction = attached.database.begin(mode, metadata=metadata)
        except BaseException:
            with self._lock:
                attached.pending -= 1
            raise
        with self._lock:
            attached.transactions.append(transaction)
            attached.pending -= 1
        return transaction

    def apply_copy(
        self,
        package: CopyPackage,
        *,
        target: str,
        idempotency_key: str,
        conflict: str = "fail",
        metadata: CommitMetadata | None = None,
        limits: CopyLimits | None = None,
    ) -> CopyReceipt:
        """Apply a detached package using the target permission/lifetime and a pinned native commit."""
        from okto_grafx.catalog_copy import CopyLimits, _copy_with_begin

        name = _alias(target, main=True)
        with self._lock:
            self._open()
            attached = self._attachments.get(name)
            if attached is None:
                _refuse("Catalog is not attached.", "alias")
            if attached.read_only:
                raise GrafxTransactionStateError(
                    "Catalog permission is read-only.", field="catalog_mode"
                )
            database = attached.database

        def begin(mode, *, metadata):
            """Pin copy work to this explicitly selected catalog alias."""
            return self.begin(mode, catalog=name, metadata=metadata)

        return _copy_with_begin(
            package,
            database,
            begin,
            idempotency_key=idempotency_key,
            conflict=conflict,
            metadata=metadata,
            limits=CopyLimits() if limits is None else limits,
        )

    def detach(self, alias: str) -> None:
        """Refuse in-use detach; close owned handles only. main cannot detach."""
        name = _alias(alias)
        with self._lock:
            self._open()
            attached = self._attachments.get(name)
            if attached is None:
                _refuse("Catalog is not attached.", "alias")
            if attached.active():
                raise GrafxTransactionStateError(
                    "Catalog has active transactions.", field="catalog_in_use"
                )
            del self._attachments[name]
            if attached.owned:
                self._retiring[name] = attached
            if self._default == name:
                self._default = "main"
        if attached.owned:
            try:
                attached.database.close()
            finally:
                with self._lock:
                    del self._retiring[name]

    def close(self) -> None:
        """Refuse active transactions; otherwise close every owned handle, preserving first failure."""
        with self._lock:
            if self._closed:
                return
            if (
                self._acquisitions
                or self._retiring
                or any(a.active() for a in self._attachments.values())
            ):
                raise GrafxTransactionStateError(
                    "Session has active transactions or attachments in progress.",
                    field="catalog_in_use",
                )
            self._closed = True
            attachments = tuple(self._attachments.values())
            self._attachments.clear()
        first = None
        for attached in attachments:
            if attached.owned:
                try:
                    attached.database.close()
                except BaseException as failure:
                    if first is None:
                        first = failure
                    else:
                        first.add_note(
                            f"Additional catalog cleanup failure: {type(failure).__name__}"
                        )
        if first is not None:
            raise first

    def __enter__(self):
        with self._lock:
            self._open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except BaseException as cleanup:
            if exc is None:
                raise
            exc.add_note(f"CatalogSession cleanup failed: {type(cleanup).__name__}")
        return False
