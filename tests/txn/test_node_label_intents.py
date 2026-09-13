"""Native label staging/reduction and journaled publication, before Cypher operators."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxTransactionBudgetExceeded, GrafxDeviceFull, GrafxUnsupportedOperation, GrafxTransactionStateError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.model.node_labels import NODE_LABELS_CAPABILITY, encode_node_labels
from okto_grafx.domain.model.schema import TableDef, ColumnDef, encode_tuple
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.txn.context import TransactionContext, TransactionMode, RowIntent, RowOperation
from okto_grafx.domain.txn.intents import reduce_row_intents
from okto_grafx.domain.txn.snapshot import Snapshot


def table():
    return TableDef(1, "N", "node", (ColumnDef("id", ValueType.INT64),), extra_node_labels=("X", "Y"))


def context(limit=None):
    return TransactionContext(txn_id=7, mode=TransactionMode.WRITE, snapshot=Snapshot(0), epoch=0,
                              owner=object(), page_staging_capability=object(), max_transaction_bytes=limit)


@pytest.mark.parametrize("pending", [True, False])
@pytest.mark.parametrize("initial", [None, (), ("N", "X")])
@pytest.mark.parametrize("replacement", [None, (), ("Y",)])
def test_label_reduction_preserves_explicit_sets_through_property_updates(pending, initial, replacement):
    txn = context()
    if pending:
        ref = txn.stage_row_insert(table(), (1,), node_labels=initial)
    else:
        ref = RecordRef(2, 1)
        txn.stage_row_update(table(), ref, (1,), node_labels=initial)
    txn.stage_row_update(table(), ref, (2,), node_labels=replacement)
    txn.stage_row_update(table(), ref, (3,))
    settled, = reduce_row_intents(txn.row_intents)
    assert settled.reference == ref and settled.values == (3,)
    assert settled.node_labels == (initial if replacement is None else replacement)
    assert settled.operation is (RowOperation.INSERT if pending else RowOperation.UPDATE)
    txn.stage_row_delete(table(), ref)
    settled = reduce_row_intents(txn.row_intents)
    assert settled == () if pending else settled[0].operation is RowOperation.DELETE


@pytest.mark.parametrize("labels", [[], ["X"], ("X", "N"), ("N", "N"), ("Z",), ("",), (1,)])
@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("limit", [None, 1000])
def test_invalid_labels_refuse_before_retention_with_or_without_a_byte_budget(labels, update, limit):
    txn = context(limit)
    with pytest.raises(GrafxConfigurationError):
        if update:
            txn.stage_row_update(table(), RecordRef(2, 1), (1,), node_labels=labels)
        else:
            txn.stage_row_insert(table(), (1,), node_labels=labels)
    assert txn.row_intents == [] and txn._staged_payload_bytes == 0
    assert txn._next_pending_token == -1


@pytest.mark.parametrize("labels", [(), ("N", "X")])
def test_exact_metadata_quota_and_statement_discard(labels):
    row_size = len(encode_tuple(table(), (1,)))
    size = row_size + len(encode_node_labels(labels))
    short = context(size - 1)
    with pytest.raises(GrafxTransactionBudgetExceeded):
        short.stage_row_insert(table(), (1,), node_labels=labels)
    assert short.row_intents == []
    txn = context(size)
    mark = txn.staging_mark()
    txn.stage_row_insert(table(), (1,), node_labels=labels)
    assert txn._staged_payload_bytes == size
    txn.validate_budgets()
    txn.discard_since(mark)
    assert txn._staged_payload_bytes == 0 and txn.row_intents == []
    txn.stage_row_insert(table(), (1,), node_labels=labels)
    txn.validate_budgets()


def test_direct_intent_mutation_cannot_hide_metadata_from_quota():
    txn = context(len(encode_tuple(table(), (1,))))
    txn.stage_row_insert(table(), (1,))
    txn.row_intents[0] = replace(txn.row_intents[0], node_labels=("N", "X"))
    with pytest.raises(GrafxTransactionBudgetExceeded):
        txn.validate_budgets()


def seed(db, *, primary_key=False):
    db.maintenance.ensure_identity_indexes()
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))" if primary_key else "CREATE NODE TABLE N(id INT64)")
    return db.catalog.catalog.table("N")


def admit(db, txn, labels=("X", "Y")):
    physical = db.catalog.catalog.table("N")
    return db._queries._admit_node_labels(txn._context, physical.table_id, labels)


def note(db, txn, native_table):
    txn._context.note_write(db._transactions.partition_of(native_table.table_id, b"label-test"))


@pytest.mark.parametrize("labels", [(), ("N", "X"), ("Y",)])
@pytest.mark.parametrize("primary_key", [False, True])
def test_label_intent_and_capability_share_one_real_commit_and_reopen(tmp_path, labels, primary_key):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        seed(db, primary_key=primary_key)
        with db.begin("write") as txn:
            native_table = admit(db, txn)
            txn._context.stage_row_insert(native_table, (1,), node_labels=labels)
            note(db, txn, native_table)
            assert not db._catalog.catalog.requires_capability(NODE_LABELS_CAPABILITY)
        ref = txn._context.row_refs[0]
        assert db._heap.read(ref).node_labels == labels
        assert db._heap.read(ref).xmin == txn._context.commit_csn
        assert db.catalog.catalog.table("N").extra_node_labels == ("X", "Y")
        assert db._catalog.catalog.requires_capability(NODE_LABELS_CAPABILITY)
        assert db._heap._node_label_write_catalog is None
        assert db.verify("all").findings == ()
    with connect(root, page_size=512) as db:
        assert db._heap.read(ref).node_labels == labels
        assert db._heap.read(ref).values == (1,)
        assert db.verify("all").findings == ()


def test_failed_or_rolled_back_admission_does_not_publish_schema(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        original = seed(db)
        before = db._catalog.catalog.serialize()
        txn = db.begin("write")
        native_table = admit(db, txn)
        txn._context.stage_row_insert(native_table, (1,), node_labels=("X",))
        note(db, txn, native_table)
        txn.rollback()
        assert db._catalog.catalog.serialize() == before
        assert db._heap.pages_of(original) == ()
        assert db._heap._node_label_write_catalog is None


@pytest.mark.parametrize("mutation", ["capability_missing", "candidate_forgery", "delete_labels"])
def test_direct_intent_forgery_refuses_before_commit_section(tmp_path, mutation):
    with connect(tmp_path / "db", page_size=512) as db:
        native_table = seed(db)
        txn = db.begin("write")
        if mutation != "capability_missing":
            native_table = admit(db, txn, ("X",))
        forged = replace(native_table, extra_node_labels=("X", "Y"))
        ref = txn._context.stage_row_insert(forged, (1,), node_labels=("Y",))
        if mutation == "delete_labels":
            txn._context.row_intents.append(RowIntent(forged, operation=RowOperation.DELETE,
                                                       reference=ref, node_labels=()))
        before = db._transactions._wal.last_lsn
        with pytest.raises(GrafxConfigurationError):
            txn.commit()
        assert db._transactions._wal.last_lsn == before
        assert db._heap.pages_of(native_table) == ()
        assert db._heap._node_label_write_catalog is None
        if txn.active:
            txn.rollback()


def test_pending_label_changes_and_later_property_change_write_one_final_version(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        seed(db)
        with db.begin("write") as txn:
            native_table = admit(db, txn)
            ref = txn._context.stage_row_insert(native_table, (1,), node_labels=("X",))
            txn._context.stage_row_update(native_table, ref, (2,), node_labels=())
            txn._context.stage_row_update(native_table, ref, (3,))
            note(db, txn, native_table)
        rows = list(db._heap.scan_all(native_table))
        assert len(rows) == 1 and rows[0][1].node_labels == () and rows[0][1].values == (3,)


def test_later_property_update_preserves_committed_labels_and_old_reader_snapshot(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as writer:
        seed(writer)
        with writer.begin("write") as txn:
            native_table = admit(writer, txn)
            txn._context.stage_row_insert(native_table, (1,), node_labels=("X",))
            note(writer, txn, native_table)
        original = txn._context.row_refs[0]
        with connect(root, page_size=512) as reader:
            with reader.begin("read") as old:
                assert old.execute("MATCH(n) RETURN n.id").rows == ((1,),)
                with writer.begin("write") as txn:
                    # Already admitted: no schema pages, no catalog publication.
                    current = admit(writer, txn)
                    assert not txn._context.page_images
                    txn._context.stage_row_update(current, original, (2,), node_labels=("Y",))
                    note(writer, txn, current)
                changed = txn._context.row_refs[0]
                assert writer._heap.read(changed).record_id == writer._heap.read(original).record_id
                assert writer._heap.read(changed).node_labels == ("Y",)
                before = list(reader._heap.scan(native_table, old._context.snapshot))
                assert len(before) == 1 and before[0][1].node_labels == ("X",)
                assert before[0][1].values == (1,)
            with writer.begin("write") as txn:
                txn._context.stage_row_update(current, changed, (3,))
                note(writer, txn, current)
            latest = writer._heap.read(txn._context.row_refs[0])
            assert latest.node_labels == ("Y",) and latest.values == (3,)
            assert reader.execute("MATCH(n) RETURN n.id").rows == ((3,),)


def test_wal_append_refusal_restores_labels_and_cannot_leak_write_scope(tmp_path, monkeypatch):
    from okto_grafx.engine.wal_manager import WalManager

    with connect(tmp_path / "db", page_size=512) as db:
        seed(db)
        with db.begin("write") as txn:
            native_table = admit(db, txn)
            txn._context.stage_row_insert(native_table, (1,), node_labels=("X",))
            note(db, txn, native_table)
        original = txn._context.row_refs[0]
        before = db._transactions._wal.last_lsn
        append = WalManager.append_many

        def fail(*args, **kwargs):
            raise GrafxDeviceFull("Injected before any WAL append.")

        monkeypatch.setattr(WalManager, "append_many", fail)
        doomed = db.begin("write")
        doomed._context.stage_row_update(native_table, original, (2,), node_labels=())
        note(db, doomed, native_table)
        with pytest.raises(GrafxDeviceFull):
            doomed.commit()
        assert db._transactions._wal.last_lsn == before
        assert db._heap._node_label_write_catalog is None
        assert db._heap.read(original).node_labels == ("X",) and db._heap.read(original).xmax == 0
        monkeypatch.setattr(WalManager, "append_many", append)
        if doomed.active:
            doomed.rollback()
        with db.begin("write") as survivor:
            survivor._context.stage_row_update(native_table, original, (3,), node_labels=("Y",))
            note(db, survivor, native_table)
        assert db._heap.read(survivor._context.row_refs[0]).node_labels == ("Y",)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("count", [1, 40])
def test_refused_first_activation_under_buffer_pressure_keeps_the_existing_store_usable(tmp_path, monkeypatch, count):
    from okto_grafx.engine.wal_manager import WalManager

    root = tmp_path / "db"
    with connect(root, page_size=512, buffer_budget_bytes=4096) as db:
        seed(db)
        before = db._catalog.catalog.serialize()
        append = WalManager.append_many

        def fail(*args, **kwargs):
            raise GrafxDeviceFull("Injected before first label COMMIT.")

        txn = db.begin("write")
        labels = ("X" * 200,)
        native_table = admit(db, txn, labels)
        for identity in range(count):
            txn._context.stage_row_insert(native_table, (identity,), node_labels=labels)
        note(db, txn, native_table)
        monkeypatch.setattr(WalManager, "append_many", fail)
        with pytest.raises(GrafxDeviceFull):
            txn.commit()
        monkeypatch.setattr(WalManager, "append_many", append)
        if txn.active:
            txn.rollback()
        assert db._catalog.catalog.serialize() == before
        assert db._heap._node_label_write_catalog is None
        with db.begin("write") as survivor:
            survivor.execute("CREATE (:N {id: 999})")
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((999,),)
        assert db.verify("all").findings == ()
    with connect(root, page_size=512, buffer_budget_bytes=4096) as db:
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((999,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("activate_first", [False, True])
def test_history_bridge_preserves_empty_membership_before_or_after_activation(tmp_path, activate_first):
    with connect(tmp_path / "db", page_size=512) as db:
        seed(db)
        db.enable_commit_history()
        if activate_first:
            db.enable_system_history(("N",))
        txn = db.begin("write")
        native_table = admit(db, txn)
        txn._context.stage_row_insert(native_table, (1,), node_labels=())
        note(db, txn, native_table)
        txn.commit()
        if not activate_first:
            db.enable_system_history(("N",))
        at = db.commit_history().entries[-1].identity
        assert db.system_as_of(at, tables=("N",)).rows[0].node_labels == ()
        assert db._heap.read(txn._context.row_refs[0]).node_labels == ()
        assert db.verify("all").findings == ()


def test_label_only_candidate_growth_is_not_a_nullable_column_schema_drift(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        seed(db)
        with db.begin("write") as ddl:
            db._queries.add_nullable_column(ddl._context, "N", ColumnDef("later", ValueType.STRING))
        assert "nullable_columns_v1" in db._catalog.catalog.required_capabilities()
        with db.begin("write") as txn:
            native_table = admit(db, txn)
            txn._context.stage_row_insert(native_table, (1, None), node_labels=("X",))
            note(db, txn, native_table)
        assert db._heap.read(txn._context.row_refs[0]).node_labels == ("X",)
        assert db._heap.read(txn._context.row_refs[0]).values == (1, None)


def test_outer_statement_unwind_removes_only_its_label_schema_suffix(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        seed(db)
        with db.begin("write") as txn:
            before = db._queries._schema_statement_mark(txn._context)
            mark = txn._context.staging_mark()
            native_table = admit(db, txn)
            txn._context.stage_row_insert(native_table, (1,), node_labels=("X",))
            txn._context.discard_since(mark)
            db._queries._restore_schema_statement(txn._context, before)
            assert not txn._context.page_images and not txn._context.row_intents
            txn.execute("CREATE (:N {id: 2})")
        assert not db._catalog.catalog.requires_capability(NODE_LABELS_CAPABILITY)
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((2,),)


def test_read_transaction_cannot_activate_or_reenter_label_admission(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        seed(db)
        for activated in (False, True):
            if activated:
                with db.begin("write") as txn:
                    admit(db, txn)
            with db.begin("read") as reader:
                with pytest.raises(GrafxTransactionStateError):
                    admit(db, reader)


def test_custom_heap_is_not_silently_given_a_label_losing_write(tmp_path, monkeypatch):
    from okto_grafx.engine.heap_store import HeapStore

    with connect(tmp_path / "db", page_size=512) as db:
        seed(db)
        txn = db.begin("write")
        native_table = admit(db, txn)
        txn._context.stage_row_insert(native_table, (1,), node_labels=("X",))
        note(db, txn, native_table)
        original = HeapStore.insert

        def custom(*args, **kwargs):
            pytest.fail("An uncertified custom heap must be refused before its write callback")

        monkeypatch.setattr(HeapStore, "insert", custom)
        with pytest.raises(GrafxUnsupportedOperation, match="custom heap"):
            txn.commit()
        assert db._heap.pages_of(native_table) == ()
        monkeypatch.setattr(HeapStore, "insert", original)
        if txn.active:
            txn.rollback()
