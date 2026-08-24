"""The public Database is an API, not a route around its own safety protocols."""

from __future__ import annotations

from collections.abc import Mapping, MutableSequence, MutableSet
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, is_dataclass
from enum import Enum
import threading
from typing import Iterator

import pytest

from okto_grafx import Database, Transaction, connect
from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.events_logging import LoggingEventSink
from okto_grafx.adapters.metrics_contained import ContainedMetricsSink
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxIndexError,
    GrafxLedgerError,
    GrafxQuarantineError,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
)
from okto_grafx.domain.ledger.entry import LedgerEntry, LedgerOriginClass, LedgerReason
from okto_grafx.domain.txn.context import TransactionContext
from okto_grafx.engine import database as database_module
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager, IndexStore
from okto_grafx.engine.ledger_store import LedgerStore
from okto_grafx.engine.public_views import (
    PUBLIC_DATABASE_VIEW_ALLOWLIST,
    CatalogView,
    IndexRegistryView,
    LedgerView,
    QuarantineView,
    StorageView,
    VectorEngineView,
)
from okto_grafx.engine.quarantine import QuarantineStore
from okto_grafx.engine.query_engine import QueryEngine
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.vector_engine import VectorEngine
from okto_grafx.engine.wal_manager import WalManager

_SCALAR_DATABASE_PROPERTIES: frozenset[str] = frozenset(
    {
        "attached_indexes",
        "close_complete",
        "closed",
        "identity",
        "label",
        "metrics_endpoint",
        "path",
        "read_only",
        "recovery_report",
        "stale_indexes",
        "unindexed_tables",
    }
)
"""Public database values that are already immutable scalars or frozen domain reports."""

_TRANSACTION_PROPERTIES: frozenset[str] = frozenset(
    {"active", "mode", "report", "snapshot", "txn_id"}
)
"""The complete observational surface of a public Transaction."""

_RAW_ENGINE_TYPES: tuple[type[object], ...] = (
    BufferPool,
    CatalogStore,
    ContainedMetricsSink,
    HeapStore,
    IndexManager,
    IndexStore,
    LedgerStore,
    LocalProcessCoordinator,
    LoggingEventSink,
    MemoryStorageDevice,
    NoOpMetricsSink,
    PageCodecV1,
    PureVectorMath,
    QuarantineStore,
    QueryEngine,
    SystemClock,
    TransactionContext,
    TransactionManager,
    VectorEngine,
    WalManager,
)
"""Concrete mutable collaborators that no public observation graph may contain."""

_BACKDOOR_NAMES: frozenset[str] = frozenset(
    {
        "callback",
        "delegate",
        "inner",
        "source",
        "_callback",
        "_delegate",
        "_inner",
        "_source",
    }
)
"""Conventional wrapper attributes that would make a view a collaborator proxy."""

_IMMUTABLE_LEAF_TYPES: tuple[type[object], ...] = (
    str,
    int,
    float,
    bool,
    bytes,
    type(None),
    Enum,
)
"""The complete set of non-container leaves allowed in an observation graph."""

_VIEW_BUILDER_HOOKS: tuple[tuple[str, str], ...] = (
    ("storage", "_storage_view"),
    ("clock", "_clock_view"),
    ("codec", "_codec_view"),
    ("metrics", "MetricsView"),
    ("events", "_component_view"),
    ("vector_math", "VectorMathView"),
    ("coordinator", "_coordinator_view"),
    ("pool", "_pool_view"),
    ("catalog", "_catalog_view_from"),
    ("heap", "_heap_view"),
    ("wal", "_wal_view"),
    ("transactions", "_transactions_view"),
    ("indexes", "_indexes_view"),
    ("ledger", "_ledger_view"),
    ("quarantine", "_quarantine_view"),
    ("vectors", "_vectors_view"),
    ("queries", "_queries_view"),
)
"""Every public composition property and the final builder call it must contain."""


def _public_properties(kind: type[object]) -> frozenset[str]:
    """Return the non-private properties declared directly by ``kind``."""
    return frozenset(
        name
        for name, member in vars(kind).items()
        if not name.startswith("_") and isinstance(member, property)
    )


def _walk_values(root: object) -> tuple[object, ...]:
    """Walk the finite value graph exposed by frozen dataclasses and immutable containers."""
    pending = [root]
    seen: set[int] = set()
    walked: list[object] = []
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        walked.append(value)
        if is_dataclass(value) and not isinstance(value, type):
            pending.extend(getattr(value, field.name) for field in fields(value))
        elif isinstance(value, (tuple, frozenset)):
            pending.extend(value)
    return tuple(walked)


def test_database_and_transaction_have_complete_static_public_property_allowlists() -> None:
    """A newly added property cannot silently publish another mutable collaborator."""
    component_names = frozenset(name for name, _kind in PUBLIC_DATABASE_VIEW_ALLOWLIST)
    assert _public_properties(Database) == _SCALAR_DATABASE_PROPERTIES | component_names
    assert frozenset(name for name, _hook in _VIEW_BUILDER_HOOKS) == component_names
    assert _public_properties(Transaction) == _TRANSACTION_PROPERTIES
    assert not hasattr(Transaction, "context")


@pytest.mark.parametrize(("property_name", "builder_name"), _VIEW_BUILDER_HOOKS)
def test_every_view_builder_defers_reentrant_close_until_its_snapshot_is_complete(
    property_name: str,
    builder_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A builder callback seals immediately but cannot release collaborators under itself."""
    expected_types = dict(PUBLIC_DATABASE_VIEW_ALLOWLIST)
    database = connect(":memory:")
    original = getattr(database_module, builder_name)
    calls: list[str] = []

    def close_then_build(*args: object, **kwargs: object) -> object:
        database.close()
        assert database.closed
        assert not database.close_complete, property_name
        answer = original(*args, **kwargs)
        calls.append(property_name)
        return answer

    with monkeypatch.context() as boundary:
        boundary.setattr(database_module, builder_name, close_then_build)
        view = getattr(database, property_name)

    assert type(view) is expected_types[property_name]
    assert calls == [property_name]
    assert database.close_complete


@pytest.mark.parametrize(("property_name", "builder_name"), _VIEW_BUILDER_HOOKS)
def test_close_winning_before_view_transition_refuses_before_any_builder(
    property_name: str,
    builder_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inner open check seals the precheck-to-transition race for every public view."""
    database = connect(":memory:")
    before_transition = threading.Event()
    release_view = threading.Event()
    builder_calls: list[str] = []
    failures: list[BaseException] = []
    original_transition = Database._public_transition

    def pause_before_transition(self: Database):  # noqa: ANN202
        inner = original_transition(self)

        @contextmanager
        def paused() -> Iterator[None]:
            if self is database and threading.current_thread().name == "late-view":
                before_transition.set()
                assert release_view.wait(5.0), "close did not release the late view"
            with inner:
                yield

        return paused()

    def forbidden_builder(*_args: object, **_kwargs: object) -> object:
        builder_calls.append(builder_name)
        raise AssertionError("a closed view reached its collaborator builder")

    def observe() -> None:
        try:
            getattr(database, property_name)
        except BaseException as failure:  # noqa: BLE001 - exact lifecycle evidence
            failures.append(failure)

    with monkeypatch.context() as boundary:
        boundary.setattr(Database, "_public_transition", pause_before_transition)
        boundary.setattr(database_module, builder_name, forbidden_builder)
        worker = threading.Thread(target=observe, name="late-view")
        try:
            worker.start()
            assert before_transition.wait(5.0), "view never reached its transition boundary"
            database.close()
            assert database.close_complete
        finally:
            release_view.set()
            worker.join(5.0)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], GrafxUnsupportedOperation)
    assert builder_calls == []


def test_storage_builder_close_callback_cannot_release_between_inventory_and_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reproduced list_files-to-file_size reentry returns one complete immutable view."""
    database = connect(":memory:")
    storage = database._storage
    storage_type = type(storage)
    original_list = storage_type.list_files
    reentered: list[tuple[str, ...]] = []

    def close_after_inventory(candidate: object, prefix: str = "") -> tuple[str, ...]:
        files = original_list(candidate, prefix)
        if candidate is storage and not reentered:
            reentered.append(files)
            database.close()
            assert database.closed and not database.close_complete
        return files

    with monkeypatch.context() as boundary:
        boundary.setattr(storage_type, "list_files", close_after_inventory)
        view = database.storage

    assert reentered
    assert view.list_files() == reentered[0]
    assert all(view.file_size(name) >= 0 for name in view.list_files())
    assert database.close_complete


def test_ledger_view_preserves_store_filtering_pagination_and_case_insensitivity() -> None:
    """The immutable inventory keeps every documented LedgerStore.list read spelling."""
    forensic = LedgerEntry(
        entry_id=1,
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.CHECKSUM_FAILURE,
    )
    reapplicable = LedgerEntry(
        entry_id=2,
        origin_class=LedgerOriginClass.REAPPLICABLE,
        reason=LedgerReason.STALE_EPOCH,
    )
    later_forensic = LedgerEntry(
        entry_id=3,
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.TRUNCATED_TAIL,
    )
    view = LedgerView(
        "ledger/unapplied.log",
        None,
        (forensic, reapplicable, later_forensic),
        (("forensic", 2), ("reapplicable", 1)),
    )

    assert view.list(origin_class="forensic") == (forensic, later_forensic)
    assert view.list(origin_class="FoReNsIc") == (forensic, later_forensic)
    assert view.list(origin_class=LedgerOriginClass.REAPPLICABLE) == (reapplicable,)
    assert view.list(reason="checksum_failure") == (forensic,)
    assert view.list(reason="CHECKSUM_FAILURE") == (forensic,)
    assert view.list(reason=LedgerReason.STALE_EPOCH) == (reapplicable,)
    assert view.list(limit=1, offset=1) == (reapplicable,)
    assert view.list(limit=0) == ()
    assert view.inspect(2) is reapplicable


@pytest.mark.parametrize(
    ("case", "field"),
    (
        ("origin", "origin_class"),
        ("reason", "reason"),
        ("limit_type", "limit"),
        ("limit_negative", "limit"),
        ("offset_type", "offset"),
        ("offset_negative", "offset"),
        ("inspect_type", "entry_id"),
        ("inspect_nonpositive", "entry_id"),
    ),
)
def test_ledger_view_invalid_arguments_use_store_configuration_taxonomy(
    case: str,
    field: str,
) -> None:
    """Bad public filters/counts/identifiers never become empty pages or raw slice errors."""
    view = LedgerView("ledger/unapplied.log", None, (), ())

    with pytest.raises(GrafxConfigurationError) as raised:
        if case == "origin":
            view.list(origin_class="unclassifiable")
        elif case == "reason":
            view.list(reason="something_else")
        elif case == "limit_type":
            view.list(limit=True)
        elif case == "limit_negative":
            view.list(limit=-1)
        elif case == "offset_type":
            view.list(offset=1.5)  # type: ignore[arg-type]
        elif case == "offset_negative":
            view.list(offset=-1)
        elif case == "inspect_type":
            view.inspect([])  # type: ignore[arg-type]
        else:
            view.inspect(0)

    assert raised.value.details["field"] == field


def test_ledger_view_missing_identifier_keeps_ledger_error_taxonomy() -> None:
    """A valid absent identifier is a ledger lookup failure, not a configuration refusal."""
    view = LedgerView("ledger/unapplied.log", None, (), ())

    with pytest.raises(GrafxLedgerError) as raised:
        view.inspect(7)

    assert raised.value.details == {"field": "entry_id", "entry_id": 7}


@pytest.mark.parametrize(
    ("operation", "error_type", "field"),
    (
        (lambda: StorageView("memory", 512, ()).list_files(1), GrafxConfigurationError, "prefix"),
        (lambda: StorageView("memory", 512, ()).exists([]), GrafxConfigurationError, "file"),
        (lambda: StorageView("memory", 512, ()).file_size(None), GrafxConfigurationError, "file"),
        (lambda: StorageView("memory", 512, ()).page_count({}), GrafxConfigurationError, "file"),
        (lambda: CatalogView((), ()).has_table([]), GrafxConfigurationError, "table"),
        (lambda: CatalogView((), ()).has_space({}), GrafxConfigurationError, "space"),
        (lambda: CatalogView((), ()).table(None), GrafxConfigurationError, "table"),
        (lambda: CatalogView((), ()).table_by_id(True), GrafxConfigurationError, "table_id"),
        (lambda: CatalogView((), ()).space([]), GrafxConfigurationError, "space"),
        (lambda: CatalogView((), ()).space_by_id("1"), GrafxConfigurationError, "space_id"),
        (lambda: QuarantineView("quarantine", ()).inspect([]), GrafxConfigurationError, "name"),
        (lambda: QuarantineView("quarantine", ()).inspect("../x"), GrafxConfigurationError, "name"),
        (lambda: VectorEngineView(0, (), ()).space({}), GrafxConfigurationError, "space"),
        (lambda: VectorEngineView(0, (), ()).index([]), GrafxIndexError, "space"),
        (lambda: IndexRegistryView((), 0).index([]), GrafxIndexError, "name"),
    ),
)
def test_snapshot_lookup_invalid_inputs_never_escape_raw_python_errors(
    operation: object,
    error_type: type[BaseException],
    field: str,
) -> None:
    """Every DTO lookup refuses hostile/unhashable inputs through a Grafx taxonomy."""
    with pytest.raises(error_type) as raised:
        operation()  # type: ignore[operator]

    assert getattr(raised.value, "details")["field"] == field


def test_snapshot_valid_missing_lookups_keep_component_taxonomy() -> None:
    """Validation does not collapse valid absence into the wrong component error."""
    with pytest.raises(GrafxQuarantineError):
        QuarantineView("quarantine", ()).inspect("missing")
    with pytest.raises(GrafxIndexError):
        VectorEngineView(0, (), ()).index("missing")
    with pytest.raises(GrafxIndexError):
        IndexRegistryView((), 0).index("missing")


def test_every_component_property_returns_the_allowlisted_immutable_value_graph() -> None:
    """No view retains a raw engine, mutable container, callback or conventional backdoor."""
    with connect(":memory:") as database:
        # Non-empty schema and index inventories make the walk cover the nested domain DTOs,
        # rather than proving only that an empty tuple is harmless.
        with database.begin("write") as schema:
            schema.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
            schema.execute(
                "CREATE NODE TABLE P(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )
        for name, expected_type in PUBLIC_DATABASE_VIEW_ALLOWLIST:
            view = getattr(database, name)
            assert type(view) is expected_type, name
            assert is_dataclass(view), name
            for value in _walk_values(view):
                if is_dataclass(value) and not isinstance(value, type):
                    with pytest.raises(FrozenInstanceError):
                        setattr(value, fields(value)[0].name, object())
                else:
                    assert isinstance(value, (tuple, frozenset, *_IMMUTABLE_LEAF_TYPES)), (
                        name,
                        type(value).__name__,
                    )
                assert not isinstance(value, _RAW_ENGINE_TYPES), (name, type(value).__name__)
                assert not isinstance(
                    value,
                    (Mapping, MutableSequence, MutableSet, bytearray, memoryview),
                ), (
                    name,
                    type(value).__name__,
                )
                assert not callable(value), (name, type(value).__name__)
                assert _BACKDOOR_NAMES.isdisjoint(dir(value)), (name, type(value).__name__)


def test_coordinator_view_never_enters_liveness_lease_or_retirement_doors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the view cannot prune reader pins or perturb lease/liveness bookkeeping."""
    with connect(":memory:") as database:
        coordinator = database._coordinator
        expected_owner = coordinator.owner_id()
        called: list[str] = []

        def forbidden(name: str):
            def refuse(*_args: object, **_kwargs: object) -> object:
                called.append(name)
                raise AssertionError(f"coordinator operation {name} was called by its view")

            return refuse

        operational = (
            "current_epoch",
            "acquire_writer_lease",
            "renew_lease",
            "release_lease",
            "validate_epoch",
            "detect_dead_owner",
            "takeover",
            "register_reader",
            "refresh_reader",
            "unregister_reader",
            "reader_horizon",
            "exclusive",
        )
        # Restore the real doors before Database.close releases its own resources.
        with monkeypatch.context() as boundary:
            for name in operational:
                boundary.setattr(coordinator, name, forbidden(name))
            view = database.coordinator

        assert called == []
        assert view.owner_id() == expected_owner
        assert view.implementation == type(coordinator).__name__


def test_clock_view_does_not_advance_an_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harmless property read cannot move liveness time in a stateful clock adapter."""
    with connect(":memory:") as database:
        clock = database._clock

        def refuse(_self: object) -> float:
            raise AssertionError("a clock source was advanced while building its identity view")

        # Database.close may legitimately stamp or measure cleanup, so the sentinels live only
        # across construction of the public view.
        with monkeypatch.context() as boundary:
            boundary.setattr(type(clock), "monotonic", refuse)
            boundary.setattr(type(clock), "wall", refuse)
            view = database.clock

        assert view.implementation == type(clock).__name__


@pytest.mark.parametrize("invalid", ["foreign", "finished", "not_refused"])
def test_retry_rejects_invalid_transactions_before_schema_bookkeeping(
    invalid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership, state and conflict validation all precede query-schema settlement."""
    settled: list[tuple[int, bool]] = []
    original = Database._settle_schema

    def observe(self: Database, context: TransactionContext, *, committed: bool) -> None:
        settled.append((context.txn_id, committed))
        original(self, context, committed=committed)

    monkeypatch.setattr(Database, "_settle_schema", observe)
    first = connect(":memory:")
    second = connect(":memory:")
    try:
        transaction = second.begin("write") if invalid == "foreign" else first.begin("write")
        if invalid == "finished":
            transaction.rollback()
        settled.clear()
        with pytest.raises(GrafxTransactionStateError):
            first.retry(transaction)
        assert settled == []
    finally:
        # Restore first so ordinary close/rollback bookkeeping does not pollute this assertion.
        monkeypatch.setattr(Database, "_settle_schema", original)
        first.close()
        second.close()


def test_vector_search_accepts_only_an_active_transaction_of_the_same_database() -> None:
    """The safe vector read door never accepts a raw context, snapshot or foreign transaction."""
    first = connect(":memory:")
    second = connect(":memory:")
    try:
        foreign = second.begin("read")
        with pytest.raises(GrafxTransactionStateError) as refused:
            first.search_vectors(foreign, space="missing", query=(1.0,), k=1)
        assert refused.value.details["field"] == "transaction_owner"

        finished = first.begin("read")
        finished.rollback()
        with pytest.raises(GrafxTransactionStateError):
            first.search_vectors(finished, space="missing", query=(1.0,), k=1)
    finally:
        first.close()
        second.close()


def test_retry_still_returns_a_fresh_public_transaction_after_a_real_conflict() -> None:
    """Sealing TransactionContext does not cost the documented optimistic retry loop."""
    with connect(":memory:") as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        with database.begin("write") as seed:
            seed.execute("CREATE (:P {id: 1, name: 'seed'})")

        winner = database.begin("write")
        loser = database.begin("write")
        winner.execute("MATCH (p:P {id: 1}) SET p.name = 'winner'")
        loser.execute("MATCH (p:P {id: 1}) SET p.name = 'loser'")
        winner.commit()
        with pytest.raises(GrafxWriteConflict):
            loser.commit()

        successor = database.retry(loser)
        assert loser.active is False
        assert successor.active is True
        assert successor.snapshot.read_lsn > loser.snapshot.read_lsn
        successor.execute("MATCH (p:P {id: 1}) SET p.name = 'retried'")
        successor.commit()
        assert database.execute("MATCH (p:P {id: 1}) RETURN p.name").rows == (("retried",),)
