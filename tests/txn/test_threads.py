"""N threads of one participant (SPEC-M1 FR-3, follow-up P1).

FR-3 asks for N processes *and N threads* holding write transactions at once, and requires that
every commit whose partition sets are disjoint **confirms**. The processes half is covered by
``test_txn_multiprocess.py``; this module covers the threads half, which is a different problem
with a different answer.

A participant holds exactly ONE writer lease. Two threads that each acquire and release "it"
therefore end one another's epoch, and the loser is refused with a non-retryable
``stale_epoch`` -- measured at 36 of 120 commits confirming before the fix. What FR-3 requires
is that they all confirm, not that they overlap in time, so the transaction manager serialises
the threads of one participant around the acquire-commit-release window and every commit lands.

The clock here is the real one. The manual clock the rest of the suite uses would bound a
contended wait by its iteration allowance rather than by elapsed time, which would turn a
correct implementation red on a loaded machine (A78).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxWriteConflict,
)
from okto_grafx.domain.txn import WalRecordType
from txn_support import Stack, build_stack, make_page_image, read_page_payloads

HEAP = "heap.dat"
WORKERS = 6
ROUNDS = 20
THREAD_BUDGET_SECONDS = 300.0
SECTION_TIMEOUT_SECONDS = 240.0
TEST_TIMEOUT_SECONDS = 900
"""Each test here carries its own bound, as pyproject.toml prescribes for anything slower
than the project default: 480 commits through a real device is minutes of honest fsync, and
a test that relied on the caller appending an option would die at 60 seconds with no junit
report at all (A75.2, A92)."""


def _threaded_stack(database_root: Path) -> Stack:
    """Return a participant several threads will share, waiting on a real clock."""
    return build_stack(
        database_root,
        clock=SystemClock(),
        commit_lock_timeout=SECTION_TIMEOUT_SECONDS,
        lease_timeout=SECTION_TIMEOUT_SECONDS,
    )


def _run(workers: int, body: object) -> list[tuple[int, str, str]]:
    """Run one body per worker thread and return whatever escaped, with the worker that saw it.

    The join is bounded by a count of threads and a wall budget, never by a predicate this
    component computes (A92), and nothing in the teardown calls the operation under test, so a
    defect shows up as a failure rather than as a run nobody can kill (A88).
    """
    failures: list[tuple[int, str, str]] = []
    guard = threading.Lock()

    def entry(index: int) -> None:
        try:
            body(index)  # type: ignore[operator]
        except BaseException as failure:  # noqa: BLE001 - the test reports what escaped
            with guard:
                failures.append((index, type(failure).__name__, str(failure)))

    threads = [
        threading.Thread(target=entry, args=(index,), name=f"c5-worker-{index}")
        for index in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=THREAD_BUDGET_SECONDS)
    still_running = [thread.name for thread in threads if thread.is_alive()]
    assert not still_running, f"these workers never finished: {still_running}"
    return failures


@pytest.mark.slow
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_every_thread_of_one_participant_confirms_on_disjoint_partitions(
    database_root: Path,
) -> None:
    """FR-3, threads half: all of them confirm, and every commit gets its own number."""
    stack = _threaded_stack(database_root)
    confirmed: list[list[int]] = [[] for _ in range(WORKERS)]

    def body(index: int) -> None:
        for round_number in range(ROUNDS):
            page = 3 + index * ROUNDS + round_number
            txn = stack.manager.begin("write")
            txn.stage_page_image(
                HEAP,
                page,
                make_page_image(stack.codec, [bytes([index, round_number])], page_index=page),
            )
            # A different table id per worker, so no two workers ever share a partition key.
            txn.note_write(stack.manager.partition_of(index + 1, bytes([round_number])))
            confirmed[index].append(stack.manager.commit(txn).csn)

    failures = _run(WORKERS, body)
    assert failures == [], f"a worker did not confirm: {failures}"
    numbers = [csn for row in confirmed for csn in row]
    assert len(numbers) == WORKERS * ROUNDS
    assert len(set(numbers)) == len(numbers), "two commits were given the same number"
    commits = [
        record for record in stack.wal.records() if record.record_type == WalRecordType.COMMIT
    ]
    assert len(commits) == WORKERS * ROUNDS
    for index in range(WORKERS):
        page = 3 + index * ROUNDS
        assert read_page_payloads(stack.pool, HEAP, page) == (bytes([index, 0]),)


@pytest.mark.slow
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_threads_opening_and_abandoning_transactions_never_report_damage(
    database_root: Path,
) -> None:
    """A thread race must never manufacture an integrity incident (A11-revised).

    Reader registration publishes through a temporary file named after the PARTICIPANT, so two
    threads of one participant reach for the same name. Left unserialised that surfaces as
    ``corruption_detected`` -- the class FR-8 and FR-10 turn into truncation, quarantine and a
    forensic ledger entry, over a race that damaged nothing.
    """
    stack = _threaded_stack(database_root)
    opened: list[int] = []
    guard = threading.Lock()

    def body(index: int) -> None:
        for _round in range(ROUNDS):
            txn = stack.manager.begin("read")
            stack.manager.refresh_due_readers()
            stack.manager.rollback(txn)
            with guard:
                opened.append(txn.txn_id)

    failures = _run(WORKERS, body)
    damaged = [entry for entry in failures if entry[1] == GrafxCorruptionDetected.__name__]
    assert damaged == [], f"a thread race was reported as damage: {damaged}"
    assert failures == [], f"a worker failed: {failures}"
    assert len(opened) == WORKERS * ROUNDS
    assert len(set(opened)) == len(opened), "two transactions were given the same number"
    assert stack.manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None


@pytest.mark.slow
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_readers_and_writers_of_one_participant_share_the_pin_table_safely(
    database_root: Path,
) -> None:
    """The pin table is walked by refreshes while other threads add to it and take from it."""
    stack = _threaded_stack(database_root)
    committed: list[int] = []
    guard = threading.Lock()

    def body(index: int) -> None:
        for round_number in range(ROUNDS):
            if index % 2 == 0:
                page = 200 + index * ROUNDS + round_number
                txn = stack.manager.begin("write")
                txn.stage_page_image(
                    HEAP, page, make_page_image(stack.codec, [b"w"], page_index=page)
                )
                txn.note_write(stack.manager.partition_of(index + 1, bytes([round_number])))
                report = stack.manager.commit(txn)
                with guard:
                    committed.append(report.csn)
            else:
                txn = stack.manager.begin("read")
                stack.manager.refresh_due_readers()
                stack.manager.commit(txn)

    failures = _run(WORKERS, body)
    assert failures == [], f"a worker failed: {failures}"
    assert len(committed) == (WORKERS // 2) * ROUNDS
    assert len(set(committed)) == len(committed)
    assert stack.manager.open_transactions == 0


def _abandon(stack: Stack, txn: object) -> None:
    """Drop a transaction that will not commit, without letting the drop become the failure."""
    try:
        stack.manager.rollback(txn)  # type: ignore[arg-type]
    except GrafxError:
        return


@pytest.mark.slow
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_no_thread_ever_sees_a_non_grafx_failure(database_root: Path) -> None:
    """Section 11 item 5: only Grafx types leave, under contention as much as anywhere else."""
    stack = _threaded_stack(database_root)

    def body(index: int) -> None:
        for round_number in range(ROUNDS):
            page = 400 + index * ROUNDS + round_number
            txn = stack.manager.begin("write")
            txn.stage_page_image(
                HEAP, page, make_page_image(stack.codec, [b"x"], page_index=page)
            )
            # One shared partition on purpose, so the conflict path runs under contention too.
            txn.note_write(stack.manager.partition_of(99, b"shared"))
            try:
                stack.manager.commit(txn)
            except GrafxWriteConflict:
                successor = stack.manager.retry(txn)
                successor.stage_page_image(
                    HEAP, page, make_page_image(stack.codec, [b"x"], page_index=page)
                )
                successor.note_write(stack.manager.partition_of(99, b"shared"))
                try:
                    stack.manager.commit(successor)
                except GrafxError:
                    _abandon(stack, successor)
            except GrafxError:
                _abandon(stack, txn)

    failures = _run(WORKERS, body)
    assert failures == [], f"something that is not a Grafx failure escaped: {failures}"
    assert stack.manager.open_transactions == 0
