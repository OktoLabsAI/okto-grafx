"""The index half of a commit (SPEC-M1 FR-12, BR-11; C7 follow-up).

BR-11 has two halves. The records an index stages travel in the same commit as the heap write
they belong to -- that is ``pending_records``, and it was covered from the start. The changes
those records describe must also be APPLIED to the index once the commit is durable, and that is
what this module covers: a committed row that is in the heap and in the log and in no index is a
missing row for an exact lookup and a silently short answer for a proximity one.

Both directions are exercised. The double proves exactly which calls happen and when, because
that is not observable through a real index; the real ``IndexManager`` then proves the entry
really lands, because a double proves nothing about the component it stands for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.domain.errors import GrafxWriteConflict
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.txn import TransactionState, WalRecord, WalRecordType
from okto_grafx.engine.index_manager import HashIndex, IndexManager
from okto_grafx.engine.txn_manager import TransactionManager
from txn_support import Stack, build_stack, make_page_image

HEAP = "heap.dat"


class _RecordingIndexManager:
    """An index manager that writes down what the transaction manager asked it to do."""

    def __init__(self, stack_barriers: object) -> None:
        self._barriers = stack_barriers
        self.committed: list[tuple[int, int, int]] = []
        self.rolled_back: list[int] = []

    def commit(self, txn: object, csn: int) -> int:
        """Record the commit, together with how many log barriers had been taken by then."""
        self.committed.append(
            (txn.txn_id, csn, int(self._barriers.barriers))  # type: ignore[attr-defined]
        )
        return 1

    def rollback(self, txn: object) -> int:
        """Record the rollback."""
        self.rolled_back.append(txn.txn_id)  # type: ignore[attr-defined]
        return 1

    def validate_staged_records(self, txn: object, records: object) -> None:
        """Accept the one synthetic index record this interaction double owns."""
        staged = tuple(records)  # type: ignore[arg-type]
        assert len(staged) == 1
        assert staged[0].txn_id == txn.txn_id  # type: ignore[attr-defined]
        assert staged[0].record_type == int(WalRecordType.INDEX_WRITE)


def _wired(database_root: Path) -> tuple[Stack, _RecordingIndexManager, TransactionManager]:
    """Return a stack, a recording index manager, and a transaction manager wired to it."""
    stack = build_stack(database_root)
    recorder = _RecordingIndexManager(stack.wal)
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        stack.coordinator,
        stack.clock,
        stack.metrics,
        recorder,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    return stack, recorder, manager


def _stage(stack: Stack, manager: TransactionManager, page: int = 3) -> object:
    """Open a write transaction with one page and one index record staged on it."""
    txn = manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, page, make_page_image(stack.codec, [b"row"], page_index=page))
    txn.note_write(manager.partition_of(1, b"row"))
    txn.stage_record(
        WalRecord(record_type=int(WalRecordType.INDEX_WRITE), payload=b"entry", txn_id=txn.txn_id)
    )
    return txn


# --- the calls, and when they happen --------------------------------------------------------


def test_a_commit_applies_the_index_changes_it_staged(database_root: Path) -> None:
    stack, recorder, manager = _wired(database_root)
    txn = _stage(stack, manager)
    report = manager.commit(txn)
    assert recorder.committed == [(txn.txn_id, report.csn, 1)]
    assert recorder.rolled_back == []


def test_the_index_is_applied_only_after_the_log_barrier_has_returned(
    database_root: Path,
) -> None:
    """C7 puts the call in the step 6 region: durable first, index pages after."""
    stack, recorder, manager = _wired(database_root)
    manager.commit(_stage(stack, manager, page=3))
    manager.commit(_stage(stack, manager, page=4))
    barriers_at_each_call = [entry[2] for entry in recorder.committed]
    assert barriers_at_each_call == [1, 2], (
        "each index commit must see the barrier of its own commit already taken"
    )


def test_the_number_handed_to_the_index_is_the_one_the_log_assigned(
    database_root: Path,
) -> None:
    stack, recorder, manager = _wired(database_root)
    numbers = [manager.commit(_stage(stack, manager, page=page)).csn for page in (3, 4, 5)]
    assert [entry[1] for entry in recorder.committed] == numbers


def test_a_rollback_drops_what_the_transaction_staged_into_the_indexes(
    database_root: Path,
) -> None:
    stack, recorder, manager = _wired(database_root)
    txn = _stage(stack, manager)
    manager.rollback(txn)
    assert recorder.rolled_back == [txn.txn_id]
    assert recorder.committed == []
    assert txn.state is TransactionState.ABORTED


def test_closing_drops_what_an_abandoned_transaction_staged(database_root: Path) -> None:
    stack, recorder, manager = _wired(database_root)
    txn = _stage(stack, manager)
    manager.close()
    assert recorder.rolled_back == [txn.txn_id]


def test_a_refused_commit_applies_nothing_to_any_index(database_root: Path) -> None:
    """Optimistic validation refuses before anything is appended, so nothing may be applied."""
    stack, recorder, manager = _wired(database_root)
    other = build_stack(database_root, owner_id="participant-b")
    shared = manager.partition_of(1, b"row")
    loser = _stage(stack, manager, page=4)
    winner = other.manager.begin("write")
    winner.owner._stage_page_image(winner, HEAP, 3, make_page_image(other.codec, [b"w"], page_index=3))
    winner.note_write(shared)
    other.manager.commit(winner)
    with pytest.raises(GrafxWriteConflict):
        manager.commit(loser)
    assert recorder.committed == []
    # The refusal itself drops what the attempt staged into the indexes. Leaving it for the
    # retry to drop was the round-2 B3 defect: a second commit(txn) after a retryable refusal
    # staged again and carried BOTH sets, one stamped with a number the log never assigned.
    assert recorder.rolled_back == [loser.txn_id]
    manager.retry(loser)
    assert recorder.rolled_back == [loser.txn_id, loser.txn_id], (
        "the retry drops again; dropping an empty staging area is harmless and dropping twice "
        "is not a second mechanism, it is the same door on two paths"
    )


def test_a_database_with_no_index_manager_commits_normally(stack: Stack) -> None:
    """The ordinary state of a database with no secondary index is nothing to apply."""
    txn = stack.manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(stack.codec, [b"row"], page_index=3))
    txn.note_write(stack.manager.partition_of(1, b"row"))
    assert stack.manager.index_manager is None
    assert stack.manager.commit(txn).wrote is True


# --- the epoch a staged record carries --------------------------------------------------------


def test_a_staged_record_is_stamped_with_the_epoch_that_commits_it(
    database_root: Path,
) -> None:
    """Section 6.5 names the epoch of the bearer, and the bearer is the writer that committed."""
    stack, _recorder, manager = _wired(database_root)
    txn = _stage(stack, manager)
    report = manager.commit(txn)
    staged = [
        record
        for record in stack.wal.records()
        if record.record_type == WalRecordType.INDEX_WRITE
    ]
    assert len(staged) == 1
    assert staged[0].payload == b"entry"
    assert staged[0].epoch > 0
    commits = [
        record for record in stack.wal.records() if record.record_type == WalRecordType.COMMIT
    ]
    assert commits[0].lsn == report.csn
    assert staged[0].epoch == commits[0].epoch, (
        "every record of one commit carries the epoch that commit was made under"
    )


# --- against the real index framework -----------------------------------------------------------


def test_an_entry_staged_through_the_real_index_is_there_after_the_commit(
    database_root: Path,
) -> None:
    """The double proves the call; this proves the change."""
    stack = build_stack(database_root)
    indexes = IndexManager(stack.pool, stack.heap, stack.metrics)
    index = indexes.register(
        HashIndex(
            IndexDefinition(
                name="by_name",
                table_id=1,
                table_name="person",
                positions=(0,),
                visibility=IndexVisibility.EXACT,
            ),
            stack.pool,
            stack.metrics,
        )
    )
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        stack.coordinator,
        stack.clock,
        stack.metrics,
        indexes,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(stack.codec, [b"row"], page_index=3))
    txn.note_write(manager.partition_of(1, b"ada"))
    reference = RecordRef(page=3, slot=1)
    record = index.stage_insert(txn, b"ada", reference, 0)
    assert record.record_type == int(WalRecordType.INDEX_WRITE)
    assert index.candidates(b"ada") == (), "staging must not touch a page"
    report = manager.commit(txn)
    assert report.wrote is True
    assert [entry.ref for entry in index.candidates(b"ada")] == [reference]


def test_a_rolled_back_transaction_leaves_the_real_index_as_it_found_it(
    database_root: Path,
) -> None:
    stack = build_stack(database_root)
    indexes = IndexManager(stack.pool, stack.heap, stack.metrics)
    index = indexes.register(
        HashIndex(
            IndexDefinition(
                name="by_name",
                table_id=1,
                table_name="person",
                positions=(0,),
                visibility=IndexVisibility.EXACT,
            ),
            stack.pool,
            stack.metrics,
        )
    )
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        stack.coordinator,
        stack.clock,
        stack.metrics,
        indexes,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(stack.codec, [b"row"], page_index=3))
    txn.note_write(manager.partition_of(1, b"ada"))
    index.stage_insert(txn, b"ada", RecordRef(page=3, slot=1), 0)
    manager.rollback(txn)
    assert index.candidates(b"ada") == ()
    assert index.pending(txn) == ()
