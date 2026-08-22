"""A crash at every write point of the commit path, and recovery from each (AC-4, AC-5, AC-10).

AC-4 asks for a crash injected at EACH write point of the commit path, and for four things to hold
at every one of them: reopening recovers to the last intact record, no ACKNOWLEDGED commit is
lost, every discarded record has a ledger entry, and the report matches what was injected.

The bench is C2's fault-injecting twin (FR-16). The write points are enumerated by running one
commit undisturbed and recording what it did, and then the same commit is replayed on a fresh
database crashing at each point in turn. That enumeration is the reason this is a matrix and not
a handful of hand-picked cases: nobody chooses which windows are interesting.
"""

from __future__ import annotations

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice, SimulatedCrash
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.recovery.report import OUTCOME_CLEAN, OUTCOME_TRUNCATED

from .conftest import (
    HEAP_FILE,
    PAGE_SIZE,
    FrozenClock,
    RecordingMetricsSink,
    Stack,
    build_stack,
    commit_pages,
    digest_of_file,
    make_page_image,
)

SEED = 20260820


def _fresh(clock: FrozenClock, metrics: RecordingMetricsSink) -> tuple[
    MemoryStorageDevice, FaultInjectingStorageDevice, Stack
]:
    """Return a brand-new database behind the fault bench."""
    inner = MemoryStorageDevice(page_size=PAGE_SIZE)
    bench = FaultInjectingStorageDevice(inner, seed=SEED)
    stack = build_stack(bench, clock=clock, metrics=metrics)
    return inner, bench, stack


FIRST = b"durable"
SECOND = b"payload"


def _workload(stack: Stack, marker: bytes) -> None:
    """Commit one heap page through the frozen protocol of section 8.5."""
    commit_pages(
        stack,
        [(HEAP_FILE, 3, make_page_image(stack.codec, [marker], page_index=3))],
        txn_id=1,
    )


def _first_commit(stack: Stack) -> None:
    """Commit the page whose survival every later crash must not put at risk."""
    _workload(stack, FIRST)


def _second_commit(stack: Stack) -> None:
    """Commit a second page; this is the one the matrix cuts the process off inside."""
    commit_pages(
        stack,
        [(HEAP_FILE, 4, make_page_image(stack.codec, [SECOND], page_index=4))],
        txn_id=2,
    )


def _write_points(clock: FrozenClock, metrics: RecordingMetricsSink) -> tuple[int, ...]:
    """Return the call index of every write point one commit produces.

    ``enumerate_write_points`` restarts the bench's call numbering at one, so the indices it
    returns are relative to the workload. Every crash run below therefore calls ``clear_trail``
    after building its database and before running the workload, so the same call wears the same
    number in the survey and in the run -- without that, a crash is armed on a number the
    bootstrap already consumed and the bench silently never fires (A72: assert the fixture
    produced the state it claims, which is what ``crashed >= 1`` at the end of the matrix does).
    """
    inner, bench, stack = _fresh(clock, metrics)
    try:
        _first_commit(stack)
        points = bench.enumerate_write_points(lambda device: _second_commit(stack))
        return tuple(point.call_index for point in points)
    finally:
        inner.close()


def test_the_commit_path_has_write_points_to_crash_at(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    points = _write_points(clock, metrics)
    assert len(points) >= 4, points


@pytest.mark.parametrize("moment", ["before", "after"])
def test_a_crash_at_every_write_point_of_the_commit_path_recovers(
    moment: str, clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    """Crash at each write point of a SECOND commit, with a first commit already acknowledged.

    The first commit is what makes every one of AC-4's four properties live at every point: a
    matrix that only ever crashes the first commit can never observe "no acknowledged commit is
    lost", because nothing was ever acknowledged. Here the durable page is checked after every
    single crash.
    """
    points = _write_points(clock, metrics)
    crashed = 0
    for call_index in points:
        inner = MemoryStorageDevice(page_size=PAGE_SIZE)
        bench = FaultInjectingStorageDevice(inner, seed=SEED)
        try:
            stack = build_stack(bench, clock=clock, metrics=metrics)
            _first_commit(stack)
            bench.clear_trail()
            bench.crash_at(call_index, moment=moment)
            try:
                _second_commit(stack)
            except SimulatedCrash:
                crashed += 1
            except GrafxError:
                # A typed refusal is not a crash: the transaction failed and said so, which is
                # the outcome AC-10 asks for. Recovery must still leave the database usable.
                pass
            bench.disarm()

            reopened = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
            report = reopened.recovery().run()

            # 1. The log reads clean afterwards, at the last intact record.
            assert reopened.wal.damage is None, (call_index, moment)
            assert report.last_good_lsn == reopened.wal.last_lsn, (call_index, moment)

            # 2. Every discarded item left exactly one ledger entry (G8, BR-3).
            assert report.ledger_entries_created == report.records_discarded
            assert len(reopened.ledger.list(limit=1000)) == report.ledger_entries_created

            # 3. The report agrees with what was injected.
            expected = OUTCOME_TRUNCATED if report.records_discarded else OUTCOME_CLEAN
            assert report.outcome == expected, (call_index, moment, report)

            # 4. The commit that WAS acknowledged is still there afterwards (BR-4, AC-4).
            reopened.pool.flush()
            with reopened.pool.pinned(HEAP_FILE, 3) as page:
                assert page.read_slot(0) == FIRST, (call_index, moment)

            # A second recovery over the same database changes nothing (idempotence).
            again = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
            second = again.recovery().run()
            assert second.records_discarded == 0
            assert second.last_good_lsn == report.last_good_lsn
            again.pool.flush()
            with again.pool.pinned(HEAP_FILE, 3) as page:
                assert page.read_slot(0) == FIRST
        finally:
            inner.close()
    # A72: the fixture must be proved to have produced the state it claims. A crash armed at a
    # write point that the survey observed is always reached, so every point really crashed.
    assert crashed == len(points), (crashed, len(points), moment)


def test_a_commit_acknowledged_before_a_later_crash_is_never_lost(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    inner = MemoryStorageDevice(page_size=PAGE_SIZE)
    bench = FaultInjectingStorageDevice(inner, seed=SEED)
    try:
        stack = build_stack(bench, clock=clock, metrics=metrics)
        _workload(stack, b"durable")
        points = bench.enumerate_write_points(
            lambda device: commit_pages(
                stack,
                [(HEAP_FILE, 4, make_page_image(stack.codec, [b"second"], page_index=4))],
                txn_id=2,
            )
        )
        assert points
        bench.clear_trail()
        bench.crash_on("append_log", occurrence=1, moment="after")
        with pytest.raises(SimulatedCrash):
            commit_pages(
                stack,
                [(HEAP_FILE, 5, make_page_image(stack.codec, [b"third"], page_index=5))],
                txn_id=3,
            )
        bench.disarm()
        reopened = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
        reopened.recovery().run()
        reopened.pool.flush()
        with reopened.pool.pinned(HEAP_FILE, 3) as page:
            assert page.read_slot(0) == b"durable"
    finally:
        inner.close()


def test_a_run_of_interior_zeros_leaves_the_main_file_byte_identical(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    """The NTFS signature of AC-5: zeros written into the middle of a segment."""
    inner = MemoryStorageDevice(page_size=PAGE_SIZE)
    bench = FaultInjectingStorageDevice(inner, seed=SEED)
    try:
        stack = build_stack(bench, clock=clock, metrics=metrics)
        _workload(stack, b"first")
        _workload(stack, b"second")
        stack.pool.flush()
        heap_before = digest_of_file(bench, HEAP_FILE)
        catalog_before = digest_of_file(bench, "catalog.dat")
        segment = stack.wal.segments()[-1].name
        size = bench.log_size(segment)
        zeroed = bench.inject_interior_zeros(segment, size // 2)
        assert zeroed > 0
        reopened = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
        report = reopened.recovery().run()
        assert report.outcome == OUTCOME_TRUNCATED
        assert report.records_discarded >= 1
        assert report.ledger_entries_created == report.records_discarded
        forensic = reopened.ledger.list(origin_class="forensic")
        assert forensic
        provenance = reopened.ledger.provenance(forensic[0].entry_id)
        assert provenance.origin == segment
        assert provenance.offset >= 0
        assert reopened.ledger.export(forensic[0].entry_id)
        assert digest_of_file(bench, HEAP_FILE) == heap_before
        assert digest_of_file(bench, "catalog.dat") == catalog_before
    finally:
        inner.close()


def test_a_full_device_during_a_commit_leaves_a_database_that_still_opens(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    """AC-10: the transaction is refused, and after a reopen no partial record is visible."""
    inner = MemoryStorageDevice(page_size=PAGE_SIZE)
    bench = FaultInjectingStorageDevice(inner, seed=SEED)
    try:
        stack = build_stack(bench, clock=clock, metrics=metrics)
        _workload(stack, b"first")
        bench.clear_trail()
        bench.fill_device_on("append_log", occurrence=1)
        with pytest.raises(GrafxError):
            _workload(stack, b"second")
        bench.disarm()
        reopened = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
        report = reopened.recovery().run()
        assert report.outcome in (OUTCOME_CLEAN, OUTCOME_TRUNCATED)
        assert reopened.wal.damage is None
        reopened.pool.flush()
        with reopened.pool.pinned(HEAP_FILE, 3) as page:
            assert page.read_slot(0) == b"first"
        commit_pages(
            reopened,
            [(HEAP_FILE, 6, make_page_image(reopened.codec, [b"after"], page_index=6))],
            txn_id=9,
        )
    finally:
        inner.close()


def test_a_lying_barrier_followed_by_a_crash_is_caught_rather_than_silently_lost(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    """AC-11's other half: what a barrier did not really pin must not read as intact."""
    inner = MemoryStorageDevice(page_size=PAGE_SIZE)
    bench = FaultInjectingStorageDevice(inner, seed=SEED)
    try:
        stack = build_stack(bench, clock=clock, metrics=metrics)
        _workload(stack, b"durable")
        stack.pool.flush()
        segment = stack.wal.segments()[-1].name
        size = bench.log_size(segment)
        bench.truncate_log(segment, size - 12)
        reopened = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
        report = reopened.recovery().run()
        assert report.records_discarded >= 1
        assert report.ledger_entries_created == report.records_discarded
        assert reopened.wal.damage is None
    finally:
        inner.close()


# --- a crash inside RECOVERY itself (TR-5) ---------------------------------------------------


def _damaged_database(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> tuple[MemoryStorageDevice, FaultInjectingStorageDevice, str, int]:
    """Return a database whose log has one intact record and one damaged range after it."""
    inner = MemoryStorageDevice(page_size=PAGE_SIZE)
    bench = FaultInjectingStorageDevice(inner, seed=SEED)
    stack = build_stack(bench, clock=clock, metrics=metrics)
    _first_commit(stack)
    segment = stack.wal.segments()[-1].name
    offset = bench.log_size(segment)
    bench.append_log(segment, bytes(96))
    return inner, bench, segment, offset


def _recovery_write_points(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> tuple[int, ...]:
    """Return the call index of every write point one RECOVERY pass produces."""
    inner, bench, _segment, _offset = _damaged_database(clock, metrics)
    try:
        reopened = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
        points = bench.enumerate_write_points(lambda device: reopened.recovery().run())
        return tuple(point.call_index for point in points)
    finally:
        inner.close()


def test_recovery_itself_has_write_points_to_crash_at(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    points = _recovery_write_points(clock, metrics)
    assert len(points) >= 4, points


@pytest.mark.parametrize("moment", ["before", "after"])
def test_a_crash_at_every_write_point_of_recovery_leaves_one_entry_per_discard(
    moment: str, clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    """TR-5: the ledger survives a crash during recovery ITSELF, and does not grow on the retry.

    The commit matrix above crashes the writer. This one crashes the REPAIR, which is the case
    TR-5 is written for and the one an interrupted open actually produces. At every point, the
    re-run must converge: one quarantine copy of the damage, exactly one ledger entry describing
    it (CONTRACT.md section 8.6 step 4, G8, BR-3), and a log that reads clean afterwards.
    """
    points = _recovery_write_points(clock, metrics)
    crashed = 0
    for call_index in points:
        inner, bench, segment, offset = _damaged_database(clock, metrics)
        try:
            reopened = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
            bench.clear_trail()
            bench.crash_at(call_index, moment=moment)
            try:
                reopened.recovery().run()
            except SimulatedCrash:
                crashed += 1
            except GrafxError:
                pass
            bench.disarm()

            # Recovery is re-run at every open, so the retry is the ordinary path, not a repair.
            settled = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
            report = settled.recovery().run()
            assert settled.wal.damage is None, (call_index, moment)

            places = _entry_places(settled)
            # BR-3, stated as the rule rather than as a count: an interrupted repair can leave
            # NEW damage in the segment it was writing, and recording that is correct. What is
            # never correct is one range wearing two entries.
            assert len(places) == len(set(places)), (call_index, moment, places)
            assert (segment, offset) in places, (call_index, moment, places)
            assert places.count((segment, offset)) == 1, (call_index, moment)
            # One preserved copy per recorded discard, both keyed on the same identity.
            assert len(settled.quarantine.list()) == len(places), (call_index, moment)

            # And a third pass changes nothing at all.
            third = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
            again = third.recovery().run()
            assert again.records_discarded == 0
            assert _entry_places(third) == places
            assert again.last_good_lsn == report.last_good_lsn
        finally:
            inner.close()
    assert crashed >= 1, (crashed, len(points), moment)


def test_recovery_interrupted_before_the_cut_writes_one_entry_not_many(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    """The exact shape of B1: interrupt after the entry, before the cut, eight times over."""
    inner, bench, segment, offset = _damaged_database(clock, metrics)
    try:
        for _attempt in range(8):
            reopened = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
            bench.clear_trail()
            # truncate_log is the first byte of the cut, so this stops between recording and
            # destroying -- the window an interrupted open really lands in.
            bench.crash_on("truncate_log", occurrence=1, moment="before")
            try:
                reopened.recovery().run()
            except SimulatedCrash:
                pass
            bench.disarm()
            assert len(reopened.quarantine.list()) == 1
            assert len(reopened.ledger.list(limit=1000)) == 1

        settled = build_stack(bench, clock=clock, metrics=metrics, bootstrap=False)
        settled.recovery().run()
        assert _entry_places(settled) == [(segment, offset)]
        assert len(settled.quarantine.list()) == 1
        assert settled.wal.damage is None
    finally:
        inner.close()


def _entry_places(stack: Stack) -> list[tuple[str, int]]:
    """Return the range every ledger entry describes, so a duplicate is visible as one."""
    return [
        (
            stack.ledger.provenance(entry.entry_id).origin,
            stack.ledger.provenance(entry.entry_id).offset,
        )
        for entry in stack.ledger.list(limit=1000)
    ]
