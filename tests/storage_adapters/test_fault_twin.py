"""The deterministic fault bench (C2, FR-16, AC-4, AC-10, AC-11, TS-4, TS-5, TS-10, TS-11).

These tests do two jobs. They prove that every fault the bench claims to inject really happens,
and they pin the shape of the evidence the later components will assert on: the call trail, the
write point enumeration and the crash exception itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from okto_grafx.adapters.storage_fault import (
    WRITE_POINT_METHODS,
    CallRecord,
    FaultInjectingStorageDevice,
    FaultPlan,
    SimulatedCrash,
    WritePoint,
)
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ports import StorageDevice

PAGE_SIZE: int = 512
"""Kept equal to the page size of the fixtures; a drift fails the device shape test."""

SEGMENT: str = "wal/000000000001.wal"
OTHER_SEGMENT: str = "wal/000000000002.wal"
HEAP: str = "heap.dat"
STATE: str = "control/commit.state"
RECORD: bytes = b"COMMIT-RECORD-0001"


def _twin(seed: int = 20260819, plan: FaultPlan | None = None) -> FaultInjectingStorageDevice:
    """Return a bench over an in-memory device, which keeps every test independent of the disk."""
    return FaultInjectingStorageDevice(MemoryStorageDevice(page_size=PAGE_SIZE), seed=seed, plan=plan)


def _commit_workload(device: Any) -> None:
    """One durable commit as the engine will perform it: append, barrier, then publish."""
    if not device.exists(SEGMENT):
        device.create(SEGMENT)
    if not device.exists(HEAP):
        device.create(HEAP)
        device.allocate(HEAP)
    device.append_log(SEGMENT, RECORD)
    device.durable_barrier(SEGMENT)
    device.write_page(HEAP, 0, bytes([0x11]) * PAGE_SIZE)
    device.create(f"{STATE}.new")
    device.append_log(f"{STATE}.new", b"lsn=1")
    device.atomic_replace(f"{STATE}.new", STATE)


# --- shape -------------------------------------------------------------------------------


def test_the_bench_satisfies_the_port_and_forwards_its_identity() -> None:
    bench = _twin()
    assert isinstance(bench, StorageDevice)
    assert bench.name == "fault"
    assert bench.page_size == PAGE_SIZE
    assert bench.seed == 20260819
    # The wrapped device is reachable and really is the one being written through.
    bench.create(SEGMENT)
    bench.append_log(SEGMENT, RECORD)
    assert bench.inner.read_log(SEGMENT, 0, len(RECORD)) == RECORD


def test_a_simulated_crash_is_not_an_ordinary_exception() -> None:
    # The bench must survive an "except Exception" anywhere in the engine, otherwise a crash
    # test would prove nothing at all about recovery.
    caught: str = "nothing"
    try:
        raise SimulatedCrash("stop", sequence=1, method="append_log", file=SEGMENT, moment="before")
    except Exception:  # pragma: no cover - the defect this test exists for
        caught = "swallowed"
    except SimulatedCrash:
        caught = "crash"
    assert caught == "crash"
    assert not isinstance(
        SimulatedCrash("s", sequence=1, method="append_log", file=None, moment="before"), Exception
    )


def test_the_bench_refuses_a_plan_it_could_not_honour() -> None:
    for invalid in (
        {"crash_call_index": 0},
        {"crash_call_index": -3},
        {"crash_occurrence": 0},
        {"crash_method": "flush"},
        {"crash_moment": "midway"},
        {"partial_write_bytes": -1},
        {"device_full_method": "append"},
    ):
        with pytest.raises(GrafxUnsupportedOperation):
            FaultPlan(**invalid)
    with pytest.raises(GrafxUnsupportedOperation):
        FaultInjectingStorageDevice(MemoryStorageDevice(), seed="7")
    with pytest.raises(GrafxUnsupportedOperation):
        _twin().arm("crash everything")


# --- the call trail ------------------------------------------------------------------------


def test_the_trail_records_every_call_in_order() -> None:
    bench = _twin()
    _commit_workload(bench)
    trail = bench.trail()
    assert [record.sequence for record in trail] == list(range(1, len(trail) + 1))
    assert bench.methods() == (
        "exists",
        "create",
        "exists",
        "create",
        "allocate",
        "append_log",
        "durable_barrier",
        "write_page",
        "create",
        "append_log",
        "atomic_replace",
    )
    appended = bench.calls_of("append_log")
    assert appended[0].file == SEGMENT
    assert appended[0].args_summary == f"bytes={len(RECORD)}"
    assert all(record.outcome == "ok" for record in trail)


def test_the_trail_proves_no_acknowledgement_precedes_the_barrier() -> None:
    # AC-11 and TS-11 read exactly this: the publication of the commit state can only appear
    # after the barrier that made the record durable.
    bench = _twin()
    _commit_workload(bench)
    methods = bench.methods()
    barrier = methods.index("durable_barrier")
    append = methods.index("append_log")
    publish = methods.index("atomic_replace")
    assert append < barrier < publish


def test_the_trail_can_be_cleared_and_renumbered() -> None:
    bench = _twin()
    _commit_workload(bench)
    bench.clear_trail()
    assert bench.trail() == ()
    bench.exists(SEGMENT)
    assert bench.trail()[0].sequence == 1


# --- enumeration and crash at every write point --------------------------------------------


def test_enumerate_write_points_returns_only_the_calls_that_change_the_device() -> None:
    bench = _twin()
    points = bench.enumerate_write_points(_commit_workload)
    # The exact enumeration of this workload: every mutating call, at the call index a crash
    # run addresses it by, and none of the reads that sit between them.
    assert tuple((point.call_index, point.method) for point in points) == (
        (2, "create"),
        (4, "create"),
        (5, "allocate"),
        (6, "append_log"),
        (7, "durable_barrier"),
        (8, "write_page"),
        (9, "create"),
        (10, "append_log"),
        (11, "atomic_replace"),
    )
    assert WRITE_POINT_METHODS.isdisjoint({"exists", "read_log", "read_page", "log_size"})


def test_the_survey_run_really_executes_and_leaves_no_fault_behind() -> None:
    # The enumeration claims the workload really runs, so it must not be held back by a
    # reordering or a volatile window left armed by an earlier scenario.
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    bench.create(OTHER_SEGMENT)
    bench.append_log(OTHER_SEGMENT, b"only-a-lie-acknowledged-this")
    assert bench.volatile_files() == (OTHER_SEGMENT,)
    bench.start_reordering()
    points = bench.enumerate_write_points(_commit_workload)
    assert len(points) == 9
    # The window the caller had is back: dropping it would let bytes that only a lying barrier
    # acknowledged survive a crash, which is the very thing AC-11 uses this bench to refute.
    assert bench.volatile_files() == (OTHER_SEGMENT,)
    bench.crash_on("page_count")
    with pytest.raises(SimulatedCrash):
        bench.page_count(OTHER_SEGMENT)
    assert bench.inner.exists(OTHER_SEGMENT) is False
    assert bench.inner.log_size(SEGMENT) == len(RECORD)
    assert bench.inner.exists(STATE) is True
    # The scenario the caller had armed is back in force afterwards, reordering included.
    assert bench.plan.lying_barrier is True
    bench.append_log(SEGMENT, b"after")
    assert bench.pending_writes() != (), "the reordering of the caller was not restored"


def test_a_crash_can_be_injected_at_every_write_point_of_a_workload() -> None:
    # TS-4 and AC-4 need exactly this loop: learn the points once, then replay crashing at each.
    survey = _twin()
    points = survey.enumerate_write_points(_commit_workload)
    assert [point.method for point in points] == [
        "create",
        "create",
        "allocate",
        "append_log",
        "durable_barrier",
        "write_page",
        "create",
        "append_log",
        "atomic_replace",
    ]
    for point in points:
        bench = _twin(plan=FaultPlan(crash_call_index=point.call_index))
        with pytest.raises(SimulatedCrash) as raised:
            _commit_workload(bench)
        assert raised.value.sequence == point.call_index
        assert raised.value.method == point.method
        assert raised.value.moment == "before"
        trail = bench.trail()
        assert len(trail) == point.call_index
        assert trail[-1].outcome == "crash_before"
        assert [record.method for record in trail] == [
            record.method for record in survey.trail()[: point.call_index]
        ]


def test_a_crash_before_the_effect_leaves_the_device_untouched() -> None:
    bench = _twin(plan=FaultPlan(crash_method="append_log", crash_moment="before"))
    bench.create(SEGMENT)
    with pytest.raises(SimulatedCrash):
        bench.append_log(SEGMENT, RECORD)
    assert bench.inner.log_size(SEGMENT) == 0


def test_a_crash_after_the_effect_leaves_the_bytes_behind() -> None:
    bench = _twin(plan=FaultPlan(crash_method="append_log", crash_moment="after"))
    bench.create(SEGMENT)
    with pytest.raises(SimulatedCrash) as raised:
        bench.append_log(SEGMENT, RECORD)
    assert raised.value.moment == "after"
    assert bench.inner.log_size(SEGMENT) == len(RECORD)
    assert bench.trail()[-1].outcome == "crash_after"


def test_a_crash_can_select_the_nth_call_of_one_method() -> None:
    bench = _twin(plan=FaultPlan(crash_method="append_log", crash_occurrence=3))
    bench.create(SEGMENT)
    bench.append_log(SEGMENT, b"one")
    bench.append_log(SEGMENT, b"two")
    with pytest.raises(SimulatedCrash) as raised:
        bench.append_log(SEGMENT, b"three")
    assert raised.value.sequence == 4
    assert bench.inner.log_size(SEGMENT) == 6


# --- partial writes (A18d) --------------------------------------------------------------------


def test_a_partial_append_stores_its_prefix_and_still_refuses_the_write() -> None:
    # A18d: a short write belongs below the port, so what the caller sees is the failure the
    # port defines, while the fragment stays on the device for recovery to find.
    bench = _twin()
    bench.create(SEGMENT)
    bench.write_partially(4, method="append_log")
    with pytest.raises(GrafxDeviceFull) as raised:
        bench.append_log(SEGMENT, RECORD)
    assert raised.value.retryable is True
    assert raised.value.details["stored"] == 4
    assert bench.inner.log_size(SEGMENT) == 4
    assert bench.inner.read_log(SEGMENT, 0, 4) == RECORD[:4]
    assert bench.trail()[-1].outcome == "partial_write"


def test_the_trail_reports_the_real_failure_of_a_torn_page_write() -> None:
    # A18e: the read that builds the torn image is part of serving the call, so its failure is
    # the outcome of the call, not the injected label.
    bench = _twin()
    bench.create(HEAP)
    bench.write_partially(8, method="write_page")
    with pytest.raises(GrafxCorruptionDetected):
        bench.write_page(HEAP, 0, bytes([0xBB]) * PAGE_SIZE)
    assert bench.trail()[-1].outcome == "corruption_detected"


def test_a_partial_page_write_tears_the_page_in_half() -> None:
    bench = _twin()
    bench.create(HEAP)
    bench.allocate(HEAP)
    bench.write_page(HEAP, 0, bytes([0xAA]) * PAGE_SIZE)
    bench.write_partially(8, method="write_page", occurrence=2)
    bench.write_page(HEAP, 0, bytes([0xBB]) * PAGE_SIZE)
    stored = bench.inner.read_page(HEAP, 0)
    assert stored[:8] == bytes([0xBB]) * 8
    assert stored[8:] == bytes([0xAA]) * (PAGE_SIZE - 8)


# --- device full ------------------------------------------------------------------------------


def test_the_bench_fills_the_device_at_a_chosen_call() -> None:
    # TS-10 and AC-10: the append is refused, nothing partial is stored, and the failure is
    # retryable so the caller knows the transaction may be tried again after freeing space.
    bench = _twin()
    bench.create(SEGMENT)
    bench.append_log(SEGMENT, b"first")
    bench.fill_device_on("append_log", occurrence=2)
    with pytest.raises(GrafxDeviceFull) as raised:
        bench.append_log(SEGMENT, RECORD)
    assert raised.value.retryable is True
    assert bench.inner.log_size(SEGMENT) == 5
    assert bench.trail()[-1].outcome == "device_full"
    # Space is back: the very next append succeeds.
    bench.disarm()
    assert bench.append_log(SEGMENT, b"second") == 11


def test_the_bench_fills_the_device_at_an_absolute_call_index() -> None:
    bench = _twin()
    bench.create(SEGMENT)
    bench.fill_device_at(3)
    bench.append_log(SEGMENT, b"first")
    with pytest.raises(GrafxDeviceFull):
        bench.append_log(SEGMENT, b"second")


def test_a_memory_device_with_a_capacity_reports_a_full_device_on_its_own() -> None:
    device = MemoryStorageDevice(page_size=PAGE_SIZE, capacity_bytes=PAGE_SIZE)
    device.create(HEAP)
    device.allocate(HEAP)
    with pytest.raises(GrafxDeviceFull) as raised:
        device.allocate(HEAP)
    assert raised.value.retryable is True
    assert device.page_count(HEAP) == 1


# --- reordering (A18b, A18c) ---------------------------------------------------------------------


def test_reordering_is_read_coherent() -> None:
    # A18c: a reordering device still answers reads with what it was given. Hiding the bytes
    # would make a WAL reader see a torn tail that the engine never produced.
    bench = _twin(seed=11)
    bench.create(SEGMENT)
    bench.start_reordering()
    for index in range(6):
        assert bench.append_log(SEGMENT, bytes([0x30 + index])) == index + 1
    assert bench.read_log(SEGMENT, 0, 6) == b"012345"
    assert bench.log_size(SEGMENT) == 6
    assert bench.inner.read_log(SEGMENT, 0, 6) == b"012345"
    assert len(bench.pending_writes()) == 6


def test_a_crash_while_reordering_keeps_a_seeded_part_of_the_writes() -> None:
    def _run(seed: int) -> bytes:
        bench = _twin(seed=seed)
        bench.create(SEGMENT)
        bench.durable_barrier(SEGMENT)
        bench.start_reordering()
        for index in range(8):
            bench.append_log(SEGMENT, bytes([0x30 + index]))
        bench.crash_on("log_size")
        with pytest.raises(SimulatedCrash):
            bench.log_size(SEGMENT)
        return bench.inner.read_log(SEGMENT, 0, 8)

    survived = _run(11)
    # A18(c) is about a TAIL still sitting in the cache when the power goes: what survives is
    # the writes issued first, not an arbitrary prefix-shaped remainder. Naming the exact bytes
    # is what separates losing the tail from losing the head.
    assert survived == b"012"
    assert 0 < len(survived) < 8, "a reordering crash loses the tail, not everything"
    assert _run(11) == survived
    assert _run(12) != survived


def test_an_honest_barrier_flushes_the_reorder_window() -> None:
    # A18b: real hardware empties its write cache on fsync. A device that keeps reordering
    # across a barrier would make a correct engine look like it lost a confirmed commit.
    bench = _twin(seed=3)
    bench.create(SEGMENT)
    bench.start_reordering()
    for index in range(6):
        bench.append_log(SEGMENT, bytes([0x30 + index]))
    bench.durable_barrier(SEGMENT)
    assert bench.pending_writes() == ()
    bench.crash_on("log_size")
    with pytest.raises(SimulatedCrash):
        bench.log_size(SEGMENT)
    assert bench.inner.read_log(SEGMENT, 0, 6) == b"012345"


def test_reordering_can_be_armed_and_flushed_by_call_index() -> None:
    bench = _twin(seed=5, plan=FaultPlan(reorder_from_call_index=2, reorder_flush_call_index=6))
    bench.create(SEGMENT)
    for index in range(4):
        bench.append_log(SEGMENT, bytes([0x30 + index]))
    assert len(bench.pending_writes()) == 4
    assert bench.log_size(SEGMENT) == 4
    assert bench.pending_writes() == ()


def test_a_lying_barrier_still_refuses_a_file_the_device_does_not_hold() -> None:
    # The injected fault is a lie about persistence, not about which files exist.
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    with pytest.raises(GrafxDurabilityBarrierFailed) as raised:
        bench.durable_barrier("absent.wal")
    assert raised.value.details["reason"] == "missing_file"
    assert bench.trail()[-1].outcome == "durability_barrier_failed"
    bench.create(SEGMENT)
    bench.durable_barrier(SEGMENT)
    assert bench.trail()[-1].outcome == "ok"


# --- interior zero runs ----------------------------------------------------------------------------


def test_interior_zero_runs_keep_the_size_and_the_records_after_the_hole() -> None:
    # TS-5: the documented signature of the reference engine, reproduced on purpose.
    bench = _twin()
    bench.create(SEGMENT)
    head = b"HEAD" * 4
    tail = b"TAIL" * 4
    bench.append_log(SEGMENT, head + bytes(PAGE_SIZE) + tail)
    size = bench.log_size(SEGMENT)
    assert bench.inject_interior_zeros(SEGMENT, len(head), PAGE_SIZE) == PAGE_SIZE
    assert bench.log_size(SEGMENT) == size
    assert bench.read_log(SEGMENT, 0, len(head)) == head
    assert bench.read_log(SEGMENT, len(head), PAGE_SIZE) == bytes(PAGE_SIZE)
    assert bench.read_log(SEGMENT, len(head) + PAGE_SIZE, len(tail)) == tail


def test_an_interior_zero_run_defaults_to_one_page_and_stays_inside_the_file() -> None:
    bench = _twin()
    bench.create(SEGMENT)
    bench.append_log(SEGMENT, b"A" * (3 * PAGE_SIZE))
    assert bench.inject_interior_zeros(SEGMENT, PAGE_SIZE) == PAGE_SIZE
    assert bench.read_log(SEGMENT, PAGE_SIZE, PAGE_SIZE) == bytes(PAGE_SIZE)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        bench.inject_interior_zeros(SEGMENT, 3 * PAGE_SIZE, PAGE_SIZE)
    assert raised.value.details["reason"] == "run_past_end"
    for offset, length in ((-1, 4), (0, 0), (0, -4)):
        with pytest.raises(GrafxUnsupportedOperation):
            bench.inject_interior_zeros(SEGMENT, offset, length)


def test_the_injection_helpers_stay_out_of_the_call_trail() -> None:
    bench = _twin()
    bench.create(SEGMENT)
    bench.append_log(SEGMENT, b"A" * (2 * PAGE_SIZE))
    before = len(bench.trail())
    bench.inject_interior_zeros(SEGMENT, 0, PAGE_SIZE)
    assert len(bench.trail()) == before


def test_a_payload_that_is_not_bytes_is_refused_by_the_bench_as_by_the_device() -> None:
    # The trail is written before the wrapped device sees the call, so a payload with no length
    # must not escape as a bare TypeError with no trail entry: an engine catching GrafxError
    # would then catch the real device and miss the bench.
    bench = _twin()
    bench.create(SEGMENT)
    for probe, method in (
        (lambda: bench.append_log(SEGMENT, 42), "append_log"),
        (lambda: bench.append_log(SEGMENT, None), "append_log"),
    ):
        before = len(bench.trail())
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            probe()
        assert raised.value.details["reason"] == "not_a_payload"
        assert len(bench.trail()) == before + 1
        assert bench.trail()[-1].method == method
        assert bench.trail()[-1].outcome == "unsupported_operation"
        assert bench.trail()[-1].args_summary == "bytes=?"


def test_a_partial_write_of_a_payload_that_is_not_bytes_still_refuses_typed() -> None:
    bench = _twin()
    bench.create(SEGMENT)
    bench.write_partially(2, method="append_log")
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        bench.append_log(SEGMENT, 42)
    assert raised.value.details["reason"] == "not_a_payload"
    bench.create(HEAP)
    bench.allocate(HEAP)
    bench.write_partially(2, method="write_page")
    with pytest.raises(GrafxUnsupportedOperation) as page:
        bench.write_page(HEAP, 0, 42)
    assert page.value.details["reason"] == "not_a_payload"
    assert bench.trail()[-1].method == "write_page"
    assert bench.trail()[-1].outcome == "unsupported_operation"


REFUSED_SIZES: tuple[object, ...] = ("5", None, -1, 9, True, False, 2.5, 10**12)
"""Arguments the device refuses, including the two a range check would otherwise mask."""


def _answer_of(device: Any, size: object) -> tuple[object, ...]:
    """Return the class, reason and code a device answers with, plus the size it is left at."""
    try:
        device.truncate_log(SEGMENT, size)
        return ("accepted", None, None, device.log_size(SEGMENT))
    except GrafxError as failure:
        return (type(failure), failure.details.get("reason"), failure.code, device.log_size(SEGMENT))


def test_the_twin_answers_a_refused_argument_exactly_as_the_device_does() -> None:
    # M1: a bench whose failure depends on whether a fault is armed cannot stand in for the
    # device. The expectation is not written down here, it is read from the wrapped device
    # itself, so a swap between two reasons cannot pass unnoticed either.
    for plan in (None, FaultPlan(lying_barrier=True), FaultPlan(crash_method="exists")):
        for size in REFUSED_SIZES:
            bare = MemoryStorageDevice(page_size=PAGE_SIZE)
            bare.create(SEGMENT)
            bare.append_log(SEGMENT, b"abcdefgh")
            expected = _answer_of(bare, size)

            bench = _twin(plan=plan)
            bench.create(SEGMENT)
            bench.append_log(SEGMENT, b"abcdefgh")
            actual = _answer_of(bench, size)

            assert actual == expected, f"plan={plan} size={size!r}"
            # The trail entry of the call itself, not of the size read that followed it.
            recorded = bench.calls_of("truncate_log")[-1]
            assert recorded.args_summary == f"size={size}"
            assert recorded.outcome == (expected[2] or "ok")
            assert bench.inner.log_size(SEGMENT) == bare.log_size(SEGMENT)


def test_a_lying_barrier_leaves_only_the_type_a_barrier_may_raise() -> None:
    # M2 and A28: the lie is about persistence, and it may not change which type the door
    # raises, or oktografx_barrier_failures_total stops firing for the antivirus case.
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    bench.create(SEGMENT)
    for name, reason in (("../escape", "parent_traversal"), ("absent.wal", "missing_file")):
        with pytest.raises(GrafxDurabilityBarrierFailed) as raised:
            bench.durable_barrier(name)
        assert raised.value.details["reason"] == reason
        assert bench.trail()[-1].outcome == "durability_barrier_failed"


def test_a_lying_barrier_still_refuses_a_device_that_cannot_serve() -> None:
    # H6: the lie is about persistence. A barrier that names no file must still consult the
    # wrapped device, or an ordering proof would count an acknowledgement that never happened.
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    bench.create(SEGMENT)
    bench.append_log(SEGMENT, RECORD)
    bench.durable_barrier()
    assert bench.trail()[-1].outcome == "ok"
    bench.inner.close()
    with pytest.raises(GrafxDurabilityBarrierFailed) as raised:
        bench.durable_barrier()
    assert raised.value.details["reason"] == "device_closed"
    assert bench.trail()[-1].outcome == "durability_barrier_failed"


def test_a_lying_barrier_failure_carries_the_same_details_as_an_honest_one() -> None:
    # H7: the caller reads details["retryable"] whatever the plan was, so the lying path may not
    # build its failure by hand and leave the classification out.
    honest = _twin()
    with pytest.raises(GrafxDurabilityBarrierFailed) as first:
        honest.durable_barrier("absent.wal")
    lying = _twin(plan=FaultPlan(lying_barrier=True))
    with pytest.raises(GrafxDurabilityBarrierFailed) as second:
        lying.durable_barrier("absent.wal")
    assert set(second.value.details) == set(first.value.details)
    assert second.value.details == first.value.details
    assert second.value.details["retryable"] is False
    assert second.value.details["attempts"] == 1


def test_a_crash_takes_back_a_page_image_and_a_truncation_it_never_made_durable() -> None:
    # A18(f) for the two operations AC-4 reads off: the undo of a page write and of a
    # truncation, each proved on its own so neither branch can rot unnoticed.
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    bench.create(HEAP)
    bench.allocate(HEAP, 2)
    bench.write_page(HEAP, 0, bytes([0x01]) * PAGE_SIZE)
    bench.write_page(HEAP, 1, bytes([0x02]) * PAGE_SIZE)
    bench.lie_on_barrier(enabled=False)
    bench.durable_barrier(HEAP)
    bench.lie_on_barrier()
    bench.write_page(HEAP, 0, bytes([0xEE]) * PAGE_SIZE)
    assert bench.inner.read_page(HEAP, 0) == bytes([0xEE]) * PAGE_SIZE
    bench.crash_on("log_size")
    with pytest.raises(SimulatedCrash):
        bench.log_size(HEAP)
    assert bench.inner.read_page(HEAP, 0) == bytes([0x01]) * PAGE_SIZE
    assert bench.inner.read_page(HEAP, 1) == bytes([0x02]) * PAGE_SIZE


def test_a_crash_puts_back_the_tail_a_truncation_removed() -> None:
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    bench.create(SEGMENT)
    bench.append_log(SEGMENT, b"HEADTAIL")
    bench.lie_on_barrier(enabled=False)
    bench.durable_barrier(SEGMENT)
    bench.lie_on_barrier()
    bench.truncate_log(SEGMENT, 4)
    assert bench.inner.read_log(SEGMENT, 0, 8) == b"HEAD"
    bench.crash_on("log_size")
    with pytest.raises(SimulatedCrash):
        bench.log_size(SEGMENT)
    assert bench.inner.read_log(SEGMENT, 0, 8) == b"HEADTAIL"


def test_a_crash_can_be_addressed_by_the_call_index_a_survey_reported() -> None:
    # crash_at is the door TS-4 drives, so it needs a test of its own.
    survey = _twin()
    points = survey.enumerate_write_points(_commit_workload)
    target = points[3]
    bench = _twin()
    bench.crash_at(target.call_index, moment="after")
    with pytest.raises(SimulatedCrash) as raised:
        _commit_workload(bench)
    assert (raised.value.sequence, raised.value.method, raised.value.moment) == (
        target.call_index,
        target.method,
        "after",
    )


def test_the_reading_wrappers_forward_and_record() -> None:
    bench = _twin()
    bench.create(HEAP)
    bench.allocate(HEAP, 2)
    bench.write_page(HEAP, 1, bytes([0x7F]) * PAGE_SIZE)
    assert bench.page_count(HEAP) == 2
    assert bench.file_size(HEAP) == 2 * PAGE_SIZE
    assert bench.read_page(HEAP, 1) == bytes([0x7F]) * PAGE_SIZE
    assert bench.methods()[-3:] == ("page_count", "file_size", "read_page")
    assert [record.outcome for record in bench.trail()[-3:]] == ["ok", "ok", "ok"]
    bench.crash_on("page_count", occurrence=2)
    with pytest.raises(SimulatedCrash):
        bench.page_count(HEAP)


# --- the crash path always crashes (B2, A18f, A18g) ----------------------------------------------


def test_a_crash_after_a_publish_raises_the_simulated_crash_and_nothing_else() -> None:
    # The publish pattern of CONTRACT section 6.1 under a lying barrier: the rollback has to
    # undo a rename, and it may never turn the crash into an ordinary exception that a retry
    # loop would swallow (A18f, A18g).
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    bench.create("staging.tmp")
    bench.append_log("staging.tmp", b"payload")
    bench.atomic_replace("staging.tmp", STATE)
    bench.crash_on("append_log", occurrence=2)
    swallowed = None
    try:
        bench.append_log(STATE, b"boom")
    except SimulatedCrash as crash:
        swallowed = crash
    except Exception as failure:  # pragma: no cover - the defect this test exists for
        pytest.fail(f"an ordinary exception escaped the crash path: {failure!r}")
    assert isinstance(swallowed, SimulatedCrash)
    assert not isinstance(swallowed, Exception)
    assert bench.rollback_failures() == ()
    assert bench.inner.exists(STATE) is False
    assert bench.inner.exists("staging.tmp") is False


def test_a_crash_undoes_a_remove_and_a_recycle_that_were_never_durable() -> None:
    for operation in ("remove", "recycle"):
        bench = _twin(plan=FaultPlan(lying_barrier=True))
        bench.create(SEGMENT)
        bench.append_log(SEGMENT, RECORD)
        bench.lie_on_barrier(enabled=False)
        bench.durable_barrier(SEGMENT)
        bench.lie_on_barrier()
        getattr(bench, operation)(SEGMENT)
        assert bench.inner.exists(SEGMENT) is False
        bench.crash_on("log_size")
        with pytest.raises(SimulatedCrash):
            bench.log_size(SEGMENT)
        assert bench.inner.exists(SEGMENT) is True, operation
        assert bench.inner.read_log(SEGMENT, 0, len(RECORD)) == RECORD


def test_global_barriers_pin_nested_namespace_publication_and_retirement() -> None:
    """The power-loss twin agrees that rename and later absence are publication state."""
    staging = "bootstrap/nested/first-open.intent.staging"
    canonical = "bootstrap/nested/first-open.intent"
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    bench.lie_on_barrier(enabled=False)

    bench.create(staging)
    bench.append_log(staging, b"complete intent")
    bench.durable_barrier(None)
    bench.atomic_replace(staging, canonical)
    bench.durable_barrier(None)
    assert bench.inner.exists(staging) is False
    assert bench.inner.exists(canonical) is True

    bench.remove(canonical)
    bench.durable_barrier(None)
    bench.lie_on_barrier()
    bench.crash_on("list_files")
    with pytest.raises(SimulatedCrash):
        bench.list_files()
    assert bench.inner.exists(staging) is False
    assert bench.inner.exists(canonical) is False


def test_a_rollback_the_device_refuses_is_reported_and_never_stops_the_crash() -> None:
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    bench.create(SEGMENT)
    bench.append_log(SEGMENT, RECORD)
    bench.inner.close()
    bench.crash_on("log_size")
    with pytest.raises(SimulatedCrash):
        bench.log_size(SEGMENT)
    assert bench.rollback_failures() != ()


# --- one barrier pins one file (A18a) -------------------------------------------------------------


def test_a_barrier_that_names_one_file_pins_only_that_file() -> None:
    # M4: barriering the WAL must not make the commit state look durable as well. That is the
    # classic hazard, and a bench that pins everything would report a false pass.
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    bench.create(SEGMENT)
    bench.create(STATE)
    bench.append_log(SEGMENT, RECORD)
    bench.append_log(STATE, b"lsn=1")
    bench.lie_on_barrier(enabled=False)
    bench.durable_barrier(SEGMENT)
    assert bench.volatile_files() == (STATE,)
    bench.lie_on_barrier()
    bench.crash_on("list_files")
    with pytest.raises(SimulatedCrash):
        bench.list_files()
    assert bench.inner.read_log(SEGMENT, 0, len(RECORD)) == RECORD
    assert bench.inner.exists(STATE) is False


def test_a_barrier_that_names_nothing_pins_everything() -> None:
    bench = _twin(plan=FaultPlan(lying_barrier=True))
    bench.create(SEGMENT)
    bench.create(STATE)
    bench.append_log(SEGMENT, RECORD)
    bench.append_log(STATE, b"lsn=1")
    bench.lie_on_barrier(enabled=False)
    bench.durable_barrier()
    assert bench.volatile_files() == ()
    bench.lie_on_barrier()
    bench.crash_on("list_files")
    with pytest.raises(SimulatedCrash):
        bench.list_files()
    assert bench.inner.read_log(SEGMENT, 0, len(RECORD)) == RECORD
    assert bench.inner.read_log(STATE, 0, 5) == b"lsn=1"


def test_a_reordering_barrier_pins_only_the_file_it_names() -> None:
    bench = _twin(seed=9)
    bench.create(SEGMENT)
    bench.create(OTHER_SEGMENT)
    bench.durable_barrier()
    bench.start_reordering()
    for index in range(4):
        bench.append_log(SEGMENT, bytes([0x30 + index]))
        bench.append_log(OTHER_SEGMENT, bytes([0x40 + index]))
    bench.durable_barrier(SEGMENT)
    bench.crash_on("list_files")
    with pytest.raises(SimulatedCrash):
        bench.list_files()
    assert bench.inner.read_log(SEGMENT, 0, 4) == b"0123"
    assert len(bench.inner.read_log(OTHER_SEGMENT, 0, 4)) < 4


# --- the trail tells the truth (A18e) --------------------------------------------------------------


def test_the_trail_records_the_real_outcome_of_a_failed_call() -> None:
    # M6: a barrier that failed must not be readable as an acknowledgement.
    bench = _twin()
    with pytest.raises(GrafxDurabilityBarrierFailed):
        bench.durable_barrier("absent.wal")
    assert bench.trail()[-1].outcome == "durability_barrier_failed"
    with pytest.raises(GrafxCorruptionDetected):
        bench.read_log("absent.wal", 0, 4)
    assert bench.trail()[-1].outcome == "corruption_detected"
    with pytest.raises(GrafxUnsupportedOperation):
        bench.create("../escape.wal")
    assert bench.trail()[-1].outcome == "unsupported_operation"
    bench.create(SEGMENT)
    bench.durable_barrier(SEGMENT)
    assert bench.trail()[-1].outcome == "ok"
    assert [record.outcome for record in bench.trail()] == [
        "durability_barrier_failed",
        "corruption_detected",
        "unsupported_operation",
        "ok",
        "ok",
    ]


def test_an_ordering_proof_reads_only_the_barriers_that_succeeded() -> None:
    bench = _twin()
    bench.create(SEGMENT)
    bench.append_log(SEGMENT, RECORD)
    with pytest.raises(GrafxDurabilityBarrierFailed):
        bench.durable_barrier("absent.wal")
    bench.durable_barrier(SEGMENT)
    bench.create(STATE)
    barriers = [record for record in bench.calls_of("durable_barrier") if record.outcome == "ok"]
    assert len(barriers) == 1
    publish = bench.calls_of("create")[-1]
    assert barriers[0].sequence < publish.sequence


# --- the bench over a real directory ------------------------------------------------------------------


def test_the_bench_wraps_the_local_device_just_as_well(tmp_path: Path) -> None:
    with LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE) as inner:
        bench = FaultInjectingStorageDevice(inner, seed=3, plan=FaultPlan(crash_method="durable_barrier"))
        bench.create(SEGMENT)
        bench.append_log(SEGMENT, RECORD)
        with pytest.raises(SimulatedCrash):
            bench.durable_barrier(SEGMENT)
        assert inner.read_log(SEGMENT, 0, len(RECORD)) == RECORD
    reopened = LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE)
    try:
        assert reopened.read_log(SEGMENT, 0, len(RECORD)) == RECORD
    finally:
        reopened.close()


def test_closing_the_bench_closes_the_device_it_wraps(tmp_path: Path) -> None:
    inner = LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE)
    with FaultInjectingStorageDevice(inner, seed=1) as bench:
        bench.create(SEGMENT)
    with pytest.raises(GrafxUnsupportedOperation):
        inner.exists(SEGMENT)
