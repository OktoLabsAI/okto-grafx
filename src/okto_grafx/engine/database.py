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

from okto_grafx.temporal_diff import TemporalDiff

from okto_grafx.domain.index.fulltext import TextIndexOptions, TextSearchLimits, TextSearchResult
from okto_grafx.domain.query.hybrid import HybridSearchOptions, HybridSearchResult
from okto_grafx.engine.hybrid import search_hybrid as _search_hybrid
from okto_grafx.engine.fulltext import create_text_index as _create_text_index, search_text as _search_text
from okto_grafx.domain.query.text_procedure import text_call

import struct
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Self, TypeVar
from okto_grafx.domain.model.schema import ColumnDef, TableDef

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.index.keys import (
    MAX_BUCKET_COUNT,
    TARGET_ENTRIES_PER_BUCKET,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    HEAP_RECLAIM_V1_CAPABILITY,
)
from okto_grafx.domain.model.value import (
    INT64_MAX,
    INT64_MIN,
    Timestamp,
    Uuid,
    Value,
    VectorValue,
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
from okto_grafx.domain.query.ast import Query as QueryStatement
from okto_grafx.domain.query.control import CancellationToken, _ReadControl, _read_control
from okto_grafx.engine.index_distribution import IndexDistribution
from okto_grafx.engine.key_page_memo import KeyPageCacheUsage
from okto_grafx.domain.query.limits import (
    DEFAULT_MAX_QUERY_VALUE_CHARACTERS,
    MAX_COLUMN_DEFINITIONS,
    MAX_MAP_ENTRIES,
    MAX_QUERY_VALUE_CHARACTERS,
)
from okto_grafx.domain.query.plan import PlanNode
from okto_grafx.domain.recovery.report import RecoveryReport
from okto_grafx.domain.txn.context import (
    CommitReport,
    TransactionContext,
    TransactionMode,
    TransactionState,
)
from okto_grafx.domain.txn.partitions import page_partition, partition_key
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.domain.txn.commit_metadata import CommitMetadata, capture_commit_metadata, decode_commit_metadata
from okto_grafx.domain.txn.commit_catalog import CommitCatalogEntry
from okto_grafx.domain.txn.commit_history import CommitHistoryPage
from okto_grafx.engine.commit_history_reader import observe_commit_catalog
from okto_grafx.domain.temporal import TemporalCompactionReport, TemporalGraph, TemporalLimits, TemporalPin, TemporalPruneReport, TemporalVersions
from okto_grafx.engine.commit_catalog_store import CommitCatalogStore
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.domain.verify.findings import VerificationReport, VerificationFinding, FindingKind, FindingLocation
from okto_grafx.domain.wal.replay import RecycleReport
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.index_cleanup import IndexCleanupReport, _cleanup_indexes
from okto_grafx.engine.heap_store import HeapStore, _HeapScanPosition
from okto_grafx.engine.metrics_catalog import metric
from okto_grafx.engine.public_views import (
    BloatReport,
    BufferPoolView,
    CatalogStoreView,
    ClockView,
    CodecView,
    ComponentView,
    CoordinatorView,
    HeapStoreView,
    IndexView,
    IndexRegistryView,
    LedgerView,
    MaintenanceStatus,
    MetricsSnapshotView,
    MetricsView,
    QuarantineView,
    QueryEngineView,
    StorageView,
    TableBloatReport,
    TableVacuumReport,
    TransactionManagerView,
    VectorEngineView,
    VectorIndexView,
    VectorMathView,
    VacuumReport,
    WalView,
    _builtin_bool,
    _builtin_bytes,
    _builtin_float,
    _builtin_int,
    _builtin_optional_text,
    _builtin_text,
    _builtin_type_name,
    _catalog_view_from,
    _clock_view,
    _codec_view,
    _component_view,
    _coordinator_view,
    _domain_field,
    _heap_view,
    _index_entry_view,
    _indexes_view,
    _ledger_view,
    _metrics_snapshot_view,
    _pool_view,
    _quarantine_view,
    _query_parameters_snapshot,
    _query_plan_view,
    _query_result_view,
    _query_statistics_snapshot,
    _query_text_snapshot,
    _query_value_snapshot,
    _record_id_filter_snapshot,
    _recycle_report_view,
    _queries_view,
    _recovery_report_view,
    _require_positive_integer,
    _storage_view,
    _transactions_view,
    _tuple_items,
    _verification_report_view,
    _vector_query_snapshot,
    _vector_search_result_view,
    _vectors_view,
    _wal_view,
)
from okto_grafx.engine.query_engine import QueryEngine, QueryResult
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.vector_engine import VectorSearchResult
from okto_grafx.engine.vector_memory import VectorMemoryUsage, VectorTotalMemoryUsage
from okto_grafx.engine.verifier import VERIFICATION_SCOPES

_HistoryResult = TypeVar("_HistoryResult")

if TYPE_CHECKING:  # pragma: no cover - typing only
    from okto_grafx.views import LogicalViews
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
    "ExecuteManyReport",
    "MetaStore",
    "Query",
    "QueryCursor",
    "ScanCursorV1",
    "ScanPageV1",
    "ScanRowV1",
    "Transaction",
]

DEFAULT_QUERY_CURSOR_BATCH_ROWS: int = 256
"""Rows pulled per iterator refill when a caller does not select a cursor batch size."""

MAX_QUERY_CURSOR_BATCH_ROWS: int = 65_536
"""Hard guard against turning one streaming pull back into an unbounded materialisation."""

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

IDENTITY_FORMAT_VERSION: int = 2
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
    return _builtin_text(value, field=field, empty=False)


def _index_columns_snapshot(columns: object) -> tuple[str, ...]:
    """Detach one bounded ordered column sequence before entering engine coordination."""
    if issubclass(type(columns), (str, bytes, bytearray, memoryview)):
        raise GrafxConfigurationError(
            "Index columns must be a sequence of column names, not a scalar string or buffer.",
            field="columns",
            value=_builtin_type_name(columns),
        )
    if not isinstance(columns, Sequence):
        raise GrafxConfigurationError(
            "Index columns must be an ordered sequence of column names.",
            field="columns",
            value=_builtin_type_name(columns),
        )
    try:
        if issubclass(type(columns), tuple):
            iterator = tuple.__iter__(columns)
        elif issubclass(type(columns), list):
            iterator = list.__iter__(columns)
        else:
            iterator = iter(columns)  # type: ignore[arg-type]
        detached: list[str] = []
        for column in iterator:
            detached.append(_builtin_text(column, field="columns", empty=False))
            if len(detached) > MAX_COLUMN_DEFINITIONS:
                raise GrafxConfigurationError(
                    f"An index may name at most {MAX_COLUMN_DEFINITIONS} columns.",
                    field="columns",
                    value=len(detached),
                    maximum=MAX_COLUMN_DEFINITIONS,
                )
    except GrafxError:
        raise
    except (TypeError, ValueError, OverflowError) as failure:
        raise GrafxConfigurationError(
            "Index columns must be an iterable of column names.",
            field="columns",
            value=_builtin_type_name(columns),
        ) from failure
    if not detached:
        raise GrafxConfigurationError(
            "An exact index must name at least one column.",
            field="columns",
            value=0,
        )
    return tuple(detached)


def _note_cleanup_failure(primary: BaseException, cleanup: BaseException) -> None:
    """Attach cleanup evidence without changing the exception that caused the unwind."""
    try:
        note = f"Additional cleanup failure: {_builtin_type_name(cleanup)}"
        arguments = BaseException.__dict__["args"].__get__(cleanup, type(cleanup))
        if type(arguments) is tuple and arguments:
            first = next(tuple.__iter__(arguments))
            if issubclass(type(first), str):
                note += f": {str.__str__(first)}"
        primary.add_note(note + ".")
    except BaseException:
        # Exception note support is diagnostic only; an exotic exception implementation must
        # not replace either the primary failure or the cleanup result it was meant to report.
        return


def _note_batch_index(failure: GrafxError, batch_index: int) -> None:
    """Add bounded batch context without letting hostile diagnostics replace the failure."""
    try:
        details = object.__getattribute__(failure, "details")
        if type(details) is dict:
            dict.setdefault(details, "batch_index", batch_index)
    except BaseException:
        # Diagnostic enrichment is optional; the original typed error remains authoritative.
        return


def _public_operation_failure(
    operation: str, failure: Exception
) -> GrafxConfigurationError:
    """Translate a collaborator's ordinary exception without executing its diagnostics."""
    observed = _builtin_type_name(failure)
    return GrafxConfigurationError(
        f"The public {operation} operation failed because a collaborator raised {observed}.",
        field=operation,
        cause=observed,
    )


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
        database_uuid = self.database_uuid
        if not issubclass(type(database_uuid), bytes):
            raise GrafxConfigurationError(
                f"A database identity is exactly {_UUID_BYTES} bytes; got "
                f"{_builtin_type_name(database_uuid)}.",
                field="database_uuid",
                value=_builtin_type_name(database_uuid),
            )
        database_uuid = _builtin_bytes(database_uuid)
        object.__setattr__(self, "database_uuid", database_uuid)
        if len(database_uuid) != _UUID_BYTES:
            raise GrafxConfigurationError(
                f"A database identity is exactly {_UUID_BYTES} bytes; got "
                f"{len(database_uuid)}.",
                field="database_uuid",
                value=len(database_uuid),
            )
        for field, value, ceiling in (
            ("format_version", self.format_version, 0xFFFF),
            ("page_size", self.page_size, 0xFFFFFFFF),
            ("partitions_per_table", self.partitions_per_table, 0xFFFF),
        ):
            if type(value) is bool or not issubclass(type(value), int):
                observed = _builtin_type_name(value)
                raise GrafxConfigurationError(
                    f"The identity field {field!r} must be an integer; got {observed}.",
                    field=field,
                    value=observed,
                )
            plain = int.__int__(value)
            object.__setattr__(self, field, plain)
            if not 0 <= plain <= ceiling:
                raise GrafxConfigurationError(
                    f"The identity field {field!r} is outside its width: {plain}.",
                    field=field,
                    value=plain,
                )
        created_at_wall = self.created_at_wall
        if (
            not issubclass(type(created_at_wall), (int, float))
            or type(created_at_wall) is bool
        ):
            observed = _builtin_type_name(created_at_wall)
            raise GrafxConfigurationError(
                f"The creation stamp must be a number; got {observed}.",
                field="created_at_wall",
                value=observed,
            )
        object.__setattr__(self, "created_at_wall", _builtin_float(created_at_wall))
        descriptor = self.granularity_descriptor
        if not issubclass(type(descriptor), str):
            observed = _builtin_type_name(descriptor)
            raise GrafxConfigurationError(
                f"The granularity descriptor must be a string; got {observed}.",
                field="granularity_descriptor",
                value=observed,
            )
        descriptor = str.__str__(descriptor)
        object.__setattr__(self, "granularity_descriptor", descriptor)
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
        raw_is_bytes = issubclass(type(raw), (bytes, bytearray))
        payload = _builtin_bytes(raw) if raw_is_bytes else b""
        if not raw_is_bytes or len(payload) < minimum:
            raise GrafxCorruptionDetected(
                f"An identity record is at least {minimum} bytes; this one holds "
                f"{len(payload)}.",
                field="identity_length",
                value=len(payload),
            )
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


def _public_identity(value: DatabaseIdentity) -> DatabaseIdentity:
    """Rebuild the identity with exact scalar leaves for the public facade.

    The private identity remains the engine's original value.  This copy has the same encoded
    record and equality semantics, while stripping any host-defined subclass accepted by the
    domain constructor's intentionally structural validation.
    """
    if not issubclass(type(value), DatabaseIdentity):
        raise GrafxConfigurationError(
            f"A public database identity must be a DatabaseIdentity; got "
            f"{_builtin_type_name(value)}.",
            field="identity",
            value=_builtin_type_name(value),
        )
    return DatabaseIdentity(
        database_uuid=_builtin_bytes(
            _domain_field(value, DatabaseIdentity, "database_uuid")
        ),
        page_size=_builtin_int(_domain_field(value, DatabaseIdentity, "page_size")),
        partitions_per_table=_builtin_int(
            _domain_field(value, DatabaseIdentity, "partitions_per_table")
        ),
        created_at_wall=_builtin_float(
            _domain_field(value, DatabaseIdentity, "created_at_wall")
        ),
        granularity_descriptor=_builtin_text(
            _domain_field(value, DatabaseIdentity, "granularity_descriptor"),
            field="identity.granularity_descriptor",
        ),
        format_version=_builtin_int(
            _domain_field(value, DatabaseIdentity, "format_version")
        ),
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


def _public_commit_report(value: CommitReport | None) -> CommitReport | None:
    """Copy a commit outcome into the exact public report type and exact scalar leaves."""
    if value is None:
        return None
    if not issubclass(type(value), CommitReport):
        raise GrafxConfigurationError(
            f"A public commit report must be a CommitReport; got "
            f"{_builtin_type_name(value)}.",
            field="report",
            value=_builtin_type_name(value),
        )
    observed_csn = _domain_field(value, CommitReport, "_csn")
    observed_durable = _domain_field(value, CommitReport, "_durable")
    observed_wrote = _domain_field(value, CommitReport, "_wrote")
    csn = _builtin_int(observed_csn)
    durable = _builtin_bool(observed_durable)
    wrote = _builtin_bool(observed_wrote)
    return CommitReport(csn=csn, durable=durable, wrote=wrote)


def _public_snapshot(value: Snapshot) -> Snapshot:
    """Copy a transaction snapshot into its exact domain type and integer leaf."""
    if not issubclass(type(value), Snapshot):
        raise GrafxConfigurationError(
            f"A public transaction snapshot must be a Snapshot; got "
            f"{_builtin_type_name(value)}.",
            field="snapshot",
            value=_builtin_type_name(value),
        )
    return Snapshot(_builtin_int(_domain_field(value, Snapshot, "read_lsn")))


def _engine_owns_prepared_plan(engine: object, plan: object) -> bool:
    """Trust memoization only after the exact built-in engine proves root ownership."""
    return type(engine) is QueryEngine and engine._owns_prepared_plan(plan)


@dataclass(frozen=True, slots=True)
class ScanRowV1:
    """One detached stored row in table-column order."""

    record_id: int
    values: tuple[Value, ...]


_SCAN_EXACT_IMMUTABLE_VALUE_TYPES: frozenset[type[object]] = frozenset(
    {type(None), bool, int, float, str, bytes, Timestamp, Uuid, VectorValue}
)


def _scan_exact_scalar_values_snapshot(
    values: tuple[Value, ...], *, max_string_characters: int
) -> tuple[Value, ...] | None:
    """Retain one decoded tuple when every leaf is an exact immutable scalar.

    Heap decoding owns this exact tuple and constructs every type admitted here from stored
    bytes. None of them can retain a page, collaborator or mutable child: UUID owns exact bytes,
    vectors own a tuple of exact floats, and the remaining shapes are built-in immutables. A
    compound value or an over-limit string declines to the canonical deep copier, which preserves
    its public detachment and refusal details. This is deliberately not a general public-value
    shortcut; it is scoped to the output of ``HeapStore.scan_page``.
    """
    for value in values:
        value_type = type(value)
        if value_type not in _SCAN_EXACT_IMMUTABLE_VALUE_TYPES:
            return None
        if value_type is int and not INT64_MIN <= value <= INT64_MAX:
            return None
        if value_type is str and len(value) > max_string_characters:
            return None
    return values


class ScanCursorV1:
    """Opaque, process-local continuation minted by :meth:`Transaction.scan_rows_v1`.

    A cursor is deliberately neither serializable nor constructible by callers.  It names a
    physical continuation only inside the read transaction that minted it; the next page checks
    that ownership again before reaching the catalog or heap.
    """

    __slots__ = (
        "_consumed",
        "_owner",
        "_position",
        "_schema_version",
        "_table_id",
        "_table_name",
        "_columns",
    )

    def __init__(self) -> None:
        raise GrafxConfigurationError(
            "A scan cursor is created only by Transaction.scan_rows_v1().",
            field="cursor",
            value="caller_constructed",
        )

    @classmethod
    def _create(
        cls,
        *,
        owner: object,
        table_name: str,
        table_id: int,
        schema_version: int,
        position: _HeapScanPosition,
        columns: tuple[str, ...] | None = None,
    ) -> ScanCursorV1:
        cursor = object.__new__(cls)
        object.__setattr__(cursor, "_consumed", False)
        object.__setattr__(cursor, "_owner", owner)
        object.__setattr__(cursor, "_table_name", table_name)
        object.__setattr__(cursor, "_table_id", table_id)
        object.__setattr__(cursor, "_schema_version", schema_version)
        object.__setattr__(cursor, "_position", position)
        object.__setattr__(cursor, "_columns", columns)
        return cursor

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ScanCursorV1 is immutable")

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("ScanCursorV1 is process-local and cannot be serialized")

    def __repr__(self) -> str:
        return "ScanCursorV1(<opaque>)"


@dataclass(frozen=True, slots=True)
class ScanPageV1:
    """One bounded page of detached rows and its optional continuation."""

    rows: tuple[ScanRowV1, ...]
    next_cursor: ScanCursorV1 | None


@dataclass(frozen=True, slots=True)
class ExecuteManyReport:
    """Bounded summary of an atomic :meth:`Transaction.executemany` call.

    The report deliberately carries no statement results, plans or input parameter payloads.
    Its statistics are the per-statement counters added in input order.
    """

    statements: int
    statistics: Mapping[str, int]

    def __post_init__(self) -> None:
        """Own the small public summary and reject forged negative counters."""
        statements = _builtin_int(self.statements, field="executemany.statements")
        if statements < 0:
            raise GrafxConfigurationError(
                "An executemany statement count cannot be negative.",
                field="executemany.statements",
                value=statements,
            )
        statistics = _query_statistics_snapshot(self.statistics)
        object.__setattr__(self, "statements", statements)
        object.__setattr__(self, "statistics", statistics)


def _scan_cursor_payload(
    value: object,
    *,
    owner: object,
    table_name: str,
) -> tuple[int, int, _HeapScanPosition, ScanCursorV1]:
    """Validate and unwrap one exact cursor without invoking caller-defined behavior."""

    if type(value) is not ScanCursorV1:
        raise GrafxConfigurationError(
            "A scan continuation must be an exact ScanCursorV1 returned by this transaction.",
            field="cursor",
            value=_builtin_type_name(value),
        )
    try:
        observed_owner = object.__getattribute__(value, "_owner")
        observed_consumed = object.__getattribute__(value, "_consumed")
        observed_name = object.__getattribute__(value, "_table_name")
        observed_table_id = object.__getattribute__(value, "_table_id")
        observed_schema_version = object.__getattribute__(value, "_schema_version")
        observed_position = object.__getattribute__(value, "_position")
    except AttributeError as failure:
        raise GrafxConfigurationError(
            "A scan continuation is incomplete and was not minted by this transaction.",
            field="cursor",
            value="malformed",
        ) from failure
    if observed_owner is not owner:
        raise GrafxTransactionStateError(
            "A scan continuation cannot cross transaction or database boundaries.",
            operation="scan_rows_v1",
            field="cursor_owner",
        )
    if type(observed_name) is not str or observed_name != table_name:
        raise GrafxTransactionStateError(
            "A scan continuation cannot be used for a different table.",
            operation="scan_rows_v1",
            field="cursor_table",
            value=(
                observed_name
                if type(observed_name) is str
                else _builtin_type_name(observed_name)
            ),
            table=table_name,
        )
    if (
        type(observed_table_id) is not int
        or type(observed_schema_version) is not int
        or type(observed_position) is not _HeapScanPosition
        or type(observed_consumed) is not bool
    ):
        raise GrafxConfigurationError(
            "A scan continuation carries malformed internal fields.",
            field="cursor",
            value="malformed",
        )
    return observed_table_id, observed_schema_version, observed_position, value


def _query_cursor_batch_size(value: object) -> int:
    """Return one bounded exact cursor batch size."""
    size = _require_positive_integer("batch_size", value)
    if size > MAX_QUERY_CURSOR_BATCH_ROWS:
        raise GrafxConfigurationError(
            f"A query cursor batch may contain at most {MAX_QUERY_CURSOR_BATCH_ROWS} rows; "
            f"got {size}.",
            field="batch_size",
            value=size,
            maximum=MAX_QUERY_CURSOR_BATCH_ROWS,
        )
    return size


class Query:
    """A canonical read statement that can open independent snapshot-owning cursors."""

    __slots__ = ("_database", "_parameters", "_text")

    def __init__(
        self,
        database: Database,
        text: str,
        parameters: Mapping[str, object] | None,
    ) -> None:
        self._database = database
        self._text = text
        self._parameters = parameters

    def cursor(
        self,
        *,
        batch_size: int = DEFAULT_QUERY_CURSOR_BATCH_ROWS,
        timeout_seconds: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> QueryCursor:
        """Open a cursor whose read transaction lives until exhaustion or explicit close."""
        return self._database._open_query_cursor(
            self._text,
            self._parameters,
            batch_size=_query_cursor_batch_size(batch_size),
            control=_read_control(self._database._clock, timeout_seconds, cancellation),
        )


class QueryCursor:
    """A bounded pull cursor over one fixed MVCC read snapshot.

    Iteration refills at most ``batch_size`` detached rows at a time.  The cursor owns its read
    transaction and releases the reader pin on exhaustion, :meth:`close`, or context-manager
    exit.  It is intentionally not a write door and is not safe for concurrent consumption.
    """

    __slots__ = (
        "_batch_size",
        "_buffer",
        "_buffer_position",
        "_closed",
        "_database",
        "_raw",
        "_control",
        "_source_done",
        "_transaction",
        "columns",
        "plan",
    )

    def __init__(
        self,
        *,
        database: Database,
        transaction: Transaction,
        raw: object,
        columns: tuple[str, ...],
        plan: PlanNode,
        batch_size: int,
        control: _ReadControl | None = None,
    ) -> None:
        self._database = database
        self._transaction = transaction
        self._raw = raw
        self._control = control
        self._batch_size = batch_size
        self._buffer: tuple[tuple[Value, ...], ...] = ()
        self._buffer_position = 0
        self._source_done = False
        self._closed = False
        self.columns = columns
        self.plan = plan

    @property
    def closed(self) -> bool:
        """Return whether this cursor has released its snapshot and buffered rows."""
        return self._closed

    @property
    def statistics(self) -> Mapping[str, int]:
        """Return an immutable-shape snapshot of counters observed so far."""
        observed = getattr(self._raw, "statistics", None)
        if not isinstance(observed, dict):
            raise GrafxConfigurationError(
                "The query cursor collaborator returned malformed statistics.",
                field="cursor.statistics",
                value=_builtin_type_name(observed),
            )
        # QueryResult performs the same exact integer/name validation as execute().  Return its
        # copied dictionary rather than exposing the engine's live counter map.
        return QueryResult(statistics=dict(observed)).statistics

    def fetchone(self) -> tuple[Value, ...] | None:
        """Return the next detached row, or ``None`` after exhaustion."""
        batch = self.fetchmany(1)
        return None if not batch else batch[0]

    def fetchmany(self, size: int | None = None) -> tuple[tuple[Value, ...], ...]:
        """Return at most ``size`` detached rows without materialising the remaining result."""
        wanted = self._batch_size if size is None else _query_cursor_batch_size(size)
        if self._closed:
            return ()
        self._require_database_open()
        selected: list[tuple[Value, ...]] = []
        while self._buffer_position < len(self._buffer) and len(selected) < wanted:
            selected.append(self._buffer[self._buffer_position])
            self._buffer_position += 1
        if self._buffer_position == len(self._buffer):
            self._buffer = ()
            self._buffer_position = 0
        if len(selected) < wanted and not self._source_done:
            rows, exhausted = self._database._fetch_query_cursor(
                self,
                wanted - len(selected),
            )
            selected.extend(rows)
            self._source_done = exhausted
        if self._source_done and not self._buffer:
            self._closed = True
        return tuple(selected)

    def close(self) -> None:
        """Discard unread rows and release the owned read snapshot idempotently."""
        if self._closed:
            return
        self._buffer = ()
        self._buffer_position = 0
        try:
            self._database._close_query_cursor(self)
        finally:
            self._source_done = True
            self._closed = True

    def __iter__(self) -> QueryCursor:
        return self

    def __next__(self) -> tuple[Value, ...]:
        if self._closed:
            raise StopIteration
        self._require_database_open()
        if self._buffer_position >= len(self._buffer):
            rows, exhausted = self._database._fetch_query_cursor(
                self,
                self._batch_size,
            )
            self._buffer = rows
            self._buffer_position = 0
            self._source_done = exhausted
            if not rows:
                self._closed = True
                raise StopIteration
        row = self._buffer[self._buffer_position]
        self._buffer_position += 1
        if self._buffer_position == len(self._buffer):
            self._buffer = ()
            self._buffer_position = 0
            if self._source_done:
                self._closed = True
        return row

    def __enter__(self) -> QueryCursor:
        return self

    def _require_database_open(self) -> None:
        """Make a database close terminal even when this cursor buffered detached rows."""
        try:
            self._database._require_open()
            if self._control is not None:
                self._control.check()
        except BaseException as failure:
            try:
                self._database._close_query_cursor(self)
            except BaseException as cleanup_failure:
                _note_cleanup_failure(failure, cleanup_failure)
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        if exc_type is None:
            self.close()
            return
        try:
            self.close()
        except BaseException as cleanup_failure:
            if exc is not None:
                _note_cleanup_failure(exc, cleanup_failure)


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

    __slots__ = (
        "_database",
        "_context",
        "_report",
        "_finished",
        "_scan_owner",
        "_batch_active",
        "__weakref__",
    )

    def __init__(self, database: Database, context: TransactionContext) -> None:
        """Adopt a transaction context the manager of ``database`` has just opened."""
        self._database: Database = database
        self._context: TransactionContext = context
        self._report: CommitReport | None = None
        self._finished: bool = False
        self._scan_owner: object = object()
        self._batch_active: bool = False

    @property
    def mode(self) -> str:
        """Return ``"read"`` or ``"write"``, the mode this transaction was opened in."""
        return _builtin_text(
            self._context.mode.value, field="transaction.mode", empty=False
        )

    @property
    def snapshot(self) -> Snapshot:
        """Return the fixed view every read of this transaction sees (SPEC-M1 FR-2)."""
        return _public_snapshot(self._context.snapshot)

    @property
    def txn_id(self) -> int:
        """Return the process-local number of this transaction."""
        return _builtin_int(self._context.txn_id)

    @property
    def active(self) -> bool:
        """Return True while this transaction can still commit or roll back."""
        return _builtin_bool(not self._finished and self._context.active)

    @property
    def report(self) -> CommitReport | None:
        """Return what the commit reported, or None while the transaction is still open."""
        return _public_commit_report(self._report)

    def execute(
        self,
        text: str,
        parameters: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> QueryResult:
        """Run one statement inside this transaction and return its result.

        Text and parameters are deeply canonicalised before the query engine is reached. A
        database composed without one refuses here with GrafxUnsupportedOperation rather than
        pretending to run anything.
        """
        self._require_active()
        self._require_batch_idle("execute")
        if (
            (timeout_seconds is not None or cancellation is not None)
            and self._context.mode is not TransactionMode.READ
        ):
            raise GrafxUnsupportedOperation(
                "Execution cancellation and deadlines require a read transaction.",
                operation="execute", field="mode",
            )
        control = _read_control(self._database._clock, timeout_seconds, cancellation)
        return self._database._run_statement(self._context, text, parameters, control=control)

    def system_as_of(self, at: CommitId | Timestamp, *, tables: tuple[str, ...],
                     limits: TemporalLimits = TemporalLimits()) -> TemporalGraph:
        """Read durable system-time rows under this transaction's snapshot, excluding private writes."""
        self._require_active()
        return self._database._read_system_history(self._context, at=at, tables=tables, limits=limits)

    def system_diff(self, before: CommitId, after: CommitId, *, tables: tuple[str, ...],
                    limits: TemporalLimits = TemporalLimits(), max_changes: int = 100_000) -> TemporalDiff:
        """Return a bounded same-store historical graph diff under this transaction's snapshot."""
        from okto_grafx.temporal_diff import diff_graph
        self._require_active()
        return diff_graph(self, before, after, tables=tables, limits=limits, max_changes=max_changes)

    def system_versions(self, table: str, record_id: int, *,
                        limits: TemporalLimits = TemporalLimits()) -> TemporalVersions:
        """Read one logical row's intervals visible to this snapshot, not later commits."""
        self._require_active()
        return self._database._read_system_history(self._context,
            at=CommitId(self._database.identity.database_uuid, self._context.snapshot.read_lsn),
            tables=(table,), limits=limits, record_id=record_id)

    def commit_history(self, *, after: CommitId | None = None, limit: int = 100) -> CommitHistoryPage:
        """Read an ascending bounded history page under this transaction's snapshot."""
        self._require_active()
        return self._database._commit_history(self._context, after=after, limit=limit)

    def lookup_commit(self, identity: CommitId) -> CommitCatalogEntry | None:
        """Look up a qualified commit visible here; None does not certify legacy absence."""
        self._require_active()
        return self._database._lookup_commit(self._context, identity)

    def executemany(
        self,
        text: str,
        parameter_sets: Iterable[Mapping[str, object]],
    ) -> ExecuteManyReport:
        """Stage one updating statement for every parameter mapping, atomically as a batch.

        The input is consumed lazily and in order. If any item refuses, every change made by
        this call is discarded while work staged before the call remains available to commit.
        The caller still owns the transaction and chooses when to commit it.
        """
        self._require_active()
        self._require_batch_idle("executemany")
        self._batch_active = True
        try:
            return self._database._run_many(self._context, text, parameter_sets)
        finally:
            self._batch_active = False

    def scan_rows_v1(
        self,
        table: str,
        *,
        limit: int,
        cursor: ScanCursorV1 | None = None,
        columns: tuple[str, ...] | None = None,
        max_batch_bytes: int | None = None,
        timeout_seconds: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> ScanPageV1:
        """Read one bounded page of physical rows under this transaction's fixed snapshot.

        By default values follow ``TableDef.columns``; relationship endpoints lead the tuple.
        Explicit ``columns`` selects values in requested order, or identities only when empty.
        Omitted payloads remain validated. The one-shot cursor binds this transaction, table
        and exact projection. Optional logical batch bounds and cooperative read controls
        refuse without returning a partial page; the caller retains its transaction.
        """

        self._require_active()
        self._require_batch_idle("scan_rows_v1")
        if self._context.mode is not TransactionMode.READ:
            raise GrafxTransactionStateError(
                "scan_rows_v1 requires a read transaction.",
                txn_id=self._context.txn_id,
                mode=self._context.mode.value,
                operation="scan_rows_v1",
            )
        table_name = _builtin_text(table, field="table", empty=False)
        page_limit = _require_positive_integer("limit", limit)
        if columns is not None and (type(columns) is not tuple or len(columns) > 256
                or any(type(c) is not str or not c for c in columns)
                or len(set(columns)) != len(columns)):
            raise GrafxConfigurationError("columns must be a tuple of distinct names or None.", field="columns")
        if max_batch_bytes is not None:
            max_batch_bytes = _require_positive_integer("max_batch_bytes", max_batch_bytes)
            if max_batch_bytes > 2**31 or page_limit > 65536:
                raise GrafxConfigurationError("Bounded scans allow <=65536 rows and <=2^31 bytes.", field="limit")
        payload = (
            None
            if cursor is None
            else _scan_cursor_payload(
                cursor,
                owner=self._scan_owner,
                table_name=table_name,
            )
        )
        return self._database._scan_rows_v1(
            self._context,
            table=table_name,
            limit=page_limit,
            cursor_payload=payload,
            cursor_owner=self._scan_owner,
            columns=columns, max_batch_bytes=max_batch_bytes,
            control=_read_control(self._database._clock, timeout_seconds, cancellation),
        )

    def commit(self) -> CommitReport:
        """Commit this transaction and return the report of CONTRACT.md section 8.5.

        A refusal leaves the transaction usable: an optimistic conflict is retryable, and the
        transaction that lost is still active and still free of side effects, so a caller may
        call :meth:`retry` on the database and try again.
        """
        with self._database._public_transition():
            self._require_active()
            self._require_batch_idle("commit")
            try:
                report = self._database._transactions.commit(self._context)
            except BaseException as failure:
                # A post-barrier apply/publication failure is reported only after the manager
                # has made the durable outcome irrevocable and retired this context.  Publish
                # the same outcome on the wrapper and settle its schema journal before the
                # failure escapes; otherwise callers see an apparently reusable transaction
                # and close has to guess whether its DDL committed.
                if self._context.state is TransactionState.COMMITTED:
                    self._report = CommitReport(
                        csn=_builtin_int(self._context.commit_csn),
                        durable=True,
                        wrote=_builtin_bool(self._context.wrote),
                    )
                    self._finished = True
                    try:
                        self._database._settle_schema(self._context, committed=True)
                    except BaseException as settlement_failure:
                        _note_cleanup_failure(failure, settlement_failure)
                elif self._context.state is TransactionState.ABORTED:
                    # Terminal close can win through a re-entrant host callback.  The wrapper
                    # must agree with that outcome, and any tracked schema work is rollback work.
                    self._finished = True
                    try:
                        self._database._settle_schema(self._context, committed=False)
                    except BaseException as settlement_failure:
                        _note_cleanup_failure(failure, settlement_failure)
                else:
                    # Validation and every other pre-barrier refusal leave the transaction
                    # ACTIVE and retryable through its documented lifecycle doors.
                    self._finished = False
                raise
            public_report = _public_commit_report(report)
            if (
                public_report is None
            ):  # pragma: no cover - manager success always has a report
                raise GrafxTransactionStateError(
                    "A successful commit did not produce its report.",
                    operation="commit",
                )
            private_report = _public_commit_report(public_report)
            if (
                private_report is None
            ):  # pragma: no cover - public_report is non-optional above
                raise AssertionError("a public commit report copy disappeared")
            self._report = private_report
            self._finished = True
            try:
                self._database._settle_schema(self._context, committed=True)
            except BaseException as settlement_failure:
                # The manager has returned a durable report. QueryEngine settlement only drops
                # facade bookkeeping; letting a hostile cleanup escape here would turn success
                # into an apparent retry invitation. Keep the context tracked so close retries
                # the journal drain before lower resources are released, and retain the failure
                # privately as lifecycle evidence.
                if self._database._close_failure is None:
                    self._database._close_failure = settlement_failure
            else:
                if public_report.wrote:
                    self._database._maybe_checkpoint()
            return public_report

    def rollback(self) -> None:
        """Abandon this transaction. Rolling back twice is a no-op, never an error."""
        with self._database._public_transition():
            self._require_batch_idle("rollback")
            if self._finished:
                return
            try:
                self._database._transactions.rollback(self._context)
            except BaseException as failure:
                # A participant-section pre-enter failure has changed no outcome: keep the
                # wrapper live so the caller (or a later close) can withdraw its reader pin.
                # Conversely, reader/index cleanup can fail after the manager has already made
                # ABORTED final. In that case finish the wrapper and its schema unwind while
                # retaining the manager failure as the primary evidence.
                self._finished = not self._context.active
                if self._context.state is TransactionState.ABORTED:
                    try:
                        self._database._settle_schema(self._context, committed=False)
                    except BaseException as settlement_failure:
                        _note_cleanup_failure(failure, settlement_failure)
                raise
            self._finished = True
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
        except BaseException as cleanup_failure:
            if exc is not None:
                _note_cleanup_failure(exc, cleanup_failure)
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

    def _require_batch_idle(self, operation: str) -> None:
        """Keep callbacks from publishing or mutating a partially consumed batch."""
        if self._batch_active:
            raise GrafxTransactionStateError(
                f"Transaction {self._context.txn_id} is consuming an executemany batch and "
                f"cannot {operation} re-entrantly.",
                txn_id=self._context.txn_id,
                operation=operation,
                active_operation="executemany",
            )

    def __repr__(self) -> str:
        """Return a representation naming the transaction, its mode and its snapshot."""
        return (
            f"Transaction(txn_id={self._context.txn_id}, mode={self.mode!r}, "
            f"read_lsn={self._context.snapshot.read_lsn})"
        )


class Maintenance:
    """Thin operational facade over the database's existing public maintenance doors."""

    __slots__ = ("_database",)

    def __init__(self, database: Database) -> None:
        self._database = database

    def status(self) -> MaintenanceStatus:
        """Return a last-observed status snapshot without claiming global linearizability."""
        transactions = self._database.transactions
        recovery_required = transactions.recovery_required
        checkpoint_lag_lsn: int | None = None
        if not recovery_required:
            state = transactions.published_state()
            checkpoint_lag_lsn = state.last_committed_lsn - state.checkpoint_lsn
        return MaintenanceStatus(
            wal_bytes=self._database.wal.total_bytes(),
            checkpoint_lag_lsn=checkpoint_lag_lsn,
            recovery_required=recovery_required,
            stale_indexes=self._database.stale_indexes,
            heap_bloat_bytes=None,
            oldest_reader_age=None,
        )

    def bloat(self, table: str | None = None) -> BloatReport:
        """Return a conservative read-only heap-bloat census."""
        return self._database._bloat(table)

    def cleanup_indexes(
        self,
        *,
        dry_run: bool = True,
        confirm_quiescent: bool = False,
        max_files: int = 10000,
        max_wal_records: int = 100000,
    ) -> IndexCleanupReport:
        """Inventory or reclaim unreferenced native generations with every other handle stopped."""
        return _cleanup_indexes(
            self._database, dry_run=dry_run, confirm_quiescent=confirm_quiescent,
            max_files=max_files, max_wal_records=max_wal_records,
        )

    def vacuum(
        self,
        table: str | None = None,
        *,
        confirm_quiescent: bool = False,
        max_versions: int | None = None,
        index_free_pages: bool = False,
    ) -> VacuumReport:
        """Run explicit foreground MVCC reclamation under the v1 quiescence contract."""

        return self._database._vacuum(
            table,
            confirm_quiescent=confirm_quiescent,
            max_versions=max_versions,
            index_free_pages=index_free_pages,
        )

    def checkpoint(self) -> RecycleReport:
        """Delegate checkpointing to :meth:`Database.checkpoint`."""
        return self._database.checkpoint()

    def verify(self, scope: str = "all") -> VerificationReport:
        """Delegate verification to :meth:`Database.verify`."""
        return self._database.verify(scope)

    def recover(self) -> RecoveryReport:
        """Delegate recovery to :meth:`Database.recover`."""
        return self._database.recover()

    def rebuild_vector_index(self, space: str) -> VectorIndexView:
        """Delegate the repair to :meth:`Database.rebuild_vector_index`."""
        return self._database.rebuild_vector_index(space)

    def ensure_identity_indexes(self) -> None:
        """Delegate explicit persistent identity-index activation to the database."""
        self._database.ensure_identity_indexes()

    def enable_wal_page_compression(self) -> None:
        """Delegate explicit one-way WAL page-image compression activation."""
        self._database.enable_wal_page_compression()

    def create_index(
        self,
        name: str,
        table: str,
        columns: Sequence[str],
        *,
        bucket_count: int | None = None,
        expected_cardinality: int | None = None,
        layout: str = "hash",
    ) -> IndexView:
        """Delegate custom exact-index creation to the database."""
        return self._database.create_index(
            name,
            table,
            columns,
            bucket_count=bucket_count,
            expected_cardinality=expected_cardinality,
            layout=layout,
        )

    def rehash_index(
        self,
        name: str,
        *,
        bucket_count: int | None = None,
        expected_cardinality: int | None = None,
    ) -> IndexView:
        """Delegate growth-only exact-index rehash to the database."""
        return self._database.rehash_index(
            name,
            bucket_count=bucket_count,
            expected_cardinality=expected_cardinality,
        )

    def rebuild_index(self, name: str) -> IndexView:
        """Delegate immutable exact-index reconstruction to the database."""
        return self._database.rebuild_index(name)

    def rehash_index_if_needed(
        self,
        name: str,
        *,
        overflow_pages_per_bucket: int = 1,
        check_skew: bool = False,
    ) -> IndexView | None:
        """Grow one physically pressured exact index by at most one directory step."""
        return self._database.rehash_index_if_needed(
            name,
            overflow_pages_per_bucket=overflow_pages_per_bucket,
            check_skew=check_skew,
        )

    def publish_metrics(self) -> None:
        """Delegate explicit metric publication to :meth:`Database.publish_metrics`."""
        self._database.publish_metrics()


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
        "_catalog_view_memo",
        "_plan_view_memo",
        "_text_stats_cache",
        "_plan_guard_factory",
        "_checksum_scope",
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
        "_descriptor_revalidation",
        "_checkpoint_interval_records",
        "_wal_max_bytes",
        "_max_query_value_characters",
        "_wal_bytes_latched",
        "_checkpointing",
        "_checkpoint_retry_pending",
        "_closers",
        "_closed",
        "_public_contexts",
        "_close_releasing",
        "_close_released",
        "_close_failure",
        "_release_failure",
        # Directly composed facades without ContainedMetricsSink keep a best-effort fallback;
        # the supported composition atomically marks/claims through that adapter instead.
        "_release_failure_pending",
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
        checkpoint_interval_records: int = 512,
        wal_max_bytes: int | None = None,
        max_query_value_characters: int = DEFAULT_MAX_QUERY_VALUE_CHARACTERS,
        read_only: bool = False,
        descriptor_revalidation: str = "strict",
        metrics_endpoint: str | None = None,
        indexes: object = None,
        ledger: object = None,
        quarantine: object = None,
        recovery: object = None,
        vectors: object = None,
        queries: object = None,
        plan_guard_factory: Callable[[], AbstractContextManager[object]] | None = None,
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
        # The last public catalog view this handle built, bound to the full identity of the
        # generation it was built from: the persisted image bytes, the identity of the live
        # catalog object, and shallow copies of its table/space content (CQ-4/QW-6). Reused
        # only while all of them still match, so an unsaved in-place mutation with unchanged
        # bytes/identity misses; every adopt()/bootstrap installs a new object and image, so
        # the memo misses there too. Never persisted, never shared between handles.
        self._catalog_view_memo: (
            tuple[bytes, int, dict[int, object], dict[int, object], CatalogStoreView]
            | None
        ) = None
        # This cache lives behind the injected facade transition rather than owning a lock.
        # Strong source identity prevents id reuse; bounded entries contain only immutable plan
        # values and their already validated, capability-free templates.
        self._plan_view_memo: OrderedDict[
            int, tuple[PlanNode, PlanNode]
        ] = OrderedDict()
        self._plan_guard_factory = plan_guard_factory
        self._checksum_scope: Callable[[], AbstractContextManager[object]] = nullcontext
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
        self._read_only: bool = _builtin_bool(read_only)
        self._descriptor_revalidation: str = _require_text(
            "descriptor_revalidation", descriptor_revalidation
        )
        if self._descriptor_revalidation not in {"strict", "generation"}:
            raise GrafxConfigurationError(
                "The descriptor revalidation mode must be 'strict' or 'generation'.",
                field="descriptor_revalidation",
                value=self._descriptor_revalidation,
            )
        self._checkpoint_interval_records: int = _builtin_int(
            checkpoint_interval_records, field="checkpoint_interval_records"
        )
        if self._checkpoint_interval_records <= 0:
            raise GrafxConfigurationError(
                "The checkpoint interval must be a positive number of WAL records.",
                field="checkpoint_interval_records",
                value=self._checkpoint_interval_records,
            )
        self._wal_max_bytes: int | None = None
        if wal_max_bytes is not None:
            self._wal_max_bytes = _builtin_int(wal_max_bytes, field="wal_max_bytes")
            if self._wal_max_bytes <= 0:
                raise GrafxConfigurationError(
                    "The WAL byte threshold must be a positive number of bytes.",
                    field="wal_max_bytes",
                    value=self._wal_max_bytes,
                )
        self._max_query_value_characters = _builtin_int(
            max_query_value_characters, field="max_query_value_characters"
        )
        if not 1 <= self._max_query_value_characters <= MAX_QUERY_VALUE_CHARACTERS:
            raise GrafxConfigurationError(
                "The query value string ceiling must be between 1 and "
                f"{MAX_QUERY_VALUE_CHARACTERS} characters.",
                field="max_query_value_characters",
                value=self._max_query_value_characters,
                minimum=1,
                maximum=MAX_QUERY_VALUE_CHARACTERS,
            )
        self._checkpointing: bool = False
        self._wal_bytes_latched: bool = False
        self._checkpoint_retry_pending: bool = False
        self._metrics_endpoint: str | None = _builtin_optional_text(
            metrics_endpoint, field="metrics_endpoint"
        )
        self._closers: tuple[Callable[[], None], ...] = tuple(closers)
        self._closed: bool = False
        # Contexts never cross the public boundary, but the facade must remember every context
        # that entered QueryEngine until its schema journal is retired. TransactionManager owns
        # pins and transaction state; this map owns only the second half of public lifecycle
        # settlement, so Database.close can finish it before storage and the pool disappear. A
        # transaction that never executed a statement needs no entry and no post-close unwind.
        self._public_contexts: dict[int, TransactionContext] = {}
        self._text_stats_cache: dict = {}
        self._close_releasing: bool = False
        self._close_released: bool = False
        self._close_failure: BaseException | None = None
        self._release_failure: BaseException | None = None
        self._release_failure_pending: bool = False
        self._recovery_report: RecoveryReport | None = _recovery_report_view(
            recovery_report
        )
        self._attached_indexes: tuple[str, ...] = tuple(
            _builtin_text(name, field="attached_index", empty=False)
            for name in attached_indexes
        )
        self._stale_indexes: tuple[str, ...] = tuple(
            _builtin_text(name, field="stale_index", empty=False)
            for name in stale_indexes
        )
        self._unindexed_tables: tuple[str, ...] = tuple(
            _builtin_text(name, field="unindexed_table", empty=False)
            for name in unindexed_tables
        )
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
        return _public_identity(self._identity)

    @property
    def read_only(self) -> bool:
        """Return True when this database refuses to open a write transaction."""
        return self._read_only

    @property
    def descriptor_revalidation(self) -> str:
        """Return this handle's process-local descriptor revalidation policy.

        The value reports effective runtime configuration. It is deliberately separate from
        :attr:`identity`, whose fields describe durable on-disk identity shared by participants.
        """
        return self._descriptor_revalidation

    @property
    def closed(self) -> bool:
        """Return True once :meth:`close` has run; a closed database refuses every door."""
        return self._closed

    @property
    def close_complete(self) -> bool:
        """Return True once the elected caller finished lower-layer release.

        A concurrent or reentrant ``close`` that observes a release already in progress returns
        without waiting on host-owned closer code. Terminal refusal is already effective, while
        this property remains False until that elected caller exhausts every release step.
        """
        return self._close_released

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
    def recovery_report(self) -> RecoveryReport | None:
        """Return the report of the recovery that ran at open, or None when none did (FR-8)."""
        return self._recovery_report

    @property
    def maintenance(self) -> Maintenance:
        """Return a thin facade over the database's existing operator operations."""
        with self._public_transition():
            self._require_open()
            return Maintenance(self)

    # --- the composition ---------------------------------------------------------------------

    @property
    def storage(self) -> StorageView:
        """Return immutable storage identity and file-size metadata.

        The device itself is deliberately not public: its allocation, append, truncate and page
        write doors bypass the WAL and recovery protocols.  This snapshot keeps useful inventory
        reads while carrying no reference or callback back to that device.
        """
        with self._public_transition():
            self._require_open()
            return _storage_view(self._storage)

    @property
    def clock(self) -> ClockView:
        """Return clock implementation identity without advancing either clock source."""
        with self._public_transition():
            self._require_open()
            return _clock_view(self._clock)

    @property
    def codec(self) -> CodecView:
        """Return immutable format metadata without exposing page encode/decode doors."""
        with self._public_transition():
            self._require_open()
            return _codec_view(
                self._codec,
                _domain_field(self._identity, DatabaseIdentity, "page_size"),
            )

    @property
    def metrics(self) -> MetricsView:
        """Return whether metrics collection is enabled, without exposing the mutable sink."""
        with self._public_transition():
            self._require_open()
            return MetricsView(_builtin_bool(self._metrics.enabled))

    @property
    def events(self) -> ComponentView:
        """Return identity-only metadata for the configured event destination."""
        with self._public_transition():
            self._require_open()
            return _component_view("events", self._events)

    @property
    def vector_math(self) -> VectorMathView:
        """Return the selected vector arithmetic implementation's immutable name."""
        with self._public_transition():
            self._require_open()
            # Adapter identity is observable without executing a host-supplied descriptor.
            # Calling ``name`` here would turn a harmless property read into another callback
            # into the host.
            return VectorMathView(
                _builtin_text(
                    _builtin_type_name(self._vector_math),
                    field="vector_math.name",
                    empty=False,
                )
            )

    @property
    def coordinator(self) -> CoordinatorView:
        """Return participant identity without touching lease, horizon or fencing state."""
        with self._public_transition():
            self._require_open()
            return _coordinator_view(self._coordinator)

    @property
    def pool(self) -> BufferPoolView:
        """Return immutable page-cache capacity and residency counters (FR-13)."""
        with self._public_transition():
            self._require_open()
            # Every public page operation enters this same section.  The snapshot therefore
            # cannot straddle an eviction, while its builder only reads in-memory counters and
            # never calls a storage or telemetry port with the section held.
            with self._transactions._participant_section():
                return _pool_view(self._pool)

    @property
    def catalog(self) -> CatalogStoreView:
        """Return a complete, linearized schema and catalog-layout snapshot."""
        with self._public_transition():
            self._require_open()
            snapshot, _epoch = self._catalog_snapshot()
            return snapshot

    @property
    def views(self) -> LogicalViews:
        """Return the bounded, persistent read-only logical view API; prepare explicitly."""
        from okto_grafx.views import LogicalViews
        self._require_open()
        return LogicalViews(self)

    @property
    def heap(self) -> HeapStoreView:
        """Return immutable heap layout metadata without row or page mutation doors."""
        with self._public_transition():
            self._require_open()
            return _heap_view(self._heap)

    @property
    def wal(self) -> WalView:
        """Return the WAL state last observed by this handle and its segment inventory (FR-5).

        This observation is deliberately cache-only.  Refreshing the directory here would both
        turn a property read into storage I/O and race ``WalManager._unflushed`` with a commit's
        durability barrier.  The participant section makes the cached fields one coherent cut
        without running storage or metrics callbacks while it is held.
        """
        with self._public_transition():
            self._require_open()
            with self._transactions._participant_section():
                return _wal_view(self._wal)

    @property
    def transactions(self) -> TransactionManagerView:
        """Return immutable transaction configuration, counts and publication state."""
        with self._public_transition():
            self._require_open()
            # Capture recovery and publication under the same section without re-entering the
            # manager's public `published_state` door. A callback may seal the manager while its
            # storage read is in flight; the facade transition defers release and this private
            # in-section path lets that already-started observation finish honestly.
            with self._transactions._participant_section():
                recovery_required = _builtin_bool(self._transactions.recovery_required)
                state = (
                    None
                    if recovery_required
                    else self._transactions._published_state_in_section()
                )
                return _transactions_view(
                    self._transactions,
                    recovery_required=recovery_required,
                    state=state,
                )

    @property
    def indexes(self) -> IndexRegistryView:
        """Return an immutable secondary-index inventory (FR-12).

        Refuses with GrafxUnsupportedOperation when the composition has no index manager.
        """
        with self._public_transition():
            self._require_open()
            indexes = self._require_component(
                "indexes", self._indexes, "the index framework (C7)"
            )
            # Query DDL registers accelerators before commit and journals them for rollback.
            # Filter that speculative registry against the same linearized committed catalog
            # the caller sees, and revalidate its epoch beside the registry snapshot.
            while True:
                catalog, epoch = self._catalog_snapshot()
                tables = catalog.catalog.table_definitions
                with self._transactions._participant_section():
                    if (
                        _builtin_int(self._catalog._view_epoch(), field="catalog.epoch")
                        != epoch
                    ):
                        continue
                    return _indexes_view(
                        indexes,
                        tables,
                        catalog=self._catalog._catalog,
                    )

    @property
    def ledger(self) -> LedgerView:
        """Return an immutable ledger inventory (SPEC-M1 FR-9, section 10).

        Refuses with GrafxUnsupportedOperation when the composition has no ledger store.
        """
        with self._public_transition():
            self._require_open()
            ledger = self._require_component(
                "ledger", self._ledger, "the ledger store (C6)"
            )
            return _ledger_view(ledger)

    @property
    def quarantine(self) -> QuarantineView:
        """Return an immutable quarantine evidence inventory (FR-10).

        Refuses with GrafxUnsupportedOperation when the composition has no quarantine store.
        """
        with self._public_transition():
            self._require_open()
            quarantine = self._require_component(
                "quarantine", self._quarantine, "quarantine (C6)"
            )
            return _quarantine_view(quarantine)

    @property
    def vectors(self) -> VectorEngineView:
        """Return immutable vector-space and index configuration (SPEC-VEC).

        Refuses with GrafxUnsupportedOperation when the composition has no vector engine.
        """
        with self._public_transition():
            self._require_open()
            return self._vectors_view_now()

    def _vectors_view_now(self) -> VectorEngineView:
        """Capture the vector view from components a caller already holds a section over.

        Split out of :attr:`vectors` so an operation running inside the facade section can take
        the same snapshot without re-asking whether the database is open. The flag flips the
        moment a close is REQUESTED, while the section defers the release that would actually
        take these components apart -- so re-entering the public accessor would make a door
        refuse its own result for a teardown that has not happened yet.
        """
        vectors = self._require_component(
            "vectors", self._vectors, "the vector engine (C9)"
        )
        # A catalog refresh can read/evict pages and publish telemetry, so it happens before
        # the participant section. _catalog_snapshot validates the page epoch under that
        # section; validate it once more beside the vector registry so a DDL commit cannot
        # land in the small gap and produce spaces from one side with indexes from the other.
        while True:
            catalog, epoch = self._catalog_snapshot()
            tables = catalog.catalog.table_definitions
            spaces = catalog.catalog.space_definitions
            with self._transactions._participant_section():
                if (
                    _builtin_int(self._catalog._view_epoch(), field="catalog.epoch")
                    != epoch
                ):
                    continue
                return _vectors_view(vectors, spaces, tables)

    @property
    def queries(self) -> QueryEngineView:
        """Return immutable diagnostics for the composed query engine.

        Refuses with GrafxUnsupportedOperation when the composition has no query engine.
        """
        with self._public_transition():
            self._require_open()
            queries = self._require_component(
                "queries", self._queries, "the query engine (C10)"
            )
            return _queries_view(queries)

    # --- transactions -------------------------------------------------------------------------

    def begin(self, mode: str = "write", *, metadata: CommitMetadata | None = None) -> Transaction:
        """Open a transaction in ``"read"`` or ``"write"`` mode (SPEC-M1 FR-2).

        A reader sees the consistent snapshot of the instant it opened for its whole life, even
        while other processes commit; readers never block writers and writers never block readers.
        The returned object is a context manager: leaving its block commits, and leaving it
        through an exception rolls back.
        """
        self._require_open()
        parsed = TransactionMode.parse(mode)
        captured = capture_commit_metadata(metadata)
        if captured is not None and parsed is not TransactionMode.WRITE:
            raise GrafxConfigurationError("Read transactions cannot publish metadata.", field="metadata")
        admitted = None if captured is None else decode_commit_metadata(captured)
        if parsed is TransactionMode.WRITE:
            self._require_writable("begin a write transaction")
        with self._public_transition():
            # Seal the check/transition race: close may publish after the preliminary guard but
            # before this process-wide facade boundary increments its settlement count.
            self._require_open()
            context = (self._transactions.begin(parsed.value) if admitted is None
                       else self._transactions.begin(parsed.value, metadata=admitted))
            return self._public_transaction(context)

    def retry(self, transaction: Transaction) -> Transaction:
        """Open the successor of a transaction optimistic validation refused (BR-6).

        The refused transaction is abandoned and a fresh one is opened above the commit that won,
        carrying the refusal count so the commit that finally succeeds is the one that reports
        ``oktografx_commit_retries_total``.
        """
        self._require_open()
        if type(transaction) is not Transaction:
            observed = _builtin_type_name(transaction)
            raise GrafxConfigurationError(
                f"A retry takes a Transaction; got {observed}.",
                field="transaction",
                value=observed,
            )
        if transaction._database is not self:
            raise GrafxTransactionStateError(
                "A transaction can only be retried by the database that opened it.",
                txn_id=transaction.txn_id,
                field="transaction_owner",
                path=self._path,
            )
        with self._public_transition():
            # As with begin, close can win between the public preliminary check and transition
            # entry. Refuse before manager/schema work; manager retry revalidates terminal state
            # again in its own participant section.
            self._require_open()
            transaction._require_active()
            transaction._require_batch_idle("retry")
            context = transaction._context
            # TransactionManager.retry revalidates the CURRENT owned ACTIVE context, aborts it
            # and registers its successor in one participant section. Keeping a second outer
            # participant section here would put manager failure telemetry under a facade lock;
            # the injected public transition tracks only wrapper/schema outcome publication.
            try:
                successor = self._transactions.retry(context)
            except BaseException as retry_failure:
                # Manager retry can fail after it has fail-completely aborted the predecessor
                # (for example if successor open fails). Its query journal then belongs to the
                # same unwind as a successful retry. A validation refusal leaves it ACTIVE.
                if context.state is TransactionState.ABORTED:
                    transaction._finished = True
                    try:
                        self._settle_schema(context, committed=False)
                    except BaseException as settlement_failure:
                        _note_cleanup_failure(retry_failure, settlement_failure)
                raise
            transaction._finished = True
            try:
                self._settle_schema(context, committed=False)
            except BaseException as settlement_failure:
                # The successor has not escaped yet. Retire it immediately so a predecessor
                # unwind failure cannot leak a reader pin. Cleanup evidence never replaces it.
                try:
                    self._transactions.rollback(successor)
                except BaseException as cleanup_failure:
                    _note_cleanup_failure(settlement_failure, cleanup_failure)
                    if successor.active:
                        # The successor has no public wrapper and a participant pre-enter bomb
                        # means ordinary rollback changed nothing. Seal this facade immediately;
                        # leaving the current public transition then resumes close and retires
                        # the otherwise unreachable reader pin. The predecessor settlement
                        # failure remains the primary outcome throughout.
                        self._closed = True
                        self._transactions.request_close()
                raise
            return self._public_transaction(successor)

    @contextmanager
    def transaction(self, mode: str = "write", *, metadata: CommitMetadata | None = None) -> Iterator[Transaction]:
        """Open a transaction as a block, committing on a clean exit and rolling back otherwise.

        The lexical boundary also retains this participant section's unlocked descriptor. Its
        physical identity is revalidated before every later lock acquisition, so a bounded
        one-statement transaction avoids repeated open/close calls without retaining the lock or
        weakening another process's admission.
        """
        captured = capture_commit_metadata(metadata)
        parsed = TransactionMode.parse(mode)
        if captured is not None and parsed is not TransactionMode.WRITE:
            raise GrafxConfigurationError("Read transactions cannot publish metadata.", field="metadata")
        admitted = None if captured is None else decode_commit_metadata(captured)
        with self._transactions._participant_descriptor_scope(revalidate_identity=True):
            txn = self.begin(parsed.value, metadata=admitted)
            with txn:
                yield txn

    def execute(
        self,
        text: str,
        parameters: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> QueryResult:
        """Run one statement in its own read transaction and return its result.

        This is the autocommit read of CONTRACT.md section 10. The transaction is opened, the
        statement is run and the transaction is committed, so the result never outlives a snapshot
        that has been released.
        """
        self._require_open()
        control = _read_control(self._clock, timeout_seconds, cancellation)
        with self._public_transition():
            # Close may win after the preliminary guard but before the transition becomes
            # visible. Refuse before retaining the unlocked participant descriptor.
            self._require_open()
            with self._transactions._participant_descriptor_scope():
                txn = self.begin("read")
                try:
                    result = (txn.execute(text, parameters) if control is None else
                              self._run_statement(txn._context, text, parameters, control=control))
                    if control is not None:
                        control.check()
                    txn.commit()
                except BaseException as failure:
                    if txn.active:
                        try:
                            txn.rollback()
                        except BaseException as cleanup_failure:
                            _note_cleanup_failure(failure, cleanup_failure)
                            if txn.active:
                                # No caller can recover this local wrapper. Seal the facade so
                                # leaving the outer transition drains its reader pin and every
                                # descriptor before lower dependencies are released.
                                self._closed = True
                                self._transactions.request_close()
                    raise
                return result

    def query(self, text: str, parameters: Mapping[str, object] | None = None) -> Query:
        """Return a reusable canonical read query whose cursors own their snapshots.

        ``execute`` remains the materialised read convenience; statements that write require
        an explicit write transaction. This builder copies text and parameter values immediately,
        so mutating the caller's containers after this call cannot change a later cursor.
        """
        with self._public_operation("query prepare"):
            self._require_open()
            statement = _query_text_snapshot(text)
            detached_parameters = _query_parameters_snapshot(
                parameters,
                max_string_characters=self._max_query_value_characters,
            )
            return Query(self, statement, detached_parameters)

    def _open_query_cursor(
        self,
        text: str,
        parameters: Mapping[str, object] | None,
        *,
        batch_size: int,
        control: _ReadControl | None = None,
    ) -> QueryCursor:
        """Open the internal stream and its owning read transaction as one public outcome."""
        self._require_open()
        transaction = self.begin("read")
        raw: object | None = None
        try:
            if control is not None:
                control.check()
            with self._public_operation("query cursor open"):
                self._require_open()
                engine = self._require_component(
                    "queries", self._queries, "the query engine (C10)"
                )
                with self._transactions.page_access_section():
                    self._require_open()
                    if not transaction._context.active:
                        raise GrafxTransactionStateError(
                            f"Transaction {transaction.txn_id} cannot open a query cursor.",
                            txn_id=transaction.txn_id,
                            state=transaction._context.state.value,
                            operation="query_cursor",
                        )
                    self._public_contexts.setdefault(
                        transaction.txn_id, transaction._context
                    )
                    opener = getattr(engine, "open_cursor", None)
                    if not callable(opener):
                        raise GrafxUnsupportedOperation(
                            "The query engine of this composition has no streaming cursor door.",
                            field="component",
                            value="query_cursor",
                        )
                    raw = (
                        opener(text, transaction._context, parameters) if control is None
                        else opener(text, transaction._context, parameters, read_control=control)
                    )
                    if control is not None:
                        control.check()
                # Rebuild metadata after page access, just like the materialised result door.
                columns = getattr(raw, "columns", None)
                plan = getattr(raw, "plan", None)
                fetch = getattr(raw, "fetch", None)
                close = getattr(raw, "close", None)
                if not callable(fetch) or not callable(close):
                    raise GrafxConfigurationError(
                        "The query engine returned a malformed cursor collaborator.",
                        field="cursor",
                        value=_builtin_type_name(raw),
                    )
                metadata = _query_result_view(
                    QueryResult(columns=columns, plan=plan),  # type: ignore[arg-type]
                    max_string_characters=self._max_query_value_characters,
                    internally_owned_plan=_engine_owns_prepared_plan(engine, plan),
                    plan_memo=self._plan_view_memo,
                    plan_guard_factory=self._plan_guard_factory,
                )
                if metadata.plan is None:
                    raise GrafxConfigurationError(
                        "A query cursor must expose the plan that produces it.",
                        field="cursor.plan",
                        value=None,
                    )
                return QueryCursor(
                    database=self,
                    transaction=transaction,
                    raw=raw,
                    columns=metadata.columns,
                    plan=metadata.plan,
                    batch_size=batch_size,
                    control=control,
                )
        except BaseException as failure:
            if raw is not None:
                try:
                    closer = getattr(raw, "close", None)
                    if callable(closer):
                        closer()
                except BaseException as cleanup_failure:
                    _note_cleanup_failure(failure, cleanup_failure)
            try:
                if transaction.active:
                    transaction.rollback()
            except BaseException as cleanup_failure:
                _note_cleanup_failure(failure, cleanup_failure)
            raise

    def _fetch_query_cursor(
        self, cursor: QueryCursor, limit: int
    ) -> tuple[tuple[tuple[Value, ...], ...], bool]:
        """Pull and detach one bounded cursor batch, settling its snapshot at EOF."""
        if type(cursor) is not QueryCursor or cursor._database is not self:
            raise GrafxConfigurationError(
                "A query cursor can only be consumed by the database that opened it.",
                field="cursor",
                value=_builtin_type_name(cursor),
            )
        try:
            with self._public_operation("query cursor fetch"):
                self._require_open()
                transaction = cursor._transaction
                transaction._require_active()
                with self._transactions.page_access_section():
                    self._require_open()
                    transaction._require_active()
                    if cursor._control is not None:
                        cursor._control.check()
                    observed = cursor._raw.fetch(limit)
                    if cursor._control is not None:
                        cursor._control.check()
                if type(observed) is not tuple or len(observed) != 2:
                    raise GrafxConfigurationError(
                        "The query cursor collaborator returned a malformed batch.",
                        field="cursor.batch",
                        value=_builtin_type_name(observed),
                    )
                raw_rows, raw_exhausted = observed
                if type(raw_exhausted) is not bool:
                    raise GrafxConfigurationError(
                        "The query cursor collaborator returned a malformed EOF marker.",
                        field="cursor.exhausted",
                        value=_builtin_type_name(raw_exhausted),
                    )
                detached = _query_result_view(
                    QueryResult(columns=cursor.columns, rows=raw_rows),  # type: ignore[arg-type]
                    max_string_characters=self._max_query_value_characters,
                )
                if cursor._control is not None:
                    cursor._control.check()
            if raw_exhausted:
                self._settle_query_cursor(cursor)
            return detached.rows, raw_exhausted
        except BaseException as failure:
            try:
                self._close_query_cursor(cursor)
            except BaseException as cleanup_failure:
                _note_cleanup_failure(failure, cleanup_failure)
            raise

    def _settle_query_cursor(self, cursor: QueryCursor) -> None:
        """Release an exhausted cursor's reader pin without re-closing its engine stream."""
        transaction = cursor._transaction
        if transaction.active:
            transaction.rollback()

    def _close_query_cursor(self, cursor: QueryCursor) -> None:
        """Close a cursor stream and its read transaction, attempting both cleanup halves."""
        failure: BaseException | None = None
        try:
            raw_close = getattr(cursor._raw, "close", None)
            if callable(raw_close):
                try:
                    if not self._closed and cursor._transaction.active:
                        with self._public_operation("query cursor close"):
                            with self._transactions.page_access_section():
                                raw_close()
                    else:
                        raw_close()
                except BaseException as caught:
                    failure = caught
            try:
                if cursor._transaction.active:
                    cursor._transaction.rollback()
            except BaseException as caught:
                if failure is None:
                    failure = caught
                else:
                    _note_cleanup_failure(failure, caught)
        finally:
            cursor._buffer = ()
            cursor._buffer_position = 0
            cursor._source_done = True
            cursor._closed = True
        if failure is not None:
            raise failure

    def explain(self, text: str) -> PlanNode:
        """Plan one statement without exposing the mutable query engine."""
        with self._public_operation("explain"):
            self._require_open()
            statement = _query_text_snapshot(text)
            engine = self._require_component(
                "queries", self._queries, "the query engine (C10)"
            )
            with self._transactions.page_access_section():
                # Recheck immediately before reaching the engine so a concurrent close cannot
                # turn validation before canonicalisation into a stale permission to plan.
                self._require_open()
                raw_plan = engine.explain(statement)  # type: ignore[attr-defined]
            return _query_plan_view(
                raw_plan,
                internally_owned=_engine_owns_prepared_plan(engine, raw_plan),
                memo=self._plan_view_memo,
            )

    def _run_statement(
        self,
        context: TransactionContext,
        text: str,
        parameters: Mapping[str, object] | None = None,
        *,
        control: _ReadControl | None = None,
    ) -> QueryResult:
        """Run one statement for a context already validated by the public Transaction."""
        with self._public_operation("query"):
            self._require_open()
            engine = self._require_component(
                "queries", self._queries, "the query engine (C10)"
            )
            statement = _query_text_snapshot(text)
            detached_parameters = _query_parameters_snapshot(
                parameters,
                max_string_characters=self._max_query_value_characters,
            )
            call = text_call(statement, detached_parameters)
            if call is not None:
                return self._run_text_procedure(context, call, control)
            # Keep native logical writes reversible until the public result and deadline
            # have both passed validation. Schema statements have their own catalog/artifact
            # journal and must not be unwound with a row-only staging snapshot.
            with self._logical_statement_publication(context, engine, statement):
                with self._transactions.page_access_section(transaction=context):
                    self._require_open()
                    if not context.active:
                        raise GrafxTransactionStateError(
                            f"Transaction {context.txn_id} is {context.state.value} and cannot execute "
                            "another statement.",
                            txn_id=context.txn_id,
                            state=context.state.value,
                        )
                    # Registration and statement execution share the participant section. Close can
                    # therefore neither miss a context that may have acquired a schema journal nor
                    # release storage while the statement is installing one.
                    self._public_contexts.setdefault(context.txn_id, context)
                    if control is not None:
                        control.check()
                        raw_result = engine.execute(statement, context, detached_parameters, read_control=control)  # type: ignore[attr-defined]
                        control.check()
                    else:
                        raw_result = engine.execute(statement, context, detached_parameters)  # type: ignore[attr-defined]
                # A collaborator result may itself be a hostile Mapping/Sequence. Rebuild it only
                # after leaving page access, while _public_operation still translates ordinary host
                # failures and deliberately lets process-control signals pass unchanged.
                result = _query_result_view(
                    raw_result,
                    max_string_characters=self._max_query_value_characters,
                    internally_owned_plan=(
                        type(raw_result) is QueryResult
                        and _engine_owns_prepared_plan(
                            engine,
                            _domain_field(raw_result, QueryResult, "plan"),
                        )
                    ),
                    plan_memo=self._plan_view_memo,
                    plan_guard_factory=self._plan_guard_factory,
                )
                if control is not None:
                    control.check()
                return result

    @contextmanager
    def _logical_statement_publication(
        self, context: TransactionContext, engine: object, statement: str,
    ) -> Iterator[None]:
        """Hold native row effects through public result canonicalization."""
        mark = None
        if context.mode is TransactionMode.WRITE and type(engine) is QueryEngine:
            parsed = engine.parse(statement)
            if isinstance(parsed, QueryStatement) and parsed.writes:
                with self._transactions.page_access_section(transaction=context):
                    self._require_open()
                    mark = context.staging_mark()
        try:
            yield
            if mark is not None:
                with self._transactions.page_access_section(transaction=context):
                    self._require_open()
                    context.settle_staging_mark(mark)
        except BaseException as failure:
            if mark is not None and context.active:
                try:
                    with self._transactions.page_access_section(transaction=context):
                        context.discard_since(mark)
                except BaseException as cleanup_failure:
                    _note_cleanup_failure(failure, cleanup_failure)
                    # If statement rollback cannot be proved, no later commit is safe.
                    try:
                        if context.active:
                            self._transactions.rollback(context)
                    except BaseException as rollback_failure:
                        _note_cleanup_failure(failure, rollback_failure)
            raise

    def _run_text_procedure(self, context: TransactionContext, call: tuple[object, ...], control: _ReadControl | None) -> QueryResult:
        """Run the closed FTS read procedure, sharing the calling statement's deadline."""
        candidate_filter = None
        if len(call) == 4:
            if type(call[3]) not in (list, tuple):
                raise GrafxConfigurationError("record_ids must be a list parameter.", field="record_ids")
            candidate_filter = RecordIdFilter.of(call[3])
        found = _search_text(self, Transaction(self, context), index=call[0], query=call[1], k=call[2],
                             filter=candidate_filter, limits=None, k1=1.2, b=0.75,
                             timeout_seconds=None, cancellation=None, _control=control)
        return QueryResult(
            columns=("record_id", "score", "matched_fields", "matched_terms", "regime", "index_built_through_commit", "snapshot_commit"),
            rows=tuple((hit.record_id, hit.score, hit.matched_fields, hit.matched_terms, found.regime,
                        found.index_built_through_commit, found.snapshot_commit) for hit in found.hits),
            statistics={"postings_visited": found.postings_visited, "candidates": found.candidates,
                        "corpus_documents": found.corpus_documents, "snapshot_commit": found.snapshot_commit,
                        "index_built_through_commit": found.index_built_through_commit, "fulltext_exact_index": 1,
                        "statistics_wal_records": found.statistics_wal_records,
                        "statistics_from_wal_delta": int(found.statistics_regime == 'wal_delta'),
                        "statistics_from_durable_summary": int(found.statistics_regime == 'durable_summary'),
                        "statistics_from_snapshot_cache": int(found.statistics_regime == 'snapshot_cache')},
        )

    def _run_many(
        self,
        context: TransactionContext,
        text: str,
        parameter_sets: Iterable[Mapping[str, object]],
    ) -> ExecuteManyReport:
        """Run one parsed updating statement over a streaming, atomic parameter batch."""
        with self._executemany_operation():
            self._require_open()
            engine = self._require_component(
                "queries", self._queries, "the query engine (C10)"
            )
            statement_text = _query_text_snapshot(text)
            with self._transactions.page_access_section():
                self._require_open()
                if not context.active:
                    raise GrafxTransactionStateError(
                        f"Transaction {context.txn_id} is {context.state.value} and cannot "
                        "execute a batch.",
                        txn_id=context.txn_id,
                        state=context.state.value,
                        operation="executemany",
                    )
                if context.mode is not TransactionMode.WRITE:
                    raise GrafxTransactionStateError(
                        "executemany requires a write transaction.",
                        txn_id=context.txn_id,
                        mode=context.mode.value,
                        operation="executemany",
                    )
                self._public_contexts.setdefault(context.txn_id, context)
                statement = engine.parse(statement_text)  # type: ignore[attr-defined]
                if (
                    not isinstance(statement, QueryStatement)
                    or not statement.writes
                    or statement.return_clause is not None
                ):
                    raise GrafxUnsupportedOperation(
                        "executemany accepts one updating query without RETURN; use execute "
                        "for reads, schema changes, UNION or result-producing writes.",
                        field="statement",
                        operation="executemany",
                        value=type(statement).__name__,
                    )
                mark = context.staging_mark()

            try:
                if isinstance(parameter_sets, Mapping):
                    raise GrafxConfigurationError(
                        "executemany parameter_sets must be an iterable of mappings, not one "
                        "mapping.",
                        field="parameter_sets",
                        value=_builtin_type_name(parameter_sets),
                    )
                try:
                    iterator = iter(parameter_sets)
                except GrafxError:
                    raise
                except Exception as failure:  # noqa: BLE001 - canonicalized public argument
                    observed = _builtin_type_name(parameter_sets)
                    raise GrafxConfigurationError(
                        f"executemany parameter_sets must be iterable; got {observed}.",
                        field="parameter_sets",
                        value=observed,
                        cause=_builtin_type_name(failure),
                    ) from failure

                completed = 0
                aggregate: dict[str, int] = {}
                while True:
                    try:
                        raw_parameters = next(iterator)
                    except StopIteration:
                        break
                    except GrafxError as failure:
                        _note_batch_index(failure, completed)
                        raise
                    if not isinstance(raw_parameters, Mapping):
                        raise GrafxConfigurationError(
                            "Every executemany parameter set must be a mapping.",
                            field="parameter_sets",
                            value=_builtin_type_name(raw_parameters),
                            batch_index=completed,
                        )
                    try:
                        detached_parameters = _query_parameters_snapshot(
                            raw_parameters,
                            max_string_characters=self._max_query_value_characters,
                        )
                    except GrafxError as failure:
                        _note_batch_index(failure, completed)
                        raise

                    try:
                        with self._transactions.page_access_section():
                            self._require_open()
                            if not context.active:
                                raise GrafxTransactionStateError(
                                    f"Transaction {context.txn_id} is "
                                    f"{context.state.value} and cannot execute a batch.",
                                    txn_id=context.txn_id,
                                    state=context.state.value,
                                    operation="executemany",
                                    batch_index=completed,
                                )
                            raw_result = engine._execute_parsed(  # type: ignore[attr-defined]
                                statement,
                                context,
                                detached_parameters,
                                cache_text=statement_text,
                            )
                    except GrafxError as failure:
                        _note_batch_index(failure, completed)
                        raise

                    try:
                        statistics = _query_statistics_snapshot(raw_result.statistics)
                    except GrafxError as failure:
                        _note_batch_index(failure, completed)
                        raise
                    for name, count in statistics.items():
                        if name not in aggregate and len(aggregate) >= MAX_MAP_ENTRIES:
                            raise GrafxConfigurationError(
                                "An executemany report may carry at most "
                                f"{MAX_MAP_ENTRIES} statistic names.",
                                field="executemany.statistics",
                                limit=MAX_MAP_ENTRIES,
                                batch_index=completed,
                            )
                        aggregate[name] = aggregate.get(name, 0) + count
                    completed += 1
                    # Do not carry the previous item's plan, result or detached payload while
                    # planning the next one. The iterator may retain its own values; this facade
                    # retains only the fixed statement and the bounded aggregate.
                    del raw_result, raw_parameters, detached_parameters, statistics

                report = ExecuteManyReport(
                    statements=completed,
                    statistics=aggregate,
                )
                with self._transactions.page_access_section():
                    self._require_open()
                    if not context.active:
                        raise GrafxTransactionStateError(
                            f"Transaction {context.txn_id} is {context.state.value} and cannot "
                            "finish a batch.",
                            txn_id=context.txn_id,
                            state=context.state.value,
                            operation="executemany",
                        )
                    context.settle_staging_mark(mark)
            except BaseException as failure:
                try:
                    context.discard_since(mark)
                except BaseException as cleanup_failure:
                    _note_cleanup_failure(failure, cleanup_failure)
                raise
            return report

    @contextmanager
    def _executemany_operation(self) -> Iterator[None]:
        """Enter the public batch transition, then bound unlocked descriptor reuse inside it."""
        with self._public_operation("executemany"):
            # Every page_access_section in _run_many still takes and drops the operating-system
            # lock. Only its permanent lock-file descriptor survives between batch items.
            with self._transactions._participant_descriptor_scope():
                yield

    def _scan_rows_v1(
        self,
        context: TransactionContext,
        *,
        table: str,
        limit: int,
        cursor_payload: tuple[int, int, _HeapScanPosition, ScanCursorV1] | None,
        cursor_owner: object,
        columns: tuple[str, ...] | None = None,
        max_batch_bytes: int | None = None,
        control: _ReadControl | None = None,
    ) -> ScanPageV1:
        """Serve the bounded scan door after its public arguments have been canonicalised."""

        with self._public_operation("scan_rows_v1"):
            self._require_open()
            with self._transactions.page_access_section():
                self._require_open()
                if not context.active:
                    raise GrafxTransactionStateError(
                        f"Transaction {context.txn_id} is {context.state.value} and cannot scan "
                        "another page.",
                        txn_id=context.txn_id,
                        state=context.state.value,
                        operation="scan_rows_v1",
                    )
                if context.mode is not TransactionMode.READ:
                    raise GrafxTransactionStateError(
                        "scan_rows_v1 requires a read transaction.",
                        txn_id=context.txn_id,
                        mode=context.mode.value,
                        operation="scan_rows_v1",
                    )
                table_def = self._catalog.catalog.table(table)
                positions = None if columns is None else tuple(table_def.column_index(c) for c in columns)
                position: _HeapScanPosition | None = None
                if cursor_payload is not None:
                    cursor_table_id, cursor_schema_version, position, cursor_token = (
                        cursor_payload
                    )
                    if (
                        cursor_table_id != table_def.table_id
                        or cursor_schema_version != table_def.schema_version
                        or getattr(cursor_token, "_columns", None) != columns
                    ):
                        raise GrafxTransactionStateError(
                            "A scan continuation no longer names the same table definition.",
                            operation="scan_rows_v1",
                            field="cursor_table",
                            table=table,
                            cursor_table_id=cursor_table_id,
                            current_table_id=table_def.table_id,
                            cursor_schema_version=cursor_schema_version,
                            current_schema_version=table_def.schema_version,
                        )
                    # The transaction manager's participant section serialises this claim.  A
                    # cursor is a one-shot continuation: consuming it before page access prevents
                    # duplicate transfer if a caller accidentally submits the same page twice,
                    # and a storage refusal cannot turn it back into a replay token.
                    if object.__getattribute__(cursor_token, "_consumed"):
                        raise GrafxTransactionStateError(
                            "A scan continuation cannot be reused.",
                            operation="scan_rows_v1",
                            field="cursor_state",
                            value="consumed",
                        )
                    object.__setattr__(cursor_token, "_consumed", True)
                options = {}
                if columns is not None or max_batch_bytes is not None or control is not None:
                    if type(self._heap) is not HeapStore:
                        raise GrafxUnsupportedOperation("The heap collaborator lacks bounded projected scans.", operation="scan_rows_v1")
                    options = dict(materialized_positions=None if positions is None else frozenset(positions),
                                   max_batch_bytes=max_batch_bytes, check=None if control is None else control.check)
                raw_rows, next_position = self._heap.scan_page(
                    table_def, context.snapshot, limit=limit, position=position, **options)

            # Heap values are decoded into owned objects, but maps are mutable and every public
            # door promises detachment. Rebuild all leaves after page access so no frame, store or
            # collaborator capability can cross the boundary.
            active: set[int] = set()
            rows: list[ScanRowV1] = []
            for row_position, (_ref, version) in enumerate(raw_rows):
                if control is not None:
                    control.check()
                raw_values = version.values if positions is None else tuple(version.values[p] for p in positions)
                detached_values = _scan_exact_scalar_values_snapshot(
                    raw_values,
                    max_string_characters=self._max_query_value_characters,
                )
                if detached_values is None:
                    detached_values = tuple(
                        _query_value_snapshot(
                            value,
                            field=f"scan.rows[{row_position}].values[{value_position}]",
                            depth=0,
                            active=active,
                            max_string_characters=self._max_query_value_characters,
                        )
                        for value_position, value in enumerate(raw_values)
                    )
                rows.append(
                    ScanRowV1(
                        record_id=_builtin_int(
                            version.record_id,
                            field=f"scan.rows[{row_position}].record_id",
                        ),
                        values=detached_values,
                    )
                )
            next_cursor = (
                None
                if next_position is None
                else ScanCursorV1._create(
                    owner=cursor_owner,
                    table_name=table,
                    table_id=table_def.table_id,
                    schema_version=table_def.schema_version,
                    position=next_position,
                    columns=columns,
                )
            )
            if control is not None:
                control.check()
            return ScanPageV1(rows=tuple(rows), next_cursor=next_cursor)

    def vector_total_memory_usage(self) -> VectorTotalMemoryUsage:
        """Return per-handle aggregate ANN reservations; disabled accounting reports zero."""
        with self._public_operation("vector_total_memory_usage"):
            vectors = self._require_component("vectors", self._vectors, "the vector engine (C9)")
            observe = getattr(vectors, "total_memory_usage", None)
            if not callable(observe):
                raise GrafxUnsupportedOperation(
                    "The vector collaborator does not expose aggregate memory observations.",
                    operation="vector_total_memory_usage",
                )
            return observe()

    def vector_memory_usage(self, space: str) -> VectorMemoryUsage:
        """Observe local HNSW cache tariffs without building or proving freshness.

        The per-picture limit is independent of query memory and is not an RSS or
        aggregate multi-handle ceiling. No persistent state is changed.
        """
        with self._public_operation("vector_memory_usage"):
            vectors = self._require_component("vectors", self._vectors, "the vector engine (C9)")
            index = vectors.index(_require_text("space", space))
            observe = getattr(index, "memory_usage", None)
            if not callable(observe):
                raise GrafxUnsupportedOperation(
                    "The vector collaborator does not expose memory observations.",
                    operation="vector_memory_usage",
                )
            return observe()

    def search_vectors(
        self,
        transaction: Transaction,
        *,
        space: str,
        query: Sequence[float] | VectorValue,
        k: int,
        candidate_filter: RecordIdFilter | None = None,
        timeout_seconds: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> VectorSearchResult:
        """Search one owned snapshot with optional cooperative read controls.

        Controls are checked inside native candidate/ranking/navigation loops, not
        preemptively inside a storage or third-party math call. No commit is cancelled.
        Custom vector engines must expose search_controlled to accept these controls.
        """
        return self._search_vectors_with_control(
            transaction, space=space, query=query, k=k, candidate_filter=candidate_filter,
            control=_read_control(self._clock, timeout_seconds, cancellation),
        )

    def _search_vectors_with_control(
        self,
        transaction: Transaction,
        *,
        space: str,
        query: Sequence[float] | VectorValue,
        k: int,
        candidate_filter: RecordIdFilter | None = None,
        control: _ReadControl | None = None,
        memory=None,
    ) -> VectorSearchResult:
        """Search vectors under the fixed snapshot of one active transaction.

        This is the safe replacement for reaching through ``database.vectors.search`` and
        supplying a raw ``TransactionContext`` or snapshot.  Ownership and liveness are checked
        before the vector engine is reached, and the page-access section keeps the recovery latch
        stable for the whole search.  The public filter is the immutable exact
        :class:`RecordIdFilter`; accepting an arbitrary predicate would let caller code roll back
        this transaction from inside the engine and withdraw its reader pin mid-search.  The
        vector collaborator itself never leaves the database.
        """
        with self._public_operation("search_vectors"):
            self._require_open()
            if type(transaction) is not Transaction:
                observed = _builtin_type_name(transaction)
                raise GrafxConfigurationError(
                    f"A vector search takes a Transaction; got {observed}.",
                    field="transaction",
                    value=observed,
                )
            if transaction._database is not self:
                raise GrafxTransactionStateError(
                    "A vector search can only use a transaction opened by this database.",
                    txn_id=transaction.txn_id,
                    field="transaction_owner",
                    path=self._path,
                )
            # Exact type, not isinstance: a subclass can add a callback or mutable backdoor to
            # the otherwise frozen DTO.  Refuse it before resolving or entering the engine.
            if (
                candidate_filter is not None
                and type(candidate_filter) is not RecordIdFilter
            ):
                observed = _builtin_type_name(candidate_filter)
                raise GrafxConfigurationError(
                    "A public vector candidate filter must be an immutable RecordIdFilter or "
                    f"None; got {observed}.",
                    field="candidate_filter",
                    value=observed,
                )
            # Detach every caller-controlled leaf and iterable before participant coordination.
            # A custom sequence may execute host code while being copied; if it rolls this
            # transaction back, the liveness recheck below refuses before search.
            wanted_space = _require_text("space", space)
            wanted_k = _require_positive_integer("k", k)
            wanted_query = _vector_query_snapshot(query)
            wanted_filter = _record_id_filter_snapshot(candidate_filter)
            vectors = self._require_component(
                "vectors", self._vectors, "the vector engine (C9)"
            )
            with self._transactions.page_access_section():
                # Liveness is checked under the same participant section that commit/rollback
                # use. Checking before it would let a racing rollback withdraw this snapshot's
                # reader pin in the gap and leave the search below a recyclable horizon.
                transaction._require_active()
                dirty_table = next(
                    (
                        table
                        for intent in transaction._context.row_intents
                        if (table := getattr(intent, "table", None)) is not None
                        and any(
                            getattr(column, "vector_space", None) == wanted_space
                            for column in getattr(table, "columns", ())
                        )
                    ),
                    None,
                )
                if dirty_table is not None:
                    raise GrafxUnsupportedOperation(
                        f"A vector search in {wanted_space!r} cannot include rows of "
                        f"{dirty_table.name!r} this transaction has staged: the vector index "
                        "describes only committed rows. Commit or roll back first; vector "
                        "read-your-own-writes is not implemented in this build.",
                        field="table",
                        value=dirty_table.name,
                        table_id=dirty_table.table_id,
                        operation="search_vectors",
                        space=wanted_space,
                    )
                snapshot = _public_snapshot(transaction._context.snapshot)
                operation = vectors.search
                extra = {}
                if control is not None or memory is not None:
                    if control is not None:
                        control.check()
                    operation = getattr(vectors, "search_controlled", None)
                    if not callable(operation):
                        raise GrafxUnsupportedOperation(
                            "The vector engine does not support cooperative controls.",
                            operation="search_vectors", field="read_control",
                        )
                    extra["control"] = control
                    extra["memory"] = memory
                result = operation(
                    space=wanted_space,
                    query=wanted_query,
                    k=wanted_k,
                    snapshot=snapshot,
                    candidate_filter=wanted_filter,
                    **extra,
                )
                if control is not None:
                    control.check()
            return _vector_search_result_view(
                result,
                requested_k=wanted_k,
                requested_space=wanted_space,
                candidate_filter=wanted_filter,
            )

    # --- operator surface ---------------------------------------------------------------------

    def search_hybrid(self, reader: Transaction | None = None, *, table: str,
                      index: str | None, query: str, space: str | None,
                      vector: Sequence[float], k: int = 20,
                      options: HybridSearchOptions | None = None,
                      filter: RecordIdFilter | None = None,
                      text_limits: TextSearchLimits | None = None,
                      timeout_seconds: float | None = None,
                      cancellation: CancellationToken | None = None) -> HybridSearchResult:
        """Fuse text/vector candidate windows with RRF-v1 and optional bounded graph evidence."""
        if reader is None:
            with self.begin("read") as owned:
                return self.search_hybrid(owned, table=table, index=index, query=query,
                    space=space, vector=vector, k=k, options=options, filter=filter,
                    text_limits=text_limits, timeout_seconds=timeout_seconds, cancellation=cancellation)
        if type(reader) is not Transaction:
            raise GrafxConfigurationError("Expected Transaction reader.", field="reader")
        return _search_hybrid(self, reader, table=table, index=index, query=query,
            space=space, vector=vector, k=k, options=options, filter=filter,
            text_limits=text_limits, timeout_seconds=timeout_seconds, cancellation=cancellation)

    def create_text_index(self, name: str, table: str, columns: tuple[str, ...], *, options: TextIndexOptions | None = None, bucket_count: int = 64) -> IndexView:
        """Create a native persisted full-text generation over one to four STRING fields."""
        return _create_text_index(self, name, table, columns, options=options, bucket_count=bucket_count)

    def replace_text_index(self, name: str, *, options: TextIndexOptions) -> IndexView:
        """Atomically replace a text analyzer/options using a fresh complete generation.

        The table, fields and sizing stay fixed. Old files are not reinterpreted
        or deleted; catalog publication is the only switch. Each new search uses
        the currently published analyzer with its owning data snapshot, while an
        in-flight certificate must still validate its selected generation.
        """
        if type(name) is not str or type(options) is not TextIndexOptions:
            raise GrafxConfigurationError("Expected an index name and TextIndexOptions.", field="replace_text_index")
        from okto_grafx.domain.index.fulltext import is_fulltext
        with self._public_operation("replace_text_index"):
            self._require_writable("replace text index")
            with self._transactions.page_access_section(fresh_read_view=True):
                logical = self._catalog.catalog.index_definition(name)
                if not is_fulltext(logical.key_derivation) or logical.active_generation() is None:
                    raise GrafxConfigurationError("Select an active full-text index.", field="name")
                table = self._catalog.catalog.table_by_id(logical.table_id)
                columns = tuple(table.columns[position].name for position in logical.positions)
                buckets = logical.active_generation().bucket_count
            return _create_text_index(self, logical.name, table.name, columns, options=options,
                bucket_count=buckets, _replace_existing=True)

    def search_text(self, reader: Transaction | None = None, *, index: str, query: str, k: int = 20,
                    filter: RecordIdFilter | None = None, limits: TextSearchLimits | None = None,
                    k1: float = 1.2, b: float = 0.75, timeout_seconds: float | None = None,
                    cancellation: CancellationToken | None = None, prefix: bool = False,
                    phrase: bool = False, return_positions: bool = False, slop: int = 0) -> TextSearchResult:
        """Read bounded BM25 hits in a caller-owned reader or a fresh autocommit snapshot."""
        if reader is None:
            with self.begin("read") as owned:
                return self.search_text(owned, index=index, query=query, k=k, filter=filter, limits=limits, k1=k1, b=b, timeout_seconds=timeout_seconds, cancellation=cancellation, prefix=prefix, phrase=phrase, return_positions=return_positions, slop=slop)
        if type(reader) is not Transaction:
            raise GrafxConfigurationError("reader must be a Transaction.", field="reader")
        return _search_text(self, reader, index=index, query=query, k=k, filter=filter, limits=limits, k1=k1, b=b, timeout_seconds=timeout_seconds, cancellation=cancellation, prefix=prefix, phrase=phrase, return_positions=return_positions, slop=slop)

    def create_index(
        self,
        name: str,
        table: str,
        columns: Sequence[str],
        *,
        bucket_count: int | None = None,
        expected_cardinality: int | None = None,
        layout: str = "hash",
    ) -> IndexView:
        """Create and atomically publish one custom exact index.

        The operation owns a fresh, dedicated write transaction. ``bucket_count`` selects the
        physical directory directly; ``expected_cardinality`` lets Grafx derive it. Supplying
        both is refused by the same planner used by textual ``CREATE INDEX``. The ordered
        layout is selected explicitly with ``layout='ordered'`` and accepts only a
        TIMESTAMP+STRING key, without hash sizing hints.
        ``layout='posting_hash'`` deduplicates repeated property keys per page;
        it remains an exact candidate index with native heap visibility checks.
        """
        with self._public_operation("create_index"):
            self._require_open()
            self._require_writable("create an exact index")
            self._require_component(
                "indexes", self._indexes, "the index framework (C7)"
            )
            engine = self._require_component(
                "queries", self._queries, "the query engine (C10)"
            )
            creator = getattr(engine, "create_index", None)
            if not callable(creator):
                raise GrafxUnsupportedOperation(
                    "The query engine of this composition has no custom exact-index door.",
                    field="component",
                    value="create_index",
                )

            wanted_name = _require_text("name", name)
            wanted_table = _require_text("table", table)
            wanted_columns = _index_columns_snapshot(columns)
            wanted_bucket_count = (
                None
                if bucket_count is None
                else _require_positive_integer("bucket_count", bucket_count)
            )
            wanted_expected_cardinality = (
                None
                if expected_cardinality is None
                else _require_positive_integer(
                    "expected_cardinality", expected_cardinality
                )
            )
            wanted_layout = _require_text("layout", layout)

            transaction = self.begin("write")
            try:
                with self._transactions.page_access_section():
                    self._require_open()
                    transaction._require_active()
                    self._public_contexts.setdefault(
                        transaction.txn_id, transaction._context
                    )
                    creator(
                        name=wanted_name,
                        table=wanted_table,
                        columns=wanted_columns,
                        bucket_count=wanted_bucket_count,
                        expected_cardinality=wanted_expected_cardinality,
                        layout=wanted_layout,
                        txn=transaction._context,
                    )
                transaction.commit()
            except BaseException as failure:
                if transaction.active:
                    try:
                        transaction.rollback()
                    except BaseException as cleanup_failure:
                        _note_cleanup_failure(failure, cleanup_failure)
                raise

            self._refresh_index_inventory()
            return self._committed_index_receipt(wanted_name)

    def rehash_index(
        self,
        name: str,
        *,
        bucket_count: int | None = None,
        expected_cardinality: int | None = None,
    ) -> IndexView:
        """Grow one exact index through an immutable foreground shadow generation.

        Exactly one sizing hint is required.  The resolved directory must be larger than the
        current ACTIVE generation; equal-size re-creation and shrinking are deliberately not
        supported.  The old file remains catalogued as STALE after the new, fully built file and
        its catalog publication are durable.
        """

        with self._public_operation("rehash_index"):
            self._require_open()
            self._require_writable("rehash an exact index")
            self._require_component(
                "indexes", self._indexes, "the index framework (C7)"
            )
            wanted_name = _require_text("name", name)
            wanted_bucket_count = (
                None
                if bucket_count is None
                else _require_positive_integer("bucket_count", bucket_count)
            )
            wanted_expected_cardinality = (
                None
                if expected_cardinality is None
                else _require_positive_integer(
                    "expected_cardinality", expected_cardinality
                )
            )

            transaction = self.begin("write")
            try:
                self._transactions.prepare_index_rehash(
                    transaction._context,
                    name=wanted_name,
                    bucket_count=wanted_bucket_count,
                    expected_cardinality=wanted_expected_cardinality,
                )
                transaction.commit()
            except BaseException as failure:
                if transaction.active:
                    try:
                        transaction.rollback()
                    except BaseException as cleanup_failure:
                        _note_cleanup_failure(failure, cleanup_failure)
                raise

            self._refresh_index_inventory()
            return self._committed_index_receipt(wanted_name)

    def rebuild_index(self, name: str) -> IndexView:
        """Rebuild one exact index into a compact, immutable fresh generation.

        The active generation remains authoritative until the complete replacement and its WAL
        publication are durable. The former generation is retained as ``STALE`` for rollback and
        audit provenance; no in-place reset can expose a partially rebuilt tree.
        """

        with self._public_operation("rebuild_index"):
            self._require_open()
            self._require_writable("rebuild an exact index")
            self._require_component(
                "indexes", self._indexes, "the index framework (C7)"
            )
            wanted_name = _require_text("name", name)

            transaction = self.begin("write")
            try:
                self._transactions.prepare_index_rehash(
                    transaction._context,
                    name=wanted_name,
                    rebuild=True,
                )
                transaction.commit()
            except BaseException as failure:
                if transaction.active:
                    try:
                        transaction.rollback()
                    except BaseException as cleanup_failure:
                        _note_cleanup_failure(failure, cleanup_failure)
                raise

            self._refresh_index_inventory()
            return self._committed_index_receipt(wanted_name)

    def rehash_index_if_needed(
        self,
        name: str,
        *,
        overflow_pages_per_bucket: int = 1,
        check_skew: bool = False,
    ) -> IndexView | None:
        """Grow one exact index after a bounded directory-pressure assessment.

        The probe validates the physical identity and only the eager head page of each bucket;
        it never follows overflow chains or decodes entries.  Its cost is therefore
        O(bucket_count), capped by the format at 65,536 pages, rather than O(index entries). One
        growth step is suggested when average occupied head slots reach the canonical sizing
        target, or when retained overflow reaches the configured integer ratio to bucket heads.
        Tombstones and pages retained after an interrupted append can make either signal
        conservative and cause an early rebuild.  Neither signal certifies index health or
        authorizes reads: the existing foreground rehash rebuilds and verifies a complete
        immutable shadow.

        This is an explicit maintenance operation, never a commit hook or background loop.  One
        call grows by at most one power-of-two step and returns ``None`` below the threshold or at
        the eager-directory ceiling.  A concurrent participant may supersede the observed ACTIVE
        generation before preparation; ordinary rehash OCC/generation/growth checks may then
        refuse the attempt.  The caller should reassess current state rather than retry blindly;
        no particular refusal is promised to be retryable.
        """

        with self._public_operation("rehash_index_if_needed"):
            if type(check_skew) is not bool:
                raise GrafxConfigurationError("check_skew must be boolean.", field="check_skew")
            self._require_open()
            self._require_writable("rehash an exact index when physically pressured")
            indexes = self._require_component(
                "indexes", self._indexes, "the index framework (C7)"
            )
            wanted_name = _require_text("name", name)
            wanted_ratio = _require_positive_integer(
                "overflow_pages_per_bucket", overflow_pages_per_bucket
            )

            with self._transactions.page_access_section(fresh_read_view=True):
                self._require_open()
                active_index = getattr(indexes, "active_index", None)
                selected = (
                    active_index(wanted_name, catalog=self._catalog._catalog)
                    if callable(active_index)
                    else indexes.index(wanted_name)
                )
                definition = selected.definition
                if definition.visibility is not IndexVisibility.EXACT:
                    raise GrafxUnsupportedOperation(
                        "Assisted rehash applies only to exact indexes.",
                        operation="rehash_index_if_needed",
                        field="visibility",
                        value=definition.visibility.value,
                        index=definition.name,
                    )
                bucket_count = definition.bucket_count
                # Even a no-op proves the selected physical identity.  At the directory ceiling
                # there is no useful reason to sample thousands of heads after that proof.
                if bucket_count >= MAX_BUCKET_COUNT:
                    selected.open()
                    return None
                head_entries, overflow_pages = selected.assisted_rehash_pressure()
                if (
                    head_entries
                    < bucket_count * TARGET_ENTRIES_PER_BUCKET
                    and overflow_pages < bucket_count * wanted_ratio
                ):
                    return None
                target_bucket_count = min(MAX_BUCKET_COUNT, bucket_count * 2)

            # Re-enter the existing public protocol after releasing the read observation.  Its
            # fresh catalog/OCC checks are the authority; the advisory page-count sample is not.
            if check_skew and self.index_distribution(wanted_name).recommendation == "inspect_key_skew":
                return None
            return self.rehash_index(
                wanted_name,
                bucket_count=target_bucket_count,
            )

    def index_cache_usage(self, name: str) -> KeyPageCacheUsage:
        """Return active-index local memo observations; not a cache/freshness certificate."""
        with self._public_operation("index_cache_usage"):
            self._require_open()
            wanted = _require_text("name", name)
            with self._transactions.page_access_section():
                return self._indexes.active_index(wanted)._key_page_memo.usage()

    def index_distribution(
        self, name: str, *, max_pages: int = 65_536, max_entries: int = 1_000_000,
        max_memory_bytes: int = 64 * 1024 * 1024,
    ) -> IndexDistribution:
        """Bounded physical HASH distribution, without exposing keys or mutating data.

        Retained old entries count too. This explicit maintenance census is O(entries
        + pages), not a per-query optimization or an authoritative live cardinality.
        """
        from okto_grafx.engine.index_distribution import index_distribution
        return index_distribution(self, name, max_pages=max_pages, max_entries=max_entries,
                                  max_memory_bytes=max_memory_bytes)

    def verify(self, scope: str = "all") -> VerificationReport:
        """Walk the database and report every finding, precisely located (SPEC-M1 FR-11).

        ``scope`` is one of ``"pages"``, ``"records"``, ``"indexes"`` or ``"all"``. A clean
        database produces a report with no findings.
        """
        with self._public_operation("verify"):
            self._require_open()
            wanted_scope = _require_text("scope", scope)
            factory = self._require_component(
                "verifier", self._verifier_factory, "the verifier (C6)"
            )
            # Built per call, not held: a verifier is given the index set it must walk, and a
            # database registers indexes for as long as it is open. A verifier captured at open
            # would quietly report a clean "indexes" scope for every index registered after it.
            with self._transactions.page_access_section(fresh_read_view=True):
                verifier = factory()  # type: ignore[operator]
                report = verifier.verify(wanted_scope)  # type: ignore[attr-defined]
                if wanted_scope != "indexes" and self._catalog.catalog.commit_catalog_activation is not None:
                    report = self._verify_commit_history(report)
                    if self._catalog.catalog.system_history_tables():
                        report = self._verify_system_history(report)
                return _verification_report_view(report, requested_scope=wanted_scope)

    def _verify_system_history(self, report: VerificationReport) -> VerificationReport:
        """Include complete temporal chain/interval validation and current-row agreement."""
        try:
            with self.begin("read") as tx:
                catalog = self._catalog.catalog
                tables = tuple(catalog.table_by_id(key) for key, _, _ in catalog.system_history_tables())
                graph = tx.system_as_of(CommitId(self.identity.database_uuid, tx.snapshot.read_lsn),
                    tables=tuple(table.name for table in tables), limits=TemporalLimits(access_path="scan"))
                current = {(table.table_id, version.record_id): tuple(version.values)
                           for table in tables for _, version in self._heap.scan(table, tx._context.snapshot)}
                historical = {(row.table_id, row.record_id): row.values for row in graph.rows}
                if current != historical:
                    raise GrafxCorruptionDetected("Current rows disagree with native history.", field="system_history_current")
            return replace(report, records_checked=report.records_checked + graph.events_scanned,
                pages_checked=report.pages_checked + self._storage.page_count("system-history.dat"),
                files_checked=tuple(dict.fromkeys((*report.files_checked, "system-history.dat"))))
        except GrafxError:
            finding = VerificationFinding(kind=FindingKind.CATALOG_UNREADABLE,
                location=FindingLocation(file="system-history.dat"),
                detail="Temporal history verification refused; complete lineage/current-state agreement could not be proved.")
            return replace(report, findings=(*report.findings, finding))

    def _verify_commit_history(self, report: VerificationReport) -> VerificationReport:
        """Include logical history in public verification, with no repair side effect."""
        files = ("commits.dir", "commits.dat")
        try:
            with self.begin("read") as transaction:
                def check(store: CommitCatalogStore) -> tuple[int, int]:
                    """Verify complete history and count the physically checked journal pages."""
                    head = store.verify()
                    pages = sum(self._storage.file_size(file) // self._pool.page_size for file in files)
                    return head.entry_count, pages

                records, pages = self._observe_history(transaction._context, check, (0, 0))
            return replace(
                report, records_checked=report.records_checked + records,
                pages_checked=report.pages_checked + pages,
                files_checked=tuple(dict.fromkeys((*report.files_checked, *files))),
            )
        except GrafxError as failure:
            # Never certify a partial/moving/unreadable history as a clean empty
            # result. Keep metadata and raw exception payloads out of diagnostics.
            finding = VerificationFinding(
                kind=FindingKind.CATALOG_UNREADABLE,
                location=FindingLocation(file="commits.dir"),
                detail="Commit history verification refused; journal publication or integrity could not be proved.",
            )
            del failure
            return replace(report, findings=(*report.findings, finding))

    def _bloat(self, table: str | None = None) -> BloatReport:
        """Measure heap bloat at the existing recyclable horizon without changing state.

        The public door lives on :class:`Maintenance`; this private database operation supplies
        the same lifecycle containment and current-page boundary as verification without adding
        a second top-level API. The transaction manager observes the checkpoint-capped WAL horizon
        without pruning TTL-stalled reader records; until mutating vacuum has a stronger
        reader-lifecycle contract, a more aggressive estimate would advertise bytes beyond even
        the existing WAL-retention boundary. The returned eligibility counts still state
        explicitly that vacuum safety is not established.
        """
        with self._public_operation("measure heap bloat"):
            self._require_open()
            wanted_table = None if table is None else _require_text("table", table)
            with self._transactions.page_access_section(
                fresh_read_view=True,
                allow_writeback=False,
            ):
                catalog = self._catalog.catalog
                tables = (
                    catalog.tables()
                    if wanted_table is None
                    else (catalog.table(wanted_table),)
                )
                horizon = self._transactions.observational_recyclable_horizon()
                samples = tuple(
                    (table_def, self._heap._measure_bloat(table_def, horizon))
                    for table_def in tables
                )

            table_reports = tuple(
                TableBloatReport(
                    table=_builtin_text(table_def.name, field="table", empty=False),
                    table_id=_builtin_int(sample.table_id, field="table_id"),
                    data_pages=_builtin_int(sample.data_pages, field="data_pages"),
                    slot_directory_entries=_builtin_int(
                        sample.slot_directory_entries,
                        field="slot_directory_entries",
                    ),
                    free_slots=_builtin_int(sample.free_slots, field="free_slots"),
                    stored_versions=_builtin_int(
                        sample.stored_versions, field="stored_versions"
                    ),
                    ended_versions=_builtin_int(
                        sample.ended_versions, field="ended_versions"
                    ),
                    horizon_eligible_versions=_builtin_int(
                        sample.horizon_eligible_versions,
                        field="horizon_eligible_versions",
                    ),
                    horizon_retained_versions=_builtin_int(
                        sample.horizon_retained_versions,
                        field="horizon_retained_versions",
                    ),
                    horizon_eligible_slot_bytes=_builtin_int(
                        sample.horizon_eligible_slot_bytes,
                        field="horizon_eligible_slot_bytes",
                    ),
                    horizon_retained_slot_bytes=_builtin_int(
                        sample.horizon_retained_slot_bytes,
                        field="horizon_retained_slot_bytes",
                    ),
                    overflow_versions=_builtin_int(
                        sample.overflow_versions,
                        field="overflow_versions",
                    ),
                    horizon_eligible_overflow_versions=_builtin_int(
                        sample.horizon_eligible_overflow_versions,
                        field="horizon_eligible_overflow_versions",
                    ),
                )
                for table_def, sample in samples
            )

            def total(field: str) -> int:
                """Sum one exact integer field from the detached per-table reports."""
                return sum(
                    _builtin_int(getattr(report, field), field=field)
                    for report in table_reports
                )

            return BloatReport(
                recyclable_horizon_lsn=_builtin_int(
                    horizon, field="recyclable_horizon_lsn"
                ),
                vacuum_safety_established=False,
                tables=table_reports,
                data_pages=total("data_pages"),
                slot_directory_entries=total("slot_directory_entries"),
                free_slots=total("free_slots"),
                stored_versions=total("stored_versions"),
                ended_versions=total("ended_versions"),
                horizon_eligible_versions=total("horizon_eligible_versions"),
                horizon_retained_versions=total("horizon_retained_versions"),
                horizon_eligible_slot_bytes=total("horizon_eligible_slot_bytes"),
                horizon_retained_slot_bytes=total("horizon_retained_slot_bytes"),
                overflow_versions=total("overflow_versions"),
                horizon_eligible_overflow_versions=total(
                    "horizon_eligible_overflow_versions"
                ),
            )

    def _vacuum(
        self,
        table: str | None = None,
        *,
        confirm_quiescent: bool,
        max_versions: int | None,
        index_free_pages: bool = False,
    ) -> VacuumReport:
        """Execute the guarded two-transaction vacuum v1 protocol."""

        with self._public_operation("vacuum MVCC history"):
            self._require_open()
            self._require_writable("vacuum MVCC history")
            if type(index_free_pages) is not bool:
                raise GrafxConfigurationError("index_free_pages must be a bool.", field="index_free_pages")
            wanted_table = None if table is None else _require_text("table", table)
            wanted_limit = (
                None
                if max_versions is None
                else _require_positive_integer("max_versions", max_versions)
            )
            with self._transactions.quiescent_maintenance_section(
                confirm_quiescent=confirm_quiescent
            ):
                with self._transactions.page_access_section(fresh_read_view=True):
                    source = self._catalog.catalog
                    if source.format_version != CATALOG_FORMAT_VERSION:
                        raise GrafxUnsupportedOperation(
                            "MVCC vacuum requires catalog v2; run "
                            "maintenance.ensure_identity_indexes() first.",
                            operation="vacuum",
                            field="format_version",
                            value=source.format_version,
                            required=CATALOG_FORMAT_VERSION,
                            remedy="maintenance.ensure_identity_indexes",
                        )
                    selected = (
                        source.tables()
                        if wanted_table is None
                        else (source.table(wanted_table),)
                    )
                    floor_before = self._heap.reclaim_floor()
                    capability_active = (
                        HEAP_RECLAIM_V1_CAPABILITY in source.required_capabilities()
                    )
                    from okto_grafx.engine.free_page_index import CAPABILITY
                    if index_free_pages and CAPABILITY not in source.required_capabilities():
                        capability_active = False

                capability_activated = False
                capability_wrote = False
                if not capability_active:
                    activation = self.begin("write")
                    try:
                        capability_activated = (
                            self._transactions.prepare_heap_reclaim_activation(
                                activation._context, index_free_pages=index_free_pages
                            )
                        )
                        activation_report = activation.commit()
                        capability_wrote = bool(activation_report.wrote)
                    except BaseException as failure:
                        if activation.active:
                            try:
                                activation.rollback()
                            except BaseException as cleanup_failure:
                                _note_cleanup_failure(failure, cleanup_failure)
                        raise
                    self._refresh_index_inventory()

                # Re-resolve table objects and the horizon after capability publication.  The
                # selected LSN is the newest globally published state inside the operator's
                # quiescent window; no TTL inference participates in it.
                with self._transactions.page_access_section(fresh_read_view=True):
                    current = self._catalog.catalog
                    selected = (
                        current.tables()
                        if wanted_table is None
                        else (current.table(wanted_table),)
                    )
                    horizon = self._transactions.published_state().last_committed_lsn

                transaction = self.begin("write")
                try:
                    plan, index_reports, planned_floor_before, planned_floor_after = (
                        self._transactions.prepare_vacuum(
                            transaction._context,
                            selected,
                            horizon,
                            max_versions=wanted_limit,
                        )
                    )
                    if planned_floor_before != floor_before:
                        raise GrafxTransactionStateError(
                            "The heap reclaim floor changed inside an asserted quiescent "
                            "vacuum window.",
                            operation="vacuum",
                            field="reclaim_floor_lsn",
                            expected=floor_before,
                            observed=planned_floor_before,
                        )
                    commit_report = transaction.commit()
                except BaseException as failure:
                    if transaction.active:
                        try:
                            transaction.rollback()
                        except BaseException as cleanup_failure:
                            _note_cleanup_failure(failure, cleanup_failure)
                    raise

                tables_by_id = {table_def.table_id: table_def for table_def in selected}
                table_reports = tuple(
                    TableVacuumReport(
                        table=_builtin_text(
                            tables_by_id[item.table_id].name,
                            field="table",
                            empty=False,
                        ),
                        table_id=_builtin_int(item.table_id, field="table_id"),
                        pages_scanned=_builtin_int(
                            item.pages_scanned, field="pages_scanned"
                        ),
                        eligible_inline_versions=_builtin_int(
                            item.eligible_inline_versions,
                            field="eligible_inline_versions",
                        ),
                        reclaimed_versions=_builtin_int(
                            item.reclaimed_versions, field="reclaimed_versions"
                        ),
                        reclaimed_slot_bytes=_builtin_int(
                            item.reclaimed_slot_bytes,
                            field="reclaimed_slot_bytes",
                        ),
                        relinked_versions=_builtin_int(
                            item.relinked_versions, field="relinked_versions"
                        ),
                        skipped_overflow_versions=_builtin_int(
                            item.skipped_overflow_versions,
                            field="skipped_overflow_versions",
                        ),
                        eligible_overflow_versions=_builtin_int(
                            item.eligible_overflow_versions, field="eligible_overflow_versions"
                        ),
                        reclaimed_overflow_pages=_builtin_int(
                            item.reclaimed_overflow_pages, field="reclaimed_overflow_pages"
                        ),
                    )
                    for item in plan.tables
                )

                def total(field: str) -> int:
                    """Sum one validated integer field across the selected table reports."""
                    return sum(
                        _builtin_int(getattr(item, field), field=field)
                        for item in table_reports
                    )

                return VacuumReport(
                    horizon_lsn=_builtin_int(horizon, field="horizon_lsn"),
                    reclaim_floor_before=_builtin_int(
                        floor_before, field="reclaim_floor_before"
                    ),
                    reclaim_floor_after=_builtin_int(
                        planned_floor_after, field="reclaim_floor_after"
                    ),
                    capability_activated=_builtin_bool(capability_activated),
                    wrote=_builtin_bool(capability_wrote or commit_report.wrote),
                    complete=_builtin_bool(plan.complete),
                    tables=table_reports,
                    pages_rewritten=len(plan.page_images),
                    reclaimed_versions=total("reclaimed_versions"),
                    reclaimed_slot_bytes=total("reclaimed_slot_bytes"),
                    relinked_versions=total("relinked_versions"),
                    skipped_overflow_versions=total("skipped_overflow_versions"),
                    reclaimed_overflow_pages=total("reclaimed_overflow_pages"),
                    indexes_reconciled=len(index_reports),
                    index_entries_removed=sum(
                        _builtin_int(report.removed, field="index_entries_removed")
                        for report in index_reports
                    ),
                )

    def add_nullable_column(self, table: str, column: ColumnDef) -> TableDef:
        """Atomically append one nullable non-vector column, without rewriting old rows.

        Requires explicit identity-index activation. Publishes the one-way
        nullable_columns_v1 capability; incompatible old binaries refuse the store.
        One dedicated native transaction, no automatic retry or backfill.
        """
        from okto_grafx.engine.public_views import _table_definition
        if type(table) is not str or table.startswith("_grafx_") or type(column) is not ColumnDef:
            raise GrafxConfigurationError("Invalid nullable-column input.", field="column")
        captured = ColumnDef(column.name, column.type, nullable=column.nullable, vector_space=column.vector_space)
        with self._public_operation("add_nullable_column"):
            self._require_open()
            self._require_writable("add nullable column")
            engine = self._require_component("queries", self._queries, "the query engine (C10)")
            with self.begin("write") as tx:
                with self._transactions.page_access_section(transaction=tx._context):
                    self._public_contexts.setdefault(tx.txn_id, tx._context)
                    updated = engine.add_nullable_column(tx._context, table, captured)
            return _table_definition(updated)

    def ensure_identity_indexes(self) -> None:
        """Persist and activate every exact access path required by endpoint identities.

        This is the explicit, one-way catalog-v1 to catalog-v2 door.  It constructs complete
        nonced shadow generations under the ordinary writer/commit fences, barriers those files
        before the catalog can name them, and publishes the complete catalog change through the
        existing WAL-before-data protocol.  Repeating it after every required identity generation
        is active and fresh performs no durable write.  Read-only handles always refuse the door,
        including when the current durable state would make it a no-op.
        """

        with self._public_operation("ensure_identity_indexes"):
            self._require_open()
            self._require_writable("ensure identity indexes")
            transaction = self.begin("write")
            try:
                self._transactions.prepare_identity_index_activation(
                    transaction._context
                )
                transaction.commit()
            except BaseException as failure:
                if transaction._context.active:
                    try:
                        transaction.rollback()
                    except BaseException as cleanup_failure:
                        _note_cleanup_failure(failure, cleanup_failure)
                raise

            self._refresh_index_inventory()
            return None

    def system_as_of(self, at: CommitId | Timestamp, *, tables: tuple[str, ...],
                     limits: TemporalLimits = TemporalLimits()) -> TemporalGraph:
        """Return a bounded historical graph at a qualified commit or ordered timestamp.

        Include both endpoint tables when requesting relationships. Historical
        identities never resolve through recreated primary keys. History before
        activation or retention has distinct typed refusal, not an empty graph.
        """
        from okto_grafx.engine.system_history_reader import _inputs
        at = _inputs(self, at, tables, limits, None)
        with self.begin("read") as transaction:
            return transaction.system_as_of(at, tables=tables, limits=limits)

    def system_versions(self, table: str, record_id: int, *,
                        limits: TemporalLimits = TemporalLimits()) -> TemporalVersions:
        """Return create/update/delete-bounded intervals for one logical row identity."""
        from okto_grafx.engine.system_history_reader import _inputs
        _inputs(self, CommitId(self.identity.database_uuid, 1), (table,), limits, record_id)
        with self.begin("read") as transaction:
            return transaction.system_versions(table, record_id, limits=limits)

    def _read_system_history(self, context, *, at, tables, limits, record_id=None):
        from okto_grafx.engine.system_history_reader import read_system_history
        with self._public_operation("read system history"):
              return read_system_history(self, context, at=at, tables=tables, limits=limits, record_id=record_id)

    def system_diff(self, before: CommitId, after: CommitId, *, tables: tuple[str, ...],
                    limits: TemporalLimits = TemporalLimits(), max_changes: int = 100_000) -> TemporalDiff:
        """Compare retained system-time commits; no valid-time or write effects are introduced."""
        from okto_grafx.temporal_diff import _validate
        _validate(before, after, limits, max_changes)
        with self.begin('read') as reader:
            return reader.system_diff(before, after, tables=tables, limits=limits, max_changes=max_changes)

    def enable_system_history(self, tables: tuple[str, ...]) -> None:
        """Atomically opt tables into durable system-time history with their current baseline.

        Requires explicit identity-index and commit-history activation first.
        Relationship history requires both endpoint tables enabled together or
        previously. A baseline exceeding native history budgets refuses intact.
        Activation is one-way; this is not retained MVCC or an automatic migration.
        """
        with self._public_operation("enable_system_history"):
            self._require_writable("enable system history")
            with self.begin("write") as transaction:
                self._transactions._history_publication.stage_activation(transaction._context, tables)

    def enable_system_history_index(self, *, max_bytes: int = 16 * 1024 * 1024) -> bool:
        """Atomically build the optional persistent temporal access path.

        Requires native system history. Activation is one-way and refuses older
        readers through a required catalog capability. A dedicated bounded build
        captures all retained events; later commits update immutable search paths.
        Returns False when already active. No connection default is changed.
        """
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2**31:
            raise GrafxConfigurationError("Invalid index build budget.", field="max_bytes")
        with self._public_operation("enable_system_history_index"):
            self._require_writable("enable system history index")
            with self.begin("write") as transaction:
                changed = self._transactions._history_publication.stage_index(
                    transaction._context, max_bytes=max_bytes)
            return changed

    def compact_system_history(self, *, confirm_quiescent: bool = False,
                               max_bytes: int = 16 * 1024 * 1024) -> TemporalCompactionReport:
        """Reclaim redacted history payloads and obsolete temporal tree paths offline.

        Requires no open local transaction and explicit confirmation that other
        processes are stopped. Retained horizons, pins and row/edge lineage do not
        change. A native full-image COMMIT and checkpoint precede truncation; a
        crash may leave an unused tail, never a partly authoritative history.
        No old WAL, physical backup or filesystem snapshot is securely erased.
        """
        from okto_grafx.engine.system_history_operations import compact
        return compact(self, confirm_quiescent=confirm_quiescent, max_bytes=max_bytes)

    def pin_system_history(self, name: str, at: CommitId, *, tables: tuple[str, ...]) -> None:
        """Persist named protection against retention beyond ``at`` for selected tables.

        Pins survive close/crash, have no TTL and require explicit unpinning. They
        protect logical history only, not physical MVCC/WAL retention. Repeating an
        identical binding is a no-op; rebinding an existing name refuses.
        """
        from okto_grafx.engine.system_history_operations import control
        control(self, operation="pin system history", name=name, before=at, tables=tables)

    def unpin_system_history(self, name: str) -> None:
        """Explicitly release a durable temporal pin; an absent valid name is a no-op."""
        from okto_grafx.engine.system_history_operations import control
        control(self, operation="unpin system history", name=name)

    def system_history_pins(self) -> tuple[TemporalPin, ...]:
        """List durable temporal pins in name order under a qualified publication read."""
        from okto_grafx.engine.system_history_operations import pins
        return pins(self)

    def prune_system_history(self, before: CommitId, *, tables: tuple[str, ...],
                             max_bytes: int = 16 * 1024 * 1024) -> TemporalPruneReport:
        """Atomically redact payloads of versions closed at/before a new retained horizon.

        Explicit pins prevent incompatible pruning. Current versions, lineage,
        schema and interval framing remain. The bounded rewrite uses native WAL,
        OCC and COMMIT; a concurrent publication can require caller retry. This
        does not shrink files or securely erase old WAL, backups or snapshots.
        ``max_bytes`` caps captured history-file bytes, not process RSS.
        """
        from okto_grafx.engine.system_history_operations import control
        return control(self, operation="prune system history", before=before, tables=tables, max_bytes=max_bytes)

    def enable_commit_history(self) -> None:
        """Activate one-way durable provenance after ensure_identity_indexes().

        The activation commit establishes untracked legacy history, not an
        invented record. Subsequent writing commits publish qualified history.
        This is opt-in and adds journal IO/storage; it cannot be disabled.
        """
        with self._public_operation("enable_commit_history"):
            self._require_writable("enable commit history")
            with self.begin("write") as transaction:
                self._transactions.prepare_commit_catalog_activation(transaction._context)

    def commit_history(self, *, after: CommitId | None = None, limit: int = 100) -> CommitHistoryPage:
        """Read a bounded history page in a new snapshot; use a read transaction for paging."""
        self._require_open()
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise GrafxConfigurationError("History limit must be 1..1000.", field="limit")
        after = None if after is None else self._history_identity(after)
        with self.begin("read") as transaction:
            return transaction.commit_history(after=after, limit=limit)

    def lookup_commit(self, identity: CommitId) -> CommitCatalogEntry | None:
        """Return a durable entry visible in a new snapshot, or None in the tracked interval."""
        self._require_open()
        identity = self._history_identity(identity)
        with self.begin("read") as transaction:
            return transaction.lookup_commit(identity)

    def _history_identity(self, identity: CommitId) -> CommitId:
        if type(identity) is not CommitId:
            raise GrafxConfigurationError("Expected a qualified CommitId.", field="identity")
        captured = CommitId(identity.database_uuid, identity.sequence)
        if captured.database_uuid != self._identity.database_uuid:
            raise GrafxConfigurationError("Commit belongs to another database.", field="database_uuid")
        return captured

    def _observe_history(
        self, context: TransactionContext,
        operation: Callable[[CommitCatalogStore], _HistoryResult], empty: _HistoryResult,
    ) -> _HistoryResult:
        with self._transactions.page_access_section(transaction=context, allow_writeback=False):
            self._transactions._require_current_active(context)
            source = self._catalog.catalog
            activation = source.commit_catalog_activation
            if activation is None or context.snapshot.read_lsn < activation:
                raise GrafxUnsupportedOperation(
                    "Commit history is not enabled in this snapshot.", field="commit_catalog",
                    remedy="enable_commit_history",
                )

            def read(file: str, index: int) -> bytes:
                """Read a fresh detached page without retaining a journal authority cache."""
                return self._pool.codec.encode_page(self._pool.read_fresh_page(file, index))

            return observe_commit_catalog(
                database_uuid=self._identity.database_uuid, page_size=self._pool.page_size,
                activation=activation, read_page=read, file_size=self._storage.file_size,
                exists=self._storage.exists,
                published=lambda: self._transactions._published_state_in_section().last_committed_lsn,
                operation=operation, empty=empty, minimum_sequence=context.snapshot.read_lsn,
            )

    def _commit_history(self, context: TransactionContext, *, after: CommitId | None, limit: int) -> CommitHistoryPage:
        with self._public_operation("commit_history"):
            if type(limit) is not int or not 1 <= limit <= 1000:
                raise GrafxConfigurationError("History limit must be 1..1000.", field="limit")
            sequence = 0 if after is None else self._history_identity(after).sequence
            read_lsn = context.snapshot.read_lsn

            def collect(store: CommitCatalogStore) -> tuple[CommitCatalogEntry, ...]:
                """Collect a bounded ascending history page within the owning snapshot."""
                return store.history(after=sequence, read_lsn=read_lsn, limit=limit + 1)

            empty_entries: tuple[CommitCatalogEntry, ...] = ()
            entries = self._observe_history(context, collect, empty_entries)
            activation = self._catalog.catalog.commit_catalog_activation
            assert activation is not None
            return CommitHistoryPage(
                self._identity.database_uuid, activation,
                read_lsn, entries[:limit], len(entries) > limit,
            )

    def _lookup_commit(self, context: TransactionContext, identity: CommitId) -> CommitCatalogEntry | None:
        with self._public_operation("lookup_commit"):
            captured = self._history_identity(identity)
            return self._observe_history(
                context, lambda store: store.lookup(captured, read_lsn=context.snapshot.read_lsn), None,
            )

    def enable_wal_page_compression(self) -> None:
        """Persist the compatibility fence before emitting compressed WAL page images.

        Activation is explicit and one way because a database that has retained WAL-v2 records
        must never be opened by a build that understands only the legacy page-image grammar.
        The catalog capability is committed in a v1-only transaction; only later commits may
        select compressed records. Repeating the operation is a zero-write no-op.
        """

        with self._public_operation("enable_wal_page_compression"):
            self._require_open()
            self._require_writable("enable WAL page compression")
            transaction = self.begin("write")
            try:
                self._transactions.prepare_wal_record_v2_activation(
                    transaction._context
                )
                transaction.commit()
            except BaseException as failure:
                if transaction._context.active:
                    try:
                        transaction.rollback()
                    except BaseException as cleanup_failure:
                        _note_cleanup_failure(failure, cleanup_failure)
                raise

            self._refresh_index_inventory()
            return None

    def _refresh_index_inventory(self) -> None:
        """Refresh cached operational names from the committed catalog authority."""
        manager = self._indexes
        active_indexes = getattr(manager, "active_indexes", None)
        if callable(active_indexes):
            active = tuple(active_indexes(catalog=self._catalog.catalog))
            self._attached_indexes = tuple(
                _builtin_text(index.name, field="attached_index", empty=False)
                for index in active
            )
            self._stale_indexes = tuple(
                _builtin_text(index.name, field="stale_index", empty=False)
                for index in active
                if index.stale
            )
        refresh_reclaim = getattr(
            self._transactions,
            "_refresh_heap_reclaim_capability",
            None,
        )
        if callable(refresh_reclaim):
            refresh_reclaim()
        refresh_wal = getattr(
            self._transactions,
            "_refresh_wal_record_v2_capability",
            None,
        )
        if callable(refresh_wal):
            refresh_wal()

    def _committed_index_receipt(self, name: str) -> IndexView:
        """Return one ACTIVE view with page-zero horizons certified after its commit."""
        manager = self._require_component(
            "indexes", self._indexes, "the index framework (C7)"
        )
        active_index = getattr(manager, "active_index", None)
        if not callable(active_index):
            raise GrafxUnsupportedOperation(
                "The index framework cannot resolve committed catalog authority.",
                field="component",
                value="active_index",
            )
        # Establish freshness before capturing the catalog epoch. Doing it inside the loop's
        # validation section can itself advance that epoch and turn a successful local commit
        # into an endless optimistic retry.
        with self._transactions.page_access_section(fresh_read_view=True):
            pass
        while True:
            catalog, epoch = self._catalog_snapshot()
            authority = self._catalog._catalog
            tables = catalog.catalog.table_definitions
            with self._transactions.page_access_section():
                if (
                    _builtin_int(self._catalog._view_epoch(), field="catalog.epoch")
                    != epoch
                ):
                    continue
                selected = active_index(name, catalog=authority)
                # The freshness boundary above rebased clean frames to current publication;
                # open now validates digest, visibility and physical generation nonce.
                header = selected.open()
                if (
                    _builtin_int(self._catalog._view_epoch(), field="catalog.epoch")
                    != epoch
                ):
                    continue
                receipt = _indexes_view(
                    manager,
                    tables,
                    catalog=authority,
                    certified_headers={name.lower(): header},
                )
            return receipt.index(name)

    def rebuild_vector_index(self, space: str) -> VectorIndexView:
        """Re-derive one vector index from the heap, and report it only once it is healthy.

        Nothing repairs an index at open, and a stale proximity index is the dangerous
        kind: it answers from its own entries without consulting the heap, so it returns a
        plausible, confidently ordered top-k that silently omits rows. This is the door an
        operator asks for that repair through, without reaching past the public surface.

        The transaction is opened here and belongs to this call. A caller's transaction is
        not accepted and not reused: the pass stages a reset followed by one record per
        committed version, so sharing it would publish an operator's repair inside somebody
        else's commit, and would make the fixed target position depend on work this door
        never saw.

        A refusal before the commit barrier rolls back and leaves the index stale, which is
        the state that keeps readers honest. A failure that escapes after the barrier has
        already won is not the same thing: the durable outcome is recovered and verified
        first, and this door refuses to call an index ready rather than certify one it
        could not prove.
        """
        with self._public_operation("rebuild_vector_index"):
            self._require_open()
            self._require_writable("rebuild a vector index")
            return self._rebuild_vector_index_inside_guard(space)

    def _rebuild_vector_index_inside_guard(self, space: str) -> VectorIndexView:
        """Run the repair with the public facade section already entered.

        The guard is taken by the door above and held across everything here, including the
        window after the transaction has stopped participating -- clearing the stale mark,
        refreshing the cache an operator reads and proving the view. That window is the one
        that mattered: a close arriving in it would otherwise take components apart while this
        operation was still using them, and the caller would meet a half-dismantled database
        instead of either a result or a refusal.
        """
        # Resolution by space is the public one, so an unknown space, a name that is not a
        # vector index and a target this database never attached all refuse identically,
        # before a transaction exists to roll back.
        target = self.vectors.index(space)
        name = target.name
        manager = self._require_component(
            "indexes", self._indexes, "the index manager (C4)"
        )
        # The claim moves the durable generation, so it happens under the commit section with
        # the redo it would otherwise strand already retired, and the token it returns is what
        # the staging pass uses instead of claiming a second time. It happens BEFORE this
        # door's own transaction exists, because the retiring half is a checkpoint and a
        # checkpoint taken inside our own open write transaction is refused.
        claim_reason = f"Index {name!r} is being rebuilt."
        active_index = getattr(manager, "active_index", None)
        selected_index = (
            active_index(name, catalog=self._catalog._catalog)
            if callable(active_index)
            else manager.index(name)  # type: ignore[attr-defined]
        )
        claimed = self._transactions.checkpoint_and_claim_index_rebuild(
            selected_index,
            claim_reason,
        )
        # Everything from here to the barrier runs under one cleanup, because the claim above
        # has ALREADY made the index stale. A failure opening the transaction, resolving the
        # table or declaring interest would otherwise leave a stale index with no name in the
        # cache an operator reads, and possibly an open transaction nobody finishes.
        transaction: Transaction | None = None
        through = 0
        try:
            transaction = self.begin("write")
            # An index stages through `txn_id` and `stage_record`, and the public wrapper
            # deliberately exposes neither; the context behind it is the staging transaction.
            context = transaction._context
            through = _builtin_int(transaction.snapshot.read_lsn)
            # A transaction that stages durable work and claims no partition could never be
            # refused by optimistic validation, so a concurrent commit could replace what it
            # wrote. The page this pass rewrites is the index header that carries the stale
            # mark and the built-through position, and naming it is what makes two rebuilds of
            # one index conflict instead of silently overwriting each other. Concurrency
            # against the HEAP is not this declaration's job: the durable rebuild generation
            # already refuses a superseded reset, and later commits stage their own entries.
            context.note_write(page_partition(target.file, HEADER_PAGE_INDEX))
            # The header alone only fences rebuild against rebuild. The claim makes the index
            # durably stale BEFORE anything is staged, and a row written to the target table in
            # the window that follows advances the very generation this pass is rebuilding: the
            # RESET then reaches the log durably and refuses to apply, which leaves a committed
            # redo nobody can complete -- recovery_required, checkpoint refused, and a database
            # that will not reopen. Reading every partition of the target table turns that into
            # an ordinary optimistic refusal BEFORE the barrier, because a row write already
            # publishes its key partition. Only this table is fenced, so unrelated commits are
            # untouched.
            table_id = _builtin_int(
                selected_index.definition.table_id  # type: ignore[union-attr]
            )
            for partition in range(self._identity.partitions_per_table):
                context.note_read(partition_key(table_id, partition))
            manager.rebuild(  # type: ignore[attr-defined]
                name,
                context,
                through,
                rebuild_token=claimed,
                defer_clear=True,
            )
        except BaseException as preparation_failure:
            # Nothing reached the log. The claim left the index stale and that is exactly
            # what a reader must keep seeing -- in the cache an operator reads as well as
            # in the view -- and no transaction may be left open behind us.
            if transaction is not None:
                try:
                    transaction.rollback()
                except BaseException as cleanup:
                    _note_cleanup_failure(preparation_failure, cleanup)
            self._remember_stale_index(name, preparation_failure)
            raise
        try:
            transaction.commit()
        except BaseException as commit_failure:
            report = transaction.report
            if report is None or not report.durable:
                try:
                    transaction.rollback()
                except BaseException as cleanup:
                    _note_cleanup_failure(commit_failure, cleanup)
                self._remember_stale_index(name, commit_failure)
                raise
            self._settle_vector_rebuild_past_barrier(name, commit_failure)
        # Everything below happens while the index is STILL durably stale. The commit published
        # the rebuilt buckets and deliberately left the refusal standing, so nothing here has to
        # recreate a refusal after the fact -- which is what made every earlier version of this
        # door fragile. A failure at any point simply leaves the claim's own mark in place.
        try:
            prepared = self._prepared_rebuilt_vector_index(space, through, claim_reason)
        except BaseException as unproved:
            self._remember_stale_index(name, unproved)
            raise
        try:
            # The last mutation, and the only one that lifts the refusal. It revalidates the
            # same generation the claim published, so a claim that arrived in the meantime
            # makes this refuse rather than certify.
            self._transactions.checkpoint_and_clear_index_rebuild(
                manager, name, through, claimed
            )
        except BaseException as unproved:
            self._remember_stale_index(name, unproved)
            raise
        try:
            self._forget_stale_index(name)
        except Exception:  # noqa: BLE001 - the clear already landed on the device
            # The cache is bookkeeping and the clear is done. Reporting a refusal for an index
            # that is durably healthy would be the fail-open one door over; the next refresh
            # corrects the name.
            pass
        # Nothing fallible runs after the clear: the answer was assembled before it, and the
        # three fields the clear decides are exactly the ones it just wrote.
        return prepared

    def _settle_vector_rebuild_past_barrier(
        self, name: str, failure: BaseException
    ) -> None:
        """Settle a durable-but-unreported rebuild, and refuse to certify it from here.

        The commit barrier won before the failure escaped, so the rebuild is probably on
        disk -- probably is not a word this door may answer with. Recovery settles the log
        and verification walks the index against the heap, because leaving an ambiguous
        outcome unsettled is worse than either verdict.

        Readiness is still not claimed. The frozen contract wants a COLD proof before an
        ambiguous outcome may be called ready, and a walk on the handle that just failed is
        not one: it shares the caches, the pool and the process whose outcome is in doubt.
        There is no safe cold reopen inside this door, so the smallest provable behaviour is
        the fail-closed one -- settle what can be settled, keep the index visibly stale, and
        re-raise the original failure. Certification is left to a reopen and to status.
        """
        try:
            self._recover_in_transition()
            report = self.verify("all")
            findings = tuple(getattr(report, "findings", ()))
            if findings:
                _note_cleanup_failure(
                    failure,
                    GrafxIndexError(
                        f"Verification after the barrier reported {len(findings)} finding(s).",
                        field="rebuild_unproved",
                        index=name,
                    ),
                )
        except BaseException as settle_failure:
            _note_cleanup_failure(failure, settle_failure)
        finally:
            # No refusal has to be published here. The commit deferred the clear, so the mark
            # the claim made is still standing and is already the durable, cold, fail-closed
            # authority. All that is left is to make the cache an operator reads agree with it.
            self._remember_stale_index(name, failure)
        raise failure

    def _prepared_rebuilt_vector_index(
        self, space: str, through: int, claim_reason: str
    ) -> VectorIndexView:
        """Assemble the answer while the index is still refusing, so nothing follows the clear.

        Every read here happens under the claim's own stale mark, which is what makes the
        preparation safe to fail: the refusal it would have had to invent is already standing.
        The three fields the clear decides -- the mark, its reason and the position -- are the
        only ones this differs from what was read, and they are stated rather than re-read
        because re-reading them would be fallible work after the last mutation.
        """
        observed = self._vectors_view_now().index(space)
        if observed.stale and observed.stale_reason != claim_reason:
            # Stale is expected here -- the claim put it there and the clear has not run. What
            # is NOT expected is stale for some OTHER reason: that means the mark this door is
            # about to lift is not the one it made, so lifting it would clear somebody else's
            # refusal and certify a rebuild that never finished.
            raise GrafxIndexError(
                f"Index {observed.name!r} is still stale after its rebuild committed.",
                field="rebuild_incomplete",
                index=observed.name,
                stale_reason=observed.stale_reason,
            )
        if observed.built_through_lsn is None:
            # The position is read from page zero and is absent when that page is not resident.
            # Absent is not "fine": it is the absence of the one number that says which
            # generation this call is about to certify, and it is checked before anything else
            # because a view that cannot show its position cannot support any later judgement.
            raise GrafxIndexError(
                f"Index {observed.name!r} does not report the position it was built through, "
                "so the generation this rebuild completed cannot be proved.",
                field="rebuild_position_unproved",
                index=observed.name,
                target=through,
            )
        if not observed.stale:
            raise GrafxIndexError(
                f"Index {observed.name!r} lifted its own refusal before this door proved the "
                "rebuild; the generation it now serves is not the one that was claimed.",
                field="rebuild_refusal_lost",
                index=observed.name,
            )
        view = VectorIndexView(
            observed.name,
            observed.file,
            observed.space_id,
            observed.space_name,
            observed.dimension,
            observed.metric_of_space,
            observed.storage_dtype,
            observed.ef_search,
            False,
            None,
            through,
        )
        built_through = observed.built_through_lsn
        if built_through is None:
            # The position is read from page zero and is absent when that page is not resident.
            # Absent is not "fine": it is the absence of the one number that would prove which
            # generation this call completed, and a door that returned the view anyway would be
            # certifying a rebuild it could not read the receipt for.
            raise GrafxIndexError(
                f"Index {view.name!r} does not report the position it was built through, so "
                "the generation this rebuild completed cannot be proved.",
                field="rebuild_position_unproved",
                index=view.name,
                target=through,
            )
        # The position this rebuild reaches is NOT compared here. While the refusal still
        # stands the header carries the old position by design, and the target is proved where
        # it is enforced: the clear consumes an authority bound to exactly this position and
        # refuses when it does not match. Comparing again here would only test the header the
        # clear has not written yet.
        return view

    def _remember_stale_index(self, name: str, primary: BaseException) -> None:
        """Publish one stale name in the cache without disturbing the others.

        The rebuild pass makes the generation durably stale before it stages anything, so
        every failure after that point leaves an index the view already refuses. The cache
        an operator reads through ``maintenance.status()`` has to agree with that view; a
        rebuild that failed and left the name out would invite a caller to trust an index
        the engine will not answer from.

        A failure to refresh is diagnostic and must never replace the failure that caused
        the unwind -- that failure is the one the caller has to act on.
        """
        try:
            current = self._stale_indexes
            if name in current:
                return
            self._stale_indexes = (*current, name)
        except BaseException as refresh_failure:  # pragma: no cover - defensive
            _note_cleanup_failure(primary, refresh_failure)

    def _forget_stale_index(self, name: str) -> None:
        """Drop one name from the cache :attr:`stale_indexes` publishes, keeping the rest.

        The cache is what an operator reads to decide what still needs repairing, so a
        rebuild that left its own name in it would invite the same repair forever.
        """
        current = self._stale_indexes
        if name not in current:
            return
        self._stale_indexes = tuple(item for item in current if item != name)

    def recover(self) -> RecoveryReport:
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
        with self._public_transition():
            return self._recover_in_transition()

    def _recover_in_transition(self) -> RecoveryReport:
        """Run recovery while one facade outcome keeps callback-requested close resumable."""
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
            with self._transactions._close_wait_hazard():
                report = manager.run()  # type: ignore[attr-defined]
            self._transactions.recovery_completed()
        public_report = _recovery_report_view(report)
        if public_report is None:
            raise GrafxConfigurationError(
                "Recovery did not return its required report.",
                field="recovery.report",
                value=None,
            )
        self._recovery_report = public_report
        indexes = self._indexes
        if indexes is not None:
            # Reacquire after recovery_section released. The manager's inventory door adds the
            # cross-process commit section: page_access_section alone protects this participant
            # but cannot stop a foreign commit from moving an index header between the published
            # reading and the freshness certificate.
            with self._transactions.page_access_section():
                registered, stale = self._transactions.refresh_index_inventory()
                self._attached_indexes = tuple(
                    _builtin_text(index.name, field="attached_index", empty=False)
                    for index in registered
                )
                self._stale_indexes = tuple(
                    _builtin_text(index.name, field="stale_index", empty=False)
                    for index in stale
                )
        return public_report

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
            return _builtin_int(self._pool.flush(), field="flush.count")

    def checkpoint(self) -> RecycleReport:
        """Put the committed state on the platter, publish the checkpoint, and reclaim the log (BR-10).

        Returns the recycling report: what the log released, what it kept back for a pinned
        reader, and what the platform deferred. A read-only database refuses -- a checkpoint
        publishes a control record and may release log segments, and neither is a reader's to do.

        Every acknowledged commit is durable in the log whether or not a checkpoint runs, so a
        failed automatic attempt costs space and recovery time, never committed data. Writable
        databases also call this door after a commit whose published WAL distance reaches the
        configured ``checkpoint_interval_records`` or whose live log reaches the optional soft
        ``wal_max_bytes`` high-water; callers may still invoke it explicitly. A reader pin,
        atomic batch or platform-deferred recycle may retain more bytes without authorizing an
        unsafe truncation.
        """
        with self._public_operation("checkpoint"):
            self._require_open()
            self._require_writable("checkpoint the database")
            # The manager's checkpoint section is re-entrant. This outer section deliberately
            # spans its operation and the facade inventory postlude so deferred metrics
            # callbacks cannot close lifecycle state between those two halves.
            with self._transactions.page_access_section():
                report = self._transactions.checkpoint()
                indexes = self._indexes
                if indexes is not None:
                    # Checkpoint redo may have adopted schema and indexes committed by another
                    # participant after this handle opened. TransactionManager refreshed their
                    # freshness inside the same cross-process commit section as the checkpoint;
                    # only copy that stable local result into the public inventory here.
                    active_indexes = getattr(indexes, "active_indexes", None)
                    registered = (
                        active_indexes(catalog=self._catalog._catalog)
                        if callable(active_indexes)
                        else indexes.indexes()  # type: ignore[attr-defined]
                    )
                    self._attached_indexes = tuple(
                        _builtin_text(index.name, field="attached_index", empty=False)
                        for index in registered
                    )
                    self._stale_indexes = tuple(
                        _builtin_text(index.name, field="stale_index", empty=False)
                        for index in registered
                        if index.stale
                    )
            return _recycle_report_view(report)

    def _maybe_checkpoint(self) -> None:
        """Attempt due maintenance without changing an already-durable commit outcome.

        The participant section makes the due check and checkpoint one local single-flight
        operation. ``_checkpoint_retry_pending`` remains set until the manager checkpoint AND
        the facade's index-refresh postlude both finish: checkpoint state is published before
        WAL recycling and before that postlude, so the numeric distance alone cannot detect a
        late failure on the next commit. The byte threshold is edge-triggered: if safe recycling
        leaves the log above it, the latch prevents a futile checkpoint on every later commit and
        rearms only after the live WAL falls below the configured level.
        """
        if self._closed or self._read_only:
            return
        checkpoint_failure: Exception | None = None
        published_lsn = 0
        checkpoint_lsn = 0
        bytes_due = False
        owns_attempt = False
        try:
            try:
                with self._transactions._participant_section():
                    if self._closed or self._checkpointing:
                        return
                    self._checkpointing = True
                    owns_attempt = True
                    state = self._transactions._published_state_in_section()
                    published_lsn = _builtin_int(
                        state.last_committed_lsn, field="checkpoint.published_lsn"
                    )
                    checkpoint_lsn = _builtin_int(
                        state.checkpoint_lsn, field="checkpoint.checkpoint_lsn"
                    )
                    distance = published_lsn - checkpoint_lsn
                    records_due = distance >= self._checkpoint_interval_records
                    need_bytes = self._wal_max_bytes is not None and (
                        self._wal_bytes_latched
                        or not (records_due or self._checkpoint_retry_pending)
                    )
                    if need_bytes and self._wal_max_bytes is not None:
                        wal_bytes = self._wal.total_bytes()
                        if wal_bytes < self._wal_max_bytes:
                            self._wal_bytes_latched = False
                        elif not self._wal_bytes_latched:
                            bytes_due = True
                    if (
                        not self._checkpoint_retry_pending
                        and not records_due
                        and not bytes_due
                    ):
                        return
                    if bytes_due:
                        # A reader pin or a platform-deferred recycle can leave the WAL above
                        # the threshold after a successful checkpoint. Treat bytes as an edge,
                        # not a level, so every later commit does not repeat maintenance that
                        # cannot yet reclaim anything. Falling below the threshold rearms it.
                        self._wal_bytes_latched = True
                    self._checkpoint_retry_pending = True
                    try:
                        self.checkpoint()
                    except Exception as failure:  # noqa: BLE001 - maintenance cannot undo commit
                        checkpoint_failure = failure
                    else:
                        self._checkpoint_retry_pending = False
            except Exception as failure:  # noqa: BLE001 - includes participant/state adapters
                self._checkpoint_retry_pending = True
                checkpoint_failure = failure

            if checkpoint_failure is not None:
                code = "foreign_error"
                retryable = False
                try:
                    if isinstance(checkpoint_failure, GrafxError):
                        code = _builtin_text(
                            checkpoint_failure.code,
                            field="checkpoint.failure_code",
                            empty=False,
                        )
                        retryable = _builtin_bool(checkpoint_failure.retryable)
                except Exception:  # noqa: BLE001 - hostile diagnostics are still diagnostics
                    code = "foreign_error"
                    retryable = False
                try:
                    self._events.emit(
                        "checkpoint.auto_failed",
                        {
                            "code": code,
                            "retryable": retryable,
                            "published_lsn": published_lsn,
                            "checkpoint_lsn": checkpoint_lsn,
                            "interval_records": self._checkpoint_interval_records,
                        },
                    )
                except Exception:  # noqa: BLE001 - observability never changes commit truth
                    pass
        finally:
            if owns_attempt:
                self._checkpointing = False

    def snapshot_metrics(self) -> MetricsSnapshotView:
        """Return the machine-readable current value of every metric this database emitted."""
        with self._public_operation("snapshot_metrics"):
            self._require_open()
            return _metrics_snapshot_view(self._metrics.snapshot())

    def publish_metrics(self) -> None:
        """Publish a configured metrics document without exposing its mutable sink.

        Sinks without an explicit publication door have nothing to do.  A JSON sink writes the
        configured destination; the same action runs automatically during :meth:`close`.
        """
        self._require_open()
        self._publish_metrics()

    def read_index_status(self, name: str) -> IndexView:
        """Read an active index's validated durable header into an immutable DTO.

        Unlike ``indexes``, this explicit I/O operation faults in a cold header.
        It validates catalog/physical generation identity, but does not rebuild,
        clear staleness, certify heap coverage, or advance a watermark.
        """
        with self._public_operation("read_index_status"):
            self._require_open()
            return self._committed_index_receipt(_require_text("index", name))

    def inspect_index(self, name: str) -> tuple[IndexEntry, ...]:
        """Return immutable entry DTOs from one secondary index.

        This is an explicit potentially expensive read.  The index store itself stays private,
        so callers cannot mark it stale, advance its header or stage changes outside a
        transaction.
        """
        with self._public_operation("inspect_index"):
            self._require_open()
            wanted = _require_text("index", name)
            indexes = self._require_component(
                "indexes", self._indexes, "the index framework (C7)"
            )
            with self._transactions.page_access_section():
                active_index = getattr(indexes, "active_index", None)
                index = (
                    active_index(wanted, catalog=self._catalog._catalog)
                    if callable(active_index)
                    else indexes.index(wanted)  # type: ignore[attr-defined]
                )
                return tuple(
                    _index_entry_view(entry)
                    for entry in index.walk()  # type: ignore[attr-defined]
                )

    def read_quarantine(self, name: str) -> bytes:
        """Return and checksum-verify the immutable bytes of one quarantine entry."""
        with self._public_transition():
            self._require_open()
            wanted = _require_text("quarantine entry", name)
            quarantine = self._require_component(
                "quarantine", self._quarantine, "quarantine (C6)"
            )
            return _builtin_bytes(
                quarantine.read(wanted),  # type: ignore[attr-defined]
                field="quarantine.payload",
            )

    def quarantine_receipts(self, name: str) -> tuple[str, ...]:
        """Return immutable restore-receipt names without exposing the quarantine store."""
        with self._public_transition():
            self._require_open()
            wanted = _require_text("quarantine entry", name)
            quarantine = self._require_component(
                "quarantine", self._quarantine, "quarantine (C6)"
            )
            observed = quarantine.receipts(wanted)  # type: ignore[attr-defined]
            return tuple(
                _builtin_text(receipt, field="quarantine.receipt", empty=False)
                for receipt in _tuple_items(observed, field="quarantine.receipts")
            )

    # --- lifecycle ----------------------------------------------------------------------------

    def close(self) -> None:
        """Release owned resources under this database's checksum selection (FR-1).

        Abort open transactions before releasing lower dependencies. If transaction/schema
        cleanup cannot complete, preserve the safe leak for a later retry. Closing twice after
        successful release is a no-op; concurrent/reentrant close may be terminal but incomplete
        until ``close_complete`` becomes true. An unobserved terminal release failure is raised
        once on the next explicit close without repeating resource release.
        """
        with self._checksum_scope():
            self._close_in_checksum_scope()

    def _close_in_checksum_scope(self) -> None:
        """Release everything this database opened, and never corrupt anything doing it (FR-1).

        Transaction close and every tracked QueryEngine schema journal must first prove complete.
        If either cannot, lower dependencies stay open and a later call retries that safe leak.
        Once quiescent, every release step runs even when an earlier one failed, and exactly one
        caller owns those steps even when close calls race or host callbacks re-enter.

        Closing twice after success is a no-op. A terminal dependency-release failure swallowed
        by automatic cleanup is re-raised unchanged to the next explicit caller without running
        any release twice. Closing with a
        transaction open aborts it: nothing of an open transaction has reached the device, so
        abandoning it is the whole of that promise. A concurrent/reentrant caller may return
        terminal but incomplete; :attr:`close_complete` distinguishes that safe intermediate
        state from completed lower-layer release.
        """
        if self._close_released:
            pending_failure = self._claim_unobserved_release_failure()
            if pending_failure is not None:
                raise pending_failure
            return
        # Marked closed BEFORE anything is released. A release path calls host-supplied code --
        # an event sink, a metrics publisher, a storage device -- and any of it may re-enter this
        # method; a flag set at the end would let the second entry release everything a second
        # time (A91). There is no lock here to make re-entry safe by exclusion, deliberately:
        # a lock held across foreign code is the defect A91 names.
        self._closed = True
        self._transactions.request_close()
        if (
            self._facade_transition_reentrant()
            or self._transactions.transition_active
            or self._close_releasing
            or self._close_wait_hazard_active()
            or self._page_access_active()
        ):
            # A callback re-entered its own facade/manager transition, this close's host release
            # phase, a narrow foreign invocation under the participant section, or page-access
            # host code is active on some thread. The terminal request is enough here;
            # transition-finally resumes after settlement. In particular, do not wait for a
            # participant held by host Clock/Coordinator/VectorMath code waiting for this call.
            return

        failures: list[BaseException] = []
        try:
            self._close_transactions()
        except BaseException as failure:
            failures.append(failure)

        journals_pending = bool(self._public_contexts)
        if not self._transactions.close_complete or journals_pending:
            # A participant-section pre-enter failure, schema-unwind failure, or same-context
            # transition means a winner may still touch pool/storage. Never release under it.
            if failures:
                if self._close_failure is None:
                    self._close_failure = failures[0]
                raise failures[0]
            return
        if self._facade_transition_active():
            # Another wrapper may still be publishing its outcome after manager quiescence. It
            # cannot start new work after the terminal seal; its transition-finally retries close
            # once the process-wide facade counter reaches zero.
            if failures:
                if self._close_failure is None:
                    self._close_failure = failures[0]
                raise failures[0]
            return

        # Elect exactly one lower-layer releaser under the manager's quiescent participant
        # section, then leave that section before any host-owned flush/publish/closer callback.
        if self._close_released or self._close_releasing:
            return
        try:
            with self._transactions.database_release_section():
                if self._close_released or self._close_releasing:
                    return
                # Recheck journals beside the release claim. A late rollback already drained by
                # close is absent-id and cannot re-register; a statement that crossed the seal
                # would have had to own this same participant section before manager close.
                if self._public_contexts:
                    return
                self._close_releasing = True
        except BaseException as failure:
            failures.append(failure)
            if self._close_failure is None:
                self._close_failure = failures[0]
            raise failures[0]

        for step in (self._flush_pages, self._publish_metrics, self._release_closers):
            try:
                step()
            except BaseException as failure:
                failures.append(failure)
        self._close_released = True
        self._close_releasing = False
        if failures:
            if self._close_failure is None:
                self._close_failure = failures[0]
            if self._release_failure is None:
                self._release_failure = failures[0]
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
        except BaseException as failure:
            self._retain_unobserved_release_failure(failure)
            return

    def _close_transactions(self) -> None:
        """Abort contexts, withdraw pins and finish every public schema journal.

        TransactionManager owns the terminal latch and reaches quiescence first. Schema unwind
        is a distinct QueryEngine responsibility, however: rollback can already have released
        its reader pin when it starts settling speculative registry and vector-map claims.
        Keeping every adopted context until that second phase succeeds lets close wait for an
        in-flight unwind or do the unwind itself, before the pool and storage closers run.
        """
        failure: BaseException | None = None
        try:
            self._transactions.close()
        except BaseException as close_failure:
            failure = close_failure

        # A failed pre-enter means manager close has not proved quiescence. Touching QueryEngine
        # journals (which inspect durable index identity and mutate process-local ownership)
        # would race the still-active winner just as surely as flushing the pool, so leave every
        # journal tracked for the retrying close.
        if self._transactions.close_complete and self._public_contexts:
            try:
                with self._transactions._participant_section():
                    contexts = tuple(self._public_contexts.values())
                    for context in contexts:
                        try:
                            self._settle_schema_in_section(
                                context,
                                committed=context.state is TransactionState.COMMITTED,
                            )
                        except BaseException as settlement_failure:
                            if failure is None:
                                failure = settlement_failure
                            else:
                                _note_cleanup_failure(failure, settlement_failure)
            except BaseException as snapshot_failure:
                if failure is None:
                    failure = snapshot_failure
                else:
                    _note_cleanup_failure(failure, snapshot_failure)
        if failure is not None:
            raise failure

    def _settle_schema(self, context: TransactionContext, *, committed: bool) -> None:
        """Tell the query engine one transaction's schema bookkeeping is over.

        On a rollback this is what takes back the two side effects a DDL statement makes outside
        the transaction -- the indexes it registered and the vector engine's space map -- and
        drops the working catalog copy. It shares the participant section with close so storage
        cannot be released between manager rollback and the durable-identity proof that releases
        process-local ownership. The context remains tracked if a foreign failure interrupts
        settlement, allowing close to retry the cleanup instead of losing it.
        """
        # A wrapper can return from manager commit/rollback after a racing close has already
        # drained this exact context and released its resources. The absent-id fast path is what
        # makes that late settlement a true no-op without even touching the coordinator.
        if self._public_contexts.get(context.txn_id) is not context or self._closed:
            return
        with self._transactions._participant_section():
            if self._closed:
                # Close owns the drain once its terminal flag is visible.
                return
            self._settle_schema_in_section(context, committed=committed)

    def _settle_schema_in_section(
        self, context: TransactionContext, *, committed: bool
    ) -> None:
        """Settle one still-tracked query context while the participant section is held."""
        if self._public_contexts.get(context.txn_id) is not context:
            return
        queries = self._queries
        settle = getattr(queries, "settle_schema", None)
        if callable(settle):
            # QueryEngine schema unwind reaches index storage and VectorEngine event callbacks.
            # Mark only that foreign-capable invocation, never the surrounding settlement body.
            with self._transactions._close_wait_hazard():
                settle(context.txn_id, committed=committed)
        self._public_contexts.pop(context.txn_id, None)

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

    @contextmanager
    def _public_operation(self, operation: str) -> Iterator[None]:
        """Contain an ordinary collaborator failure at one explicit facade operation.

        Grafx failures already carry the stable public taxonomy and therefore retain their
        identity. ``Exception`` deliberately excludes ``KeyboardInterrupt``, ``SystemExit`` and
        other process-control signals; those are not adapter failures and pass unchanged.
        """
        try:
            with self._public_transition():
                yield
        except GrafxError:
            raise
        except Exception as failure:  # noqa: BLE001 - translated at the public boundary
            raise _public_operation_failure(operation, failure) from failure

    @contextmanager
    def _public_transition(self) -> Iterator[None]:
        """Defer dependency release until one wrapper outcome and schema settlement finish.

        The contained metrics adapter owns the process-local transition mechanism required by
        G2; the pure facade only consumes its injected context. A clock or telemetry callback can
        publish terminal state immediately, while lower dependency release waits for its wrapper
        transition to leave. The null context supports direct internal construction without
        claiming callback-reentrancy guarantees that only the standard composition supplies.
        """
        transition = getattr(self._metrics, "transition", None)
        boundary = transition() if callable(transition) else nullcontext()
        try:
            with self._checksum_scope(), boundary:
                yield
        finally:
            if self._closed and not self._close_released:
                try:
                    self.close()
                except BaseException as failure:
                    # A close requested by host telemetry/clock resumes only after transaction
                    # and schema outcome settlement. It cannot replace a durable commit result.
                    if self._close_failure is None:
                        self._close_failure = failure
                    self._retain_unobserved_release_failure(failure)

    def _facade_transition_active(self) -> bool:
        """Read the contained adapter's host-free cross-thread settlement capability."""
        try:
            return bool(getattr(self._metrics, "facade_transition_active", False))
        except BaseException:  # noqa: BLE001 - telemetry cannot control dependency release
            return False

    def _facade_transition_reentrant(self) -> bool:
        """Read whether this execution context re-entered its own facade transition."""
        try:
            return bool(getattr(self._metrics, "transition_active", False))
        except BaseException:  # noqa: BLE001 - telemetry cannot control dependency release
            return False

    def _page_access_active(self) -> bool:
        """Read the contained adapter's host-free cross-thread page-access capability."""
        try:
            return bool(getattr(self._metrics, "page_access_active", False))
        except BaseException:  # noqa: BLE001 - telemetry cannot control dependency release
            return False

    def _close_wait_hazard_active(self) -> bool:
        """Read the contained adapter's narrow cross-thread foreign-call capability."""
        try:
            return bool(getattr(self._metrics, "close_wait_hazard_active", False))
        except BaseException:  # noqa: BLE001 - telemetry cannot control dependency release
            return False

    def _retain_unobserved_release_failure(self, failure: BaseException) -> None:
        """Make a swallowed terminal release failure visible to one explicit claimant.

        Standard composition delegates the identity test, pending mark and later claim to one
        lock in ContainedMetricsSink. The bool is only a compatibility fallback for a directly
        assembled facade and does not claim cross-thread exactly-once semantics.
        """
        retain = getattr(self._metrics, "retain_release_failure", None)
        if callable(retain):
            try:
                if retain(failure, self._release_failure) is True:
                    return
            except BaseException:  # noqa: BLE001 - raw telemetry cannot hide close evidence
                pass
        if failure is self._release_failure:
            self._release_failure_pending = True

    def _claim_unobserved_release_failure(self) -> BaseException | None:
        """Claim an automatically swallowed release failure at most once when supported."""
        expected = self._release_failure
        if expected is None:
            return None
        claim = getattr(self._metrics, "claim_release_failure", None)
        if callable(claim):
            try:
                claimed = claim(expected)
            except BaseException:  # noqa: BLE001 - use the direct-composition fallback below
                pass
            else:
                return expected if claimed is expected else None
        if self._release_failure_pending:
            self._release_failure_pending = False
            return expected
        return None

    def _public_transaction(self, context: TransactionContext) -> Transaction:
        """Wrap a manager context only if it still belongs to an open public facade.

        Manager begin/retry may finish just as Database.close publishes its terminal flag. A
        context that won before that flag is a valid pre-close result; one observed after close
        is refused without entering coordination again, and manager.close owns its retirement.
        """
        self._require_open()
        if not context.active:
            raise GrafxTransactionStateError(
                f"Transaction {context.txn_id} is {context.state.value} and cannot be handed "
                "to a caller.",
                txn_id=context.txn_id,
                state=context.state.value,
            )
        return Transaction(self, context)

    def _catalog_snapshot(self) -> tuple[CatalogStoreView, int]:
        """Read one whole catalog generation without executing host code under a section.

        A DDL commit applies several catalog pages while holding the participant section.  Page
        reads and their storage/codec/metrics ports cannot safely run under that section, so the
        observation is optimistic: copy the already-current in-memory catalog or read a fresh
        value outside, then accept it only when the derived epoch stayed unchanged and still
        matches after acquiring the participant section.  A read failure is validated the same
        way: if pages moved while it was raised, retry instead of publishing transient partial
        DDL as corruption.
        """
        store = self._catalog
        while True:
            before = _builtin_int(store._view_epoch(), field="catalog.epoch")
            observed: object | None = None
            snapshot: CatalogStoreView | None = None
            reused = False
            failure: BaseException | None = None
            try:
                if (
                    _builtin_int(store._loaded_epoch, field="catalog.loaded_epoch")
                    == before
                ):
                    observed = store._catalog
                else:
                    observed = store.read_from_pages()
                # CQ-4/QW-6: reuse the immutable view while the generation is provably the one
                # it was built from -- same persisted image bytes, the very same live object,
                # and the very same table/space content (compared against shallow copies taken
                # when the memo was filled, so a live catalog mutated in place with unchanged
                # bytes/identity is never answered from memory). The content comparison reads
                # the slots directly and dispatches through no public Catalog method, because
                # this is the same boundary _catalog_view_from defends: a hostile subclass's
                # readers are callback doors and must not run here. The epoch proof below still
                # runs on every access; only the reconstruction is skipped.
                memo = self._catalog_view_memo
                if (
                    memo is not None
                    and observed is store._catalog
                    and memo[1] == id(observed)
                    and memo[0] == store._persisted_image
                    and memo[2] == observed._tables_by_id
                    and memo[3] == observed._spaces_by_id
                ):
                    snapshot = memo[4]
                    reused = True
                else:
                    snapshot = _catalog_view_from(store, observed)
            except BaseException as caught:  # noqa: BLE001 - preserve the original taxonomy
                failure = caught
            after = _builtin_int(store._view_epoch(), field="catalog.epoch")
            with self._transactions._participant_section():
                current = _builtin_int(store._view_epoch(), field="catalog.epoch")
                if before != after or after != current:
                    continue
                if failure is not None:
                    raise failure
                if (
                    snapshot is None or observed is None
                ):  # pragma: no cover - total above
                    raise AssertionError(
                        "a successful catalog observation produced no value"
                    )
                if (
                    _builtin_int(store._loaded_epoch, field="catalog.loaded_epoch")
                    != current
                ):
                    # Pure in-memory publication after the epoch proof.  It prevents every later
                    # view/query from paying for the same refresh and runs no adapter callback.
                    store.adopt(observed)
                if not reused and store._catalog is observed:
                    # Memoize only after the epoch proof, and only a view of the ADOPTED
                    # generation: an observation the store never adopted (a torn read the proof
                    # rejected never reaches here; a foreign image the store does not hold yet
                    # must not pre-bless later reads) stays unmemoized. The shallow dict copies
                    # freeze the content the view was built from; any in-place mutation of the
                    # live catalog afterwards makes the comparison above miss.
                    self._catalog_view_memo = (
                        store._persisted_image,
                        id(observed),
                        dict(observed._tables_by_id),
                        dict(observed._spaces_by_id),
                        snapshot,
                    )
                return snapshot, current

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
