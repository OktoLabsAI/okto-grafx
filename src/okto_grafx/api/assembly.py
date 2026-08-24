"""Engine assembly: turning a complete port registry into an open Database (C11, A13).

This module is the second half of the composition root. :mod:`okto_grafx.runtime.bootstrap` owns
the adapter table -- which concrete adapter fills which port slot -- and this module owns what is
built on top of them: the page cache, the identity page, the catalog, the heap, the log, the
ledger, quarantine, recovery, the index registry, the vector engine and the transaction manager,
in the one order that makes each of them true when the next one is built.

**The open sequence is an order, not a list.** Every step below depends on the ones before it and
is depended on by the ones after it:

1. the page cache, because every store reads and writes through it;
2. the identity page, because SPEC-M1 FR-1 says a database has an identity before it has content,
   and because opening a database at a different page size than it was created with must be
   refused before a single page is decoded against the wrong layout;
3. the log, the ledger and quarantine, because recovery needs all three;
4. **recovery, before any transaction can be opened** (FR-1, FR-8) -- the whole point of running
   it at open is that nothing observes the database until the log has been believed or truncated;
5. the catalog and the heap, whose bootstrap must not run before recovery has replayed the pages
   that decide what they hold;
6. the index registry, the vector engine and the transaction manager, which serve callers.

**Anything opened is registered before the next step can fail.** The assembly runs inside one
guard: a failure at any step releases what has already been opened, in reverse, and re-raises the
original failure untouched (A47 -- the class and ``details["retryable"]`` of a device failure are
what a caller retries on, so nothing here re-dresses one).

**Ownership decides what a close releases.** Adapters this composition built are closed with the
database; adapters a caller bound into its own registry are left alone, because the caller may be
using them for something else.
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from collections.abc import Callable
from typing import TypeVar, cast

from okto_grafx.domain.errors import (
    GrafxUnsupportedOperation,
    GrafxIndexError,
    GrafxConfigurationError,
    GrafxError,
    GrafxSchemaVersionMismatch,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.codec import PageCodec
from okto_grafx.domain.ports.coordination import ProcessCoordinator
from okto_grafx.domain.ports.events import EventSink
from okto_grafx.domain.ports.metrics import MetricsSink
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.ports.vectormath import VectorMath
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import MINIMUM_FRAMES as CATALOG_FRAMES
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.database import META_FILE, Database, DatabaseIdentity, MetaStore
from okto_grafx.engine.heap_store import MINIMUM_FRAMES as HEAP_FRAMES
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.adapters.metrics_contained import ContainedMetricsSink
from okto_grafx.engine.index_manager import (
    IndexManager,
    edge_from_index_name,
    edge_to_index_name,
    primary_key_index,
    primary_key_index_name,
    relationship_endpoint_indexes,
)
from okto_grafx.engine.ledger_store import LedgerStore
from okto_grafx.engine.metrics_catalog import register_catalog
from okto_grafx.engine.quarantine import QuarantineStore
from okto_grafx.engine.recovery_manager import RecoveryManager
from okto_grafx.engine.query_engine import QueryEngine
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.verifier import Verifier
from okto_grafx.engine.vector_engine import VectorEngine
from okto_grafx.engine.wal_manager import WalManager
from okto_grafx.runtime.config import MEMORY_PATH, DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry

__all__ = [
    "LABEL_DIGEST_BYTES",
    "LABEL_PREFIX",
    "MINIMUM_STORE_FRAMES",
    "WAL_DIRECTORY",
    "assemble_database",
    "database_label",
    "new_database_uuid",
]

WAL_DIRECTORY: str = "wal"
"""Where the log segments of a database live (CONTRACT.md section 6.1)."""

LABEL_PREFIX: str = "db"
"""What every database metric label starts with, so a label never begins with a digit."""

LABEL_DIGEST_BYTES: int = 8
"""Bytes of digest behind a database label: sixteen hex characters, well inside the bound."""

MINIMUM_STORE_FRAMES: int = max(CATALOG_FRAMES, HEAP_FRAMES)
"""Frames the stores of one database need at once, taken from the stores themselves (A24).

The catalog and the heap each declare their own floor; the composition root needs the larger of
the two and must never re-state either number, because a second copy of a bound is how two
validators that agree today stop agreeing tomorrow.
"""

_Port = TypeVar("_Port")
"""The protocol a port slot is read back as."""


def database_label(path: str) -> str:
    """Return the bounded metric label for a database at this path (TR-7, amendment A79).

    A path may not appear in a label value: a metrics endpoint is scraped by third parties, and a
    path discloses the layout of a deployment to every one of them. A digest of the path is
    stable across the life of a database, distinguishes two databases in one process, and is a
    short hash rather than free text -- which is exactly what CONTRACT.md section 9 says the
    ``db`` label carries.
    """
    # os.fsencode, never str.encode: a path that came back from os.listdir, os.fsdecode or
    # sys.argv can hold an unpaired surrogate -- a non-UTF-8 name on POSIX, an unpaired
    # UTF-16 unit on Windows -- and str.encode('utf-8') raises a bare UnicodeEncodeError for
    # it. That is a non-Grafx exception out of the front door of the package (section 2, DoD
    # item 5), reached by the ordinary bytes-to-str round trip rather than by a hostile
    # input. fsencode is the exact inverse of the decode that produced the string on both
    # families, so it never raises for a name the file system itself yielded, and it keeps
    # the digest injective.
    digest = hashlib.blake2s(os.fsencode(path), digest_size=LABEL_DIGEST_BYTES).hexdigest()
    return f"{LABEL_PREFIX}{digest}"


def new_database_uuid() -> bytes:
    """Return the sixteen identity bytes of a database that is being created (FR-1).

    ``uuid`` lives here rather than in the engine because randomness and wall-clock reads are
    mechanism (G2b, A5): the pure core is handed the bytes and only ever stores and compares them.
    """
    return uuid.uuid4().bytes


def assemble_database(
    config: DatabaseConfig, ports: PortRegistry, *, owns_ports: bool = False
) -> Database:
    """Build every engine of one database over a complete registry and return it open.

    ``owns_ports`` says whether this composition built the adapters. When it did, closing the
    database closes them; when the caller bound its own registry, they are left open.
    """
    storage = _port(ports, "storage", StorageDevice)
    clock = _port(ports, "clock", Clock)
    codec = _port(ports, "codec", PageCodec)
    # The shell, not the sink. A host-supplied sink may do anything at all, and the engine
    # calls it from inside public doors -- including after a commit's invariants have settled,
    # where a raise made a DURABLY COMMITTED transaction report failure and the caller's retry
    # duplicated the row. Same rule as the connect guard below: only Grafx types leave a public
    # door. The raw sink stays reachable through .inner for the doors that own its lifecycle.
    metrics = ContainedMetricsSink(_port(ports, "metrics", MetricsSink))
    events = _port(ports, "events", EventSink)
    vector_math = _port(ports, "vector_math", VectorMath)
    coordinator = _port(ports, "coordinator", ProcessCoordinator)

    # Acquisition order. Everything opened is registered here BEFORE the next step can fail, and
    # released in reverse -- by the guard below when the open fails, and by Database.close when it
    # succeeds. There is exactly one release mechanism per window, so each is observable on its
    # own (A67): a factory failure is released by build_default_registry, an assembly failure by
    # this guard, and an ordinary close by the database.
    closers: list[Callable[[], None]] = []
    endpoint: str | None = None
    try:
        if owns_ports:
            closers.append(_closer_for(storage))
        # G7 and A51: registration is the authority, and a sink refuses an emission under a name
        # it was never given. Declaring the WHOLE frozen catalogue once, before anything can
        # emit, is belt and braces rather than the guarantee: the component that emits a metric
        # owns registering it. It is here because a component that forgot could otherwise only be
        # found in production -- measured, with the writer lease, where
        # `oktografx_lease_wait_seconds` was emitted by the coordinator and registered by nobody,
        # so the first write commit of any database with a RECORDING sink refused. A no-op sink
        # hid it, and every suite below this one uses a no-op sink or a double (LESSONS L12).
        # It sits INSIDE the guard so a sink that refuses a descriptor cannot leave the device
        # this composition already opened holding its descriptors.
        register_catalog(metrics)
        # Computed inside the guard, not before it: it was the one fallible statement in the
        # window between a complete registry and the guard that releases it, so a path it
        # could not encode leaked the device this composition had already opened.
        label = database_label(config.path)
        started = _start_publisher(config, metrics, events, owns_ports)
        if started is not None:
            endpoint, stop = started
            closers.append(stop)

        if storage.page_size != config.page_size:
            raise GrafxConfigurationError(
                f"The storage device lays out {storage.page_size}-byte pages and the "
                f"configuration asks for {config.page_size}. A device and a database must agree "
                f"on the page size before anything is read.",
                field="page_size",
                value=config.page_size,
                device=storage.page_size,
            )
        _require_budget_for_the_stores(config)
        _require_page_size_of_record(config, storage)

        pool = BufferPool(
            storage,
            codec,
            metrics,
            budget_bytes=config.buffer_budget_bytes,
            db_label=label,
            # The pool is reached by every thread of this participant -- a commit applying pages
            # in the participant section, searches and scans pinning outside it -- and its doors
            # must be atomic against each other. The lock is mechanism, so it is handed in here
            # rather than imported by the pool (the pure core imports none), and it is
            # re-entrant because a checkpoint flushes and an invalidation writes back.
            guard=threading.RLock(),
        )
        identity = _open_identity(config, pool, clock)

        wal = WalManager(
            storage,
            clock,
            metrics,
            directory=WAL_DIRECTORY,
            segment_bytes=config.wal_segment_bytes,
            descriptor=config.granularity_descriptor,
        )
        wal.open()
        ledger = LedgerStore(storage, clock, metrics)
        quarantine = QuarantineStore(storage, clock, metrics)
        catalog = CatalogStore(pool)

        recovery = RecoveryManager(
            storage,
            wal,
            ledger,
            quarantine,
            pool,
            metrics,
            catalog=catalog,
            policy=config.recovery_policy,
            coordinator=coordinator,
        )
        # FR-1: reopening runs the recovery of FR-8 BEFORE any transaction is accepted. A
        # read-only open runs none: recovery quarantines, truncates and replays, and a database
        # opened read-only must not write to a database another process may own.
        report = None if config.read_only else recovery.run()

        heap = HeapStore(pool, catalog)
        if not config.read_only:
            catalog.bootstrap()
            heap.bootstrap()
            # The bootstrap of each store reserves its header page IN THE CACHE and marks it
            # dirty; nothing below this layer decides when a page reaches the device. Handing back
            # a database whose heap and catalog still hold a zero-filled page 0 is not a
            # cosmetic delay: `verify()` reads the device and reported three checksum failures on
            # a database that had just been created cleanly (FR-11 says a clean database produces
            # an empty report), and a crash in that window leaves C6 a zero page it can only read
            # as damage -- routing an intact new database to quarantine and a forensic ledger
            # entry (FR-8, FR-10). The open sequence is the only place that knows the creation is
            # finished, so it is the place that makes it durable.
            pool.flush()
        elif catalog.is_bootstrapped():
            catalog.load()

        indexes = IndexManager(pool, heap, metrics)
        vectors = VectorEngine(
            catalog=catalog,
            heap=heap,
            math=vector_math,
            metrics=metrics,
            clock=clock,
            events=events,
            # CF-10: a vector index is a paged file registered with the index manager, so the
            # engine needs the pool that pages it and the registry that owns it. Both are
            # optional in its signature and it refuses `attach` with a typed error naming the
            # field rather than falling back to a memory index -- a fallback would be a second
            # implementation of the same index. Passing them is what turns that refusal off, and
            # the composition root is the only layer that holds both objects.
            pool=pool,
            indexes=indexes,
            exact_scan_threshold=config.vector_exact_scan_threshold,
            # P0.5: the derived HNSW graph of every vector index is published under this
            # condition -- one complete picture per reference assignment, one build in flight
            # per index. Mechanism, so it is handed in here like the pool's guard above rather
            # than imported by the engine (the pure core imports none). A Condition and not a
            # bare lock because a search that arrives mid-build waits on it for the build.
            guard=threading.Condition(),
        )
        queries = QueryEngine(
            catalog=catalog,
            heap=heap,
            pool=pool,
            metrics=metrics,
            clock=clock,
            indexes=indexes,
            vectors=vectors,
        )
        transactions = TransactionManager(
            wal,
            pool,
            heap,
            catalog,
            coordinator,
            clock,
            metrics,
            indexes,
            partitions_per_table=config.partitions_per_table,
            commit_lock_timeout=config.commit_lock_timeout_seconds,
            lease_timeout=config.lease_timeout_seconds,
            reader_stall_threshold=config.reader_stall_threshold_seconds,
            descriptor=config.granularity_descriptor,
        )
        attached = _attach_primary_key_indexes(catalog, indexes, pool, metrics)
        adopted = set(attached)
        unindexed = tuple(
            table.name
            for table in catalog.catalog.tables()
            if (
                table.primary_key is not None
                and primary_key_index_name(table.name) not in adopted
            )
            or (
                getattr(table, "kind", None) == "rel"
                and (
                    edge_from_index_name(table.name) not in adopted
                    or edge_to_index_name(table.name) not in adopted
                )
            )
        )
        attached += _attach_declared_vector_indexes(catalog, vectors)
        stale = tuple(
            index.name for index in indexes.open(transactions.published_lsn())
        )
    except GrafxError:
        # A47: the class a component chose and the retryable detail it carries are what a caller
        # switches on, so a Grafx failure leaves this guard exactly as it arrived.
        _release(closers)
        raise
    except Exception as failure:
        # A port implementation the CALLER supplied may do anything, including returning a value
        # its Protocol does not describe -- the registry refuses a wrong SHAPE statically and
        # deliberately runs no adapter code, so a wrong BEHAVIOUR can only be met here. Section 2
        # and DoD item 5 say only Grafx types leave a public door, so a foreign exception is
        # brought into the taxonomy rather than allowed to escape. Nothing is hidden: the
        # original is chained, so its type, message and traceback all survive.
        _release(closers)
        raise GrafxConfigurationError(
            f"A database could not be assembled at {config.path!r}: an adapter did not honour "
            f"its port and raised {type(failure).__name__}: {failure}",
            field="ports",
            path=config.path,
            cause=type(failure).__name__,
        ) from failure
    except BaseException:
        # KeyboardInterrupt and SystemExit are not failures of this composition and are never
        # converted; the resources are still released on the way past.
        _release(closers)
        raise

    return Database(
        storage=storage,
        clock=clock,
        codec=codec,
        metrics=metrics,
        events=events,
        vector_math=vector_math,
        coordinator=coordinator,
        pool=pool,
        catalog=catalog,
        heap=heap,
        wal=wal,
        transactions=transactions,
        identity=identity,
        path=config.path,
        label=label,
        read_only=config.read_only,
        metrics_endpoint=endpoint,
        indexes=indexes,
        ledger=ledger,
        quarantine=quarantine,
        recovery=recovery,
        vectors=vectors,
        queries=queries,
        verifier_factory=_verifier_factory(pool, metrics, heap, catalog, indexes),
        recovery_report=report,
        attached_indexes=attached,
        stale_indexes=stale,
        unindexed_tables=unindexed,
        closers=tuple(closers),
    )


def _verifier_factory(
    pool: BufferPool,
    metrics: MetricsSink,
    heap: HeapStore,
    catalog: CatalogStore,
    indexes: IndexManager,
) -> Callable[[], Verifier]:
    """Return a callable that builds a verifier over the index set registered AT THAT MOMENT.

    A verifier is handed the indexes it must walk, and a database registers indexes for as long as
    it is open. Capturing the set here, at open, would make ``verify("indexes")`` report a clean
    walk of an empty set for every index registered afterwards -- a wrong answer rather than a
    missing one, which is the one outcome verification may never produce.
    """

    def build() -> Verifier:
        """Return a verifier over the index set registered at this moment."""
        return Verifier(
            pool,
            metrics,
            heap=heap,
            catalog=catalog,
            indexes=indexes.indexes(),
        )

    return build


def _attach_primary_key_indexes(
    catalog: CatalogStore,
    indexes: IndexManager,
    pool: BufferPool,
    metrics: MetricsSink,
) -> tuple[str, ...]:
    """Register the primary-key index of every table the catalog holds, and name them.

    The DDL that creates a table creates its index, so this is the RE-ADOPTION path: the next
    process to open the database has an index file on disk and no object for it. It mirrors
    `_attach_declared_vector_indexes` exactly, and for the same reason -- an index that exists on
    the device and is registered by nobody is an index the planner cannot see, which is a silent
    fall back to a full scan rather than an error.

    Registering OPENS an existing file rather than replacing it (G6), so this adopts what is there
    instead of rebuilding it, and `IndexManager.open` decides freshness afterwards. A database
    written before primary keys were indexed has no such file: one is created, it is empty while
    the heap is not, and it is therefore STALE -- which is the honest answer and the safe one,
    because a stale index is excluded from planning and the query falls back to the scan it used
    to do.
    """
    attached: list[str] = []
    for table in catalog.catalog.tables():
        try:
            for endpoint in relationship_endpoint_indexes(table, pool, metrics):
                attached.append(indexes.register(endpoint).name)
            index = primary_key_index(table, pool, metrics)
            if index is None:
                continue
            attached.append(indexes.register(index).name)
        except (GrafxIndexError, GrafxUnsupportedOperation):
            # AN INDEX MAY NEVER MAKE A DATABASE UNOPENABLE. A catalog can hold a table whose
            # index name is illegal or collides -- two names differing only by case fold to one
            # file -- and raising here meant every later `connect()` on that database refused,
            # with every row in it unreachable through the only door there is. The accelerator
            # declines instead: the table is readable, its keyed reads plan a scan, and the name
            # is reported through `Database.unindexed_tables`.
            continue
    return tuple(attached)


def _attach_declared_vector_indexes(
    catalog: CatalogStore, vectors: VectorEngine
) -> tuple[str, ...]:
    """Attach the index of every vector column the catalog declares, and name what was attached.

    An index covers a ``(table, space)`` PAIR, because the framework keys it on a table and a
    column position -- so declaring a space does not make it searchable and a table has to attach
    it. The statement that creates the table does that on the way in; nothing did it on the way
    back, so a reopened database held the durable index file and no registered index at all, and
    the engine answered ``GrafxIndexError`` for a space whose index was sitting on disk.

    Re-attaching is safe to repeat: the store OPENS an existing file rather than replacing it
    (G6), so a second composition over the same device adopts the durable index instead of
    rebuilding it. Nothing is repaired here -- a repair writes to the log and belongs inside a
    transaction the caller owns -- so an index that opens stale is reported through
    :attr:`okto_grafx.engine.database.Database.stale_indexes` rather than silently rebuilt.
    """
    attached: list[str] = []
    for table in catalog.catalog.tables():
        for column in table.columns:
            space = column.vector_space
            if space is None:
                continue
            attached.append(vectors.attach(table, space).name)
    return tuple(attached)


def _require_budget_for_the_stores(config: DatabaseConfig) -> None:
    """Refuse a buffer budget that cannot hold the frames the stores need (FR-13).

    ``DatabaseConfig`` accepts any positive number of bytes, and the catalog and the heap each
    refuse a budget below :data:`MINIMUM_STORE_FRAMES` pages when they are built -- correctly, and
    naming their own parameter ``budget_bytes``. Checking it here as well is not a second
    mechanism answering one question (A67): the stores are answering "can I run", this is
    answering "is this configuration openable", and only this one can name the field the caller
    actually wrote and say so before a directory has been created. The two are ordered, not
    redundant -- a mutation that removes this check still leaves the stores refusing, which is why
    the test for it asserts the field name this refusal alone produces (A62).
    """
    frames = config.buffer_budget_bytes // config.page_size
    if frames >= MINIMUM_STORE_FRAMES:
        return
    raise GrafxConfigurationError(
        f"A database needs a buffer budget of at least {MINIMUM_STORE_FRAMES} pages of "
        f"{config.page_size} bytes, which is {MINIMUM_STORE_FRAMES * config.page_size} bytes; "
        f"{config.buffer_budget_bytes} bytes holds {frames}.",
        field="buffer_budget_bytes",
        value=config.buffer_budget_bytes,
        required_bytes=MINIMUM_STORE_FRAMES * config.page_size,
    )


def _require_page_size_of_record(config: DatabaseConfig, storage: StorageDevice) -> None:
    """Refuse to open an existing database at a page size it was not created with (FR-1).

    Every paged door of the device divides a file by the page size it was built with, so a
    database created with 512-byte pages and reopened with 1024-byte ones reports
    ``corruption_detected`` from the first ``page_count`` -- before the identity record of section
    6.2, which exists to catch exactly this, can be read at all. That misclassification is not
    cosmetic: FR-8 and FR-10 turn "corruption" into truncation, quarantine and forensic ledger
    entries, so a caller who mistyped a page size would manufacture an integrity incident on a
    database that is perfectly intact (A11-revised).

    A length check on its own covers only HALF the domain: a database created at 8192 and
    reopened at 4096 divides evenly, sails past, and reports corruption from the first page
    read -- which is the misclassification this guard exists to prevent. The exact answer is
    available because :class:`okto_grafx.engine.database.MetaStore` is the only writer of
    this file and writes it as exactly ONE page, so its length IS the page size the database
    was created with. Pinned by ``test_the_identity_file_is_exactly_one_page``, which is what
    makes the equality below a fact about the format rather than an assumption about it.
    """
    if config.path == MEMORY_PATH or not storage.exists(META_FILE):
        return
    size = storage.file_size(META_FILE)
    if size == config.page_size:
        return
    raise GrafxSchemaVersionMismatch(
        f"The database at {config.path!r} was created with {size}-byte pages and is being "
        f"opened with {config.page_size}-byte ones. Open it at the page size it was created "
        f"with.",
        field="page_size",
        value=config.page_size,
        stored=size,
        file=META_FILE,
    )


def _open_identity(config: DatabaseConfig, pool: BufferPool, clock: Clock) -> DatabaseIdentity:
    """Read the identity of an existing database, or stamp one on a database being created."""
    meta = MetaStore(pool)
    expected = DatabaseIdentity(
        database_uuid=new_database_uuid(),
        page_size=config.page_size,
        partitions_per_table=config.partitions_per_table,
        created_at_wall=clock.wall(),
        granularity_descriptor=config.granularity_descriptor,
    )
    if not config.read_only:
        return meta.open(expected)
    if not meta.exists():
        raise GrafxUnsupportedOperation(
            f"There is no database at {config.path!r} and read_only was requested, so there is "
            "nothing to open and nothing may be created.",
            path=config.path,
            field="read_only",
        )
    stored = meta.read()
    if stored.page_size != config.page_size:
        raise GrafxConfigurationError(
            f"This database was created with {stored.page_size}-byte pages and is being opened "
            f"with {config.page_size}-byte pages.",
            field="page_size",
            value=config.page_size,
            stored=stored.page_size,
        )
    return stored


def _start_publisher(
    config: DatabaseConfig,
    metrics: MetricsSink,
    events: EventSink,
    owns_ports: bool,
) -> tuple[str, Callable[[], None]] | None:
    """Start the OpenMetrics endpoint of a default install, and return its URL and its stop.

    (SPEC-M1 FR-14, OR-6.)

    Only a composition that built its own adapters starts one: a caller that bound its own sink
    already decided how that sink is exposed, and binding a socket on its behalf would be this
    layer taking a decision that is not its own. SPEC-M1 FR-14 and OR-6 make ``GET /metrics`` the
    default install's endpoint, so this is where that sentence becomes true end to end.
    """
    if not owns_ports or config.metrics != "openmetrics":
        return None
    from okto_grafx.adapters.metrics_openmetrics import OpenMetricsPublisher
    from okto_grafx.runtime.bootstrap import metrics_destination

    destination = metrics_destination(config)
    if destination is None:  # pragma: no cover - A8 always resolves a destination here
        return None
    host, _, port = destination.rpartition(":")
    # The publisher owns the sink's LIFECYCLE, so it gets the sink itself, not the containment
    # shell the engine records through -- the shell is for recording calls inside public doors,
    # and a publisher handed the shell cannot see the aggregator and answers 503.
    sink = getattr(metrics, "inner", metrics)
    publisher = OpenMetricsPublisher(sink, host=host, port=int(port), events=events)
    publisher.start()
    # Read the URL back AFTER start: with the A8 default of port zero the operating system chose
    # the port, and the requested one says nothing about where the endpoint actually is.
    return publisher.url, publisher.stop


def _closer_for(instance: object) -> Callable[[], None]:
    """Return a callable that closes an adapter, or one that does nothing when it has no close."""
    closer = getattr(instance, "close", None)
    if closer is None or not callable(closer):
        return _nothing_to_close
    return closer


def _nothing_to_close() -> None:
    """Release an adapter that holds no host resource: there is nothing to do."""


def _release(closers: list[Callable[[], None]]) -> None:
    """Run every registered release in reverse, swallowing failures.

    This runs while an earlier failure is already travelling to the caller. Replacing that
    failure with a failure of the closing path would hide the reason the composition is being
    abandoned, so a release that cannot finish is dropped and the original error continues.
    """
    for closer in reversed(closers):
        try:
            closer()
        except (GrafxError, OSError):
            continue


def _port(ports: PortRegistry, slot: str, protocol: type[_Port]) -> _Port:
    """Return the adapter bound to a slot, typed as the protocol that owns it.

    The registry has already refused a wrong shape at bind time, statically and without running
    adapter code, so this only reads the binding back. The protocol is named for the reader and
    for the type checker; re-verifying it here would be a second mechanism answering one question
    (A67), and the one that fails closed is the registry's.
    """
    return cast(_Port, ports.get(slot))
