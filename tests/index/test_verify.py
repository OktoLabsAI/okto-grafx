"""Walking the indexes against the heap and naming every divergence (FR-11, BR-11, AC-12).

Both directions are checked and only one of them is a wrong answer.

An entry the heap does not confirm is a STALE entry. The exact contract absorbs it -- every hit
is validated -- and the proximity contract does not, so the two kinds get different names for the
same shape and an operator can tell a cost from a defect.

A row with no entry is an OMISSION, and that is a wrong answer under either contract: the caller
asked by key and was told the row is not there. It is reported the same way whichever kind of
index left it out.
"""

from __future__ import annotations

from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index import IndexDefinition, IndexVisibility
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.engine.index_manager import HashIndex, IndexFinding

from .conftest import TEST_BUCKET_COUNT, Database, TransactionDouble

BORN: int = 10
ENDED: int = 20


def _kinds(findings: tuple[IndexFinding, ...]) -> list[str]:
    """Return the kind of every finding, in order."""
    return [finding.kind for finding in findings]


def _commit(database: Database, record_id: int, name: str, csn: int) -> RecordRef:
    """Insert a row and commit the index changes it owes."""
    txn = TransactionDouble(txn_id=record_id)
    ref = database.insert(record_id, name, csn)
    database.manager.stage_row_insert(txn, database.table.table_id, ref, (record_id, name), csn)
    database.manager.commit(txn, csn)
    return ref


def test_a_clean_database_produces_an_empty_report(database: Database) -> None:
    _commit(database, 1, "Ada", BORN)
    _commit(database, 2, "Grace", BORN)

    assert database.manager.verify() == ()


def test_a_row_with_no_entry_is_reported_as_an_omission(database: Database) -> None:
    """The crash between a heap write and its index update, seen by the verifier."""
    ref = database.insert(1, "Ada", BORN)

    findings = database.manager.verify()

    assert _kinds(findings) == ["missing_entry", "missing_entry"]
    assert {finding.index for finding in findings} == {"person_by_name", "person_near_name"}
    for finding in findings:
        assert finding.page == ref.page
        assert finding.slot == ref.slot
        assert finding.lsn == BORN
        assert finding.location()["index"] == finding.index


def test_an_entry_the_heap_does_not_confirm_is_named_by_the_contract_it_breaks(
    database: Database,
) -> None:
    """The same shape, two names: what an exact index absorbs, a proximity index returns."""
    ref = database.insert(1, "Ada", BORN)
    key = database.key(1, "Ada")
    new_ref = database.heap.update(database.table, ref, (1, "Grace"), ENDED)
    txn = TransactionDouble(txn_id=5)
    database.exact.stage_insert(txn, key, new_ref, 0)
    database.proximity.stage_insert(txn, key, new_ref, ENDED)
    database.manager.commit(txn, ENDED)

    findings = database.manager.verify()
    kinds = {
        name: {finding.kind for finding in findings if finding.index == name}
        for name in ("person_by_name", "person_near_name")
    }

    assert "stale_entry" in kinds["person_by_name"]
    assert "index_heap_divergence" not in kinds["person_by_name"]
    assert "index_heap_divergence" in kinds["person_near_name"]
    assert "stale_entry" not in kinds["person_near_name"]


def test_an_entry_pointing_at_a_location_the_heap_cannot_read_is_reported(
    database: Database,
) -> None:
    txn = TransactionDouble(txn_id=5)
    database.exact.stage_insert(txn, database.key(1, "Ada"), RecordRef(9999, 0), 0)
    database.exact.commit(txn, BORN)

    findings = database.manager.verify("person_by_name")

    assert _kinds(findings) == ["unreadable_reference"]
    assert findings[0].ref == RecordRef(9999, 0)
    assert findings[0].file == "index/person_by_name.idx"


def test_a_damaged_index_page_is_reported_rather_than_raised(database: Database) -> None:
    _commit(database, 1, "Ada", BORN)
    entry = database.exact.walk()[0]
    database.pool.flush()
    database.device.poke_page(
        database.exact.file, entry.page, b"\x00" * database.device.page_size
    )
    database.pool.invalidate()

    findings = database.manager.verify("person_by_name")

    assert _kinds(findings) == ["index_page_damaged"]
    assert findings[0].page == entry.page


def test_a_deleted_row_whose_entry_was_never_tombstoned_is_reported(
    database: Database,
) -> None:
    """For a proximity index this is the shape that returns a deleted row to a caller."""
    ref = _commit(database, 1, "Ada", BORN)
    database.heap.delete(database.table, ref, ENDED)

    findings = database.manager.verify()
    by_index = {finding.index: finding for finding in findings}

    assert by_index["person_near_name"].kind == "missing_tombstone"
    assert by_index["person_by_name"].kind == "stale_stamp"
    assert by_index["person_near_name"].lsn == ENDED


def test_an_entry_a_reconciliation_released_is_not_reported_as_missing(
    database: Database,
) -> None:
    """Below the recorded horizon an absent entry is the pass working, not a row going missing."""
    ref = _commit(database, 1, "Ada", BORN)
    ending = TransactionDouble(txn_id=6)
    database.heap.delete(database.table, ref, ENDED)
    database.manager.stage_row_delete(ending, database.table.table_id, ref, (1, "Ada"), ENDED)
    database.manager.commit(ending, ENDED)
    pass_txn = TransactionDouble(txn_id=7)
    database.manager.reconcile(ENDED, pass_txn)
    database.manager.commit(pass_txn, ENDED + 1)
    database.manager.note_reconciled(ENDED)

    assert database.entries() == {"person_by_name": (), "person_near_name": ()}
    assert database.manager.verify() == ()


def test_the_same_absence_above_the_horizon_is_still_reported(database: Database) -> None:
    """The other side of the horizon comparison, so it cannot be satisfied by never reporting."""
    ref = _commit(database, 1, "Ada", BORN)
    ending = TransactionDouble(txn_id=6)
    database.heap.delete(database.table, ref, ENDED)
    database.manager.stage_row_delete(ending, database.table.table_id, ref, (1, "Ada"), ENDED)
    database.manager.commit(ending, ENDED)
    pass_txn = TransactionDouble(txn_id=7)
    database.manager.reconcile(ENDED, pass_txn)
    database.manager.commit(pass_txn, ENDED + 1)
    database.manager.note_reconciled(ENDED - 1)

    assert _kinds(database.manager.verify()) == ["missing_entry", "missing_entry"]


def test_an_entry_pointing_at_another_tables_row_is_reported(database: Database) -> None:
    other = TableDef(
        table_id=database.catalog.catalog.next_table_id(),
        name="Company",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
    )
    database.catalog.catalog.add_table(other)
    database.catalog.save()
    foreign = database.heap.insert(other, 1, (1,), BORN)
    txn = TransactionDouble(txn_id=5)
    database.exact.stage_insert(txn, database.key(1, "Ada"), foreign, 0)
    database.exact.commit(txn, BORN)

    findings = database.manager.verify("person_by_name")

    assert _kinds(findings) == ["foreign_row"]
    assert findings[0].ref == foreign


def test_an_index_over_a_table_the_catalog_does_not_know_is_reported(
    database: Database,
) -> None:
    ghost = IndexDefinition(
        name="ghost_index",
        table_id=4242,
        table_name="Ghost",
        positions=(0,),
        visibility=IndexVisibility.EXACT,
        bucket_count=TEST_BUCKET_COUNT,
    )
    database.manager.register(HashIndex(ghost, database.pool, database.metrics))

    all_findings = database.manager.verify()
    findings = database.manager.verify("ghost_index")

    assert _kinds(all_findings) == ["unknown_table"]
    assert all_findings[0].index == "ghost_index"
    assert _kinds(findings) == ["unknown_table"]
    assert findings[0].index == "ghost_index"


def test_verifying_one_index_says_nothing_about_the_others(database: Database) -> None:
    database.insert(1, "Ada", BORN)

    assert {finding.index for finding in database.manager.verify("person_by_name")} == {
        "person_by_name"
    }
    assert len(database.manager.verify()) == 2


def test_an_exact_lookup_still_answers_correctly_while_a_stale_entry_is_reported(
    database: Database,
) -> None:
    """A stale entry is a cost for an exact index, never a wrong answer.

    The report says the entry disagrees with the heap; the validated door drops it. Both
    statements are true at once, and that is what the exact contract buys.
    """
    ref = database.insert(1, "Ada", BORN)
    key = database.key(1, "Ada")
    new_ref = database.heap.update(database.table, ref, (1, "Grace"), ENDED)
    txn = TransactionDouble(txn_id=5)
    database.exact.stage_insert(txn, key, new_ref, 0)
    database.exact.commit(txn, ENDED)

    from .conftest import SnapshotDouble

    assert "stale_entry" in _kinds(database.manager.verify("person_by_name"))
    assert database.manager.lookup("person_by_name", key, SnapshotDouble(ENDED)) == ()
