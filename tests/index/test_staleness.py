"""A stale index refuses rather than omits, and a rebuild is how it is repaired (FR-12, BR-11).

The failure this file is about is the quietest one an index has. A crash between a heap write and
the index update, an index created on a table that already has rows, a file restored from an
older copy: in every case the structure is behind the heap, and the symptom is a lookup that
returns fewer rows than it should. An empty answer and a short answer look identical to a caller,
which is why the index must not answer at all.

The detector compares the position the file claims with the committed physical history of its
own table, under the database's published ceiling, and the gate on the read path is one durable
flag. They are kept apart on purpose: a test can set the flag with no published position, and can
move the required position with no flag already set, so neither can pass for the other
(amendment A67).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.engine.index_manager import INDEX_FLAG_STALE, HashIndex

from .conftest import (
    Database,
    RecordingMetrics,
    SnapshotDouble,
    TransactionDouble,
    build_database,
    cold_view,
    exact_definition,
    header_on_device,
    make_pool,
)

BORN: int = 10
ENDED: int = 20


def _commit_row(database: Database, record_id: int, name: str, csn: int) -> object:
    """Insert a row and commit the index changes it owes."""
    txn = TransactionDouble(txn_id=record_id)
    ref = database.insert(record_id, name, csn)
    database.manager.stage_row_insert(
        txn, database.table.table_id, ref, (record_id, name), csn
    )
    database.manager.commit(txn, csn)
    return ref


def test_an_index_behind_the_published_position_is_marked_stale(
    database: Database,
) -> None:
    _commit_row(database, 1, "Ada", BORN)
    database.insert(
        2, "Grace", BORN + 5
    )  # committed heap row omitted from both indexes

    stale = database.manager.open(BORN + 5)

    assert {index.name for index in stale} == {"person_by_name", "person_near_name"}
    assert database.exact.header.flags & INDEX_FLAG_STALE
    assert "published" in (database.exact.stale_reason or "")


def test_an_index_level_with_the_published_position_is_not_stale(
    database: Database,
) -> None:
    _commit_row(database, 1, "Ada", BORN)

    assert database.manager.open(BORN) == ()
    assert not database.exact.header.flags & INDEX_FLAG_STALE
    assert database.exact.stale_reason is None


def test_a_commit_to_another_table_does_not_rewrite_or_stale_this_index(
    database: Database,
) -> None:
    """The table watermark removes page-0 fanout without weakening the freshness gate."""
    person = _commit_row(database, 1, "Ada", BORN)
    assert database.manager.open(BORN) == ()
    book = TableDef(
        table_id=database.catalog.catalog.next_table_id(),
        name="Book",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="title", type=ValueType.STRING),
        ),
        primary_key="id",
    )
    database.catalog.catalog.add_table(book)
    database.catalog.save()
    book_index = HashIndex(
        exact_definition(book, name="book_by_title", columns=("title",)),
        database.pool,
        database.metrics,
    )
    database.manager.register(book_index)
    exact_before = database.device.raw_page(database.exact.file, 0)
    proximity_before = database.device.raw_page(database.proximity.file, 0)
    database.device.write_calls.clear()

    ref = database.heap.insert(book, 1, (1, "Graph Databases"), ENDED)
    txn = TransactionDouble(txn_id=41)
    txn.row_intents = (SimpleNamespace(table=book),)
    database.manager.stage_row_insert(
        txn, book.table_id, ref, (1, "Graph Databases"), ENDED
    )
    database.manager.commit(txn, ENDED)

    assert database.device.raw_page(database.exact.file, 0) == exact_before
    assert database.device.raw_page(database.proximity.file, 0) == proximity_before
    assert all(
        file not in {database.exact.file, database.proximity.file}
        for file, _page in database.device.write_calls
    )
    assert database.exact.built_through_lsn == BORN
    assert book_index.built_through_lsn == ENDED
    assert database.manager.lookup(
        database.exact.name, database.key(1, "Ada"), SnapshotDouble(ENDED)
    ) == (person,)
    assert database.manager.lookup(
        database.proximity.name, database.key(1, "Ada"), SnapshotDouble(ENDED)
    ) == (person,)
    assert database.manager.open(ENDED) == ()


def test_committed_heap_state_ahead_of_the_published_ceiling_fails_closed(
    database: Database,
) -> None:
    database.insert(1, "Ada", ENDED)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.manager.open(BORN)

    assert refused.value.details["field"] == "table_high_water"
    assert refused.value.details["table"] == "Person"
    assert refused.value.details["value"] == ENDED


def test_a_local_table_write_that_omits_its_indexes_refuses_immediately(
    database: Database,
) -> None:
    """A missing observation durably refuses before this handle can answer short."""
    _commit_row(database, 1, "Ada", BORN)
    assert database.manager.open(BORN) == ()
    database.insert(2, "Grace", ENDED)
    defective = TransactionDouble(txn_id=42)
    defective.row_intents = (SimpleNamespace(table=database.table),)

    database.manager.commit(defective, ENDED)

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.lookup(
            database.exact.name,
            database.key(2, "Grace"),
            SnapshotDouble(ENDED),
        )
    assert refused.value.details["field"] == "stale"
    assert (
        header_on_device(database.device, database.exact.file).flags & INDEX_FLAG_STALE
    )


def test_a_foreign_live_reader_observes_a_durable_refusal_for_an_omitted_index(
    database: Database,
) -> None:
    """A cached table watermark cannot hide another participant's short commit."""
    _commit_row(database, 1, "Ada", BORN)
    reader = cold_view(database)
    assert reader.manager.open(BORN) == ()
    database.insert(2, "Grace", ENDED)
    defective = TransactionDouble(txn_id=43)
    defective.row_intents = (SimpleNamespace(table=database.table),)

    database.manager.commit(defective, ENDED)

    assert not reader.exact.stale  # this handle still has its older in-memory verdict
    with pytest.raises(GrafxIndexError) as refused:
        reader.manager.lookup(
            reader.exact.name,
            database.key(2, "Grace"),
            SnapshotDouble(ENDED),
        )
    assert refused.value.details["field"] == "index_view_unavailable"
    assert (
        header_on_device(database.device, database.exact.file).flags & INDEX_FLAG_STALE
    )


def test_an_untouched_table_does_not_become_stale_at_a_later_replay_floor(
    database: Database,
) -> None:
    assert database.manager.check_replay_floor(ENDED) == ()
    assert not database.exact.stale
    assert not database.proximity.stale


def test_later_heap_write_does_not_hide_a_real_pre_floor_omission(database: Database) -> None:
    """Historical floor calculation must still reject a genuinely short baseline."""
    _commit_row(database, 1, "Ada", BORN)
    database.insert(2, "omitted before checkpoint", BORN + 5)
    database.insert(3, "after checkpoint", ENDED + 20)
    stale = database.manager.check_replay_floor(ENDED)
    assert {index.name for index in stale} == {"person_by_name", "person_near_name"}


def test_an_index_ahead_of_the_published_position_is_marked_stale(
    database: Database,
) -> None:
    """A future watermark may belong to another database state and cannot be trusted."""
    # ST-7: the manager's completion mark now leaves a header alone when it already covers its
    # table, so the foreign-file scenario is built the way it actually happens -- the FILES
    # claim the future position themselves.
    database.exact.advance_built_through(ENDED)
    database.proximity.advance_built_through(ENDED)

    stale = database.manager.open(BORN)

    assert {index.name for index in stale} == {"person_by_name", "person_near_name"}
    assert "ahead" in (database.exact.stale_reason or "")
    assert (
        header_on_device(database.device, database.exact.file).flags & INDEX_FLAG_STALE
    )


def test_checking_a_replay_floor_does_not_regress_the_published_position(
    database: Database,
) -> None:
    """A floor is a lower replay bound, not a new published ceiling."""
    database.manager.mark_built_through(ENDED)
    assert database.manager.published_lsn == ENDED

    stale = database.manager.check_replay_floor(BORN)

    assert stale == ()
    assert database.manager.published_lsn == ENDED
    assert not database.exact.stale
    assert not database.proximity.stale


def test_a_stale_index_refuses_to_answer_rather_than_omitting_a_row(
    database: Database,
) -> None:
    """The whole point: a short answer is a wrong answer that looks like an empty one."""
    ref = _commit_row(database, 1, "Ada", BORN)
    key = database.key(1, "Ada")
    assert database.manager.lookup("person_by_name", key, SnapshotDouble(BORN)) == (
        ref,
    )

    database.insert(2, "Grace", BORN + 5)
    database.manager.open(BORN + 5)

    for name in ("person_by_name", "person_near_name"):
        with pytest.raises(GrafxIndexError) as refused:
            database.manager.lookup(name, key, SnapshotDouble(BORN))
        assert refused.value.details["field"] == "stale"
        assert refused.value.details["index"] == name


def test_a_stale_index_still_accepts_the_changes_of_new_transactions(
    database: Database,
) -> None:
    """Refusing writes would take the whole table down for a structure that is only derived.

    Reads lie when an index is short; writes do not. The entries a stale index receives are
    correct, they are simply not all of the entries it should have, so they are accepted and the
    index stays stale until it is rebuilt.
    """
    _commit_row(database, 1, "Ada", BORN)
    database.insert(99, "Missing", BORN + 5)
    database.manager.open(BORN + 5)

    ref = _commit_row(database, 2, "Grace", BORN + 10)

    assert (database.key(2, "Grace"), ref, False, 0, 0) in database.entries()[
        "person_by_name"
    ]


def test_a_stale_index_does_not_stop_looking_stale_by_seeing_a_newer_commit(
    database: Database,
) -> None:
    """An index missing entries does not become complete by receiving a later one.

    Letting the claimed position move on a commit into a stale index is how it would quietly
    stop looking stale while still omitting every row it never received.
    """
    _commit_row(database, 1, "Ada", BORN)
    database.insert(99, "Missing", BORN + 5)
    database.manager.open(BORN + 5)
    before = database.exact.built_through_lsn

    _commit_row(database, 2, "Grace", BORN + 10)

    assert database.exact.built_through_lsn == before
    assert database.exact.stale
    with pytest.raises(GrafxIndexError):
        database.manager.lookup(
            "person_by_name", database.key(2, "Grace"), SnapshotDouble(BORN + 10)
        )


def test_a_rebuild_re_derives_every_entry_from_the_heap(database: Database) -> None:
    """The repair, and the crash it repairs.

    The row is written to the heap and committed with no index change at all, which is exactly
    what a crash between the two leaves once the log has been replayed for the heap alone. The
    index is short, it is detected, it refuses, and the rebuild puts it right.
    """
    ref = database.insert(1, "Ada", BORN)
    key = database.key(1, "Ada")
    assert database.manager.open(BORN) != ()

    txn = TransactionDouble(txn_id=50)
    staged = database.manager.rebuild("person_by_name", txn, BORN)
    database.manager.commit(txn, BORN)
    database.manager.clear_stale("person_by_name", BORN)

    assert staged == 2, "one reset plus one insert"
    assert database.manager.lookup("person_by_name", key, SnapshotDouble(BORN)) == (
        ref,
    )
    assert database.manager.verify("person_by_name") == ()


def test_a_repaired_index_stays_repaired_across_the_next_freshness_check(
    database: Database,
) -> None:
    """A repair that does not stick is not a repair.

    A stale index refuses to move the position it claims, which is what stops it looking fresh
    while it is broken. The consequence is that clearing the flag has to say what the rebuild
    covered: leave the position where it was and the very next check calls the index stale again,
    forever, with nothing to say why.
    """
    ref = database.insert(1, "Ada", BORN)
    database.manager.open(BORN)
    txn = TransactionDouble(txn_id=54)
    database.manager.rebuild("person_by_name", txn, BORN)
    database.manager.commit(txn, BORN)
    database.manager.clear_stale("person_by_name", BORN)

    assert database.exact.built_through_lsn == BORN
    assert "person_by_name" not in {index.name for index in database.manager.open(BORN)}
    assert database.manager.lookup(
        "person_by_name", database.key(1, "Ada"), SnapshotDouble(BORN)
    ) == (ref,)


def test_a_rebuild_carries_the_stamps_of_every_stored_version(
    database: Database,
) -> None:
    """A proximity index rebuilt from the heap must reproduce the tombstones too.

    Rebuilding only the live rows would leave an older snapshot with no entry for a row it can
    still see, which is the same omission the rebuild exists to repair, arriving from the repair.
    """
    ref = database.insert(1, "Ada", BORN)
    database.heap.delete(database.table, ref, ENDED)
    database.manager.open(ENDED)

    txn = TransactionDouble(txn_id=51)
    database.manager.rebuild("person_near_name", txn, ENDED)
    database.manager.commit(txn, ENDED)
    database.manager.clear_stale("person_near_name", ENDED)

    key = database.key(1, "Ada")
    assert database.manager.lookup(
        "person_near_name", key, SnapshotDouble(ENDED - 1)
    ) == (ref,)
    assert database.manager.lookup("person_near_name", key, SnapshotDouble(ENDED)) == ()


def test_a_rebuild_that_is_staged_and_never_committed_leaves_the_index_stale(
    database: Database,
) -> None:
    """A repair that the log never accepted has not happened."""
    database.insert(1, "Ada", BORN)
    database.manager.open(BORN)
    txn = TransactionDouble(txn_id=52)

    database.manager.rebuild("person_by_name", txn, BORN)

    assert database.exact.stale
    assert database.entries()["person_by_name"] == ()
    with pytest.raises(GrafxIndexError):
        database.manager.lookup(
            "person_by_name", database.key(1, "Ada"), SnapshotDouble(BORN)
        )


def test_a_rebuild_replaces_what_the_index_held_rather_than_adding_to_it(
    database: Database,
) -> None:
    """A rebuild REPLACES, so an entry the heap does not justify has to disappear.

    Re-inserting the entries the heap justifies is idempotent, so a rebuild with no reset looks
    correct for as long as the index held nothing wrong. The reset is what the rebuild exists
    for: it is the repair of an index that has entries the heap cannot account for, and without
    it the repair leaves exactly those entries in place.
    """
    ref = _commit_row(database, 1, "Ada", BORN)
    invented = TransactionDouble(txn_id=52)
    from okto_grafx.domain.ids import RecordRef

    database.exact.stage_insert(invented, b"a key no row carries", RecordRef(1, 9), 0)
    database.exact.commit(invented, BORN)
    assert len(database.entries()["person_by_name"]) == 2

    txn = TransactionDouble(txn_id=53)
    database.manager.rebuild("person_by_name", txn, BORN)
    database.manager.commit(txn, BORN)

    entries = database.entries()["person_by_name"]

    assert entries == ((database.key(1, "Ada"), ref, False, 0, 0),)


def test_declaring_a_replay_complete_keeps_an_untouched_index_out_of_a_rebuild(
    database: Database,
) -> None:
    """Recovery knows something no index can: that the replay it just finished was complete.

    Without this door, an index that none of the final commits happened to touch would be
    indistinguishable from one that missed them, and every open after a crash would rebuild
    indexes that were never behind.
    """
    _commit_row(database, 1, "Ada", BORN)

    database.manager.mark_built_through(BORN + 40)

    assert database.manager.open(BORN + 40) == ()
    # ST-7 (reopening R3-do-plano): the header already covers its table, which is the
    # strongest position open() ever requires, so the declaration no longer rewrites it to
    # the global number -- the index stays out of a rebuild either way.
    assert database.exact.built_through_lsn == BORN


def test_a_file_written_under_another_definition_is_refused_and_not_rebuilt(
    database: Database,
) -> None:
    """A different definition is a different question, and rebuilding would answer the wrong one."""
    from okto_grafx.domain.index import IndexDefinition, IndexVisibility
    from okto_grafx.engine.index_manager import HashIndex

    other = IndexDefinition.on(
        database.table,
        name="person_by_name",
        columns=("id",),
        visibility=IndexVisibility.EXACT,
        bucket_count=database.exact.definition.bucket_count,
    )
    impostor = HashIndex(other, database.pool, database.metrics)

    with pytest.raises(GrafxIndexError) as refused:
        impostor.open()

    assert refused.value.details["field"] == "digest"


def test_the_position_an_index_claims_never_moves_backwards(database: Database) -> None:
    """A record replayed out of order must not make a current index look behind.

    The position only ever rises. Letting it fall would turn a fresh index into a stale one on
    the strength of an older record -- and the remedy for staleness is a full rebuild, so the
    cost of getting this wrong is paid over the whole table.
    """
    ref = database.insert(1, "Ada", BORN)
    late = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(
        late, database.table.table_id, ref, (1, "Ada"), BORN
    )
    database.manager.commit(late, 100)
    assert database.exact.built_through_lsn == 100

    early = TransactionDouble(txn_id=4)
    second = database.insert(2, "Grace", BORN)
    database.manager.stage_row_insert(
        early, database.table.table_id, second, (2, "Grace"), BORN
    )
    for position, record in enumerate(early.staged):
        database.manager.apply(record.with_lsn(50 + position))

    assert database.exact.built_through_lsn == 100
    assert database.manager.open(100) == ()


def test_a_header_keeps_the_higher_of_the_two_positions_it_is_offered() -> None:
    """The same rule at the level that owns it, where nothing else can answer for it."""
    from okto_grafx.domain.index import IndexHeader, IndexVisibility

    header = IndexHeader(
        visibility=IndexVisibility.EXACT,
        table_id=1,
        bucket_count=4,
        digest=bytes(16),
        built_through_lsn=100,
        reconciled_through_lsn=100,
    )

    assert header.advanced_to(50) is header
    assert header.advanced_to(100) is header
    assert header.advanced_to(101).built_through_lsn == 101
    assert header.reconciled_to(50) is header
    assert header.reconciled_to(101).reconciled_through_lsn == 101


def test_a_freshness_check_refuses_a_position_that_is_not_one(
    database: Database,
) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        database.exact.check_freshness(-1)

    assert refused.value.details["field"] == "published_lsn"


def test_the_stale_flag_reaches_the_device_and_not_only_the_page_cache(
    database: Database,
) -> None:
    """The verdict is DURABLE, asserted against the device rather than through the writer.

    This is the assertion the old shape of this test could not make. Reopening the file through
    ``database.pool`` reads the page the mark was written into, so the cache answers and the test
    passes whatever the platter says -- exactly the defect a durability claim is most likely to
    have, because the check and the thing checked share the cache. Nothing in the log covers this
    flag, so no redo can put it back: if it is not on the device it does not exist.
    """
    _commit_row(database, 1, "Ada", BORN)
    database.insert(2, "Grace", BORN + 5)

    database.manager.open(BORN + 5)

    for index in (database.exact, database.proximity):
        stored = header_on_device(database.device, index.file)
        assert stored.flags & INDEX_FLAG_STALE, (
            f"{index.name} is stale in this process and the device says it is fine"
        )


def test_another_participant_reads_the_refusal_off_the_device(
    database: Database,
) -> None:
    """The same property as the consequence a second process meets, through a cold cache."""
    ref = _commit_row(database, 1, "Ada", BORN)
    database.insert(2, "Grace", BORN + 5)
    database.manager.open(BORN + 5)

    other = cold_view(database)

    assert other.exact.stale
    with pytest.raises(GrafxIndexError) as refused:
        other.manager.lookup(
            "person_by_name", database.key(1, "Ada"), SnapshotDouble(BORN)
        )
    assert refused.value.details["field"] == "stale"
    assert ref is not None


def test_a_crash_after_the_mark_does_not_let_a_complete_replay_declare_it_fresh(
    database: Database,
) -> None:
    """The end-to-end failure the durable flag exists to stop, every step a sanctioned door.

    An index registered over a table that already holds rows is behind the heap from the moment
    it is created, and nothing in the log says so -- the rows were written before it existed, so
    a replay of the whole log touches it not once. Recovery is then entitled to call
    ``mark_built_through`` on a complete replay, and the only thing standing between that and an
    index that claims to cover everything while holding nothing is the mark on the device. Lose
    the mark, and the position moves, the index opens fresh, and every lookup answers EMPTY for
    rows the heap is holding.
    """
    first = _commit_row(database, 1, "Ada", BORN)
    second = _commit_row(database, 2, "Ada", BORN)
    key = database.key(1, "Ada")
    assert database.manager.lookup("person_by_name", key, SnapshotDouble(BORN)) == (
        first,
        second,
    )

    late = HashIndex(
        exact_definition(database.table, name="person_by_name_late"),
        database.pool,
        database.metrics,
    )
    database.manager.register(late)
    assert late in database.manager.open(BORN + 30)

    # The crash: this pool is discarded and never flushed, so only what reached the device is
    # left. Recovery replays a log that carries no record for this index and says so.
    survivor = cold_view(database)
    recovered = HashIndex(late.definition, survivor.pool, RecordingMetrics())
    survivor.manager.register(recovered)
    survivor.manager.mark_built_through(BORN + 30)

    assert recovered in survivor.manager.open(BORN + 30)
    assert recovered.built_through_lsn == 0, (
        "a stale index may not move the position it claims"
    )
    with pytest.raises(GrafxIndexError) as refused:
        survivor.manager.lookup("person_by_name_late", key, SnapshotDouble(BORN + 30))
    assert refused.value.details["field"] == "stale"


def test_a_crash_after_marking_stale_by_hand_does_not_answer_a_short_set(
    database: Database,
) -> None:
    """The second route to the same omission, with no freshness check anywhere in it.

    ``mark_stale`` is a door of its own -- a caller that KNOWS the index is behind says so -- and
    it must be as durable as the verdict a freshness check reaches, or the index answers a short
    set after a restart while the caller believes it refused.
    """
    first = _commit_row(database, 1, "Ada", BORN)
    key = database.key(1, "Ada")
    second = database.insert(2, "Ada", BORN)  # a heap row no index was ever told about
    assert first != second

    database.exact.mark_stale("a heap row never reached this index")

    survivor = cold_view(database)
    # The freshness check is given a position the index already claims, so nothing here can
    # re-derive the verdict: the only thing that can refuse is the mark on the device.
    assert survivor.manager.open(BORN) == (survivor.exact,)
    assert len(survivor.exact.walk()) == 1, (
        "the index really is one entry short of the heap"
    )
    with pytest.raises(GrafxIndexError) as refused:
        survivor.manager.lookup("person_by_name", key, SnapshotDouble(BORN + 5))
    assert refused.value.details["field"] == "stale"


def test_clearing_the_mark_reaches_the_device_too(database: Database) -> None:
    """The pair must be symmetrical, or two participants disagree about one index.

    A durable mark that only a cached clear can undo means this process reads a repaired index
    and every other process goes on refusing it -- which is worse than either verdict on its own,
    because neither participant can tell that the other disagrees.
    """
    _commit_row(database, 1, "Ada", BORN)
    database.exact.mark_stale("operator-declared test refusal")
    assert (
        header_on_device(database.device, database.exact.file).flags & INDEX_FLAG_STALE
    )

    database.manager.clear_stale("person_by_name", BORN)

    stored = header_on_device(database.device, database.exact.file)
    assert not stored.flags & INDEX_FLAG_STALE
    assert stored.built_through_lsn == BORN
    other = cold_view(database)
    assert not other.exact.stale


def test_a_completed_replay_is_recorded_on_the_device(database: Database) -> None:
    """Recovery's declaration is unlogged too, and losing it costs a rebuild nobody needed."""
    _commit_row(database, 1, "Ada", BORN)

    database.manager.mark_built_through(BORN + 30)

    # ST-7: the declaration reaches the device exactly when an index NEEDS it -- pinned by
    # test_an_index_behind_its_table_still_advances_to_a_durable_header. A header already
    # covering its table is left as the commit flushed it, and a cold participant still
    # accepts it, because open() requires the table's high water, never a global number.
    assert (
        header_on_device(database.device, database.exact.file).built_through_lsn == BORN
    )
    other = cold_view(database)
    assert other.manager.open(BORN + 30) == ()


def test_a_reconciliation_horizon_reaches_the_device_so_verify_does_not_cry_wolf(
    database: Database,
) -> None:
    """The removals a pass made are logged; the horizon that authorised them is not.

    Keep the removals and lose the horizon and the two halves disagree in the dangerous
    direction: ``_verify_coverage`` reads the horizon to tell an entry a pass correctly reclaimed
    from one that went missing, so a horizon back at zero turns every reclaimed row into a
    ``missing_entry`` finding on an index that is perfectly clean.
    """
    ref = _commit_row(database, 1, "Ada", BORN)
    ending = TransactionDouble(txn_id=2)
    database.manager.stage_row_delete(
        ending, database.table.table_id, ref, (1, "Ada"), ENDED
    )
    database.manager.commit(ending, ENDED)
    database.heap.delete(database.table, ref, ENDED)
    pass_txn = TransactionDouble(txn_id=3)
    database.proximity.reconcile(ENDED + 10, pass_txn)
    database.proximity.commit(pass_txn, ENDED + 10)

    database.manager.note_reconciled(ENDED + 10)

    stored = header_on_device(database.device, database.proximity.file)
    assert stored.reconciled_through_lsn == ENDED + 10
    other = cold_view(database)
    assert other.manager.verify("person_near_name") == ()


def test_clearing_the_stale_flag_lets_the_index_answer_again(
    database: Database,
) -> None:
    ref = _commit_row(database, 1, "Ada", BORN)
    database.exact.mark_stale("operator-declared test refusal")

    database.manager.clear_stale("person_by_name", BORN)

    assert not database.exact.header.flags & INDEX_FLAG_STALE
    assert database.manager.lookup(
        "person_by_name", database.key(1, "Ada"), SnapshotDouble(BORN)
    ) == (ref,)


def test_a_second_database_does_not_share_the_registry_of_the_first() -> None:
    """No module-level state anywhere (FR-13, BR-8), stated for this component."""
    first = build_database(name="first")
    second = build_database(name="second")

    first.exact.mark_stale("first database only")

    assert first.exact.stale
    assert not second.exact.stale


def test_a_file_that_says_stale_refuses_before_any_freshness_check(
    database: Database,
) -> None:
    """The file's own flag has to refuse on its own, with no comparison to help it.

    Through :meth:`IndexManager.register` this is invisible: register calls ``create()``, which
    opens the file, and THEN ``check_freshness``, and both read the flag -- so removing either
    one leaves the other answering and every test still passes. The door where only ``open()``
    runs is an index built directly on a pool and read directly, which is a public door of this
    component and the one a caller takes when it has no published position to compare against.
    A freshness check needs a number that comes from outside the index; the flag needs nothing,
    which is exactly why it is the thing that survives a restart.
    """
    _commit_row(database, 1, "Ada", BORN)
    database.exact.mark_stale("durable test refusal")

    cold = HashIndex(
        exact_definition(database.table),
        make_pool(database.device, RecordingMetrics()),
        RecordingMetrics(),
    )
    cold.open()

    assert cold.stale, "the verdict came from the file and nothing else"
    with pytest.raises(GrafxIndexError) as refused:
        cold.candidates(database.key(1, "Ada"))
    assert refused.value.details["field"] == "stale"


def test_marking_an_index_stale_needs_a_reason_a_reader_can_act_on(
    database: Database,
) -> None:
    """A refusal that names nothing is a dead end for whoever meets it.

    The reason is the whole payload of the refusal: it travels into ``_require_readable`` and out
    of every lookup the index then declines, so an empty one leaves an operator holding an error
    that says an index may not be read and not one word about why. Both halves are checked --
    the wrong TYPE and the empty STRING -- because a guard that only rejects non-strings accepts
    the empty one, and that is the shape this test exists to keep out.
    """
    for useless in ("", None, 7):
        with pytest.raises(GrafxIndexError) as refused:
            database.exact.mark_stale(useless)  # type: ignore[arg-type]
        assert refused.value.details["field"] == "reason"

    assert not database.exact.stale
    assert (
        not header_on_device(database.device, database.exact.file).flags
        & INDEX_FLAG_STALE
    )
