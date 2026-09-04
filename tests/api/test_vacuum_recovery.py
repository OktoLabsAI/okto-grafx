"""Focused crash/recovery matrix for the quiescent vacuum transaction."""

from __future__ import annotations

from collections.abc import Callable

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
from okto_grafx.runtime.bootstrap import coordinator_settings, install_checksum
from okto_grafx.runtime.config import DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry


PAGE_SIZE = 512
SEED = 20260904


def registry(
    storage: FaultInjectingStorageDevice, *, namespace: object
) -> PortRegistry:
    config = DatabaseConfig(path=":memory:", page_size=PAGE_SIZE)
    install_checksum(config)
    clock = SystemClock()
    metrics = NoOpMetricsSink()
    ports = PortRegistry()
    ports.bind("storage", storage)
    ports.bind("clock", clock)
    ports.bind("codec", PageCodecV1(PAGE_SIZE))
    ports.bind("metrics", metrics)
    ports.bind("events", LoggingEventSink())
    ports.bind("vector_math", PureVectorMath())
    ports.bind(
        "coordinator",
        LocalProcessCoordinator(
            storage,
            clock,
            namespace=namespace,
            metrics=metrics,
            **coordinator_settings(config),
        ),
    )
    return ports


def open_database(
    storage: FaultInjectingStorageDevice, *, namespace: object
) -> Database:
    return connect(
        ":memory:",
        page_size=PAGE_SIZE,
        partitions_per_table=8,
        registry=registry(storage, namespace=namespace),
    )


def prepared_database() -> tuple[
    MemoryStorageDevice,
    FaultInjectingStorageDevice,
    Database,
]:
    """Return capability-active indexed history awaiting its first physical reclaim."""

    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = FaultInjectingStorageDevice(memory, seed=SEED)
    with open_database(fault, namespace=memory) as setup:
        with setup.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
        setup.ensure_identity_indexes()
        activation = setup.maintenance.vacuum(confirm_quiescent=True)
        assert activation.capability_activated is True
        assert activation.reclaimed_versions == 0
        for statement in (
            "CREATE (:Person {id: 1, name: 'old'})",
            "MATCH (p:Person {id: 1}) SET p.name = 'middle'",
            "MATCH (p:Person {id: 1}) SET p.name = 'current'",
        ):
            with setup.begin("write") as writer:
                writer.execute(statement)
    return memory, fault, open_database(fault, namespace=memory)


def only(
    points: tuple[WritePoint, ...], predicate: Callable[[WritePoint], bool]
) -> WritePoint:
    selected = tuple(point for point in points if predicate(point))
    assert selected, points
    return selected[0]


def survey_points() -> tuple[tuple[str, WritePoint, str], ...]:
    memory, fault, database = prepared_database()
    try:
        points = fault.enumerate_write_points(
            lambda _device: database.maintenance.vacuum(confirm_quiescent=True)
        )
        barrier = only(
            points,
            lambda point: (
                point.method == "durable_barrier"
                and bool(point.file)
                and str(point.file).startswith("wal/")
            ),
        )
        append = tuple(
            point
            for point in points
            if point.method == "append_log" and point.call_index < barrier.call_index
        )[-1]
        heap = only(
            points,
            lambda point: (
                point.method == "write_page"
                and point.file == "heap.dat"
                and point.call_index > barrier.call_index
            ),
        )
        index = only(
            points,
            lambda point: (
                point.method == "write_page"
                and bool(point.file)
                and str(point.file).startswith("index/")
                and point.call_index > barrier.call_index
            ),
        )
        publication = only(
            points,
            lambda point: (
                point.method == "write_page" and point.file == "control/commit.state"
            ),
        )
        return (
            ("before_wal_commit", append, "before"),
            ("after_wal_barrier", barrier, "after"),
            ("during_heap_apply", heap, "after"),
            ("during_index_apply", index, "after"),
            ("after_publication", publication, "after"),
        )
    finally:
        database.close()
        memory.close()


@pytest.fixture(scope="module")
def vacuum_crash_points() -> tuple[tuple[str, WritePoint, str], ...]:
    return survey_points()


def test_vacuum_crash_windows_recover_to_complete_pre_or_post_state(
    vacuum_crash_points: tuple[tuple[str, WritePoint, str], ...],
) -> None:
    outcomes: dict[str, int] = {}
    for phase, point, moment in vacuum_crash_points:
        memory, fault, crashed = prepared_database()
        try:
            fault.clear_trail()
            fault.crash_at(point.call_index, moment=moment)
            with pytest.raises(SimulatedCrash) as stopped:
                crashed.maintenance.vacuum(confirm_quiescent=True)
            fault.disarm()
            assert stopped.value.sequence == point.call_index
            assert stopped.value.method == point.method

            with open_database(fault, namespace=memory) as recovered:
                table = recovered._catalog.catalog.table("Person")
                stored = tuple(recovered._heap.scan_all(table))
                outcomes[phase] = len(stored)
                assert len(stored) in {1, 3}
                assert recovered.execute(
                    "MATCH (p:Person) RETURN p.name AS name"
                ).rows == (("current",),)
                assert recovered.verify("all").findings == ()

                # Recovery and vacuum are both idempotent after either complete outcome.
                again = recovered.maintenance.vacuum(confirm_quiescent=True)
                assert again.reclaimed_versions == (len(stored) - 1)
                assert recovered.verify("all").findings == ()
        finally:
            memory.close()

    assert outcomes["before_wal_commit"] == 3
    assert all(
        count == 1 for phase, count in outcomes.items() if phase != "before_wal_commit"
    )
