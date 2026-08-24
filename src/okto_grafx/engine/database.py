"""The engine level Database (CONTRACT.md section 10, SPEC-M1 FR-1 and FR-11).

This module is where every other component becomes reachable. It is pure by the same rule as the
rest of ``engine/`` (G2, A12): it holds no mechanism at all -- no file system, no clock, no
threads, no platform reading. Everything it touches, it touches through a port or through another
engine object that was handed to it. The concrete adapters are chosen by the composition root in
:mod:`okto_grafx.runtime.bootstrap` and by :mod:`okto_grafx.api`, and neither of those is imported
from here.

**Locks are owned by collaborators, never invented here** (A91, LESSONS L2). Lifecycle calls
still run with no section held, so a re-entrant ``close()`` from host code cannot deadlock.
Page-touching transaction doors deliberately enter the transaction manager's re-entrant
participant section: that is the shared guard which keeps a failed post-barrier commit from
racing an older cached frame into the device.

**Lifecycle is the property this component is judged on.** Two rules, and both are structural
rather than remembered:

* *Everything opened is registered before it can fail.* The identity page, the log, the reader
  registrations and whatever the composition root opened are all released by :meth:`Database.close`,
  which is idempotent and which reports the first failure only after it has tried every release.
* *A database closes what it opened and nothing else.* When the caller supplies its own
  ``PortRegistry`` the ports belong to the caller, so closing the database leaves them open. The
  composition root says which is which by handing over the closers it owns.

**Errors compose unchanged** (A47, section 2). Nothing here catches a ``GrafxError`` in order to
re-raise a different one: a device failure arrives at the caller as the class the device chose,
with ``details["retryable"]`` intact, because a retry predicate that reads the class instead of the
detail is exactly the defect A47 was written about.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxSchemaVersionMismatch,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.page.file_header import (
    HEADER_PAGE_INDEX,
    FileHeader,
    FileHeaderPage,
    FileKind,
)
from okto_grafx.domain.page.layout import PageType
from okto_grafx.domain.page.slotted import Page
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.codec import PageCodec
from okto_grafx.domain.ports.coordination import ProcessCoordinator
from okto_grafx.domain.ports.events import EventSink
from okto_grafx.domain.ports.metrics import MetricsSink
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.ports.vectormath import VectorMath
from okto_grafx.domain.txn.context import (
    CommitReport,
    TransactionContext,
    TransactionMode,
)
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.metrics_catalog import metric
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.verifier import VERIFICATION_SCOPES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from okto_grafx.engine.wal_manager import WalManager

__all__ = [
    "DATABASE_OPENS_TOTAL",
    "DATABASE_METRICS",
    "IDENTITY_MAGIC",
    "IDENTITY_FORMAT_VERSION",
    "IDENTITY_SLOT",
    "MAX_DESCRIPTOR_BYTES",
    "META_FILE",
    "VERIFY_SCOPES",
    "Database",
    "DatabaseIdentity",
    "MetaStore",
    "Transaction",
]

META_FILE: str = "grafx.meta"
"""The identity file of a database (CONTRACT.md section 6.1).

The name is the same string :mod:`okto_grafx.engine.recovery_manager` reads at step 1 of the
frozen recovery algorithm. It is repeated here rather than imported because recovery is optional
in a composition and this module must be able to create the file before recovery exists (A24 bans
a second DEFINITION of a shared rule; a file name that two components must agree on is pinned by
``test_the_identity_file_is_the_one_recovery_reads``).
"""

IDENTITY_MAGIC: bytes = b"OKTOGRFX"
"""The eight bytes that open the identity record (CONTRACT.md section 6.2)."""

IDENTITY_FORMAT_VERSION: int = 1
"""The identity format this build writes. Every earlier version stays readable."""

IDENTITY_SLOT: int = 1
"""Slot of page 0 that carries the identity record.

Slot 0 of page 0 is the file header C1 owns and C6 reads, so the identity of the database goes in
the slot after it: section 6.3 gives the rest of a header page to whoever owns the file, and the
identity page is owned by exactly this module.
"""

MAX_DESCRIPTOR_BYTES: int = 0xFFFF
"""Longest granularity descriptor the identity record can carry; its length is a u16."""

DATABASE_OPENS_TOTAL: str = "oktografx_database_opens_total"
"""Counter of databases opened through this component (CONTRACT.md section 9)."""

DATABASE_METRICS: tuple[object, ...] = (metric(DATABASE_OPENS_TOTAL),)
"""Every metric this component registers, taken from the frozen catalogue by name."""

VERIFY_SCOPES: frozenset[str] = VERIFICATION_SCOPES
"""The scopes :meth:`Database.verify` accepts (SPEC-M1 FR-11).

Derived from the verifier that answers them, never re-listed (A24). The scope is checked once,
by the verifier, and not again here: a battery showed a second check in this method surviving
its own deletion, because the verifier refuses the same input a moment later with the same class
and the same ``field`` detail. Two mechanisms answering one question make neither observable
(A67), so the redundant one is gone rather than dressed up to look different.
"""

_IDENTITY_HEAD: struct.Struct = struct.Struct("<8sHI16sdHH")
"""magic | format_version | page_size | database_uuid | created_at_wall | partitions | length."""

_IDENTITY_TAIL: struct.Struct = struct.Struct("<I")
"""The CRC-32C that closes the identity record."""

_UUID_BYTES: int = 16
"""Width of the database identity, as section 6.2 fixes it."""


def _require_text(field: str, value: object) -> str:
    """Return the value as a non-empty string, or refuse with the field named."""
    if not isinstance(value, str) or not value:
        raise GrafxConfigurationError(
            f"The {field} must be a non-empty string; got {value!r}.",
            field=field,
            value=repr(value),
        )
    return value


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    """Who a database is: the record CONTRACT.md section 6.2 puts on the identity page.

    ``database_uuid`` is the raw 16 bytes rather than a formatted string, because ``uuid`` is not
    a module the pure core may import (G2b, A5): the composition root produces the bytes and this
    layer only stores and compares them.
    """

    database_uuid: bytes
    page_size: int
    partitions_per_table: int
    created_at_wall: float
    granularity_descriptor: str
    format_version: int = IDENTITY_FORMAT_VERSION

    def __post_init__(self) -> None:
        """Reject an identity that could not be encoded or could not be true."""
        if not isinstance(self.database_uuid, bytes) or len(self.database_uuid) != _UUID_BYTES:
            raise GrafxConfigurationError(
                f"A database identity is exactly {_UUID_BYTES} bytes; got "
                f"{len(self.database_uuid) if isinstance(self.database_uuid, bytes) else '?'}.",
                field="database_uuid",
                value=repr(self.database_uuid),
            )
        for field, value, ceiling in (
            ("format_version", self.format_version, 0xFFFF),
            ("page_size", self.page_size, 0xFFFFFFFF),
            ("partitions_per_table", self.partitions_per_table, 0xFFFF),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= ceiling:
                raise GrafxConfigurationError(
                    f"The identity field {field!r} is outside its width: {value!r}.",
                    field=field,
                    value=repr(value),
                )
        if not isinstance(self.created_at_wall, (int, float)) or isinstance(
            self.created_at_wall, bool
        ):
            raise GrafxConfigurationError(
                f"The creation stamp must be a number; got {self.created_at_wall!r}.",
                field="created_at_wall",
                value=repr(self.created_at_wall),
            )
        descriptor = self.granularity_descriptor
        if not isinstance(descriptor, str):
            raise GrafxConfigurationError(
                f"The granularity descriptor must be a string; got {descriptor!r}.",
                field="granularity_descriptor",
                value=repr(descriptor),
            )
        if len(descriptor.encode("utf-8")) > MAX_DESCRIPTOR_BYTES:
            raise GrafxConfigurationError(
                f"The granularity descriptor is longer than {MAX_DESCRIPTOR_BYTES} bytes.",
                field="granularity_descriptor",
                value=len(descriptor),
            )

    def encode(self) -> bytes:
        """Return the identity record exactly as CONTRACT.md section 6.2 lays it out."""
        descriptor = self.granularity_descriptor.encode("utf-8")
        head = _IDENTITY_HEAD.pack(
            IDENTITY_MAGIC,
            self.format_version,
            self.page_size,
            self.database_uuid,
            float(self.created_at_wall),
            self.partitions_per_table,
            len(descriptor),
        )
        body = head + descriptor
        return body + _IDENTITY_TAIL.pack(crc32c(body))

    @classmethod
    def decode(cls, raw: bytes) -> DatabaseIdentity:
        """Return the identity a record holds, refusing damaged bytes as corruption."""
        minimum = _IDENTITY_HEAD.size + _IDENTITY_TAIL.size
        if not isinstance(raw, (bytes, bytearray)) or len(raw) < minimum:
            raise GrafxCorruptionDetected(
                f"An identity record is at least {minimum} bytes; this one holds "
                f"{len(raw) if isinstance(raw, (bytes, bytearray)) else 0}.",
                field="identity_length",
                value=len(raw) if isinstance(raw, (bytes, bytearray)) else 0,
            )
        payload = bytes(raw)
        (
            magic,
            format_version,
            page_size,
            database_uuid,
            created_at_wall,
            partitions_per_table,
            descriptor_length,
        ) = _IDENTITY_HEAD.unpack_from(payload, 0)
        if magic != IDENTITY_MAGIC:
            raise GrafxCorruptionDetected(
                "The identity page does not start with the Okto Grafx magic.",
                field="magic",
                value=repr(magic),
            )
        end = _IDENTITY_HEAD.size + descriptor_length
        if len(payload) < end + _IDENTITY_TAIL.size:
            raise GrafxCorruptionDetected(
                f"The identity record claims a {descriptor_length}-byte descriptor that does not "
                f"fit in its {len(payload)} bytes.",
                field="granularity_descriptor_len",
                value=descriptor_length,
            )
        (stored,) = _IDENTITY_TAIL.unpack_from(payload, end)
        if stored != crc32c(payload[:end]):
            raise GrafxCorruptionDetected(
                "The checksum of the identity record does not match its bytes.",
                field="crc32c",
                value=stored,
            )
        if format_version > IDENTITY_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                f"The identity page was written by format version {format_version} and this "
                f"build reads up to {IDENTITY_FORMAT_VERSION}.",
                field="format_version",
                value=format_version,
                supported=IDENTITY_FORMAT_VERSION,
            )
        try:
            descriptor = payload[_IDENTITY_HEAD.size : end].decode("utf-8")
        except UnicodeDecodeError as failure:
            raise GrafxCorruptionDetected(
                "The granularity descriptor on the identity page is not valid UTF-8.",
                field="granularity_descriptor",
                value=descriptor_length,
            ) from failure
        return cls(
            database_uuid=database_uuid,
            page_size=page_size,
            partitions_per_table=partitions_per_table,
            created_at_wall=created_at_wall,
            granularity_descriptor=descriptor,
            format_version=format_version,
        )


class MetaStore:
    """Reads and writes the identity page of one database (SPEC-M1 FR-1).

    The identity page is ``grafx.meta``: page 0 is the reserved header page C1 defines, slot 0 is
    its file header, and slot 1 is the identity record of section 6.2. Recovery reads only slot 0,
    so the two components share the page without either having to know the other's record.
    """

    __slots__ = ("_pool", "_file")

    def __init__(self, pool: BufferPool, *, file: str = META_FILE) -> None:
        """Bind the store to the buffer pool of one database and to its identity file."""
        self._pool: BufferPool = pool
        self._file: str = _require_text("file", file)

    @property
    def file(self) -> str:
        """Return the name of the identity file this store reads and writes."""
        return self._file

    def exists(self) -> bool:
        """Return True when this database already carries an identity page."""
        storage = self._pool.storage
        return storage.exists(self._file) and storage.page_count(self._file) > 0

    def read(self) -> DatabaseIdentity:
        """Return the identity this database was created with.

        Raises GrafxCorruptionDetected when the page carries no identity record: a database whose
        identity is gone is damaged, and calling it a configuration problem would route it away
        from the quarantine and forensic paths FR-8 and FR-10 exist for (A11-revised).
        """
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            if page.slot_count <= IDENTITY_SLOT or page.is_slot_free(IDENTITY_SLOT):
                raise GrafxCorruptionDetected(
                    f"The identity page of {self._file!r} carries no identity record.",
                    file=self._file,
                    field="identity_slot",
                    value=page.slot_count,
                )
            return DatabaseIdentity.decode(page.read_slot(IDENTITY_SLOT))

    def create(self, identity: DatabaseIdentity) -> DatabaseIdentity:
        """Write a fresh identity page and return the identity it now holds.

        The page is reserved as a header page first, so a database created by this door is
        readable by recovery even if the process dies between the two slot writes: an identity
        page with a header and no identity record is damage this module names precisely, while a
        page with neither is simply a database that was never created.
        """
        storage = self._pool.storage
        if not storage.exists(self._file):
            storage.create(self._file)
        if storage.page_count(self._file) == 0:
            page = self._pool.allocate(self._file, int(PageType.META))
            try:
                if page.page_index != HEADER_PAGE_INDEX:
                    raise GrafxCorruptionDetected(
                        f"The identity page of {self._file!r} must be page "
                        f"{HEADER_PAGE_INDEX}; the device handed out {page.page_index}.",
                        file=self._file,
                        page=page.page_index,
                    )
                self._stamp(page, identity)
            finally:
                self._pool.unpin(self._file, page.page_index, dirty=True)
        else:
            with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
                self._stamp(page, identity)
        # The database UUID is not reconstructible from WAL. Returning it after a cache flush
        # alone lets a power loss erase the identity and the next open silently create another
        # database in the same directory.
        self._pool.checkpoint(self._file)
        return identity

    def open(self, expected: DatabaseIdentity) -> DatabaseIdentity:
        """Return the identity of this database, creating it when the database is new.

        ``expected`` is what the configuration asks for. An existing database keeps the identity
        it was created with; the only field whose disagreement is fatal is the page size, because
        every byte on disk is laid out against it. ``partitions_per_table`` deliberately does NOT
        refuse: SPEC-M1 FR-4 and TR-4 make the granularity self-describing precisely so that
        changing it needs no migration, and the stored value is reported rather than enforced.
        """
        if self.exists():
            stored = self.read()
            if stored.page_size != expected.page_size:
                raise GrafxSchemaVersionMismatch(
                    f"This database was created with {stored.page_size}-byte pages and is being "
                    f"opened with {expected.page_size}-byte pages.",
                    field="page_size",
                    value=expected.page_size,
                    stored=stored.page_size,
                )
            return stored
        return self.create(expected)

    def _stamp(self, page: Page, identity: DatabaseIdentity) -> None:
        """Write the file header and the identity record onto the reserved page."""
        header = FileHeader(kind=FileKind.META, page_size=identity.page_size)
        FileHeaderPage.write(page, header)
        record = identity.encode()
        if page.slot_count <= IDENTITY_SLOT:
            slot = page.insert_slot(record)
            if slot != IDENTITY_SLOT:
                raise GrafxCorruptionDetected(
                    f"The identity record must live in slot {IDENTITY_SLOT} of the identity "
                    f"page; the page handed out slot {slot}.",
                    file=self._file,
                    field="identity_slot",
                    value=slot,
                )
        else:
            page.update_slot(IDENTITY_SLOT, record)
        page.dirty = True

    def __repr__(self) -> str:
        """Return a representation naming the file this store reads."""
        return f"MetaStore(file={self._file!r})"


class Transaction:
    """One open transaction, as CONTRACT.md section 10 hands it to a caller.

    It is a thin, honest wrapper: the snapshot, the partition sets and the commit protocol all
    belong to :class:`okto_grafx.engine.txn_manager.TransactionManager`, and this class adds only
    the two things a public surface owes -- a block that always finishes the transaction, and
    refusals that name what went wrong instead of letting a half-used object drift on.

    Leaving the block normally commits; leaving it because of an exception rolls back and lets the
    original exception continue, because replacing the caller's failure with a failure of the
    closing path hides the reason the block is being left at all.
    """

    __slots__ = ("_database", "_context", "_report", "_finished", "__weakref__")

    def __init__(self, database: Database, context: TransactionContext) -> None:
        """Adopt a transaction context the manager of ``database`` has just opened."""
        self._database: Database = database
        self._context: TransactionContext = context
        self._report: CommitReport | None = None
        self._finished: bool = False

    @property
    def context(self) -> TransactionContext:
        """Return the engine level transaction this object wraps."""
        return self._context

    @property
    def mode(self) -> str:
        """Return ``"read"`` or ``"write"``, the mode this transaction was opened in."""
        return self._context.mode.value

    @property
    def snapshot(self) -> Snapshot:
        """Return the fixed view every read of this transaction sees (SPEC-M1 FR-2)."""
        return self._context.snapshot

    @property
    def txn_id(self) -> int:
        """Return the process-local number of this transaction."""
        return self._context.txn_id

    @property
    def active(self) -> bool:
        """Return True while this transaction can still commit or roll back."""
        return not self._finished and self._context.active

    @property
    def report(self) -> CommitReport | None:
        """Return what the commit reported, or None while the transaction is still open."""
        return self._report

    def execute(
        self, text: str, parameters: Mapping[str, object] | None = None
    ) -> object:
        """Run one statement inside this transaction and return its result.

        The statement text is handed to the query engine unchanged. A database composed without
        one refuses here with GrafxUnsupportedOperation rather than pretending to run anything.
        """
        self._require_active()
        return self._database.run_statement(self._context, text, parameters)

    def commit(self) -> CommitReport:
        """Commit this transaction and return the report of CONTRACT.md section 8.5.

        A refusal leaves the transaction usable: an optimistic conflict is retryable, and the
        transaction that lost is still active and still free of side effects, so a caller may
        call :meth:`retry` on the database and try again.
        """
        self._require_active()
        report = self._database.transactions.commit(self._context)
        self._report = report
        self._finished = True
        self._database._settle_schema(self._context, committed=True)
        return report

    def rollback(self) -> None:
        """Abandon this transaction. Rolling back twice is a no-op, never an error."""
        if self._finished:
            return
        self._finished = True
        self._database.transactions.rollback(self._context)
        self._database._settle_schema(self._context, committed=False)

    def __enter__(self) -> Self:
        """Return this transaction so a ``with`` block can use it."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """Commit on a clean exit, roll back on any exception, and never mask the original."""
        if exc_type is None:
            if not self._finished:
                self.commit()
            return
        try:
            self.rollback()
        except GrafxError:
            return

    def _require_active(self) -> None:
        """Refuse to use a transaction that has already been committed or rolled back."""
        if self._finished or not self._context.active:
            raise GrafxTransactionStateError(
                f"Transaction {self._context.txn_id} is {self._context.state.value} and cannot "
                "be used again; open a new one.",
                txn_id=self._context.txn_id,
                state=self._context.state.value,
            )

    def __repr__(self) -> str:
        """Return a representation naming the transaction, its mode and its snapshot."""
        return (
            f"Transaction(txn_id={self._context.txn_id}, mode={self.mode!r}, "
            f"read_lsn={self._context.snapshot.read_lsn})"
        )


class Database:
    """One open database: the object every public entry point of Okto Grafx hands back.

    It is built by the composition root, never directly by an application: the ports, the stores
    and the managers all arrive through the constructor, so this class holds no knowledge of which
    adapters were selected and no way to reach the file system except through them.

    The engines that are not part of a composition are simply absent. Asking for one refuses with
    GrafxUnsupportedOperation naming the component that would have answered, which is a typed,
    documented seam rather than a stub: a caller can tell the difference between "this database
    was built without a query engine" and "the query failed".
    """

    __slots__ = (
        "_storage",
        "_clock",
        "_codec",
        "_metrics",
        "_events",
        "_vector_math",
        "_coordinator",
        "_pool",
        "_catalog",
        "_heap",
        "_wal",
        "_transactions",
        "_indexes",
        "_ledger",
        "_quarantine",
        "_recovery",
        "_vectors",
        "_queries",
        "_verifier_factory",
        "_identity",
        "_label",
        "_path",
        "_metrics_endpoint",
        "_read_only",
        "_closers",
        "_closed",
        "_recovery_report",
        "_attached_indexes",
        "_stale_indexes",
        "_unindexed_tables",
        # A public object a host cannot hold weakly is a public object a host must hold
        # strongly, which is how a cache of databases becomes a leak of databases.
        "__weakref__",
    )

    def __init__(
        self,
        *,
        storage: StorageDevice,
        clock: Clock,
        codec: PageCodec,
        metrics: MetricsSink,
        events: EventSink,
        vector_math: VectorMath,
        coordinator: ProcessCoordinator,
        pool: BufferPool,
        catalog: CatalogStore,
        heap: HeapStore,
        wal: WalManager,
        transactions: TransactionManager,
        identity: DatabaseIdentity,
        path: str,
        label: str,
        read_only: bool = False,
        metrics_endpoint: str | None = None,
        indexes: object = None,
        ledger: object = None,
        quarantine: object = None,
        recovery: object = None,
        vectors: object = None,
        queries: object = None,
        verifier_factory: Callable[[], object] | None = None,
        recovery_report: object = None,
        attached_indexes: Sequence[str] = (),
        stale_indexes: Sequence[str] = (),
        unindexed_tables: Sequence[str] = (),
        closers: Sequence[Callable[[], None]] = (),
    ) -> None:
        """Adopt a complete composition. Nothing here opens anything: the root already did."""
        self._storage: StorageDevice = storage
        self._clock: Clock = clock
        self._codec: PageCodec = codec
        self._metrics: MetricsSink = metrics
        self._events: EventSink = events
        self._vector_math: VectorMath = vector_math
        self._coordinator: ProcessCoordinator = coordinator
        self._pool: BufferPool = pool
        self._catalog: CatalogStore = catalog
        self._heap: HeapStore = heap
        self._wal: WalManager = wal
        self._transactions: TransactionManager = transactions
        self._indexes: object = indexes
        self._ledger: object = ledger
        self._quarantine: object = quarantine
        self._recovery: object = recovery
        self._vectors: object = vectors
        self._queries: object = queries
        self._verifier_factory: Callable[[], object] | None = verifier_factory
        self._identity: DatabaseIdentity = identity
        self._path: str = _require_text("path", path)
        self._label: str = _require_text("label", label)
        self._read_only: bool = bool(read_only)
        self._metrics_endpoint: str | None = metrics_endpoint
        self._closers: tuple[Callable[[], None], ...] = tuple(closers)
        self._closed: bool = False
        self._recovery_report: object = recovery_report
        self._attached_indexes: tuple[str, ...] = tuple(attached_indexes)
        self._stale_indexes: tuple[str, ...] = tuple(stale_indexes)
        self._unindexed_tables: tuple[str, ...] = tuple(unindexed_tables)
        if metrics.enabled:
            for declared in DATABASE_METRICS:
                metrics.register(declared)
            metrics.increment(DATABASE_OPENS_TOTAL, 1.0)

    # --- identity ---------------------------------------------------------------------------

    @property
    def path(self) -> str:
        """Return the path this database was opened at, or ``":memory:"``."""
        return self._path

    @property
    def label(self) -> str:
        """Return the bounded label this database reports under, safe as a metric value (TR-7)."""
        return self._label

    @property
    def identity(self) -> DatabaseIdentity:
        """Return the identity record of section 6.2 that this database carries (FR-1)."""
        return self._identity

    @property
    def read_only(self) -> bool:
        """Return True when this database refuses to open a write transaction."""
        return self._read_only

    @property
    def closed(self) -> bool:
        """Return True once :meth:`close` has run; a closed database refuses every door."""
        return self._closed

    @property
    def metrics_endpoint(self) -> str | None:
        """Return the URL the metrics of this database are exposed at, or None (FR-14, OR-6).

        A default install configured for OpenMetrics listens on a loopback port, and the default
        port is zero -- the operating system picks a free one, so a database never fails to open
        because a fixed port was taken. An operator cannot scrape a port nobody can discover, so
        the composition root reports the address it actually bound. None means this database
        exposes no endpoint of its own, which is the case for every other metrics selector and
        for a caller that composed its own sink.
        """
        return self._metrics_endpoint

    @property
    def attached_indexes(self) -> tuple[str, ...]:
        """Return the name of every index the open sequence attached, in catalog order.

        A vector index covers a ``(table, space)`` pair, so it is attached by the table that
        declares the column -- on the statement that creates it, and again on every later open,
        because a registry belongs to one composition and a reopened database starts with none.
        """
        return self._attached_indexes

    @property
    def unindexed_tables(self) -> tuple[str, ...]:
        """Return the tables whose automatic indexes could not be adopted at open.

        The open-time twin of ``QueryEngine.skipped_indexes``: an index whose name is illegal or
        collides declines rather than failing the open, and the table it covers answers keyed
        reads and traversals by the scan it always did. This is where an operator learns WHICH
        tables run without their accelerator, instead of discovering it as a slow query.
        """
        return self._unindexed_tables

    @property
    def stale_indexes(self) -> tuple[str, ...]:
        """Return the name of every index that opened behind the position this database published.

        Nothing is repaired at open. A rebuild re-derives the index from the heap and is covered
        by the log like any other index change, so it writes -- and a write belongs inside a
        transaction the caller owns, at a moment the caller chose, not inside ``connect``. The
        honest answer is to say which indexes are behind and let the caller decide between
        rebuilding them and running without them.
        """
        return self._stale_indexes

    @property
    def recovery_report(self) -> object:
        """Return the report of the recovery that ran at open, or None when none did (FR-8)."""
        return self._recovery_report

    # --- the composition ---------------------------------------------------------------------

    @property
    def storage(self) -> StorageDevice:
        """Return the storage device this database persists through."""
        return self._storage

    @property
    def clock(self) -> Clock:
        """Return the clock this database measures liveness and stamps wall times with."""
        return self._clock

    @property
    def codec(self) -> PageCodec:
        """Return the page codec this database encodes and verifies pages with."""
        return self._codec

    @property
    def metrics(self) -> MetricsSink:
        """Return the metrics sink every component of this database emits through (FR-14)."""
        return self._metrics

    @property
    def events(self) -> EventSink:
        """Return the event sink this database narrates through."""
        return self._events

    @property
    def vector_math(self) -> VectorMath:
        """Return the vector math adapter this database scores with (SPEC-VEC FR-7)."""
        return self._vector_math

    @property
    def coordinator(self) -> ProcessCoordinator:
        """Return the coordinator that fences this database against other processes (FR-7)."""
        return self._coordinator

    @property
    def pool(self) -> BufferPool:
        """Return the page cache of this database, bounded by its own budget (FR-13)."""
        return self._pool

    @property
    def catalog(self) -> CatalogStore:
        """Return the catalog store that holds the schema of this database."""
        return self._catalog

    @property
    def heap(self) -> HeapStore:
        """Return the heap store that holds the rows of this database."""
        return self._heap

    @property
    def wal(self) -> WalManager:
        """Return the write ahead log of this database (FR-5)."""
        return self._wal

    @property
    def transactions(self) -> TransactionManager:
        """Return the transaction manager that runs the frozen commit protocol (FR-2, FR-3)."""
        return self._transactions

    @property
    def indexes(self) -> object:
        """Return the secondary index registry of this database (FR-12).

        Refuses with GrafxUnsupportedOperation when the composition has no index manager.
        """
        return self._require_component("indexes", self._indexes, "the index framework (C7)")

    @property
    def ledger(self) -> object:
        """Return the ledger of unapplied work (SPEC-M1 FR-9, CONTRACT.md section 10).

        Refuses with GrafxUnsupportedOperation when the composition has no ledger store.
        """
        return self._require_component("ledger", self._ledger, "the ledger store (C6)")

    @property
    def quarantine(self) -> object:
        """Return the quarantine store of this database (FR-10).

        Refuses with GrafxUnsupportedOperation when the composition has no quarantine store.
        """
        return self._require_component("quarantine", self._quarantine, "quarantine (C6)")

    @property
    def vectors(self) -> object:
        """Return the vector engine of this database (SPEC-VEC).

        Refuses with GrafxUnsupportedOperation when the composition has no vector engine.
        """
        return self._require_component("vectors", self._vectors, "the vector engine (C9)")

    @property
    def queries(self) -> object:
        """Return the query engine of this database.

        Refuses with GrafxUnsupportedOperation when the composition has no query engine.
        """
        return self._require_component("queries", self._queries, "the query engine (C10)")

    # --- transactions -------------------------------------------------------------------------

    def begin(self, mode: str = "write") -> Transaction:
        """Open a transaction in ``"read"`` or ``"write"`` mode (SPEC-M1 FR-2).

        A reader sees the consistent snapshot of the instant it opened for its whole life, even
        while other processes commit; readers never block writers and writers never block readers.
        The returned object is a context manager: leaving its block commits, and leaving it
        through an exception rolls back.
        """
        self._require_open()
        parsed = TransactionMode.parse(mode)
        if parsed is TransactionMode.WRITE:
            self._require_writable("begin a write transaction")
        return Transaction(self, self._transactions.begin(parsed.value))

    def retry(self, transaction: Transaction) -> Transaction:
        """Open the successor of a transaction optimistic validation refused (BR-6).

        The refused transaction is abandoned and a fresh one is opened above the commit that won,
        carrying the refusal count so the commit that finally succeeds is the one that reports
        ``oktografx_commit_retries_total``.
        """
        self._require_open()
        if not isinstance(transaction, Transaction):
            raise GrafxConfigurationError(
                f"A retry takes a Transaction; got {type(transaction).__name__}.",
                field="transaction",
                value=type(transaction).__name__,
            )
        # The loser's schema bookkeeping is settled BEFORE the successor opens: its working
        # catalog is dropped and its registered indexes pruned, so the successor's re-executed
        # DDL registers cleanly instead of being refused by its own predecessor's leftovers --
        # which made the documented BR-6 retry loop unable to ever succeed for a schema change,
        # and let the successor's later rows commit against a phantom table.
        self._settle_schema(transaction.context, committed=False)
        return Transaction(self, self._transactions.retry(transaction.context))

    @contextmanager
    def transaction(self, mode: str = "write") -> Iterator[Transaction]:
        """Open a transaction as a block, committing on a clean exit and rolling back otherwise."""
        txn = self.begin(mode)
        with txn:
            yield txn

    def execute(
        self, text: str, parameters: Mapping[str, object] | None = None
    ) -> object:
        """Run one statement in its own read transaction and return its result.

        This is the autocommit read of CONTRACT.md section 10. The transaction is opened, the
        statement is run and the transaction is committed, so the result never outlives a snapshot
        that has been released.
        """
        self._require_open()
        txn = self.begin("read")
        try:
            result = self.run_statement(txn.context, text, parameters)
        except BaseException:
            txn.rollback()
            raise
        txn.commit()
        return result

    def run_statement(
        self,
        context: TransactionContext,
        text: str,
        parameters: Mapping[str, object] | None = None,
    ) -> object:
        """Run one statement through the query engine on behalf of a transaction.

        Public because :class:`Transaction` reaches it, and documented as the single door every
        statement of this database passes through -- there is deliberately no second path that
        could diverge from it.
        """
        self._require_open()
        _require_text("statement", text)
        engine = self._require_component("queries", self._queries, "the query engine (C10)")
        with self._transactions.page_access_section():
            return engine.execute(text, context, parameters)  # type: ignore[attr-defined]

    # --- operator surface ---------------------------------------------------------------------

    def verify(self, scope: str = "all") -> object:
        """Walk the database and report every finding, precisely located (SPEC-M1 FR-11).

        ``scope`` is one of ``"pages"``, ``"records"``, ``"indexes"`` or ``"all"``. A clean
        database produces a report with no findings.
        """
        self._require_open()
        factory = self._require_component(
            "verifier", self._verifier_factory, "the verifier (C6)"
        )
        # Built per call, not held: a verifier is given the index set it must walk, and a
        # database registers indexes for as long as it is open. A verifier captured at open would
        # quietly report a clean "indexes" scope for every index registered after it -- a wrong
        # answer, which is worse than no answer.
        with self._transactions.page_access_section():
            verifier = factory()  # type: ignore[operator]
            return verifier.verify(scope)  # type: ignore[attr-defined]

    def recover(self) -> object:
        """Run a recovery pass and return its report (SPEC-M1 FR-8).

        The open sequence already ran one; this door exists so an operator can run another after
        repairing something, and so a caller that supplied its own registry can drive the pass
        itself.

        A read-only database refuses. Recovery is the most destructive sanctioned operation in
        the engine -- it quarantines byte ranges, truncates the log and replays pages -- and the
        open sequence already declines to run it for exactly that reason. Offering it again here
        handed a read-only handle the capability the open had just withheld, and it does so
        holding NO writer lease, so it would cut the log while another process appends under one.
        """
        self._require_open()
        self._require_writable("run recovery")
        manager = self._require_component("recovery", self._recovery, "recovery (C6)")
        # Quiescence is checked under the participant-local section and held through the pass;
        # checking ``open_transactions`` one instruction earlier would race with begin(). The
        # recovery manager itself takes the cross-process COMMIT_SECTION.
        with self._transactions.recovery_section():
            # Latch before the first replay effect. If the pass fails after installing only a
            # prefix, no later transaction on this handle may publish over the missing suffix.
            self._transactions.require_recovery()
            report = manager.run()  # type: ignore[attr-defined]
            self._transactions.recovery_completed()
        self._recovery_report = report
        indexes = self._indexes
        if indexes is not None:
            # Reacquire after recovery_section released. A concurrent post-barrier failure may
            # latch this participant in that gap; index open can mark/flush headers, so it must
            # either finish before that latch or refuse after it, never straddle it.
            with self._transactions.page_access_section():
                registered = indexes.indexes()  # type: ignore[attr-defined]
                self._attached_indexes = tuple(index.name for index in registered)
                self._stale_indexes = tuple(
                    index.name
                    for index in indexes.open(  # type: ignore[attr-defined]
                        self._transactions.published_lsn()
                    )
                )
        return report

    def flush(self) -> int:
        """Write every dirty page of this database back to the device and return the count.

        A read-only database refuses. It holds no dirty page today, so the write would be empty
        -- but "empty because of how the caller happened to use it" is not a guarantee, and the
        one thing a read-only handle promises is that no byte of another process's database
        moves because of it. The promise belongs at the door, not in the arithmetic behind it.
        """
        self._require_open()
        self._require_writable("flush pages")
        with self._transactions.page_access_section():
            return self._pool.flush()

    def checkpoint(self) -> object:
        """Put the committed state on the platter, publish the checkpoint, and reclaim the log (BR-10).

        Returns the recycling report: what the log released, what it kept back for a pinned
        reader, and what the platform deferred. A read-only database refuses -- a checkpoint
        publishes a control record and may release log segments, and neither is a reader's to do.

        The log grows until this is called. Every acknowledged commit is durable in the log
        whether or not a checkpoint ever runs, so skipping it costs space and recovery time,
        never data; when to run it is a lifecycle decision left to the caller (see CF-11).
        """
        self._require_open()
        self._require_writable("checkpoint the database")
        report = self._transactions.checkpoint()
        indexes = self._indexes
        if indexes is not None:
            # Checkpoint redo may have adopted schema and indexes committed by another
            # participant after this handle opened. Keep the public inventory aligned with the
            # registry that now serves queries, just as operator recovery does.
            with self._transactions.page_access_section():
                registered = indexes.indexes()  # type: ignore[attr-defined]
                self._attached_indexes = tuple(index.name for index in registered)
                self._stale_indexes = tuple(
                    index.name
                    for index in indexes.open(  # type: ignore[attr-defined]
                        self._transactions.published_lsn()
                    )
                )
        return report

    def snapshot_metrics(self) -> Mapping[str, object]:
        """Return the machine-readable current value of every metric this database emitted."""
        self._require_open()
        return self._metrics.snapshot()

    # --- lifecycle ----------------------------------------------------------------------------

    def close(self) -> None:
        """Release everything this database opened, and never corrupt anything doing it (FR-1).

        The order is the reverse of the order things were acquired, and every step runs even when
        an earlier one failed: an open transaction is aborted, its reader registration withdrawn,
        the dirty pages written back, and whatever the composition root opened is released. A
        database that fails to release something is still CLOSED afterwards -- refusing to record
        that would leave a caller with an object it can neither use nor retire -- and the first
        failure is raised once every step has been attempted.

        Closing twice is a no-op. Closing with a transaction open aborts it: nothing of an open
        transaction has reached the device, so abandoning it is the whole of that promise.
        """
        if self._closed:
            return
        # Marked closed BEFORE anything is released. A release path calls host-supplied code --
        # an event sink, a metrics publisher, a storage device -- and any of it may re-enter this
        # method; a flag set at the end would let the second entry release everything a second
        # time (A91). There is no lock here to make re-entry safe by exclusion, deliberately:
        # a lock held across foreign code is the defect A91 names.
        self._closed = True
        failures: list[BaseException] = []
        for step in (
            self._close_transactions,
            self._flush_pages,
            self._publish_metrics,
            self._release_closers,
        ):
            try:
                step()
            except BaseException as failure:
                failures.append(failure)
        if failures:
            raise failures[0]

    def __enter__(self) -> Self:
        """Return this database so a ``with`` block can use it."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """Close the database on the way out, never masking the exception that is unwinding."""
        if exc_type is None:
            self.close()
            return
        try:
            self.close()
        except BaseException:
            return

    def _close_transactions(self) -> None:
        """Abort every open transaction and withdraw every reader registration."""
        self._transactions.close()

    def _settle_schema(self, context: TransactionContext, *, committed: bool) -> None:
        """Tell the query engine one transaction's schema bookkeeping is over.

        On a rollback this is what takes back the two side effects a DDL statement makes outside
        the transaction -- the indexes it registered and the vector engine's space map -- and
        drops the working catalog copy. Never raises: it runs on the rollback path.
        """
        queries = self._queries
        settle = getattr(queries, "settle_schema", None)
        if callable(settle):
            settle(context.txn_id, committed=committed)

    def _flush_pages(self) -> None:
        """Write dirty pages back, unless the database was opened read-only.

        The write ahead log is the authority for durability, so this is a courtesy rather than a
        commit: every acknowledged commit is already durable in the log and redo is idempotent
        through ``page_lsn``. It is skipped on a read-only database, which has nothing of its own
        to write and must not touch a database another process owns.

        It is also skipped after this participant has latched ``recovery_required``. A failed
        post-COMMIT redo can leave an old dirty frame resident while another participant completes
        the same WAL gap and writes a newer page. Flushing the failed participant during close
        would then overwrite the newer page with that stale frame. Retaining WAL and dropping the
        cache is the only fail-closed close: the next writable open replays the authoritative log.
        """
        if self._read_only or self._transactions.recovery_required:
            return
        self._pool.flush()
        # Last chance to say what the pages an abandoned attempt allocated actually are. After
        # this pool stops they can never be reused, and left unwritten they are indistinguishable
        # from the pages a crash leaves half-allocated.
        self._pool.settle_abandoned()

    def _publish_metrics(self) -> None:
        """Publish the metrics document, on the one sink whose selector promises a file.

        ``metrics="json"`` documents that the sink writes to the destination the configuration
        names, and nothing in the shipped composition ever called ``publish()`` -- so the
        documented outcome was unreachable and the flag was silently inert. Close is the moment
        the numbers are final. A sink with no ``publish`` door has nothing to do here, and a
        refusal is collected with the other close failures rather than lost.
        """
        publish = getattr(self._metrics, "publish", None)
        if callable(publish):
            publish()

    def _release_closers(self) -> None:
        """Run every release the composition root handed over, and raise the first failure.

        Each closer runs even when an earlier one raised: a socket left bound because a device
        failed to close is the leak this method exists to prevent. Every ``BaseException`` is
        retained unchanged, including process-control signals; the first is re-raised only after
        every remaining resource has had its one chance to close.
        """
        failures: list[BaseException] = []
        # Reverse of the order the composition root opened them, so an inner resource is released
        # before the outer one it depends on.
        for closer in reversed(self._closers):
            try:
                closer()
            except BaseException as failure:
                failures.append(failure)
        if failures:
            raise failures[0]

    # --- internals ----------------------------------------------------------------------------

    def _require_writable(self, operation: str) -> None:
        """Refuse an operation that would write, on a database opened read-only.

        One guard for every writing door, because the alternative is a per-door habit and this
        component already lost that argument once: the write-transaction door had the check and
        the recovery door did not, so a read-only handle could truncate the log, delete a segment
        and create a quarantine directory -- while holding no writer lease.
        """
        if not self._read_only:
            return
        raise GrafxUnsupportedOperation(
            f"This database at {self._path!r} was opened read-only, so it cannot {operation}.",
            path=self._path,
            operation=operation,
            field="read_only",
        )

    def _require_open(self) -> None:
        """Refuse every door of a database that has been closed."""
        if self._closed:
            raise GrafxUnsupportedOperation(
                f"This database at {self._path!r} is closed; open it again to use it.",
                path=self._path,
            )

    def _require_component(self, slot: str, component: object, owner: str) -> object:
        """Return a composed component, or refuse naming which one is absent."""
        if component is None:
            raise GrafxUnsupportedOperation(
                f"This database was composed without {owner}, so {slot!r} is not available.",
                component=slot,
                path=self._path,
            )
        return component

    def __repr__(self) -> str:
        """Return a representation naming the path and whether the database is still open."""
        state = "closed" if self._closed else "open"
        return f"Database(path={self._path!r}, {state})"
