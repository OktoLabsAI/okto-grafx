"""Native parameters, typed/ANY row storage and transactional capability fencing."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.temporal_codec import TEMPORAL_VALUES_CAPABILITY
from tests.storage_core.test_temporal_admission import VALUES


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("typed", [False, True])
def test_temporal_native_create_commit_read_and_reopen(tmp_path, value, typed):
    from okto_grafx.domain.model.value import value_type_of
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            kind = value_type_of(value).name if typed else "ANY"
            tx.execute(f"CREATE NODE TABLE N(id INT64, val {kind}, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1, val:$v})", {"v": value})
        assert db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((value,),)
        assert not db.verify("all").findings
    with connect(path, page_size=512) as db:
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((value,),)
        assert not db.verify("all").findings


def test_first_nested_any_value_and_later_schema_commit_together(tmp_path):
    value = {"items": tuple(VALUES)}
    with connect(tmp_path / "db", page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
        assert not db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:1, val:$v})", {"v": value})
            tx.execute("CREATE NODE TABLE Added(id INT64, PRIMARY KEY(id))")
        assert db._catalog.catalog.has_table("Added")
        assert db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((value,),)


def test_rollback_first_temporal_value_keeps_capability_and_data_unpublished(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
        tx = db.begin()
        tx.execute("CREATE (:N {id:1, val:$v})", {"v": VALUES[0]})
        tx.rollback()
        assert not db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:N) RETURN count(n)").rows == ((0,),)


def test_first_temporal_commit_retries_after_pre_wal_fault(tmp_path, monkeypatch):
    from okto_grafx.domain.errors import GrafxTransactionBudgetExceeded
    from okto_grafx.engine.wal_manager import WalManager
    with connect(tmp_path / "db", page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
        tx = db.begin()
        try:
            tx.execute("CREATE (:N {id:1, val:$v})", {"v": VALUES[0]})
            def fail(manager, records):
                raise GrafxTransactionBudgetExceeded("injected pre-WAL fault", field="test")
            with monkeypatch.context() as patch:
                patch.setattr(WalManager, "planned_terminal_lsn", fail)
                with pytest.raises(GrafxTransactionBudgetExceeded):
                    tx.commit()
            assert tx.active
            assert not db._catalog.read_from_pages().requires_capability(TEMPORAL_VALUES_CAPABILITY)
            tx.commit()
            assert db.execute("MATCH (n:N) RETURN n.val").rows == ((VALUES[0],),)
        finally:
            if tx.active:
                tx.rollback()


def test_temporal_row_and_capability_recover_after_durable_apply_fault(tmp_path, monkeypatch):
    from okto_grafx.domain.errors import GrafxTransactionStateError
    from okto_grafx.engine.txn_manager import TransactionManager
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as schema:
            schema.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
        tx = db.begin()
        tx.execute("CREATE (:N {id:1, val:$v})", {"v": {"all": tuple(VALUES)}})
        def fail(manager, images):
            raise RuntimeError("injected post-durable apply fault")
        with monkeypatch.context() as patch:
            patch.setattr(TransactionManager, "_apply_images", fail)
            with pytest.raises(GrafxTransactionStateError) as failure:
                tx.commit()
            assert failure.value.details["committed"] is True
            assert failure.value.details["durable"] is True
    with connect(path, page_size=512) as db:
        assert db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:N) RETURN n.val").rows == (({"all": tuple(VALUES)},),)
        assert not db.verify("all").findings


def test_immutable_reader_snapshot_survives_temporal_update(tmp_path):
    path = tmp_path / "db"
    with connect(path, page_size=512) as first:
        first.ensure_identity_indexes()
        with first.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1, val:'original'})")
        with connect(path, page_size=512) as second:
            reader = first.begin("read")
            try:
                assert reader.execute("MATCH (n:N) RETURN n.val").rows == (("original",),)
                with second.begin() as writer:
                    writer.execute("MATCH (n:N) SET n.val=$v", {"v": VALUES[4]})
                assert reader.execute("MATCH (n:N) RETURN n.val").rows == (("original",),)
            finally:
                reader.rollback()
            assert first.execute("MATCH (n:N) RETURN n.val").rows == ((VALUES[4],),)


def test_subsequent_temporal_write_needs_no_schema_admission_lock(tmp_path, monkeypatch):
    from okto_grafx.engine.txn_manager import TransactionManager
    with connect(tmp_path / "db", page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1, val:$v})", {"v": VALUES[0]})
        tx = db.begin()
        tx.execute("MATCH (n:N) SET n.val=$v", {"v": VALUES[5]})
        def forbidden(*args, **kwargs):
            raise AssertionError("Already active row commit must not acquire schema admission lock")
        with monkeypatch.context() as patch:
            patch.setattr(TransactionManager, "schema_artifact_section", forbidden)
            tx.commit()
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((VALUES[5],),)
