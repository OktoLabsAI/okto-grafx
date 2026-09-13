"""Component index effects require the same physical owner as native search."""

import pytest

from okto_grafx.domain.errors import GrafxIndexError, GrafxSpaceRetired
from okto_grafx.domain.index.records import IndexOperation, change_of

from .conftest import SnapshotDouble, TransactionDouble


def owners(database):
    space = database.create_space("s", 2)
    tables = (database.create_table("First", "s"), database.create_table("Second", "s"))
    vectors = ((1.0, 0.0), (0.0, 1.0))
    refs = tuple(database.insert_row(table, 1, 0, space, values, 10, apply_index=False)
                 for table, values in zip(tables, vectors))
    return space, tables, vectors, refs


def stage_both(database, tables, vectors, refs):
    txn = TransactionDouble()
    for table, vector, ref in zip(tables, vectors, refs):
        database.engine.stage_insert("s", 1, ref, vector, 10, txn, table_id=table.table_id)
    return txn


@pytest.mark.parametrize("first", [0, 1])
def test_commit_applies_only_selected_owner_with_shared_transaction(database, first):
    _space, tables, vectors, refs = owners(database)
    engine = database.engine
    txn = stage_both(database, tables, vectors, refs)
    indexes = tuple(engine.index("s", table_id=table.table_id) for table in tables)
    assert {change_of(record).index for record in txn.records} == {i.name for i in indexes}
    assert all(index.walk() == () for index in indexes)
    assert engine.commit("s", txn, 10, table_id=tables[first].table_id) == 1
    assert len(indexes[first].walk()) == 1 and indexes[1-first].walk() == ()
    assert engine.commit("s", txn, 10, table_id=tables[1-first].table_id) == 1
    for table, expected in zip(tables, (1.0, 0.0)):
        result = engine.search(space="s", table_id=table.table_id, query=(1.0, 0.0),
                               k=2, snapshot=SnapshotDouble(15))
        assert len(result.hits) == 1 and result.hits[0].record_id == 1
        assert result.hits[0].score == pytest.approx(expected)
    assert database.registry.verify() == ()


@pytest.mark.parametrize("rolled", [0, 1])
def test_rollback_does_not_drop_siblings_staged_changes(database, rolled):
    _space, tables, vectors, refs = owners(database)
    engine = database.engine
    txn = stage_both(database, tables, vectors, refs)
    assert engine.rollback("s", txn, table_id=tables[rolled].table_id) == 1
    assert engine.rollback("s", txn, table_id=tables[rolled].table_id) == 0
    assert engine.commit("s", txn, 10, table_id=tables[1-rolled].table_id) == 1
    assert engine.index("s", table_id=tables[rolled].table_id).walk() == ()
    assert len(engine.index("s", table_id=tables[1-rolled].table_id).walk()) == 1


@pytest.mark.parametrize("deleted", [0, 1])
def test_delete_and_reconciliation_preserve_sibling_and_snapshot_horizon(database, deleted):
    space, tables, vectors, refs = owners(database)
    engine = database.engine
    txn = stage_both(database, tables, vectors, refs)
    for table in tables:
        engine.commit("s", txn, 10, table_id=table.table_id)
    selected, sibling = tables[deleted], tables[1-deleted]
    sibling_index = engine.index("s", table_id=sibling.table_id)
    sibling_entries = sibling_index.walk()
    remove = TransactionDouble()
    database.delete_row(selected, refs[deleted], 1, space, vectors[deleted], 20, apply_index=False)
    engine.stage_delete("s", 1, refs[deleted], vectors[deleted], 20, remove, table_id=selected.table_id)
    assert change_of(remove.records[0]).operation is IndexOperation.TOMBSTONE
    assert engine.commit("s", remove, 20, table_id=selected.table_id) == 1
    assert engine.reconcile("s", 19, table_id=selected.table_id).retained == 1
    assert engine.reconcile("s", 20, table_id=selected.table_id).removed == 0
    old = engine.search(space="s", table_id=selected.table_id, query=vectors[deleted],
                        k=1, snapshot=SnapshotDouble(15))
    assert len(old.hits) == 1
    reclaim = TransactionDouble()
    report = engine.reconcile("s", 20, reclaim, table_id=selected.table_id)
    assert report.removed == 1 and report.index != sibling_index.name
    assert change_of(reclaim.records[0]).operation is IndexOperation.REMOVE
    assert engine.rollback("s", reclaim, table_id=selected.table_id) == 1
    assert len(engine.index("s", table_id=selected.table_id).walk()) == 1
    retry = TransactionDouble()
    engine.reconcile("s", 20, retry, table_id=selected.table_id)
    assert engine.commit("s", retry, 30, table_id=selected.table_id) == 1
    assert engine.index("s", table_id=selected.table_id).walk() == ()
    assert sibling_index.walk() == sibling_entries
    # The standalone component fixture has no C5 checkpoint/replay driver.
    # Replay the committed REMOVE as that driver does: it also publishes the
    # recorded horizon required to distinguish reclamation from missing data.
    engine.index("s", table_id=selected.table_id).apply(retry.records[0])
    assert engine.index("s", table_id=selected.table_id).reconciled_through_lsn == 20
    assert sibling_index.reconciled_through_lsn == 0
    assert database.registry.verify() == ()


@pytest.mark.parametrize("operation", ["stage_insert", "stage_delete", "commit", "rollback", "reconcile"])
@pytest.mark.parametrize("identity", [None, 999, True])
def test_invalid_or_ambiguous_owner_refuses_before_component_effects(database, operation, identity):
    _space, tables, vectors, refs = owners(database)
    engine = database.engine
    txn = stage_both(database, tables, vectors, refs)
    records = tuple(txn.records)
    indexes = tuple(engine.index("s", table_id=table.table_id) for table in tables)
    before = tuple((index.header, index.walk()) for index in indexes)
    with pytest.raises(GrafxIndexError):
        if operation in ("stage_insert", "stage_delete"):
            getattr(engine, operation)("s", 1, refs[0], vectors[0], 20, txn, table_id=identity)
        elif operation == "commit":
            engine.commit("s", txn, 10, table_id=identity)
        elif operation == "rollback":
            engine.rollback("s", txn, table_id=identity)
        else:
            engine.reconcile("s", 20, txn, table_id=identity)
    assert tuple(txn.records) == records
    assert tuple((index.header, index.walk()) for index in indexes) == before
    assert all(engine.commit("s", txn, 10, table_id=table.table_id) == 1 for table in tables)


def test_retirement_refuses_new_vectors_but_allows_selected_owner_cleanup(database):
    space, tables, vectors, refs = owners(database)
    engine = database.engine
    txn = stage_both(database, tables, vectors, refs)
    for table in tables:
        engine.commit("s", txn, 10, table_id=table.table_id)
    engine.retire_space("s")
    with pytest.raises(GrafxSpaceRetired):
        engine.stage_insert("s", 1, refs[0], vectors[0], 20, TransactionDouble(), table_id=tables[0].table_id)
    database.delete_row(tables[0], refs[0], 1, space, vectors[0], 20, apply_index=False)
    txn = TransactionDouble()
    engine.stage_delete("s", 1, refs[0], vectors[0], 20, txn, table_id=tables[0].table_id)
    engine.commit("s", txn, 20, table_id=tables[0].table_id)
    assert engine.reconcile("s", 20, table_id=tables[0].table_id).reclaimable == 1
    assert engine.reconcile("s", 20, table_id=tables[1].table_id).reclaimable == 0
