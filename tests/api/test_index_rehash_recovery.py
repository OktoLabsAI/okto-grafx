"""Crash/recovery boundaries for growth-only exact-index rehash.

These cases intentionally use tiny graphs and a short phase matrix.  The global recovery
suite already cuts every generic commit write point; this file pins the rehash-specific
contract instead: an unreachable shadow is never authority, catalog publication resolves to
one complete generation, and logical index WAL after a rehash can only mutate the new ACTIVE
generation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from okto_grafx import Database, connect
from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.events_logging import LoggingEventSink
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.storage_fault import (
    FaultInjectingStorageDevice,
    SimulatedCrash,
    WritePoint,
)
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index import IndexGenerationState, change_of
from okto_grafx.domain.txn import WalRecordType
from okto_grafx.runtime.bootstrap import coordinator_settings, install_checksum
from okto_grafx.runtime.config import DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry


PAGE_SIZE = 512
SEED = 20260903
INDEX_NAME = "by_email"


def _registry(
    storage: FaultInjectingStorageDevice, *, namespace: object
) -> PortRegistry:
    """Compose a fresh process facade over one persistent in-memory namespace."""

    config = DatabaseConfig(path=":memory:", page_size=PAGE_SIZE)
    install_checksum(config)
    clock = SystemClock()
    metrics = NoOpMetricsSink()
    registry = PortRegistry()
    registry.bind("storage", storage)
    registry.bind("clock", clock)
    registry.bind("codec", PageCodecV1(PAGE_SIZE))
    registry.bind("metrics", metrics)
    registry.bind("events", LoggingEventSink())
    registry.bind("vector_math", PureVectorMath())
    registry.bind(
        "coordinator",
        LocalProcessCoordinator(
            storage,
            clock,
            namespace=namespace,
            metrics=metrics,
            **coordinator_settings(config),
        ),
    )
    return registry


def _connect(storage: FaultInjectingStorageDevice, *, namespace: object) -> Database:
    """Open a new Database object without replacing the underlying bytes."""

    return connect(
        ":memory:",
        page_size=PAGE_SIZE,
        registry=_registry(storage, namespace=namespace),
    )


def _seed_people(database: Database) -> None:
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Person(id INT64, email STRING, PRIMARY KEY(id))"
        )
    with database.begin("write") as rows:
        rows.execute("CREATE (:Person {id: 1, email: 'ada@example.test'})")
        rows.execute("CREATE (:Person {id: 2, email: 'grace@example.test'})")


def _prepared_fault_database() -> tuple[
    MemoryStorageDevice,
    FaultInjectingStorageDevice,
    Database,
    int,
    str,
]:
    """Return a v2 database with one durable eight-bucket custom index."""

    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = FaultInjectingStorageDevice(memory, seed=SEED)
    with _connect(fault, namespace=memory) as setup:
        _seed_people(setup)
        original = setup.create_index(
            INDEX_NAME,
            "Person",
            ("email",),
            bucket_count=8,
        )
        assert original.active_nonce is not None
        original_nonce = original.active_nonce
        original_file = original.file
    return (
        memory,
        fault,
        _connect(fault, namespace=memory),
        original_nonce,
        original_file,
    )


def _file_bytes(storage: MemoryStorageDevice, file: str) -> bytes:
    return bytes(storage.read_log(file, 0, storage.log_size(file)))


def _semantic_entries(index: object) -> frozenset[tuple[object, ...]]:
    return frozenset(
        (
            entry.key,
            entry.ref,
            entry.versioned,
            entry.born_csn,
            entry.dead_csn,
        )
        for entry in index.walk()
    )


def _only(
    points: tuple[WritePoint, ...], predicate: Callable[[WritePoint], bool]
) -> WritePoint:
    selected = tuple(point for point in points if predicate(point))
    assert selected, points
    return selected[-1]


def _survey_rehash_points() -> tuple[tuple[str, WritePoint], ...]:
    """Locate one cut on each rehash-specific side of the durable decision."""

    memory, fault, database, _nonce, _file = _prepared_fault_database()
    try:
        points = fault.enumerate_write_points(
            lambda _device: database.rehash_index(INDEX_NAME, bucket_count=16)
        )
        shadow = _only(
            points,
            lambda point: (
                point.method == "durable_barrier"
                and bool(point.file)
                and str(point.file).startswith("index/g_")
            ),
        )
        wal = _only(
            points,
            lambda point: (
                point.method == "durable_barrier"
                and bool(point.file)
                and str(point.file).startswith("wal/")
            ),
        )
        catalog = _only(
            points,
            lambda point: point.method == "write_page" and point.file == "catalog.dat",
        )
        assert shadow.call_index < wal.call_index < catalog.call_index
        return (("shadow", shadow), ("wal", wal), ("catalog", catalog))
    finally:
        database.close()
        memory.close()


def _survey_post_rehash_heap_write() -> WritePoint:
    """Locate the first data apply after a post-rehash DML COMMIT is durable."""

    memory, fault, database, _nonce, _file = _prepared_fault_database()
    try:
        database.rehash_index(INDEX_NAME, bucket_count=16)
        database.close()
        database = _connect(fault, namespace=memory)
        transaction = database.begin("write")
        transaction.execute("CREATE (:Person {id: 3, email: 'barbara@example.test'})")
        points = fault.enumerate_write_points(lambda _device: transaction.commit())
        wal_barriers = tuple(
            point
            for point in points
            if (
                point.method == "durable_barrier"
                and bool(point.file)
                and str(point.file).startswith("wal/")
            )
        )
        assert len(wal_barriers) >= 2, points
        user_barrier = wal_barriers[-1]
        heap_writes = tuple(
            point
            for point in points
            if (
                point.call_index > user_barrier.call_index
                and point.method == "write_page"
                and point.file == "heap.dat"
            )
        )
        assert heap_writes, points
        first_data_apply = heap_writes[0]
        assert not any(
            point.method == "write_page"
            and bool(point.file)
            and str(point.file).startswith("index/g_")
            and user_barrier.call_index < point.call_index < first_data_apply.call_index
            for point in points
        ), points
        return first_data_apply
    finally:
        database.close()
        memory.close()


@pytest.fixture(scope="module")
def rehash_critical_points() -> tuple[tuple[str, WritePoint], ...]:
    return _survey_rehash_points()


@pytest.fixture(scope="module")
def post_rehash_heap_write() -> WritePoint:
    return _survey_post_rehash_heap_write()


def _assert_complete_authority(
    database: Database,
    *,
    original_nonce: int,
    original_file: str,
) -> str:
    """Assert a cold open selected the complete old or complete new authority."""

    logical = database._catalog.catalog.index_definition(INDEX_NAME)
    active = logical.active_generation()
    assert active is not None
    assert (
        sum(
            generation.state is IndexGenerationState.ACTIVE
            for generation in logical.generations
        )
        == 1
    )
    assert database._storage.exists(active.file)
    store = database._indexes.active_index(
        INDEX_NAME, catalog=database._catalog.catalog
    )
    store.open()
    assert len(_semantic_entries(store)) == 2
    assert database.execute(
        "MATCH (p:Person) WHERE p.email = 'grace@example.test' RETURN p.id"
    ).rows == ((2,),)
    assert database.verify("all").findings == ()

    if active.bucket_count == 8:
        assert active.artifact_nonce == original_nonce
        assert active.file == original_file
        assert len(logical.generations) == 1
        return "old"

    assert active.bucket_count == 16
    assert active.artifact_nonce != original_nonce
    assert active.file != original_file
    assert logical.generation(original_nonce).state is IndexGenerationState.STALE
    assert database._storage.exists(original_file)
    return "new"


def test_complete_shadow_failure_keeps_old_authority_and_retry_uses_new_nonce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "database"
    with connect(root, page_size=PAGE_SIZE) as database:
        _seed_people(database)
        original = database.create_index(
            INDEX_NAME,
            "Person",
            ("email",),
            bucket_count=8,
        )
        assert original.active_nonce is not None
        before_catalog = database._catalog.read_from_pages().serialize()
        before_state = database._transactions.published_state()
        before_lsn = database.wal.last_lsn

        manager_type = type(database._indexes)
        original_build = manager_type._build_detached_exact_generation
        failed: list[tuple[int, str]] = []

        def fail_after_complete_shadow(
            manager: object,
            definition: object,
            through_lsn: int,
        ) -> object:
            original_build(manager, definition, through_lsn)
            failed.append(
                (
                    int(getattr(definition, "artifact_nonce")),
                    str(getattr(definition, "file")),
                )
            )
            raise GrafxIndexError("injected failure after complete rehash shadow")

        monkeypatch.setattr(
            manager_type,
            "_build_detached_exact_generation",
            fail_after_complete_shadow,
        )
        with pytest.raises(GrafxIndexError, match="injected failure"):
            database.rehash_index(INDEX_NAME, bucket_count=16)

        failed_nonce, failed_file = failed[0]
        persisted = database._catalog.read_from_pages()
        assert persisted.serialize() == before_catalog
        assert persisted.index_definition(
            INDEX_NAME
        ).active_generation().artifact_nonce == (original.active_nonce)
        assert database._transactions.published_state() == before_state
        assert database.wal.last_lsn == before_lsn
        assert database._storage.exists(failed_file)
        assert database._transactions.open_transactions == 0

        monkeypatch.setattr(
            manager_type,
            "_build_detached_exact_generation",
            original_build,
        )
        grown = database.rehash_index(INDEX_NAME, bucket_count=16)
        assert grown.active_nonce not in {original.active_nonce, failed_nonce}
        assert grown.file != failed_file
        assert database._storage.exists(failed_file)
        logical = database._catalog.catalog.index_definition(INDEX_NAME)
        assert logical.generation(original.active_nonce).state is (
            IndexGenerationState.STALE
        )
        assert logical.generation(grown.active_nonce).state is (
            IndexGenerationState.ACTIVE
        )
        assert database.verify("all").findings == ()


def test_rehash_critical_crash_points_cold_open_to_old_or_new_complete_authority(
    rehash_critical_points: tuple[tuple[str, WritePoint], ...],
) -> None:
    outcomes: dict[str, str] = {}
    for phase, point in rehash_critical_points:
        memory, fault, crashed, original_nonce, original_file = (
            _prepared_fault_database()
        )
        try:
            fault.clear_trail()
            fault.crash_at(point.call_index, moment="after")
            with pytest.raises(SimulatedCrash) as stopped:
                crashed.rehash_index(INDEX_NAME, bucket_count=16)
            fault.disarm()
            assert stopped.value.sequence == point.call_index
            assert stopped.value.method == point.method

            with _connect(fault, namespace=memory) as recovered:
                outcomes[phase] = _assert_complete_authority(
                    recovered,
                    original_nonce=original_nonce,
                    original_file=original_file,
                )
        finally:
            memory.close()

    assert outcomes == {"shadow": "old", "wal": "new", "catalog": "new"}


def test_post_wal_dml_recovery_resolves_logical_name_only_to_new_active(
    post_rehash_heap_write: WritePoint,
) -> None:
    memory, fault, database, original_nonce, original_file = _prepared_fault_database()
    try:
        grown = database.rehash_index(INDEX_NAME, bucket_count=16)
        assert grown.active_nonce is not None
        stale_before = _file_bytes(memory, original_file)
        active_before = _semantic_entries(
            database._indexes.active_index(
                INDEX_NAME, catalog=database._catalog.catalog
            )
        )
        before_lsn = database.wal.last_lsn
        database.close()

        crashed = _connect(fault, namespace=memory)
        transaction = crashed.begin("write")
        transaction.execute("CREATE (:Person {id: 3, email: 'barbara@example.test'})")
        fault.clear_trail()
        fault.crash_at(post_rehash_heap_write.call_index, moment="before")
        with pytest.raises(SimulatedCrash) as stopped:
            transaction.commit()
        fault.disarm()
        assert stopped.value.file == "heap.dat"
        assert any(
            call.method == "durable_barrier"
            and call.outcome == "ok"
            and bool(call.file)
            and str(call.file).startswith("wal/")
            and call.sequence < stopped.value.sequence
            for call in fault.trail()
        )

        with _connect(fault, namespace=memory) as recovered:
            assert recovered.recovery_report.records_replayed > 0
            logical = recovered._catalog.catalog.index_definition(INDEX_NAME)
            assert logical.generation(original_nonce).state is (
                IndexGenerationState.STALE
            )
            assert logical.generation(grown.active_nonce).state is (
                IndexGenerationState.ACTIVE
            )
            assert _file_bytes(memory, original_file) == stale_before
            active_after = _semantic_entries(
                recovered._indexes.active_index(
                    INDEX_NAME, catalog=recovered._catalog.catalog
                )
            )
            assert recovered.execute(
                "MATCH (p:Person) WHERE p.email = 'barbara@example.test' RETURN p.id"
            ).rows == ((3,),)

            changes = tuple(
                change_of(record)
                for record in recovered._wal.read_from(before_lsn)
                if record.record_type == int(WalRecordType.INDEX_WRITE)
            )
            custom = tuple(
                change for change in changes if change.index.lower() == INDEX_NAME
            )
            assert len(custom) == 1
            assert custom[0].index == INDEX_NAME
            assert grown.file not in {change.index for change in changes}
            assert str(grown.active_nonce) not in {change.index for change in changes}
            assert len(active_after) == len(active_before) + 1
            assert recovered.verify("all").findings == ()
    finally:
        memory.close()
