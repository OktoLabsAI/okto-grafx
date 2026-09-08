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
   refused before a single page is decoded against the wrong layout. On the very first open this
   step publishes the identity, empty catalog and empty heap together from durable staging files;
3. the log, the ledger and quarantine, because recovery needs all three;
4. **recovery, before any transaction can be opened** (FR-1, FR-8) -- the whole point of running
   it at open is that nothing observes the database until the log has been believed or truncated;
5. the catalog and the heap: an existing database validates and loads them only after recovery
   has replayed the pages that decide what they hold; a new database validates the complete empty
   files that step 2 already published and never bootstraps an authoritative partial file;
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
import struct
import threading
import uuid
import warnings
from collections.abc import Callable
from typing import TypeVar, cast

from okto_grafx.adapters.coordination_local import (
    CONTROL_DIRECTORY,
    LEASE_SECTION,
    LOCK_FILE_SUFFIX,
)
from okto_grafx.adapters.graph_guard import ConditionGuard
from okto_grafx.adapters.metrics_contained import ContainedMetricsSink
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_read_only import ReadOnlyStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.catalog import CATALOG_FORMAT_VERSION
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.codec import PageCodec
from okto_grafx.domain.ports.coordination import ProcessCoordinator
from okto_grafx.domain.ports.events import EventSink
from okto_grafx.domain.ports.metrics import MetricsSink
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.ports.vectormath import VectorMath
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CATALOG_FILE, CatalogStore
from okto_grafx.engine.database import META_FILE, Database, DatabaseIdentity, MetaStore
from okto_grafx.engine.heap_store import HEAP_FILE, HeapStore
from okto_grafx.engine.index_manager import (
    INDEX_DIRECTORY,
    HashIndex,
    IndexManager,
    edge_from_index_name,
    edge_to_index_name,
    index_file,
    primary_key_index_name,
)
from okto_grafx.engine.ordered_index import OrderedIndex
from okto_grafx.engine.ledger_store import LedgerStore
from okto_grafx.engine.metrics_catalog import register_catalog
from okto_grafx.engine.quarantine import QuarantineStore
from okto_grafx.engine.query_engine import QueryEngine
from okto_grafx.engine.recovery_manager import RecoveryManager
from okto_grafx.engine.txn_manager import COMMIT_SECTION, TransactionManager
from okto_grafx.engine.vector_engine import VectorEngine
from okto_grafx.engine.verifier import Verifier
from okto_grafx.engine.wal_manager import WalManager
from okto_grafx.runtime.config import (
    MEMORY_PATH,
    MINIMUM_STORE_FRAMES,
    DatabaseConfig,
    _canonical_database_config,
    _is_loopback_metrics_host,
    _openmetrics_host_port,
)
from okto_grafx.runtime.registry import PortRegistry, _snapshot_port_registry

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

_FIRST_OPEN_SECTION: str = "first-open"
"""Cross-process section that publishes a fresh database exactly once."""

_FIRST_OPEN_SECTION_LOCK: str = (
    f"{CONTROL_DIRECTORY}/{_FIRST_OPEN_SECTION}{LOCK_FILE_SUFFIX}"
)
"""Existing liveness evidence that lets a default reader wait without claiming a new path."""

_FIRST_OPEN_DIRECTORY: str = "bootstrap"
"""Private namespace for first-open intent and unpublished paged files."""

_FIRST_OPEN_INTENT: str = f"{_FIRST_OPEN_DIRECTORY}/first-open.intent"
"""Durable identity of an interrupted first open."""

_FIRST_OPEN_INTENT_STAGING: str = f"{_FIRST_OPEN_INTENT}.staging"
"""Unpublished intent bytes; safe to discard before any final file exists."""

_FIRST_OPEN_COMPLETE: str = f"{_FIRST_OPEN_DIRECTORY}/first-open.complete"
"""Permanent positive authority that the first-open unit reached durable completion."""

_FIRST_OPEN_COMPLETE_STAGING: str = f"{_FIRST_OPEN_COMPLETE}.staging"
"""Unpublished complete marker bytes before their atomic canonical publication."""

_FIRST_OPEN_META_STAGING: str = f"{_FIRST_OPEN_DIRECTORY}/grafx.meta.staging"
"""Complete identity page before its atomic publication."""

_FIRST_OPEN_CATALOG_STAGING: str = f"{_FIRST_OPEN_DIRECTORY}/catalog.dat.staging"
"""Complete empty catalog before its atomic publication."""

_FIRST_OPEN_HEAP_STAGING: str = f"{_FIRST_OPEN_DIRECTORY}/heap.dat.staging"
"""Complete empty heap before its atomic publication."""

_FIRST_OPEN_INTENT_MAGIC: bytes = b"OKGFXOPN"
"""Eight-byte discriminator of a first-open intent record."""

_FIRST_OPEN_COMPLETE_MAGIC: bytes = b"OKGFXCMP"
"""Eight-byte discriminator of a completed first-open record."""

_FIRST_OPEN_INTENT_VERSION: int = 1
"""Newest first-open intent format understood by this build."""

_FIRST_OPEN_INTENT_HEAD: struct.Struct = struct.Struct("<8sHH")
"""Intent magic, format version and encoded identity length."""

_FIRST_OPEN_INTENT_TAIL: struct.Struct = struct.Struct("<I")
"""CRC-32C over the intent header and encoded identity."""

_FIRST_OPEN_IDENTITY_MAX_BYTES: int = 0xFFFF
"""Hard bound imposed by the intent's unsigned 16-bit identity length."""

_FIRST_OPEN_INTENT_MAX_BYTES: int = (
    _FIRST_OPEN_INTENT_HEAD.size
    + _FIRST_OPEN_IDENTITY_MAX_BYTES
    + _FIRST_OPEN_INTENT_TAIL.size
)
"""Largest read the intent decoder can ever ask a storage adapter to serve."""

_FIRST_OPEN_STAGING_FILES: frozenset[str] = frozenset(
    {
        _FIRST_OPEN_INTENT_STAGING,
        _FIRST_OPEN_COMPLETE_STAGING,
        _FIRST_OPEN_META_STAGING,
        _FIRST_OPEN_CATALOG_STAGING,
        _FIRST_OPEN_HEAP_STAGING,
    }
)
"""Names this protocol may retire as unpublished debris on an otherwise empty path."""

_FIRST_OPEN_PROTOCOL_FILES: frozenset[str] = _FIRST_OPEN_STAGING_FILES | {
    _FIRST_OPEN_INTENT,
    _FIRST_OPEN_COMPLETE,
}
"""Complete whitelist of names owned by the first-open state machine."""

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
    digest = hashlib.blake2s(
        os.fsencode(path), digest_size=LABEL_DIGEST_BYTES
    ).hexdigest()
    return f"{LABEL_PREFIX}{digest}"


def new_database_uuid() -> bytes:
    """Return the sixteen identity bytes of a database that is being created (FR-1).

    ``uuid`` lives here rather than in the engine because randomness and wall-clock reads are
    mechanism (G2b, A5): the pure core is handed the bytes and only ever stores and compares them.
    """
    return uuid.uuid4().bytes


def _builtin_type_name(value: object) -> str:
    """Name a public argument without invoking a hostile metaclass descriptor."""
    value_type = type(value)
    declared = type.__dict__["__name__"].__get__(value_type, type(value_type))
    return str.__str__(declared)


def assemble_database(
    config: DatabaseConfig, ports: PortRegistry, *, owns_ports: bool = False
) -> Database:
    """Build every engine of one database over a complete registry and return it open.

    ``owns_ports`` says whether this composition built the adapters. When it did, closing the
    database closes them; when the caller bound its own registry, they are left open.
    """
    config = _canonical_database_config(config)
    ports = _snapshot_port_registry(ports)
    if type(owns_ports) is not bool:
        observed = _builtin_type_name(owns_ports)
        raise GrafxConfigurationError(
            f"owns_ports must be a bool; got {observed}.",
            field="owns_ports",
            value=observed,
        )
    raw_storage = _port(ports, "storage", StorageDevice)
    storage: StorageDevice = (
        ReadOnlyStorageDevice(raw_storage) if config.read_only else raw_storage
    )
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
            closers.append(_closer_for(raw_storage, observational=config.read_only))
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
        if type(raw_storage) is LocalStorageDevice:
            # The storage protocol remains frozen: descriptor caching is an adapter detail.
            # Bind its counters through the metrics port only for the concrete default adapter,
            # and call the concrete method so a host subclass cannot inject a callback into the
            # assembly window. LocalStorageDevice publishes outside its own descriptor guard.
            LocalStorageDevice.bind_metrics(raw_storage, metrics)
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
            # must be atomic against each other. The condition is mechanism, so it is handed in
            # here rather than imported by the pool (the pure core imports none). It is
            # re-entrant because checkpoint/invalidation nest pool doors, and its atomic
            # wait/wake half lets cold reads leave this guard without duplicating one page load.
            guard=ConditionGuard(),
            # This is the trusted composition adapter's existing deferral boundary, passed
            # explicitly rather than discovered on a host-supplied metrics implementation.
            # It drains pool and nested descriptor-cache emissions after the pool guard.
            metrics_defer=metrics.defer,
            # Page 0 is a non-wrapping cross-process freshness clock. BufferPool owns the CAS,
            # while the composition root supplies its mechanism: one deterministic section per
            # index file in this database. Readers never take it; unrelated page-0 protocols are
            # deliberately outside this index-specific fence.
            page_write_section=lambda file, _page_index: coordinator.exclusive(
                f"page0-{crc32c(file.encode('utf-8')):08x}",
                timeout=config.commit_lock_timeout_seconds,
            ),
            page_sequence_fence=lambda file, _page_index: file.startswith(
                f"{INDEX_DIRECTORY}/"
            ),
        )
        identity = _open_identity(config, pool, clock, storage, coordinator)
        if not config.read_only:
            identity = _upgrade_control_record_identity(
                config, storage, pool, coordinator, identity
            )
        _bind_coordinator_control_format(coordinator, identity)

        wal = WalManager(
            storage,
            clock,
            metrics,
            directory=WAL_DIRECTORY,
            segment_bytes=config.wal_segment_bytes,
            descriptor=config.granularity_descriptor,
        )
        wal.open()
        quarantine = QuarantineStore(storage, clock, metrics)
        ledger = LedgerStore(storage, clock, metrics, quarantine=quarantine)
        catalog = CatalogStore(pool)
        heap = HeapStore(pool, catalog)
        indexes = IndexManager(
            pool, heap, metrics, artifact_nonce=_new_control_file_nonce
        )
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
            ef_search=config.vector_ef_search,
            # Production schema changes use QueryEngine staging and the normal WAL commit.
            # Closing the two legacy direct-save doors is the proof TXN-4 needs to retain a
            # catalog view across a CE-3 interval containing only ordinary DML.
            catalog_changes_are_wal_logged=True,
            # P0.5: the derived HNSW graph of every vector index is published under this
            # guard -- one complete picture per reference assignment, one build in flight per
            # index. Mechanism, so it is handed in here like the pool's guard above rather than
            # imported by the engine (the pure core imports none). A condition and not a bare
            # lock because a search that arrives mid-build waits on it for the build; and one
            # that knows the calling thread, so the builder's own re-entrant call never waits.
            guard=ConditionGuard(),
        )

        attached_names: list[str] = []
        catalog_loaded = False

        def sync_indexes(*, existing_only: bool) -> tuple[str, ...]:
            """Adopt every declared index from one proved directory inventory."""
            existing_files = frozenset(storage.list_files("index/"))
            active_definitions = catalog.catalog.active_index_definitions()
            newly_attached = _attach_primary_key_indexes(
                catalog,
                indexes,
                pool,
                metrics,
                definitions=active_definitions,
                existing_only=existing_only,
                existing_files=existing_files,
            )
            newly_attached += _attach_declared_vector_indexes(
                catalog,
                vectors,
                definitions=active_definitions,
                storage=storage,
                existing_only=existing_only,
                existing_files=existing_files,
            )
            if not existing_only:
                indexes.ensure_artifact_identities()
            for name in newly_attached:
                if name not in attached_names:
                    attached_names.append(name)
            return newly_attached

        def load_catalog() -> bool:
            """Interpret proved catalog bytes before constructing catalog-dependent services."""
            nonlocal catalog_loaded
            if not catalog.is_bootstrapped():
                return False
            if not catalog_loaded:
                # Re-derive then adopt, the same non-destructive route recovery itself uses.
                # When catalog pages were replayed this is an idempotent second reading; when
                # they were not, it is the startup's first interpretation of the device bytes.
                # Remembering it matters for an operator recovery on the live Database: direct
                # unsaved changes must never be discarded by a later callback.
                catalog.adopt(catalog.read_from_pages())
                catalog_loaded = True
            return True

        def load_catalog_and_sync_existing_indexes() -> tuple[str, ...]:
            """Load then adopt the existing baseline required by writable recovery."""
            if not load_catalog():
                return ()
            # Existing files form the only baseline recovery may certify. Creating an empty
            # index from a retained WAL suffix would turn absence into a silently short path.
            return sync_indexes(existing_only=True)

        recovery = RecoveryManager(
            storage,
            wal,
            ledger,
            quarantine,
            pool,
            metrics,
            catalog=catalog,
            index_manager=indexes,
            index_sync=load_catalog_and_sync_existing_indexes,
            policy=config.recovery_policy,
            coordinator=coordinator,
            commit_lock_timeout=config.commit_lock_timeout_seconds,
            database_uuid=identity.database_uuid,
            control_format_version=identity.format_version,
            control_file_nonce=_new_control_file_nonce(),
        )
        # FR-1: a writable reopen replays BEFORE catalog payloads are interpreted. A read-only
        # reopen proves from both commit.state and the WAL that replay is unnecessary, then may
        # load the catalog; the proof performs no repair or other persistence.
        if config.read_only:
            recovery.require_read_only_consistent()
            _require_published_stores(catalog, heap)
            # No redo runs on this path. The manager constructor needs the catalog
            # capability flags but does not consume index artifacts. Adopt those
            # once at the existing final read-only sync below, not twice around a
            # constructor that only wires transaction state. Recovery's callback
            # and all later transaction-level synchronization remain unchanged.
            load_catalog()
            report = None
        else:
            report = recovery.run()

        if not config.read_only:
            _require_published_stores(catalog, heap)
            catalog.bootstrap()
            heap.bootstrap()
            # Identity, catalog and heap headers define whether this directory is a database at
            # all. A successful connect must not return while those pages are only in volatile
            # device caches; unlike row data, their identity/bootstrap state is not reconstructible
            # from WAL.
            pool.checkpoint()

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
            identity_lease_size=config.identity_lease_size,
            commit_lock_timeout=config.commit_lock_timeout_seconds,
            lease_timeout=config.lease_timeout_seconds,
            reader_stall_threshold=config.reader_stall_threshold_seconds,
            descriptor=config.granularity_descriptor,
            index_sync=lambda: sync_indexes(existing_only=True),
            writable=not config.read_only,
            max_transaction_rows=config.max_transaction_rows,
            max_transaction_bytes=config.max_transaction_bytes,
            max_wal_batch_bytes=config.max_wal_batch_bytes,
            max_index_build_entries=config.max_index_build_entries,
            automatic_index_expected_cardinality=(
                config.automatic_index_expected_cardinality
            ),
            database_uuid=identity.database_uuid,
            control_format_version=identity.format_version,
            control_file_nonce=_new_control_file_nonce(),
            process_identity_provider=os.getpid,
            catalog_changes_are_wal_logged=True,
        )
        # A writable open may create a missing accelerator only under the same artifact section
        # as DDL attach and commit publication.  That section first adopts existing files and
        # rebases the durable catalog, then holds COMMIT_SECTION through create/legacy-nonce
        # upgrade.  Read-only open performs only the non-mutating existing-file adoption.
        if config.read_only:
            sync_indexes(existing_only=True)
        else:
            with transactions.schema_artifact_section(sync_if=lambda: True):
                sync_indexes(existing_only=False)
        queries = QueryEngine(
            catalog=catalog,
            heap=heap,
            pool=pool,
            metrics=metrics,
            clock=clock,
            indexes=indexes,
            vectors=vectors,
            page_stager=transactions._stage_page_image,
            schema_artifact_section=transactions.schema_artifact_section,
            custom_index_preparer=transactions.prepare_custom_exact_index,
            # Separate from BufferPool's lock: endpoint memo accounting is atomic, while the
            # heap walk it enables never holds this guard across page I/O.
            endpoint_locator_guard=threading.RLock(),
            compiled_predicate_guard=threading.Lock(),
            max_statement_writes=config.max_statement_writes,
            max_result_rows=config.max_result_rows,
            max_intermediate_rows=config.max_intermediate_rows,
            query_memory_budget_bytes=config.query_memory_budget_bytes,
            query_spill=LocalQuerySpillFactory(),
            max_traversal_expansions=config.max_traversal_expansions,
            max_traversal_paths=config.max_traversal_paths,
            max_index_build_entries=config.max_index_build_entries,
            automatic_index_expected_cardinality=(
                config.automatic_index_expected_cardinality
            ),
        )
        attached = tuple(attached_names)
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
        _registered, stale_indexes = transactions.refresh_index_inventory(
            persist_stale=not config.read_only
        )
        stale = tuple(index.name for index in stale_indexes)
    except GrafxError:
        # A47: the class a component chose and the retryable detail it carries are what a caller
        # switches on, so a Grafx failure leaves this guard exactly as it arrived.
        _release(closers)
        raise
    except Exception as failure:
        # A port implementation the CALLER supplied may do anything, including returning a value
        # its Protocol does not describe -- the registry refuses a wrong SHAPE statically and
        # deliberately runs no adapter code, so a wrong BEHAVIOUR can only be met here. Although
        # custom adapters are trusted host code after open, this assembly boundary can still
        # classify an ordinary failure as invalid composition while releasing everything already
        # acquired. Nothing is hidden: the original is chained, so its type, message and traceback
        # all survive.
        _release(closers)
        cause = _builtin_type_name(failure)
        raise GrafxConfigurationError(
            f"A database could not be assembled at {config.path!r}: an adapter did not honour "
            f"its port and raised {cause}.",
            field="ports",
            path=config.path,
            cause=cause,
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
        checkpoint_interval_records=config.checkpoint_interval_records,
        wal_max_bytes=config.wal_max_bytes,
        max_query_value_characters=config.max_query_value_characters,
        read_only=config.read_only,
        descriptor_revalidation=config.descriptor_revalidation,
        metrics_endpoint=endpoint,
        indexes=indexes,
        ledger=ledger,
        quarantine=quarantine,
        recovery=recovery,
        vectors=vectors,
        queries=queries,
        plan_guard_factory=threading.Lock,
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
    """Return a callable that builds a verifier over committed indexes registered then.

    A verifier is handed the indexes it must walk, and a database registers indexes for as long as
    it is open. Capturing the set here, at open, would make ``verify("indexes")`` report a clean
    walk of an empty set for every index registered afterwards -- a wrong answer rather than a
    missing one, which is the one outcome verification may never produce. Query DDL also
    registers speculative indexes before commit; the complete table identity filters those from
    the durable database being verified even when a foreign table reused their numeric id.
    """

    def build() -> Verifier:
        """Return a verifier over the index set registered at this moment."""
        authority = catalog.catalog
        committed_indexes = indexes.active_indexes(
            catalog=authority,
        )
        if authority.format_version == CATALOG_FORMAT_VERSION:
            covered = {
                index.definition.registry_key: index.definition
                for index in committed_indexes
            }
            missing = tuple(
                definition.name
                for definition in authority.active_index_definitions()
                if definition.visibility is IndexVisibility.EXACT
                and covered.get(definition.registry_key) != definition
            )
            if missing:
                raise GrafxIndexError(
                    "Verification cannot cover every exact ACTIVE generation selected by "
                    f"catalog v2; missing or mismatched registrations: {', '.join(missing)}.",
                    field="index_authority",
                    missing=missing,
                )
        return Verifier(
            pool,
            metrics,
            heap=heap,
            catalog=catalog,
            indexes=committed_indexes,
        )

    return build


def _attach_primary_key_indexes(
    catalog: CatalogStore,
    indexes: IndexManager,
    pool: BufferPool,
    metrics: MetricsSink,
    *,
    definitions: tuple[IndexDefinition, ...] | None = None,
    existing_only: bool = False,
    existing_files: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Register every exact index selected by the catalog's runtime authority.

    In catalog v1 these are the schema-derived primary-key and relationship endpoint definitions.
    In v2 they are only persisted logical definitions with an ``ACTIVE`` physical generation.
    The latter names an immutable generation file by nonce; its absence or any header mismatch is
    an authority failure, never permission for startup to manufacture a new empty file.

    Legacy writable composition retains its established compatibility behaviour: a missing
    derived accelerator may be created and then marked stale against a populated heap. Existing-
    only sync never creates an artifact in either format.
    """
    catalog_value = catalog.catalog
    selected = (
        catalog_value.active_index_definitions() if definitions is None else definitions
    )
    catalog_managed = catalog_value.format_version == CATALOG_FORMAT_VERSION
    attached: list[str] = []
    for definition in selected:
        if definition.visibility is not IndexVisibility.EXACT:
            continue
        try:
            index = (
                OrderedIndex(definition, pool, metrics)
                if definition.layout is IndexLayout.ORDERED
                else HashIndex(definition, pool, metrics)
            )
            try:
                current = indexes.index(index.name)
            except GrafxIndexError as failure:
                if failure.details.get("field") != "name":
                    raise
                current = None
            if (
                not catalog_managed
                and current is not None
                and current.definition == index.definition
            ):
                continue
            proved_present = existing_files is not None and index.file in existing_files
            present = (
                proved_present
                if existing_files is not None
                else pool.storage.exists(index.file)
            )
            if not present:
                if catalog_managed:
                    raise GrafxIndexError(
                        f"Catalog v2 selects active index {index.name!r}, but its physical "
                        f"generation {index.file!r} is absent.",
                        field="file",
                        file=index.file,
                        index=index.name,
                        artifact_nonce=definition.artifact_nonce,
                    )
                if existing_only:
                    continue
            attached.append(
                (
                    indexes.adopt_committed(
                        index,
                        persist_stale=False,
                        proved_present=present,
                    )
                    if existing_only or catalog_managed
                    else indexes.register(
                        index,
                        existing_only=existing_only,
                        persist_stale=not existing_only,
                        proved_present=present,
                    )
                ).name
            )
        except (GrafxIndexError, GrafxUnsupportedOperation):
            if catalog_managed:
                raise
            # AN INDEX MAY NEVER MAKE A DATABASE UNOPENABLE. A catalog can hold a table whose
            # index name is illegal or collides -- two names differing only by case fold to one
            # file -- and raising here meant every later `connect()` on that database refused,
            # with every row in it unreachable through the only door there is. The accelerator
            # declines instead: the table is readable, its keyed reads plan a scan, and the name
            # is reported through `Database.unindexed_tables`.
            continue
    return tuple(attached)


def _attach_declared_vector_indexes(
    catalog: CatalogStore,
    vectors: VectorEngine,
    *,
    definitions: tuple[IndexDefinition, ...] | None = None,
    storage: StorageDevice | None = None,
    existing_only: bool = False,
    existing_files: frozenset[str] | None = None,
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
    selected = (
        catalog.catalog.active_index_definitions()
        if definitions is None
        else definitions
    )
    active_names = {
        definition.registry_key
        for definition in selected
        if definition.visibility is IndexVisibility.PROXIMITY
    }
    attached: list[str] = []
    for table in catalog.catalog.tables():
        for column in table.columns:
            space = column.vector_space
            if space is None:
                continue
            name = f"vector_{table.name}_{space}"
            if name.lower() not in active_names:
                continue
            file = index_file(name)
            proved_present = existing_files is not None and file in existing_files
            if existing_only and (
                not proved_present
                if existing_files is not None
                else storage is None or not storage.exists(file)
            ):
                continue
            try:
                attached.append(
                    vectors.attach(
                        table,
                        space,
                        existing_only=existing_only,
                        persist_stale=not existing_only,
                        proved_present=proved_present,
                    ).name
                )
            except (GrafxIndexError, GrafxUnsupportedOperation):
                # A derived accelerator that cannot be adopted must not make the authoritative
                # catalog and heap unreachable. Vector search will refuse the unattached space;
                # ordinary graph reads remain available and a writable repair can rebuild it.
                continue
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


def _require_page_size_of_record(
    config: DatabaseConfig, storage: StorageDevice
) -> None:
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

    This size-only preflight runs before the first-open section and may therefore notice the
    complete meta rename just before its durability barrier. It cannot hand out or decode the
    database: every identity/page observation that follows crosses ``_FIRST_OPEN_SECTION`` and
    waits for that barrier. Its only early outcome is refusing an already visible size mismatch.
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


def _configured_identity(config: DatabaseConfig, clock: Clock) -> DatabaseIdentity:
    """Return the identity a first open would publish from this configuration."""
    return DatabaseIdentity(
        database_uuid=new_database_uuid(),
        page_size=config.page_size,
        partitions_per_table=config.partitions_per_table,
        created_at_wall=clock.wall(),
        granularity_descriptor=config.granularity_descriptor,
    )


def _open_identity(
    config: DatabaseConfig,
    pool: BufferPool,
    clock: Clock,
    storage: StorageDevice,
    coordinator: ProcessCoordinator,
) -> DatabaseIdentity:
    """Read an existing identity or atomically publish one complete empty database.

    A fresh database is assembled under a cross-process section.  Its versioned intent is
    durable before any authoritative file appears, and every paged file is first checkpointed
    under an unpublished name.  Atomic replacement therefore exposes either no final file or a
    complete file; a retry reads the intent and uses the same UUID.
    """
    meta = MetaStore(pool)
    with coordinator.exclusive(
        _FIRST_OPEN_SECTION, timeout=config.commit_lock_timeout_seconds
    ):
        # Readers take the same section even though they mutate no data file.  More importantly,
        # they classify the intent BEFORE opening grafx.meta: atomic_replace makes complete
        # finals visible before the global barrier makes the publication durable.  A creator
        # that dies in that interval releases this section but leaves the intent as the durable
        # authority; a reader must refuse it rather than interpret the visible meta page.
        intent = _read_first_open_intent(storage)
        complete = _read_first_open_complete(storage)
        if complete is not None:
            return _open_completed_identity(
                config,
                storage,
                meta,
                intent=intent,
                complete=complete,
            )
        if config.read_only:
            if intent is not None:
                _refuse_pending_first_open(config)
            _require_no_bootstrap_orphan_for_read_only(config, storage)
            return _read_existing_identity(config, storage, meta)

        if intent is None:
            bootstrap_files = _bootstrap_files(storage)
            if bootstrap_files and (
                _database_files_without_identity(storage)
                or storage.exists(COMMIT_STATE_FILE)
            ):
                _require_no_bootstrap_without_intent(storage, bootstrap_files)
            if storage.exists(META_FILE):
                return _read_existing_identity(config, storage, meta)
            _require_no_database_without_identity(storage)
            _retire_unpublished_bootstrap(storage, bootstrap_files)
            intent = _configured_identity(config, clock)
            _write_first_open_intent(storage, intent)
        else:
            _require_known_bootstrap_with_intent(storage)
            _retire_duplicate_intent_staging(storage, intent)
        _require_identity_configuration(config, intent)
        _resume_first_open(storage, pool, intent)
        return intent


def _new_control_file_nonce() -> int:
    """Return one host-generated nonce for a control file that may need bootstrapping."""
    nonce = 0
    while nonce == 0:
        nonce = int.from_bytes(uuid.uuid4().bytes[:8], "little")
    return nonce


def _identity_with_format(
    identity: DatabaseIdentity, format_version: int
) -> DatabaseIdentity:
    """Return the same database identity carrying a different physical format version."""
    return DatabaseIdentity(
        database_uuid=identity.database_uuid,
        page_size=identity.page_size,
        partitions_per_table=identity.partitions_per_table,
        created_at_wall=identity.created_at_wall,
        granularity_descriptor=identity.granularity_descriptor,
        format_version=format_version,
    )


def _same_identity_except_format(
    left: DatabaseIdentity, right: DatabaseIdentity
) -> bool:
    """Return whether two identities differ, if at all, only in their format version."""
    return _identity_with_format(left, right.format_version) == right


def _replace_first_open_complete(
    storage: StorageDevice, identity: DatabaseIdentity
) -> None:
    """Atomically replace the completion authority during a control-format upgrade."""
    payload = _encode_first_open_intent(identity, magic=_FIRST_OPEN_COMPLETE_MAGIC)
    if storage.exists(_FIRST_OPEN_COMPLETE_STAGING):
        storage.remove(_FIRST_OPEN_COMPLETE_STAGING)
    storage.create(_FIRST_OPEN_COMPLETE_STAGING, exclusive=True)
    terminal = storage.append_log(_FIRST_OPEN_COMPLETE_STAGING, payload)
    if terminal != len(payload):
        raise GrafxCorruptionDetected(
            "The upgraded completion marker was not staged in full.",
            file=_FIRST_OPEN_COMPLETE_STAGING,
            field="terminal_offset",
            value=terminal,
            expected=len(payload),
        )
    storage.durable_barrier(_FIRST_OPEN_COMPLETE_STAGING)
    storage.atomic_replace(_FIRST_OPEN_COMPLETE_STAGING, _FIRST_OPEN_COMPLETE)
    storage.durable_barrier(_FIRST_OPEN_COMPLETE)


def _upgrade_control_record_identity(
    config: DatabaseConfig,
    storage: StorageDevice,
    pool: BufferPool,
    coordinator: ProcessCoordinator,
    identity: DatabaseIdentity,
) -> DatabaseIdentity:
    """Upgrade format 1 to 2 before any two-slot control file can be published.

    ``grafx.meta`` is replaced and barriered first.  That is the compatibility fence: an old
    build refuses format 2 before it can mistake a slot file for a legacy record.  The permanent
    completion marker follows; a crash between the two leaves the explicit, resumable
    ``meta=v2/complete=v1`` state handled by :func:`_open_completed_identity`.
    """
    if identity.format_version >= 2:
        return identity
    with coordinator.exclusive(
        _FIRST_OPEN_SECTION, timeout=config.commit_lock_timeout_seconds
    ):
        with coordinator.exclusive(
            COMMIT_SECTION, timeout=config.commit_lock_timeout_seconds
        ):
            with coordinator.exclusive(
                LEASE_SECTION, timeout=config.commit_lock_timeout_seconds
            ):
                current = MetaStore(pool).read()
                if current.format_version >= 2:
                    return current
                if current != identity:
                    raise GrafxCorruptionDetected(
                        "The database identity changed while its control format was upgraded.",
                        file=META_FILE,
                        field="identity",
                        state="upgrade_race",
                    )
                upgraded = _identity_with_format(current, 2)
                _stage_meta(storage, pool, upgraded)
                storage.atomic_replace(_FIRST_OPEN_META_STAGING, META_FILE)
                storage.durable_barrier(META_FILE)
                pool.invalidate(META_FILE)
                pool.invalidate(_FIRST_OPEN_META_STAGING)
                if storage.exists(_FIRST_OPEN_COMPLETE):
                    complete = _read_first_open_complete(storage)
                    if complete is None or not _same_identity_except_format(
                        upgraded, complete
                    ):
                        raise GrafxCorruptionDetected(
                            "The completion marker cannot be upgraded with this database identity.",
                            file=_FIRST_OPEN_COMPLETE,
                            field="identity",
                            state="upgrade_mismatch",
                        )
                    _replace_first_open_complete(storage, upgraded)
                return upgraded


def _bind_coordinator_control_format(
    coordinator: ProcessCoordinator, identity: DatabaseIdentity
) -> None:
    """Select the shipped coordinator's envelope without imposing it on custom ports."""
    bind = getattr(coordinator, "bind_control_record_format", None)
    if callable(bind):
        bind(
            database_uuid=identity.database_uuid,
            format_version=identity.format_version,
        )


def _preflight_default_read_only_storage(
    config: DatabaseConfig,
    storage: StorageDevice,
    codec: PageCodec,
) -> None:
    """Refuse an unclaimed default filesystem path before its coordinator creates ``control/``.

    The full and final classification still happens in :func:`_open_identity` while holding
    ``first-open``. This earlier observation answers only whether the default composition has
    enough Grafx-owned evidence to construct that lock. Canonical first-open authorities are
    decoded by the same readers used under the section; isolated staging and an absent identity
    are refused by the same helpers used by the final classifier. A lone ``grafx.meta`` is read
    through an ephemeral read-only pool and the same :class:`MetaStore`, so its page envelope,
    checksum, identity record and configured page size must all validate before liveness exists.

    An already-existing first-open lock is not database proof. It merely permits construction so
    a reader that arrived behind a creator can wait on the exact existing lock. Once admitted,
    the reader still crosses the section and receives the authoritative outcome. Opening that
    existing file neither creates a namespace entry nor changes its bytes.
    """
    if not config.read_only or config.path == MEMORY_PATH:
        return
    intent = _read_first_open_intent(storage)
    complete = _read_first_open_complete(storage)
    if intent is not None or complete is not None:
        return
    if storage.exists(_FIRST_OPEN_SECTION_LOCK):
        return
    _require_no_bootstrap_orphan_for_read_only(config, storage)
    _require_existing_identity_name(config, storage)
    observational = ReadOnlyStorageDevice(storage)
    _require_budget_for_the_stores(config)
    _require_page_size_of_record(config, observational)
    pool = BufferPool(
        observational,
        codec,
        NoOpMetricsSink(),
        budget_bytes=config.buffer_budget_bytes,
        db_label=database_label(config.path),
        guard=ConditionGuard(),
    )
    _read_existing_identity(config, observational, MetaStore(pool))


def _observe_default_read_only_identity(
    config: DatabaseConfig,
    storage: StorageDevice,
    codec: PageCodec,
) -> DatabaseIdentity:
    """Validate one filesystem identity without coordination or a writable capability.

    The ordinary read-only composition first performs a conservative preflight and then crosses
    the ``first-open`` coordination section before making its authoritative decision.  A forensic
    quarantine inventory must not construct that coordinator because doing so can create lock
    names in the namespace being diagnosed.  It therefore needs the same final classification
    expressed entirely as observations.

    A concurrent first open can make those observations fail closed, but can never make them
    write.  Completed publications are checked against their permanent marker, ``grafx.meta`` and
    both required final stores; pending publications and orphan staging remain explicit refusals.
    The page-size check happens before any page is decoded through the caller's codec.
    """
    if not config.read_only:
        raise GrafxConfigurationError(
            "An observational identity read requires a read-only configuration.",
            field="read_only",
            value=False,
        )
    if config.path == MEMORY_PATH:
        raise GrafxUnsupportedOperation(
            "An in-memory database has no persistent identity to inspect.",
            path=config.path,
            field="path",
            read_only=True,
        )

    observational = ReadOnlyStorageDevice(storage)
    _require_budget_for_the_stores(config)
    _require_page_size_of_record(config, observational)
    pool = BufferPool(
        observational,
        codec,
        NoOpMetricsSink(),
        budget_bytes=config.buffer_budget_bytes,
        db_label=database_label(config.path),
        guard=ConditionGuard(),
    )
    meta = MetaStore(pool)
    intent = _read_first_open_intent(observational)
    complete = _read_first_open_complete(observational)
    if complete is not None:
        identity = _open_completed_identity(
            config,
            observational,
            meta,
            intent=intent,
            complete=complete,
        )
    else:
        if intent is not None:
            _refuse_pending_first_open(config)
        _require_no_bootstrap_orphan_for_read_only(config, observational)
        identity = _read_existing_identity(config, observational, meta)
        # A completion marker was introduced with the durable first-open protocol, but databases
        # created by earlier releases legitimately have none.  Their identity is not sufficient
        # on its own: the same two final stores required beside a marker distinguish a complete
        # legacy database from a copied or orphaned ``grafx.meta``.
        _require_complete_final_files(observational)

    # Existence and alignment are not content proofs: an aligned page full of zeros (or a page
    # belonging to another file kind) must not turn a copied identity into a database.  These are
    # the same predicates a normal read-only open uses after its consistency proof.  They decode
    # envelopes, checksums, headers and the catalog chain without bootstrapping or repairing.
    catalog = CatalogStore(pool)
    _require_published_stores(catalog, HeapStore(pool, catalog))
    return identity


def _open_completed_identity(
    config: DatabaseConfig,
    storage: StorageDevice,
    meta: MetaStore,
    *,
    intent: DatabaseIdentity | None,
    complete: DatabaseIdentity,
) -> DatabaseIdentity:
    """Validate positive completion authority and reinforce it before any writable history."""
    _require_identity_configuration(config, complete)
    if intent is not None and intent != complete:
        raise GrafxCorruptionDetected(
            "Pending and completed first-open records name different database identities; "
            "neither record will be changed.",
            file=_FIRST_OPEN_COMPLETE,
            field="identity",
            state="pending_complete_mismatch",
        )
    bootstrap_files = _bootstrap_files(storage)
    allowed = {_FIRST_OPEN_COMPLETE}
    if intent is not None:
        allowed.add(_FIRST_OPEN_INTENT)
    duplicate_complete_staging = storage.exists(_FIRST_OPEN_COMPLETE_STAGING)
    if duplicate_complete_staging:
        staged_complete = _read_first_open_intent_file(
            storage,
            _FIRST_OPEN_COMPLETE_STAGING,
            expected_magic=_FIRST_OPEN_COMPLETE_MAGIC,
        )
        if staged_complete != complete:
            raise GrafxCorruptionDetected(
                "Canonical and staging completion markers name different database identities; "
                "neither record will be changed.",
                file=_FIRST_OPEN_COMPLETE_STAGING,
                field="identity",
                state="divergent_complete_staging",
            )
        allowed.add(_FIRST_OPEN_COMPLETE_STAGING)
    unexpected = tuple(name for name in bootstrap_files if name not in allowed)
    if unexpected:
        raise GrafxCorruptionDetected(
            "A completed first-open marker coexists with unexpected bootstrap staging. The "
            "evidence will not be deleted or ignored.",
            file=unexpected[0],
            files=bootstrap_files,
            field="bootstrap_orphan",
            state="unexpected_after_complete",
        )
    if not storage.exists(META_FILE):
        raise GrafxCorruptionDetected(
            "The first-open completion marker exists but grafx.meta is absent.",
            file=META_FILE,
            field="first_open_complete",
            state="final_missing",
        )
    stored = _read_existing_identity(config, storage, meta)
    if stored != complete:
        resumable_upgrade = (
            stored.format_version == 2
            and complete.format_version == 1
            and _same_identity_except_format(stored, complete)
        )
        if not resumable_upgrade:
            raise GrafxCorruptionDetected(
                "The permanent first-open completion identity does not match grafx.meta.",
                file=_FIRST_OPEN_COMPLETE,
                field="identity",
                state="complete_meta_mismatch",
            )
        if config.read_only:
            raise GrafxUnsupportedOperation(
                "A writable open must finish the interrupted control-format upgrade.",
                file=_FIRST_OPEN_COMPLETE,
                field="format_version",
                state="upgrade_pending",
                repairable=True,
            )
        _replace_first_open_complete(storage, stored)
        complete = stored
    _require_complete_final_files(storage)
    if config.read_only:
        # Read-only validates every byte above but never cleans pending state or manufactures a
        # durability acknowledgement. A writable participant owns both actions.
        return stored

    # A new process has no process-local knowledge of the marker's original rename. Re-fsyncing
    # the positive authority is the fence before WalManager can create transaction history.
    storage.durable_barrier(_FIRST_OPEN_COMPLETE)
    cleanup = []
    if duplicate_complete_staging:
        cleanup.append(_FIRST_OPEN_COMPLETE_STAGING)
    if intent is not None:
        cleanup.append(_FIRST_OPEN_INTENT)
    for file in cleanup:
        storage.remove(file)
    if cleanup:
        storage.durable_barrier(None)
    return stored


def _require_complete_final_files(storage: StorageDevice) -> None:
    """Require page-aligned, non-empty catalog and heap beside positive completion authority."""
    for file in (CATALOG_FILE, HEAP_FILE):
        if not storage.exists(file):
            raise GrafxCorruptionDetected(
                f"The first-open completion marker exists but final file {file!r} is absent.",
                file=file,
                field="first_open_complete",
                state="final_missing",
            )
        pages = storage.page_count(file)
        if pages < 1:
            raise GrafxCorruptionDetected(
                f"The first-open completion marker exists but final file {file!r} is empty.",
                file=file,
                field="first_open_complete",
                state="final_empty",
            )


def _refuse_pending_first_open(config: DatabaseConfig) -> None:
    """Keep read-only inspection behind a still-authoritative first-open intent."""
    raise GrafxUnsupportedOperation(
        f"The database at {config.path!r} has a durable first-open intent. A writable open "
        "must finish or diagnose that publication before read-only can observe its files.",
        path=config.path,
        file=_FIRST_OPEN_INTENT,
        field="first_open_intent",
        state="publication_pending",
        repairable=True,
    )


def _bootstrap_files(storage: StorageDevice) -> tuple[str, ...]:
    """Return all private first-open names, including unknown evidence."""
    return storage.list_files(f"{_FIRST_OPEN_DIRECTORY}/")


def _require_no_bootstrap_orphan_for_read_only(
    config: DatabaseConfig, storage: StorageDevice
) -> None:
    """Refuse unpublished bootstrap evidence without changing one byte or name.

    A partial intent staging file is decoded through the same bounded reader as the canonical
    intent.  Thus damaged staging is reported as damage rather than silently collapsed into
    absence; valid staging is still unpublished and receives the repairable refusal below.
    """
    files = _bootstrap_files(storage)
    if not files:
        return
    if _FIRST_OPEN_INTENT_STAGING in files:
        _read_first_open_intent_file(storage, _FIRST_OPEN_INTENT_STAGING)
    if _FIRST_OPEN_COMPLETE_STAGING in files:
        _read_first_open_intent_file(
            storage,
            _FIRST_OPEN_COMPLETE_STAGING,
            expected_magic=_FIRST_OPEN_COMPLETE_MAGIC,
        )
    raise GrafxUnsupportedOperation(
        f"The database at {config.path!r} has unpublished first-open staging files. A writable "
        "open must retire or resume them before read-only can observe the path.",
        path=config.path,
        file=files[0],
        files=files,
        field="bootstrap_orphan",
        state="unpublished_staging",
        repairable=True,
    )


def _require_no_bootstrap_without_intent(
    storage: StorageDevice, files: tuple[str, ...]
) -> None:
    """Never hide orphan bootstrap evidence behind a published identity."""
    if not files:
        return
    raise GrafxCorruptionDetected(
        "Database state coexists with bootstrap staging but no canonical first-open "
        "intent. The staging may be authoritative restore evidence and will not be discarded.",
        file=files[0],
        files=files,
        field="bootstrap_orphan",
        state="published_without_intent",
        repairable=False,
    )


def _require_known_bootstrap_with_intent(storage: StorageDevice) -> None:
    """Refuse foreign names even while a canonical intent authorises known staging."""
    files = _bootstrap_files(storage)
    allowed = _FIRST_OPEN_STAGING_FILES | {_FIRST_OPEN_INTENT}
    unknown = tuple(name for name in files if name not in allowed)
    if not unknown:
        return
    raise GrafxCorruptionDetected(
        "A first-open intent coexists with unknown private bootstrap files. The intent cannot "
        "authorise deleting or ignoring possible restore/operator evidence.",
        file=unknown[0],
        files=files,
        field="bootstrap_orphan",
        state="unknown_with_intent",
        repairable=False,
    )


def _retire_unpublished_bootstrap(
    storage: StorageDevice, files: tuple[str, ...]
) -> None:
    """Durably discard only recognised staging on a path proved otherwise empty.

    This is the retry path for a crash after creating or partly appending the intent staging
    file.  Unknown names are preserved as possible operator/restore evidence.  The caller has
    already proved that no final or transaction state exists, so recognised names cannot have
    become authoritative.
    """
    if not files:
        return
    unknown = tuple(name for name in files if name not in _FIRST_OPEN_STAGING_FILES)
    if unknown:
        raise GrafxCorruptionDetected(
            "The private bootstrap namespace contains files this first-open protocol does not "
            "own; they will not be discarded or treated as an empty database.",
            file=unknown[0],
            files=files,
            field="bootstrap_orphan",
            state="unknown_staging",
            repairable=False,
        )
    for name in files:
        storage.remove(name)
    # Removal is a namespace mutation.  On POSIX the storage adapter retains the nested
    # bootstrap directory as a barrier debt even though no file descriptor remains.
    storage.durable_barrier(None)


def _retire_duplicate_intent_staging(
    storage: StorageDevice, canonical: DatabaseIdentity
) -> None:
    """Retire a byte-equivalent duplicate staging intent, refusing every divergence."""
    if not storage.exists(_FIRST_OPEN_INTENT_STAGING):
        return
    staged = _read_first_open_intent_file(storage, _FIRST_OPEN_INTENT_STAGING)
    if staged != canonical:
        raise GrafxCorruptionDetected(
            "Canonical and staging first-open intents name different database identities; "
            "neither will be overwritten or discarded.",
            file=_FIRST_OPEN_INTENT_STAGING,
            field="identity",
            state="divergent_intent_staging",
        )
    storage.remove(_FIRST_OPEN_INTENT_STAGING)
    storage.durable_barrier(None)


def _read_existing_identity(
    config: DatabaseConfig, storage: StorageDevice, meta: MetaStore
) -> DatabaseIdentity:
    """Read a complete published identity, never interpreting a partial file as absence."""
    _require_existing_identity_name(config, storage)
    if not meta.exists():
        raise GrafxCorruptionDetected(
            f"The published identity file {META_FILE!r} exists without its complete page; "
            "it is an interrupted or damaged database, not an empty path.",
            file=META_FILE,
            field="page_count",
            value=storage.page_count(META_FILE),
        )
    stored = meta.read()
    _require_identity_configuration(config, stored)
    return stored


def _require_existing_identity_name(
    config: DatabaseConfig, storage: StorageDevice
) -> None:
    """Require ``grafx.meta`` or issue the one authoritative absent-identity classification."""
    if storage.exists(META_FILE):
        return
    evidence = _database_files_without_identity(storage)
    commit_state = storage.exists(COMMIT_STATE_FILE)
    if evidence or commit_state:
        raise GrafxCorruptionDetected(
            f"The database identity {META_FILE!r} is absent while authoritative files remain: "
            f"{evidence}. This is damage, not an empty path.",
            file=META_FILE,
            field="identity_missing",
            files=evidence,
            commit_state=commit_state,
        )
    raise GrafxUnsupportedOperation(
        f"There is no database at {config.path!r} and read_only was requested, so there is "
        "nothing to open and nothing may be created.",
        path=config.path,
        field="read_only",
    )


def _database_files_without_identity(storage: StorageDevice) -> tuple[str, ...]:
    """Return namespace evidence that cannot belong to a path awaiting its first identity."""
    return tuple(
        name
        for name in storage.list_files()
        if not name.startswith("control/")
        and not name.startswith(f"{_FIRST_OPEN_DIRECTORY}/")
    )


def _require_no_database_without_identity(storage: StorageDevice) -> None:
    """Refuse to mint a UUID over files whose original identity is missing."""
    evidence = _database_files_without_identity(storage)
    if not evidence and not storage.exists(COMMIT_STATE_FILE):
        return
    raise GrafxCorruptionDetected(
        f"The database identity {META_FILE!r} is absent while authoritative state remains; "
        "a new UUID would mask an existing damaged database.",
        file=META_FILE,
        field="identity_missing",
        files=evidence,
        commit_state=storage.exists(COMMIT_STATE_FILE),
    )


def _require_identity_configuration(
    config: DatabaseConfig, identity: DatabaseIdentity
) -> None:
    """Refuse to decode paged files at a size other than their durable identity declares."""
    if identity.page_size != config.page_size:
        raise GrafxSchemaVersionMismatch(
            f"This database was created with {identity.page_size}-byte pages and is being opened "
            f"with {config.page_size}-byte pages.",
            field="page_size",
            value=config.page_size,
            stored=identity.page_size,
        )


def _encode_first_open_intent(
    identity: DatabaseIdentity, *, magic: bytes = _FIRST_OPEN_INTENT_MAGIC
) -> bytes:
    """Encode a versioned, checksummed first-open authority carrying the database UUID."""
    encoded_identity = identity.encode()
    if len(encoded_identity) > _FIRST_OPEN_IDENTITY_MAX_BYTES:
        raise GrafxConfigurationError(
            "The encoded database identity is too large for a first-open intent.",
            field="granularity_descriptor",
            value=len(encoded_identity),
            maximum=_FIRST_OPEN_IDENTITY_MAX_BYTES,
        )
    head = _FIRST_OPEN_INTENT_HEAD.pack(
        magic,
        _FIRST_OPEN_INTENT_VERSION,
        len(encoded_identity),
    )
    body = head + encoded_identity
    return body + _FIRST_OPEN_INTENT_TAIL.pack(crc32c(body))


def _decode_first_open_intent(
    raw: bytes,
    *,
    file: str = _FIRST_OPEN_INTENT,
    expected_magic: bytes = _FIRST_OPEN_INTENT_MAGIC,
) -> DatabaseIdentity:
    """Decode one exact first-open intent, refusing truncation, extensions and damage."""
    minimum = _FIRST_OPEN_INTENT_HEAD.size + _FIRST_OPEN_INTENT_TAIL.size
    if len(raw) < minimum:
        raise GrafxCorruptionDetected(
            f"The first-open intent is at least {minimum} bytes; this one holds {len(raw)}.",
            file=file,
            field="length",
            value=len(raw),
        )
    if len(raw) > _FIRST_OPEN_INTENT_MAX_BYTES:
        raise GrafxCorruptionDetected(
            f"A first-open intent can hold at most {_FIRST_OPEN_INTENT_MAX_BYTES} bytes; "
            f"this one holds {len(raw)}.",
            file=file,
            field="length",
            value=len(raw),
            maximum=_FIRST_OPEN_INTENT_MAX_BYTES,
        )
    magic, version, identity_length = _FIRST_OPEN_INTENT_HEAD.unpack_from(raw, 0)
    if magic != expected_magic:
        raise GrafxCorruptionDetected(
            "The first-open intent carries a foreign magic and cannot authorise publication.",
            file=file,
            field="magic",
            value=repr(magic),
        )
    if version > _FIRST_OPEN_INTENT_VERSION:
        raise GrafxSchemaVersionMismatch(
            f"The first-open intent uses format {version}; this build reads up to "
            f"{_FIRST_OPEN_INTENT_VERSION}.",
            file=file,
            field="format_version",
            value=version,
            supported=_FIRST_OPEN_INTENT_VERSION,
        )
    if version != _FIRST_OPEN_INTENT_VERSION:
        raise GrafxCorruptionDetected(
            f"The first-open intent uses invalid format version {version}.",
            file=file,
            field="format_version",
            value=version,
        )
    identity_start = _FIRST_OPEN_INTENT_HEAD.size
    identity_end = identity_start + identity_length
    expected = identity_end + _FIRST_OPEN_INTENT_TAIL.size
    if len(raw) != expected:
        raise GrafxCorruptionDetected(
            f"The first-open intent declares {identity_length} identity bytes and therefore "
            f"must hold {expected} bytes; it holds {len(raw)}.",
            file=file,
            field="length",
            value=len(raw),
            expected=expected,
        )
    (stored_checksum,) = _FIRST_OPEN_INTENT_TAIL.unpack_from(raw, identity_end)
    observed_checksum = crc32c(raw[:identity_end])
    if stored_checksum != observed_checksum:
        raise GrafxCorruptionDetected(
            "The checksum of the first-open intent does not match its bytes.",
            file=file,
            field="crc32c",
            value=stored_checksum,
            observed=observed_checksum,
        )
    encoded_identity = raw[identity_start:identity_end]
    identity = DatabaseIdentity.decode(encoded_identity)
    if identity.encode() != encoded_identity:
        raise GrafxCorruptionDetected(
            "The first-open intent carries non-canonical identity bytes.",
            file=file,
            field="identity",
        )
    return identity


def _read_first_open_intent(storage: StorageDevice) -> DatabaseIdentity | None:
    """Return the durable first-open identity, or None only when its name is absent."""
    if not storage.exists(_FIRST_OPEN_INTENT):
        return None
    return _read_first_open_intent_file(storage, _FIRST_OPEN_INTENT)


def _read_first_open_complete(storage: StorageDevice) -> DatabaseIdentity | None:
    """Return the permanent completion identity, or None only when its name is absent."""
    if not storage.exists(_FIRST_OPEN_COMPLETE):
        return None
    return _read_first_open_intent_file(
        storage,
        _FIRST_OPEN_COMPLETE,
        expected_magic=_FIRST_OPEN_COMPLETE_MAGIC,
    )


def _read_first_open_intent_file(
    storage: StorageDevice,
    file: str,
    *,
    expected_magic: bytes = _FIRST_OPEN_INTENT_MAGIC,
) -> DatabaseIdentity:
    """Read one intent with a hard bound proved from its fixed header first.

    ``log_size`` is metadata, not a read request.  No value obtained from it is ever handed
    directly to ``read_log``: the fixed-size header is read first, its unsigned length is
    checked against the protocol maximum and against the exact file size, and only that bounded
    expected length is requested.  A hostile multi-gigabyte file therefore costs one stat and
    no multi-gigabyte allocation.
    """
    size = storage.log_size(file)
    minimum = _FIRST_OPEN_INTENT_HEAD.size + _FIRST_OPEN_INTENT_TAIL.size
    if size < minimum or size > _FIRST_OPEN_INTENT_MAX_BYTES:
        raise GrafxCorruptionDetected(
            f"A first-open intent must hold between {minimum} and "
            f"{_FIRST_OPEN_INTENT_MAX_BYTES} bytes; this one holds {size}.",
            file=file,
            field="length",
            value=size,
            minimum=minimum,
            maximum=_FIRST_OPEN_INTENT_MAX_BYTES,
        )
    header = storage.read_log(file, 0, _FIRST_OPEN_INTENT_HEAD.size)
    if len(header) != _FIRST_OPEN_INTENT_HEAD.size:
        raise GrafxCorruptionDetected(
            "The first-open intent header could not be read in full.",
            file=file,
            field="length",
            value=len(header),
            expected=_FIRST_OPEN_INTENT_HEAD.size,
        )
    magic, version, identity_length = _FIRST_OPEN_INTENT_HEAD.unpack(header)
    if magic != expected_magic:
        raise GrafxCorruptionDetected(
            "The first-open intent carries a foreign magic and cannot authorise publication.",
            file=file,
            field="magic",
            value=repr(magic),
        )
    if version > _FIRST_OPEN_INTENT_VERSION:
        raise GrafxSchemaVersionMismatch(
            f"The first-open intent uses format {version}; this build reads up to "
            f"{_FIRST_OPEN_INTENT_VERSION}.",
            file=file,
            field="format_version",
            value=version,
            supported=_FIRST_OPEN_INTENT_VERSION,
        )
    if version != _FIRST_OPEN_INTENT_VERSION:
        raise GrafxCorruptionDetected(
            f"The first-open intent uses invalid format version {version}.",
            file=file,
            field="format_version",
            value=version,
        )
    if identity_length > _FIRST_OPEN_IDENTITY_MAX_BYTES:  # defensive: the field is u16
        raise GrafxCorruptionDetected(
            "The first-open intent declares an identity beyond the protocol bound.",
            file=file,
            field="identity_length",
            value=identity_length,
            maximum=_FIRST_OPEN_IDENTITY_MAX_BYTES,
        )
    expected = (
        _FIRST_OPEN_INTENT_HEAD.size + identity_length + _FIRST_OPEN_INTENT_TAIL.size
    )
    if size != expected:
        raise GrafxCorruptionDetected(
            f"The first-open intent declares {identity_length} identity bytes and therefore "
            f"must hold {expected} bytes; it holds {size}.",
            file=file,
            field="length",
            value=size,
            expected=expected,
        )
    raw = storage.read_log(file, 0, expected)
    if len(raw) != expected:
        raise GrafxCorruptionDetected(
            "The first-open intent changed or was truncated while it was being read.",
            file=file,
            field="length",
            value=len(raw),
            expected=expected,
        )
    return _decode_first_open_intent(
        raw,
        file=file,
        expected_magic=expected_magic,
    )


def _write_first_open_intent(
    storage: StorageDevice, identity: DatabaseIdentity
) -> None:
    """Durably stage then atomically publish intent before any authoritative file."""
    payload = _encode_first_open_intent(identity)
    if storage.exists(_FIRST_OPEN_INTENT):
        raise GrafxCorruptionDetected(
            "A first-open intent already exists and will not be overwritten.",
            file=_FIRST_OPEN_INTENT,
            field="first_open_intent",
            state="already_published",
        )
    storage.create(_FIRST_OPEN_INTENT_STAGING)
    terminal = storage.append_log(_FIRST_OPEN_INTENT_STAGING, payload)
    if terminal != len(payload):
        raise GrafxCorruptionDetected(
            f"The first-open intent append reported terminal offset {terminal}; "
            f"{len(payload)} was required.",
            file=_FIRST_OPEN_INTENT_STAGING,
            field="terminal_offset",
            value=terminal,
            expected=len(payload),
        )
    storage.durable_barrier(_FIRST_OPEN_INTENT_STAGING)
    # The section is the engine-level exclusion.  This second observation is a fail-closed
    # guard against an out-of-band restore racing the device namespace.
    if storage.exists(_FIRST_OPEN_INTENT):
        raise GrafxCorruptionDetected(
            "A first-open intent appeared while its staging payload was being prepared; "
            "neither name will be overwritten.",
            file=_FIRST_OPEN_INTENT,
            field="first_open_intent",
            state="publication_race",
        )
    storage.atomic_replace(_FIRST_OPEN_INTENT_STAGING, _FIRST_OPEN_INTENT)
    # Pins both the already-fsynced payload under its canonical name and the nested directory
    # rename.  No final database file is allowed to appear before this returns.
    storage.durable_barrier(_FIRST_OPEN_INTENT)


def _write_first_open_complete(
    storage: StorageDevice, identity: DatabaseIdentity
) -> None:
    """Publish the permanent positive authority before retiring the pending intent."""
    payload = _encode_first_open_intent(identity, magic=_FIRST_OPEN_COMPLETE_MAGIC)
    if storage.exists(_FIRST_OPEN_COMPLETE):
        raise GrafxCorruptionDetected(
            "A first-open completion marker already exists and will not be overwritten.",
            file=_FIRST_OPEN_COMPLETE,
            field="first_open_complete",
            state="already_published",
        )
    if storage.exists(_FIRST_OPEN_COMPLETE_STAGING):
        storage.remove(_FIRST_OPEN_COMPLETE_STAGING)
        storage.durable_barrier(None)
    storage.create(_FIRST_OPEN_COMPLETE_STAGING)
    terminal = storage.append_log(_FIRST_OPEN_COMPLETE_STAGING, payload)
    if terminal != len(payload):
        raise GrafxCorruptionDetected(
            f"The first-open completion append reported terminal offset {terminal}; "
            f"{len(payload)} was required.",
            file=_FIRST_OPEN_COMPLETE_STAGING,
            field="terminal_offset",
            value=terminal,
            expected=len(payload),
        )
    storage.durable_barrier(_FIRST_OPEN_COMPLETE_STAGING)
    if storage.exists(_FIRST_OPEN_COMPLETE):
        raise GrafxCorruptionDetected(
            "A first-open completion marker appeared while its staging payload was being "
            "prepared; neither name will be overwritten.",
            file=_FIRST_OPEN_COMPLETE,
            field="first_open_complete",
            state="publication_race",
        )
    storage.atomic_replace(_FIRST_OPEN_COMPLETE_STAGING, _FIRST_OPEN_COMPLETE)
    storage.durable_barrier(_FIRST_OPEN_COMPLETE)


def _resume_first_open(
    storage: StorageDevice, pool: BufferPool, identity: DatabaseIdentity
) -> None:
    """Idempotently publish missing first-open files and retire the intent last.

    A valid intent does not make an existing final expendable.  All three canonical files are
    rebuilt under staging names first.  A final from an interrupted publication is accepted only
    when every byte equals that canonical staging payload; equality lets the retry discard the
    duplicate staging name and complete the missing finals.  One divergent byte refuses before
    any final is replaced.  Without an intent this function is unreachable, so a partial final
    can never be reclassified as a fresh database.
    """
    _require_resumable_first_open(storage)
    _require_pending_first_open_namespace(storage)
    _stage_meta(storage, pool, identity)
    _stage_catalog(storage, pool)
    _stage_heap(storage, pool)
    publications = (
        (_FIRST_OPEN_META_STAGING, META_FILE),
        (_FIRST_OPEN_CATALOG_STAGING, CATALOG_FILE),
        (_FIRST_OPEN_HEAP_STAGING, HEAP_FILE),
    )
    # Preflight EVERY existing final before publishing even one missing final.  A divergent
    # catalog must not leave a previously absent meta installed merely because meta came first.
    for staging, published in publications:
        _require_identical_if_published(storage, staging, published)
    for staging, published in publications:
        _publish_missing_or_require_identical(storage, staging, published)

    # The replacements above are atomic visibility operations.  This barrier is the durability
    # operation: it pins their complete payloads and the namespace entries before the intent can
    # disappear.  A power loss before here retains the intent and retries the same UUID.
    storage.durable_barrier(None)
    # Absence can never be the authority that publication completed.  This permanent marker is
    # staged and pinned while the intent is still present; only its positive, checksummed UUID
    # lets a later process classify a resurrected intent beside real WAL as already completed.
    _write_first_open_complete(storage, identity)
    storage.remove(_FIRST_OPEN_INTENT)
    # The absence is itself part of the protocol.  Pinning it means an old intent cannot return
    # after connect() has handed the database to user code and accepted real transactions.
    storage.durable_barrier(None)


def _require_resumable_first_open(storage: StorageDevice) -> None:
    """Refuse any transaction history before rebuilding canonical staging payloads."""
    wal_files = storage.list_files(f"{WAL_DIRECTORY}/")
    if wal_files or storage.exists(COMMIT_STATE_FILE):
        raise GrafxCorruptionDetected(
            "A first-open intent coexists with transaction history, so it cannot authorise "
            "replacement of any authoritative file.",
            file=_FIRST_OPEN_INTENT,
            field="transaction_history",
            wal_files=wal_files,
            commit_state=storage.exists(COMMIT_STATE_FILE),
        )


def _require_pending_first_open_namespace(storage: StorageDevice) -> None:
    """Allow pending publication to coexist only with its control and canonical files."""
    finals = {META_FILE, CATALOG_FILE, HEAP_FILE}
    foreign = tuple(
        name
        for name in storage.list_files()
        if not name.startswith("control/")
        and name not in _FIRST_OPEN_PROTOCOL_FILES
        and name not in finals
    )
    if not foreign:
        return
    raise GrafxCorruptionDetected(
        "A pending first-open intent coexists with files outside its complete namespace "
        "whitelist. They are preserved as possible database/restore evidence.",
        file=foreign[0],
        files=foreign,
        field="first_open_namespace",
        state="foreign_evidence",
    )


def _discard_staging(storage: StorageDevice, file: str) -> None:
    """Remove one unpublished staging name left by an interrupted first open."""
    if storage.exists(file):
        storage.remove(file)


def _stage_meta(
    storage: StorageDevice, pool: BufferPool, identity: DatabaseIdentity
) -> None:
    """Checkpoint the canonical identity page under its unpublished name."""
    _discard_staging(storage, _FIRST_OPEN_META_STAGING)
    MetaStore(pool, file=_FIRST_OPEN_META_STAGING).create(identity)


def _stage_catalog(storage: StorageDevice, pool: BufferPool) -> None:
    """Checkpoint the canonical empty catalog under its unpublished name."""
    _discard_staging(storage, _FIRST_OPEN_CATALOG_STAGING)
    catalog = CatalogStore(pool, file=_FIRST_OPEN_CATALOG_STAGING)
    catalog.bootstrap()
    pool.checkpoint(_FIRST_OPEN_CATALOG_STAGING)


def _stage_heap(storage: StorageDevice, pool: BufferPool) -> None:
    """Checkpoint the canonical empty heap under its unpublished name."""
    _discard_staging(storage, _FIRST_OPEN_HEAP_STAGING)
    catalog = CatalogStore(pool, file=_FIRST_OPEN_CATALOG_STAGING)
    heap = HeapStore(pool, catalog, file=_FIRST_OPEN_HEAP_STAGING)
    heap.bootstrap()
    pool.checkpoint(_FIRST_OPEN_HEAP_STAGING)


def _publish_missing_or_require_identical(
    storage: StorageDevice, staging: str, published: str
) -> None:
    """Publish a missing final, or discard staging only after byte-for-byte equality."""
    if not storage.exists(published):
        storage.atomic_replace(staging, published)
        return
    _require_identical_if_published(storage, staging, published)
    storage.remove(staging)


def _require_identical_if_published(
    storage: StorageDevice, staging: str, published: str
) -> None:
    """Preflight one existing final against every byte of its canonical staging file."""
    if not storage.exists(published):
        return
    staging_pages = storage.page_count(staging)
    published_pages = storage.page_count(published)
    identical = staging_pages == published_pages and all(
        storage.read_page(staging, page) == storage.read_page(published, page)
        for page in range(staging_pages)
    )
    if not identical:
        raise GrafxCorruptionDetected(
            f"The published first-open file {published!r} differs from its canonical staging "
            "payload and will not be overwritten.",
            file=published,
            field="published_bytes",
            expected_pages=staging_pages,
            observed_pages=published_pages,
        )


def _require_published_stores(catalog: CatalogStore, heap: HeapStore) -> None:
    """Refuse missing or partial authoritative stores after recovery had its chance to replay."""
    if not catalog.is_bootstrapped() or not catalog.chain_pages():
        raise GrafxCorruptionDetected(
            f"The identity exists but {CATALOG_FILE!r} has no complete published catalog; "
            "it is damage, not a fresh database.",
            file=CATALOG_FILE,
            field="bootstrap",
        )
    if not heap.is_bootstrapped():
        raise GrafxCorruptionDetected(
            f"The identity exists but {HEAP_FILE!r} has no complete published heap header; "
            "it is damage, not a fresh database.",
            file=HEAP_FILE,
            field="bootstrap",
        )


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
    host, port = _openmetrics_host_port(destination)
    if not _is_loopback_metrics_host(host):
        warnings.warn(
            f"OpenMetrics is binding to non-loopback address {destination!r} because "
            "allow_remote_metrics=True; protect the endpoint at the network boundary.",
            RuntimeWarning,
            stacklevel=3,
        )
    # The publisher owns the sink's LIFECYCLE, so it gets the sink itself, not the containment
    # shell the engine records through -- the shell is for recording calls inside public doors,
    # and a publisher handed the shell cannot see the aggregator and answers 503.
    sink = getattr(metrics, "inner", metrics)
    publisher = OpenMetricsPublisher(sink, host=host, port=port, events=events)
    publisher.start()
    # Read the URL back AFTER start: with the A8 default of port zero the operating system chose
    # the port, and the requested one says nothing about where the endpoint actually is.
    return publisher.url, publisher.stop


def _closer_for(
    instance: object,
    *,
    observational: bool = False,
) -> Callable[[], None]:
    """Return the owned closer, avoiding writable housekeeping for observational opens."""
    closer = getattr(instance, "close_read_only", None) if observational else None
    if closer is None or not callable(closer):
        closer = getattr(instance, "close", None)
    if closer is None or not callable(closer):
        return _nothing_to_close
    return closer


def _nothing_to_close() -> None:
    """Release an adapter that holds no host resource: there is nothing to do."""


def _release(closers: list[Callable[[], None]]) -> None:
    """Run every registered release in reverse, preserving the active assembly failure.

    This runs while an earlier failure is already travelling to the caller. Replacing that
    failure with a failure of the closing path would hide the reason the composition is being
    abandoned, so every closer gets its chance and every secondary failure -- including a
    process-control ``BaseException`` -- is suppressed while the original error continues.
    """
    for closer in reversed(closers):
        try:
            closer()
        except BaseException:
            continue


def _port(ports: PortRegistry, slot: str, protocol: type[_Port]) -> _Port:
    """Return the adapter bound to a slot, typed as the protocol that owns it.

    The registry has already refused a wrong shape at bind time, statically and without running
    adapter code, so this only reads the binding back. The protocol is named for the reader and
    for the type checker; re-verifying it here would be a second mechanism answering one question
    (A67), and the one that fails closed is the registry's.
    """
    return cast(_Port, ports.get(slot))
