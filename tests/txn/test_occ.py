"""Optimistic partition validation (CONTRACT.md section 8.5 step 3.3; SPEC-M1 FR-4, BR-6, AC-2).

The predicate is: a commit conflicts when a COMMIT record appended after its snapshot WROTE a
partition it read or wrote. Every term is probed from both sides -- a missing term would admit a
conflicting write, an extra one would refuse a commit BR-6 says must succeed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxTransactionStateError,
    GrafxWriteConflict,
)
from okto_grafx.domain.txn import CommitPayload, TransactionState, WalRecordType
from okto_grafx.engine.txn_manager import (
    COMMIT_RETRIES_TOTAL,
    WRITE_CONFLICTS_TOTAL,
    TransactionManager,
)
from txn_support import LogRecord, LogWal, Stack, make_page_image

HEAP = "heap.dat"
StackFactory = object


def _write(stack: Stack, *, reads: tuple[int, ...] = (), writes: tuple[int, ...] = (), page: int = 3):
    """Open a write transaction with the given sets and one staged page."""
    txn = stack.manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, page, make_page_image(stack.codec, [b"row"], page_index=page))
    for partition in reads:
        txn.note_read(partition)
    for partition in writes:
        txn.note_write(partition)
    if not writes:
        # A transaction that staged a page is a writer even when its write set is empty; the
        # page alone is what makes the commit take the writing path.
        pass
    return txn


# --- the terms that must be there ----------------------------------------------------------


def test_two_commits_writing_the_same_partition_refuse_the_second(make_stack) -> None:
    """AC-2: exactly one confirms; the other is refused, retryably."""
    first = make_stack()
    second = make_stack()
    shared = first.manager.partition_of(1, b"row")
    loser = _write(second, writes=(shared,), page=4)
    winner = _write(first, writes=(shared,), page=3)
    first.manager.commit(winner)
    with pytest.raises(GrafxWriteConflict) as raised:
        second.manager.commit(loser)
    assert raised.value.retryable is True
    assert raised.value.details["partitions"] == [shared]


def test_a_commit_that_wrote_a_partition_this_transaction_only_READ_is_a_conflict(
    make_stack,
) -> None:
    """The read half of the predicate. Without it a decision made from a row survives the row."""
    first = make_stack()
    second = make_stack()
    observed = first.manager.partition_of(1, b"observed")
    written = first.manager.partition_of(2, b"written")
    loser = _write(second, reads=(observed,), writes=(written,), page=4)
    winner = _write(first, writes=(observed,), page=3)
    first.manager.commit(winner)
    with pytest.raises(GrafxWriteConflict) as raised:
        second.manager.commit(loser)
    assert raised.value.details["partitions"] == [observed]


# --- the terms that must NOT be there -------------------------------------------------------


def test_disjoint_partition_sets_both_commit(make_stack) -> None:
    """BR-6 and AC-1: conflict is intersection, never the existence of another writer."""
    first = make_stack()
    second = make_stack()
    mine = first.manager.partition_of(1, b"mine")
    yours = first.manager.partition_of(2, b"yours")
    assert mine != yours
    left = _write(first, writes=(mine,), page=3)
    right = _write(second, writes=(yours,), page=4)
    first_report = first.manager.commit(left)
    second_report = second.manager.commit(right)
    assert first_report.wrote and second_report.wrote
    assert second_report.csn > first_report.csn
    assert first.metrics.total(WRITE_CONFLICTS_TOTAL) == 0.0
    assert second.metrics.total(WRITE_CONFLICTS_TOTAL) == 0.0


def test_a_commit_that_only_READ_the_partition_this_one_writes_is_not_a_conflict(
    make_stack,
) -> None:
    """An extra term on the other side's READ set would refuse a commit BR-6 allows."""
    first = make_stack()
    second = make_stack()
    shared = first.manager.partition_of(1, b"shared")
    reader_side = _write(first, reads=(shared,), writes=(first.manager.partition_of(9, b"x"),), page=3)
    first.manager.commit(reader_side)
    writer_side = _write(second, writes=(shared,), page=4)
    report = second.manager.commit(writer_side)
    assert report.wrote is True
    assert second.metrics.total(WRITE_CONFLICTS_TOTAL) == 0.0


def test_a_commit_at_or_below_the_snapshot_is_not_a_conflict(make_stack) -> None:
    """A transaction has already seen everything at its snapshot; it cannot conflict with it."""
    first = make_stack()
    shared = first.manager.partition_of(1, b"shared")
    first.manager.commit(_write(first, writes=(shared,), page=3))
    later = _write(first, writes=(shared,), page=4)
    assert later.snapshot.read_lsn == first.manager.published_lsn()
    assert first.manager.commit(later).wrote is True


def test_an_explicit_materialization_baseline_still_detects_every_later_commit(
    make_stack,
) -> None:
    """The page-half floor excludes incorporated history, never future writes.

    The target transaction predates both commits. Supplying the first commit as the durable
    materialisation baseline must hide exactly that commit and still return the second. Moving the
    baseline to the second then returns no conflict. This pins both inequalities independently of
    COMMIT_SECTION, which normally prevents the later interleaving from occurring in production.
    """
    observer = make_stack()
    first = make_stack()
    target = observer.manager.partition_of(1, b"materialized-page")
    candidate = observer.manager.begin("write")

    first_report = first.manager.commit(_write(first, writes=(target,), page=3))
    second = make_stack()
    second_report = second.manager.commit(_write(second, writes=(target,), page=4))

    interested = frozenset((target,))
    assert observer.manager._find_conflict(
        candidate,
        interested_partitions=interested,
        baseline_lsn=first_report.csn,
    ) == (target,)
    assert (
        observer.manager._find_conflict(
            candidate,
            interested_partitions=interested,
            baseline_lsn=second_report.csn,
        )
        is None
    )


def test_a_record_that_is_not_a_commit_never_conflicts(stack: Stack) -> None:
    """The record type is part of the predicate: an index write is not a commit."""
    shared = stack.manager.partition_of(1, b"shared")
    impostor = CommitPayload.build(snapshot_lsn=0, write_partitions=[shared]).encode()
    stack.wal.append_many(
        [
            LogRecord(
                record_type=int(WalRecordType.INDEX_WRITE),
                lsn=0,
                epoch=1,
                txn_id=99,
                payload=impostor,
            ),
            LogRecord(
                record_type=int(WalRecordType.ABORT),
                lsn=0,
                epoch=1,
                txn_id=99,
                payload=b"",
            ),
        ]
    )
    txn = _write(stack, writes=(shared,), page=3)
    assert stack.manager.commit(txn).wrote is True


def test_a_transaction_with_no_partitions_never_conflicts(make_stack) -> None:
    """The empty intersection is empty: skipping the scan cannot change the answer."""
    first = make_stack()
    second = make_stack()
    everything = tuple(first.manager.partition_of(1, bytes([n])) for n in range(8))
    blind = _write(second, page=4)
    first.manager.commit(_write(first, writes=everything, page=3))
    assert second.manager.commit(blind).wrote is True


# --- what a refusal leaves behind --------------------------------------------------------------


def test_a_refused_commit_puts_nothing_in_the_log_and_changes_no_page(make_stack) -> None:
    """FR-3: a real conflict fails with no side effect at all."""
    first = make_stack()
    second = make_stack()
    shared = first.manager.partition_of(1, b"row")
    loser = _write(second, writes=(shared,), page=4)
    first.manager.commit(_write(first, writes=(shared,), page=3))
    log_before = second.storage.log_size(second.wal.file)
    state_before = second.manager.published_state()
    with pytest.raises(GrafxWriteConflict):
        second.manager.commit(loser)
    assert second.storage.log_size(second.wal.file) == log_before
    assert second.manager.published_state() == state_before
    assert second.storage.page_count(HEAP) <= 4 or _page_is_absent(second, 4)
    assert loser.state is TransactionState.ACTIVE


def _page_is_absent(stack: Stack, page_index: int) -> bool:
    """Return True when the page holds nothing, which is what a refused commit must leave."""
    from txn_support import read_page_payloads

    return read_page_payloads(stack.pool, HEAP, page_index) == ()


def test_a_conflict_is_counted_exactly_once(make_stack) -> None:
    first = make_stack()
    second = make_stack()
    shared = first.manager.partition_of(1, b"row")
    loser = _write(second, writes=(shared,), page=4)
    first.manager.commit(_write(first, writes=(shared,), page=3))
    with pytest.raises(GrafxWriteConflict):
        second.manager.commit(loser)
    assert second.metrics.total(WRITE_CONFLICTS_TOTAL) == 1.0


# --- the retry ---------------------------------------------------------------------------------


def test_the_loser_of_a_conflict_commits_on_its_retry(make_stack) -> None:
    """TS-2: the state that follows holds both effects, in a serialisable order."""
    first = make_stack()
    second = make_stack()
    shared = first.manager.partition_of(1, b"row")
    loser = _write(second, writes=(shared,), page=4)
    winner_report = first.manager.commit(_write(first, writes=(shared,), page=3))
    with pytest.raises(GrafxWriteConflict):
        second.manager.commit(loser)
    successor = second.manager.retry(loser)
    assert successor.snapshot.read_lsn >= winner_report.csn
    successor.owner._stage_page_image(successor, HEAP, 4, make_page_image(second.codec, [b"retried"], page_index=4))
    successor.note_write(shared)
    retried = second.manager.commit(successor)
    assert retried.csn > winner_report.csn
    assert second.metrics.total(COMMIT_RETRIES_TOTAL) == 1.0


def test_a_transaction_nobody_refused_cannot_be_retried(stack: Stack) -> None:
    """The retry door exists for a refusal; using it for anything else hides a caller mistake."""
    txn = stack.manager.begin("write")
    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager.retry(txn)
    assert raised.value.details["conflicts"] == 0


def test_the_retry_counter_stays_still_for_a_first_attempt(stack: Stack) -> None:
    stack.manager.commit(_write(stack, writes=(stack.manager.partition_of(1, b"a"),), page=3))
    assert stack.metrics.total(COMMIT_RETRIES_TOTAL) == 0.0


# --- the range is decided here, not by the log --------------------------------------------------


class _GenerousLog(LogWal):
    """A log whose ``read_from`` ignores the start it was given and answers with everything."""

    def read_from(self, lsn: int) -> Iterator[LogRecord]:  # noqa: D102
        yield from self.records()


def test_a_log_that_ignores_the_start_lsn_cannot_manufacture_a_conflict(
    database_root: Path, make_stack
) -> None:
    """The comparison in this component is the authority on the range, not the argument."""
    stack = make_stack()
    shared = stack.manager.partition_of(1, b"row")
    stack.manager.commit(_write(stack, writes=(shared,), page=3))
    generous = _GenerousLog(stack.storage)
    manager = TransactionManager(
        generous,
        stack.pool,
        stack.heap,
        stack.catalog,
        stack.coordinator,
        stack.clock,
        stack.metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 5, make_page_image(stack.codec, [b"row"], page_index=5))
    txn.note_write(shared)
    assert txn.snapshot.read_lsn == stack.manager.published_lsn()
    assert manager.commit(txn).wrote is True


def test_a_conflicting_commit_that_is_not_the_last_record_is_still_found(make_stack) -> None:
    """The range is every COMMIT after the snapshot, not the tail of the log.

    Every other scenario here has the conflicting commit sitting at the end of the log, so a
    validator that looked only at the last record would answer them all correctly. Here a
    disjoint commit lands on top of the conflicting one first, which is the ordinary shape as
    soon as a third writer exists -- and it is the case a narrowed range gets wrong.
    """
    first = make_stack()
    second = make_stack()
    third = make_stack()
    contended = first.manager.partition_of(1, b"contended")
    unrelated = first.manager.partition_of(2, b"unrelated")
    assert contended != unrelated
    loser = _write(second, writes=(contended,), page=4)
    first.manager.commit(_write(first, writes=(contended,), page=3))
    third.manager.commit(_write(third, writes=(unrelated,), page=5))
    tail = second.wal.records()[-1]
    conflicting = [
        record
        for record in second.wal.records()
        if record.record_type == WalRecordType.COMMIT
        and contended in CommitPayload.decode(record.payload).write_partitions
    ]
    assert len(conflicting) == 1
    assert conflicting[0].lsn < tail.lsn, (
        "this bench needs the conflicting commit to be behind the tail of the log"
    )
    with pytest.raises(GrafxWriteConflict) as raised:
        second.manager.commit(loser)
    assert raised.value.details["partitions"] == [contended]


def test_three_participants_writing_one_catalog_page_cannot_all_commit(make_stack) -> None:
    """Defect E1: a commit that staged a page image but declared nothing could never be refused.

    Measured before the fix, through the real composition root: three processes each created a
    node table, all three reported ``durable=True``, a fresh reopen saw ONE of them, and
    ``verify("all")`` called the database clean. The log held every record; the catalog page
    image of the last writer had replaced the other two.

    The interest set was empty, so :meth:`TransactionManager._find_conflict` short-circuited and
    the predicate was never consulted. Staging a page now declares interest in that page, so the
    second and third writers meet the first in the log and are refused, retryably.
    """
    from okto_grafx.domain.txn import page_partition

    participants = [make_stack() for _ in range(3)]
    catalog_page = 1
    staged = []
    for participant in participants:
        txn = participant.manager.begin("write")
        txn.owner._stage_page_image(
            txn,
            "catalog.dat",
            catalog_page,
            participant.storage.read_page("catalog.dat", catalog_page),
        )
        staged.append((participant, txn))
        assert page_partition("catalog.dat", catalog_page) in txn.write_partitions

    reports = []
    refusals = []
    for participant, txn in staged:
        try:
            reports.append(participant.manager.commit(txn))
        except GrafxWriteConflict as conflict:
            refusals.append(conflict)

    assert len(reports) == 1, "exactly one writer of one page may commit"
    assert len(refusals) == 2
    for conflict in refusals:
        assert conflict.retryable is True
        assert conflict.details["partitions"] == [
            page_partition("catalog.dat", catalog_page)
        ]


def test_a_caller_staged_index_record_without_a_private_change_is_refused(stack: Stack) -> None:
    """A syntactically valid logical record is not proof that an index produced it."""
    from okto_grafx.domain.txn import WalRecord, WalRecordType

    txn = stack.manager.begin("write")
    txn.stage_record(
        WalRecord(record_type=int(WalRecordType.INDEX_WRITE), payload=b"orphan", txn_id=txn.txn_id)
    )
    assert txn.wrote is True
    assert txn.read_partitions == set() and txn.write_partitions == set()
    with pytest.raises(GrafxConfigurationError) as raised:
        stack.manager.commit(txn)
    assert raised.value.details["field"] == "pending_records"
    assert raised.value.details["count"] == 1
    assert stack.wal.records() == () or all(
        record.record_type != WalRecordType.COMMIT for record in stack.wal.records()
    )


def test_a_transaction_with_nothing_to_write_still_commits(stack: Stack) -> None:
    """The guard must refuse silence, not emptiness: a read-only commit has nothing to declare."""
    txn = stack.manager.begin("write")
    assert txn.wrote is False
    report = stack.manager.commit(txn)
    assert report.wrote is False and report.durable is True
