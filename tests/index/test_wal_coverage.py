"""Every index change is covered by the log, and nothing moves before the commit (BR-11, FR-12).

Two properties are proved here and they hold each other up.

*Nothing before the commit.* Staging touches no page and writes no byte, so a transaction that
aborts, or that optimistic validation refuses, leaves the index exactly as it was. For a
proximity index that is the difference between a correct answer and returning a row that was
never committed, because nothing validates its hits against the heap.

*Everything after the crash.* The record staged on the transaction is appended inside the same
commit as the heap change, so a replay of the log rebuilds the index change with it. Redo is
idempotent on the entry, so replaying the same log twice produces the same index.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import (
    GrafxBufferBudgetExceeded,
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxStorageError,
)
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index import IndexOperation, change_of
from okto_grafx.domain.wal.record import WalRecordType
from okto_grafx.engine.index_manager import INDEX_FLAG_STALE, IndexManager, IndexStore

from .conftest import (
    Database,
    SnapshotDouble,
    TransactionDouble,
    build_database,
    header_on_device,
)

BORN: int = 10
ENDED: int = 20


def test_staging_moves_no_page_and_writes_no_byte(database: Database) -> None:
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    before = len(database.device.write_calls)

    database.manager.stage_row_insert(txn, database.table.table_id, ref, (1, "Ada"), BORN)

    assert database.entries() == {"person_by_name": (), "person_near_name": ()}
    assert len(txn.staged) == 2
    assert len(database.device.write_calls) == before


def test_a_transaction_that_never_commits_leaves_no_entry(database: Database) -> None:
    """The one that matters most for a proximity index.

    An entry written at staging time would carry the commit number the caller predicted. If that
    transaction never commits, a later one takes that number, and the entry becomes visible to
    every snapshot from then on -- pointing at a row that was never committed. Nothing validates
    a proximity hit, so that is a wrong result with no second line of defence.
    """
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(txn, database.table.table_id, ref, (1, "Ada"), BORN)

    assert database.manager.rollback(txn) == 2
    assert database.manager.commit(txn, BORN) == 0
    assert database.entries() == {"person_by_name": (), "person_near_name": ()}
    assert (
        database.manager.lookup("person_near_name", database.key(1, "Ada"), SnapshotDouble(0))
        == ()
    )


def test_an_insert_and_a_tombstone_travel_as_index_write_records(database: Database) -> None:
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)

    inserts = database.manager.stage_row_insert(
        txn, database.table.table_id, ref, (1, "Ada"), BORN
    )
    deletes = database.manager.stage_row_delete(
        txn, database.table.table_id, ref, (1, "Ada"), ENDED
    )

    assert {record.record_type for record in inserts + deletes} == {
        int(WalRecordType.INDEX_WRITE)
    }
    assert [change_of(record).operation for record in inserts] == [
        IndexOperation.INSERT,
        IndexOperation.INSERT,
    ]
    assert [change_of(record).operation for record in deletes] == [
        IndexOperation.TOMBSTONE,
        IndexOperation.TOMBSTONE,
    ]


def test_every_removal_travels_as_an_index_reconcile_record(database: Database) -> None:
    """CONTRACT.md section 8.7 names the record type a reclamation must use."""
    ref = database.insert(1, "Ada", BORN)
    live = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(live, database.table.table_id, ref, (1, "Ada"), BORN)
    database.manager.stage_row_delete(live, database.table.table_id, ref, (1, "Ada"), ENDED)
    database.manager.commit(live, ENDED)

    pass_txn = TransactionDouble(txn_id=4)
    reports = database.manager.reconcile(ENDED, pass_txn)

    assert [report.removed for report in reports] == [1, 1]
    assert {record.record_type for record in pass_txn.staged} == {
        int(WalRecordType.INDEX_RECONCILE)
    }
    assert {change_of(record).operation for record in pass_txn.staged} == {
        IndexOperation.REMOVE
    }


def test_a_measuring_pass_removes_nothing_and_says_what_it_could_release(
    database: Database,
) -> None:
    """``reconcile(horizon)`` with no transaction is the signature the contract spells.

    It may not remove anything, because a removal that the log never saw is the cleanup
    SPEC-VEC BR-3 forbids, so it reports the two numbers apart instead of pretending it did
    nothing to do.
    """
    ref = database.insert(1, "Ada", BORN)
    live = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(live, database.table.table_id, ref, (1, "Ada"), BORN)
    database.manager.stage_row_delete(live, database.table.table_id, ref, (1, "Ada"), ENDED)
    database.manager.commit(live, ENDED)

    before = database.entries()
    reports = database.manager.reconcile(ENDED)

    assert [(report.reclaimable, report.removed) for report in reports] == [(1, 0), (1, 0)]
    assert database.entries() == before


def test_replaying_the_staged_records_reaches_the_state_the_commit_reached() -> None:
    """The live path and the redo path must produce the same index, entry for entry.

    The two databases are built by the same function and given the same work; one commits it and
    the other replays the very records the first one staged. A framework that re-stamped an entry
    at commit time would pass every test that only looks at the live path and fail this one, and
    the failure would otherwise appear for the first time in recovery.
    """
    committed = build_database(name="committed")
    replayed = build_database(name="replayed")
    for database in (committed, replayed):
        database.insert(1, "Ada", BORN)

    live = TransactionDouble(txn_id=3)
    ref = RecordRef(1, 1)
    committed.manager.stage_row_insert(live, committed.table.table_id, ref, (1, "Ada"), BORN)
    committed.manager.stage_row_delete(live, committed.table.table_id, ref, (1, "Ada"), ENDED)
    committed.manager.commit(live, ENDED)

    for position, record in enumerate(live.staged):
        assert replayed.manager.apply(record.with_lsn(100 + position))

    assert replayed.entries() == committed.entries()


def test_replaying_the_same_records_twice_changes_nothing(database: Database) -> None:
    """Redo is idempotent on the entry, which is what a log replayed twice needs."""
    ref = database.insert(1, "Ada", BORN)
    live = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(live, database.table.table_id, ref, (1, "Ada"), BORN)
    database.manager.stage_row_delete(live, database.table.table_id, ref, (1, "Ada"), ENDED)

    numbered = [record.with_lsn(100 + position) for position, record in enumerate(live.staged)]
    for record in numbered:
        database.manager.apply(record)
    once = database.entries()
    for record in numbered:
        database.manager.apply(record)

    assert database.entries() == once
    assert len(once["person_by_name"]) == 1


def test_replaying_a_removal_twice_changes_nothing(database: Database) -> None:
    ref = database.insert(1, "Ada", BORN)
    live = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(live, database.table.table_id, ref, (1, "Ada"), BORN)
    database.manager.stage_row_delete(live, database.table.table_id, ref, (1, "Ada"), ENDED)
    database.manager.commit(live, ENDED)
    pass_txn = TransactionDouble(txn_id=4)
    database.manager.reconcile(ENDED, pass_txn)

    numbered = [
        record.with_lsn(200 + position) for position, record in enumerate(pass_txn.staged)
    ]
    for record in numbered:
        database.manager.apply(record)
    once = database.entries()
    for record in numbered:
        database.manager.apply(record)

    assert database.entries() == once
    assert once == {"person_by_name": (), "person_near_name": ()}


def test_a_second_tombstone_does_not_move_the_stamp_the_first_one_set(
    database: Database,
) -> None:
    """The commit that ended an entry is the FIRST one, and a later one must not push it out.

    The consequence is a wrong result rather than an untidy stamp. An entry ended at 20 that gets
    re-stamped at 30 becomes visible to every snapshot between the two, so a proximity lookup
    returns a row that stopped applying ten commits ago -- and nothing validates a proximity hit
    against the heap.
    """
    ref = database.insert(1, "Ada", BORN)
    key = database.key(1, "Ada")
    opening = TransactionDouble(txn_id=3)
    database.proximity.stage_insert(opening, key, ref, BORN)
    database.proximity.commit(opening, BORN)
    first = TransactionDouble(txn_id=4)
    database.proximity.stage_delete(first, key, ref, ENDED)
    database.proximity.commit(first, ENDED)

    second = TransactionDouble(txn_id=5)
    database.proximity.stage_delete(second, key, ref, ENDED + 10)
    database.proximity.commit(second, ENDED + 10)

    assert [entry.dead_csn for entry in database.proximity.walk()] == [ENDED]
    assert database.manager.lookup(
        "person_near_name", key, SnapshotDouble(ENDED + 5)
    ) == ()


def test_a_tombstone_whose_insert_was_discarded_is_a_no_op_and_not_a_refusal(
    database: Database,
) -> None:
    """Recovery must not be able to wedge on a log it is in the middle of repairing.

    An entry that is absent returns nothing, so a tombstone with no target cannot produce a wrong
    answer. Raising here would stop a replay that has more to do; the divergence is reported by
    verify(), which is the door that exists to name it.
    """
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_delete(txn, database.table.table_id, ref, (1, "Ada"), ENDED)

    for position, record in enumerate(txn.staged):
        assert database.manager.apply(record.with_lsn(300 + position))

    assert database.entries() == {"person_by_name": (), "person_near_name": ()}
    assert database.exact.missing_targets == 1
    assert database.proximity.missing_targets == 1


def test_ending_an_entry_that_was_never_created_is_counted_rather_than_hidden(
    database: Database,
) -> None:
    """A change that did nothing must not read the same as one that worked.

    The caller here names a row version the index has no entry for, which is what passing the
    wrong values to ``stage_row_delete`` produces. It cannot make a lookup wrong -- an absent
    entry returns nothing -- but the row stays visible in a proximity index, and a caller doing
    this should be able to see it before a verification pass tells it.
    """
    ref = database.insert(1, "Ada", BORN)
    live = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(live, database.table.table_id, ref, (1, "Ada"), BORN)
    database.manager.commit(live, BORN)
    assert database.proximity.missing_targets == 0

    wrong = TransactionDouble(txn_id=4)
    database.heap.delete(database.table, ref, ENDED)
    database.manager.stage_row_delete(wrong, database.table.table_id, ref, (1, "Grace"), ENDED)
    database.manager.commit(wrong, ENDED)

    assert database.proximity.missing_targets == 1
    assert database.manager.lookup(
        "person_near_name", database.key(1, "Ada"), SnapshotDouble(ENDED)
    ) == (ref,)
    assert "missing_tombstone" in {
        finding.kind for finding in database.manager.verify("person_near_name")
    }


def test_a_refusal_part_way_through_a_commit_retries_to_the_same_place(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retryable failure mid-commit must leave a state a retry converges from.

    Two things are asserted and each is load-bearing. The position the index claims does NOT move
    when only some of the staged changes landed, so the index is stale rather than short if the
    caller never comes back. And the staging survives the refusal, so the retry re-applies every
    change from the start -- which is safe precisely because applying a change twice is a no-op.

    A retry that gets all the way through is also the only evidence a short index can offer that
    it is whole again, so it -- and nothing else -- takes back the mark the failure set. The two
    tests below hold the corners where it must not be taken back.
    """
    first = database.insert(1, "Ada", BORN)
    second = database.insert(2, "Grace", BORN)
    txn = TransactionDouble(txn_id=3)
    database.exact.stage_insert(txn, database.key(1, "Ada"), first, 0)
    database.exact.stage_insert(txn, database.key(2, "Grace"), second, 0)
    applied = {"calls": 0}
    real = IndexStore._apply_change

    def refusing(store: IndexStore, change: object, lsn: int) -> bool:
        applied["calls"] += 1
        if applied["calls"] == 2:
            raise GrafxBufferBudgetExceeded("No frame is free for this database.")
        return real(store, change, lsn)  # type: ignore[arg-type]

    monkeypatch.setattr(IndexStore, "_apply_change", refusing)
    with pytest.raises(GrafxBufferBudgetExceeded):
        database.exact.commit(txn, BORN)

    assert applied["calls"] == 2, "the refusal has to happen part way through, not before"
    assert database.exact.built_through_lsn == 0
    monkeypatch.undo()

    assert database.exact.commit(txn, BORN) == 2
    assert database.exact.built_through_lsn == BORN
    assert len(database.exact.walk()) == 2
    assert not database.exact.stale
    assert not header_on_device(database.device, database.exact.file).flags & INDEX_FLAG_STALE


def test_a_commit_that_failed_part_way_refuses_instead_of_answering_short() -> None:
    """The window between a failed commit and the next open, where a short index answers.

    Leaving the claimed position alone is not enough on its own. It makes the NEXT freshness
    check call the index stale -- and that check runs at ``open()``, so a caller that catches the
    failure and carries on meets a structure missing most of its entries that says it is fine.
    Every lookup for a row that did not land then answers EMPTY, which is the omission this
    component exists to refuse rather than return.
    """
    names = [f"n{index:03d}" for index in range(24)]
    small = build_database(name="tiny", budget_pages=2)
    txn = TransactionDouble(txn_id=7)
    for number, name in enumerate(names, start=1):
        ref = small.insert(number, name, BORN)
        small.manager.stage_row_insert(txn, small.table.table_id, ref, (number, name), BORN)
    small.device.refuse_write_number(1, GrafxStorageError("The device stopped.", file="x"))

    with pytest.raises(GrafxStorageError):
        small.manager.commit(txn, BORN)
    small.device.disarm()

    assert small.exact.stale, "a part-applied index may not answer"
    assert len(small.exact.walk()) < len(names), "the fixture really did land only some entries"
    with pytest.raises(GrafxIndexError) as refused:
        small.exact.lookup(small.exact.definition.key_for((1, names[0])), SnapshotDouble(BORN))
    assert refused.value.details["field"] == "stale"
    assert header_on_device(small.device, small.exact.file).flags & INDEX_FLAG_STALE


def test_abandoning_a_part_applied_commit_keeps_the_refusal(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollback is the caller saying the retry is not coming, so the index really is short.

    And the right to lift the mark dies with that transaction: a LATER transaction that happens
    to carry the same number never touched the changes that went missing, so it must not be able
    to clear a refusal it knows nothing about.
    """
    first = database.insert(1, "Ada", BORN)
    second = database.insert(2, "Grace", BORN)
    txn = TransactionDouble(txn_id=3)
    database.exact.stage_insert(txn, database.key(1, "Ada"), first, 0)
    database.exact.stage_insert(txn, database.key(2, "Grace"), second, 0)
    calls = {"n": 0}
    real = IndexStore._apply_change

    def refusing(store: IndexStore, change: object, lsn: int) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:
            raise GrafxBufferBudgetExceeded("No frame is free for this database.")
        return real(store, change, lsn)  # type: ignore[arg-type]

    monkeypatch.setattr(IndexStore, "_apply_change", refusing)
    with pytest.raises(GrafxBufferBudgetExceeded):
        database.exact.commit(txn, BORN)
    monkeypatch.undo()

    assert database.exact.rollback(txn) == 2
    assert database.exact.stale

    third = database.insert(3, "Hedy", ENDED)
    reused = TransactionDouble(txn_id=3)
    database.exact.stage_insert(reused, database.key(3, "Hedy"), third, 0)
    database.exact.commit(reused, ENDED)

    assert database.exact.stale, "a later transaction may not inherit the right to clear it"
    assert header_on_device(database.device, database.exact.file).flags & INDEX_FLAG_STALE
    assert database.exact.built_through_lsn == 0


def test_a_commit_that_fails_on_an_already_stale_index_still_needs_a_rebuild(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a commit that found the index HEALTHY may claim to be the reason it is stale.

    A retry converging proves the transaction's own changes all landed, and nothing more. Where
    the index was already stale for another reason -- a freshness verdict, which is about the log
    position and which no commit answers -- that older verdict owns the mark, and a completed
    retry must leave it exactly where it was.
    """
    ref = database.insert(1, "Ada", BORN)
    database.manager.stage_row_insert(
        TransactionDouble(txn_id=1), database.table.table_id, ref, (1, "Ada"), BORN
    )
    database.exact.mark_stale("the log has moved past what this index covers")
    second = database.insert(2, "Grace", BORN)
    txn = TransactionDouble(txn_id=4)
    database.exact.stage_insert(txn, database.key(2, "Grace"), second, 0)
    calls = {"n": 0}
    real = IndexStore._apply_change

    def refusing(store: IndexStore, change: object, lsn: int) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise GrafxBufferBudgetExceeded("No frame is free for this database.")
        return real(store, change, lsn)  # type: ignore[arg-type]

    monkeypatch.setattr(IndexStore, "_apply_change", refusing)
    with pytest.raises(GrafxBufferBudgetExceeded):
        database.exact.commit(txn, BORN)
    monkeypatch.undo()

    assert database.exact.commit(txn, BORN) == 1

    assert database.exact.stale, "the freshness verdict still owns the mark"
    assert header_on_device(database.device, database.exact.file).flags & INDEX_FLAG_STALE


def test_staging_the_same_entry_twice_in_one_transaction_stores_it_once(
    database: Database,
) -> None:
    """An entry is a set member, and the same idempotence holds on the live path as on redo.

    Two entries for one key and one location would be returned twice by every lookup, which is a
    duplicated row in a result set rather than merely a wasted slot.
    """
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    key = database.key(1, "Ada")
    database.exact.stage_insert(txn, key, ref, 0)
    database.exact.stage_insert(txn, key, ref, 0)

    assert database.exact.commit(txn, BORN) == 2
    assert database.manager.lookup("person_by_name", key, SnapshotDouble(BORN)) == (ref,)
    assert len(database.exact.walk()) == 1


def test_committing_the_same_transaction_twice_applies_it_once(database: Database) -> None:
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(txn, database.table.table_id, ref, (1, "Ada"), BORN)

    assert database.manager.commit(txn, BORN) == 2
    assert database.manager.commit(txn, BORN) == 0
    assert len(database.exact.walk()) == 1


def test_a_record_for_an_index_this_database_does_not_have_is_reported_not_raised(
    database: Database,
) -> None:
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(txn, database.table.table_id, ref, (1, "Ada"), BORN)
    record = txn.staged[0]
    # A manager that has registered nothing is exactly the state recovery meets when an index
    # was dropped after the record was written: the record is not an error, it is history.
    empty = IndexManager(database.pool, database.heap, database.metrics)

    assert empty.apply(record.with_lsn(1)) is False


def test_a_record_offered_to_the_wrong_index_is_refused(database: Database) -> None:
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(txn, database.table.table_id, ref, (1, "Ada"), BORN)
    for_exact = txn.staged[0]

    with pytest.raises(GrafxIndexError) as refused:
        database.proximity.apply(for_exact.with_lsn(5))

    assert refused.value.details["field"] == "index"
    assert refused.value.details["index"] == "person_near_name"


def test_a_record_of_another_type_is_not_an_index_record(database: Database) -> None:
    from okto_grafx.domain.wal.record import WalRecord

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.apply(
            WalRecord(record_type=int(WalRecordType.COMMIT), payload=b"", lsn=1)
        )

    assert refused.value.details["field"] == "record_type"


def test_a_commit_puts_the_index_pages_on_the_device(database: Database) -> None:
    """A page that only exists in this process is invisible to every other one.

    The commit is about to publish a position that says the entry is there, so the pages go to
    the device before that. It is a flush and not a barrier: the log is the authority on
    durability and the redo is idempotent (CONTRACT.md section 8.5 step 6).
    """
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(txn, database.table.table_id, ref, (1, "Ada"), BORN)
    database.device.barriers.clear()
    # The write trail is CUMULATIVE, and registering an index now flushes its fresh pages, so
    # both index files are already in it before this commit runs. Reading the trail without
    # clearing it first asserts that the pages reached the device at SOME point, which the
    # creation flush satisfies on its own -- and the test then passes with the commit's flush
    # deleted. Clearing here is what keeps the assertion about the commit (amendment A83.1: a fix
    # that removes a failure mode un-tests the test that needed it).
    database.device.write_calls.clear()
    database.manager.commit(txn, BORN)

    written = {file for file, _page in database.device.write_calls}
    assert "index/person_by_name.idx" in written
    assert "index/person_near_name.idx" in written
    assert database.device.barriers == []


def test_the_page_that_holds_an_entry_carries_the_position_of_the_commit(
    database: Database,
) -> None:
    """CONTRACT.md section 6.3 defines page_lsn as the position of the last record applied."""
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(txn, database.table.table_id, ref, (1, "Ada"), BORN)
    database.manager.commit(txn, BORN)

    entry = database.exact.walk()[0]
    with database.pool.pinned(database.exact.file, entry.page) as page:
        assert page.page_lsn == BORN


def test_the_position_an_index_claims_moves_only_when_a_commit_lands(
    database: Database,
) -> None:
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(txn, database.table.table_id, ref, (1, "Ada"), BORN)

    assert database.exact.built_through_lsn == 0
    database.manager.commit(txn, BORN)
    assert database.exact.built_through_lsn == BORN


def test_staging_refuses_a_transaction_that_cannot_carry_a_record(
    database: Database,
) -> None:
    class NotATransaction:
        txn_id = 1

    with pytest.raises(GrafxIndexError) as refused:
        database.exact.stage_insert(NotATransaction(), b"k", RecordRef(1, 1), 0)

    assert refused.value.details["field"] == "txn"


def test_ending_an_entry_needs_the_number_it_ended_at(database: Database) -> None:
    txn = TransactionDouble(txn_id=3)

    with pytest.raises(GrafxIndexError) as refused:
        database.exact.stage_delete(txn, b"k", RecordRef(1, 1), 0)

    assert refused.value.details["field"] == "csn"
    # The change value refuses the same input a moment later with the same class and the same
    # field, so the class alone cannot say which guard answered. Only the store's refusal names
    # the index it was asked of, and that is what tells the two apart (amendment A62).
    assert refused.value.details["index"] == database.exact.name
    assert txn.staged == []


def test_a_change_payload_with_a_trailing_byte_is_refused(database: Database) -> None:
    """The declared lengths and the payload size have to agree, or the key is whatever is left.

    Without the check the trailing byte is simply absorbed into the key, so the record decodes
    into an entry filed under a key nobody wrote -- silently, and only for records that were
    damaged in transit.
    """
    from okto_grafx.domain.index import IndexChange

    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.exact.stage_insert(txn, database.key(1, "Ada"), ref, 0)
    payload = txn.staged[0].payload

    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexChange.decode(payload + b"\x00")

    assert refused.value.details["field"] == "payload"
    assert IndexChange.decode(payload).key == database.key(1, "Ada")
