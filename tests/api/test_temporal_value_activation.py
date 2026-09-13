"""Internal temporal metadata activation through real transaction/WAL boundaries."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxTransactionStateError, GrafxUnsupportedOperation,
    GrafxWriteConflict, GrafxSchemaVersionMismatch,
    GrafxTransactionBudgetExceeded,
)
from okto_grafx.domain.model.temporal_codec import TEMPORAL_VALUES_CAPABILITY, TEMPORAL_VALUES_CAPABILITY_BIT
from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.wal_manager import WalManager
from okto_grafx.domain.wal.record import WalRecordType


def prepare(db,tx):
    return db._transactions._prepare_temporal_values_activation(tx._context)


def activated(db):
    return db._catalog.read_from_pages().requires_capability(TEMPORAL_VALUES_CAPABILITY)


def test_atomic_commit_reopen_noop_and_ordinary_values_remain_usable(tmp_path):
    path = tmp_path / "db"
    with connect(path,page_size=512) as db:
        db.ensure_identity_indexes()
        before = db._catalog.read_from_pages().serialize()
        with db.begin() as tx:
            assert prepare(db,tx)
            assert not activated(db)
            assert db._catalog.read_from_pages().serialize() == before
        assert activated(db)
        assert not db._transactions._temporal_activation_plans
        after = db._catalog.read_from_pages().serialize()
        with db.begin() as tx:
            assert not prepare(db,tx)
            assert not tx._context.wrote
            assert int(tx._context.txn_id) not in db._transactions._maintenance_txns
        assert db._catalog.read_from_pages().serialize() == after
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:7})")
        assert not db.verify("all").findings
    with connect(path,page_size=512) as db:
        assert activated(db)
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((7,),)
        assert not db.verify("all").findings


def test_rollback_does_not_publish_fence(tmp_path):
    path = tmp_path / "db"
    with connect(path,page_size=512) as db:
        db.ensure_identity_indexes()
        before = db._catalog.read_from_pages().serialize()
        tx = db.begin()
        assert prepare(db,tx)
        tx.rollback()
        assert db._catalog.read_from_pages().serialize() == before
        assert not db._transactions._temporal_activation_plans
    with connect(path,page_size=512) as db:
        assert not activated(db)


def test_read_and_nonfresh_transactions_refuse(tmp_path):
    with connect(tmp_path / "db",page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin("read") as tx:
            with pytest.raises(GrafxTransactionStateError):
                prepare(db,tx)
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            with pytest.raises(GrafxTransactionStateError):
                prepare(db,tx)
        assert not activated(db)


def test_legacy_catalog_refuses_without_implicit_index_migration(tmp_path):
    with connect(tmp_path / "db",page_size=512) as db:
        before = db._catalog.read_from_pages().serialize()
        with db.begin() as tx:
            with pytest.raises(GrafxUnsupportedOperation):
                prepare(db,tx)
            assert not tx._context.wrote
            assert int(tx._context.txn_id) not in db._transactions._maintenance_txns
        assert db._catalog.read_from_pages().serialize() == before


def test_partial_staging_failure_restores_exact_transaction_state(tmp_path,monkeypatch):
    with connect(tmp_path / "db",page_size=512) as db:
        db.ensure_identity_indexes()
        original = TransactionManager._stage_page_image
        count = 0
        def fail_after_stage(self,txn,file,index,image):
            nonlocal count
            original(self,txn,file,index,image)
            count += 1
            raise OSError("injected staging fault")
        tx = db.begin()
        try:
            with monkeypatch.context() as patch:
                patch.setattr(TransactionManager,"_stage_page_image",fail_after_stage)
                with pytest.raises(OSError,match="staging fault"):
                    prepare(db,tx)
            assert count == 1
            assert not tx._context.page_images and not tx._context._page_image_proofs
            assert not tx._context.read_partitions and not tx._context.write_partitions
            assert not tx._context._staging_marks
            assert not db._transactions._temporal_activation_plans
            assert int(tx._context.txn_id) not in db._transactions._maintenance_txns
            assert prepare(db,tx)
            tx.commit()
        finally:
            if tx._context.active:
                tx.rollback()
        assert activated(db)


def test_later_schema_overwrite_cannot_silently_cancel_prepared_activation(tmp_path):
    with connect(tmp_path / "db",page_size=512) as db:
        db.ensure_identity_indexes()
        tx = db.begin()
        try:
            assert prepare(db,tx)
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            with pytest.raises(GrafxConfigurationError,match="dedicated"):
                tx.commit()
            assert not activated(db)
        finally:
            tx.rollback()
        assert not db._transactions._temporal_activation_plans


def test_concurrent_schema_publication_wins_before_activation_can_overwrite(tmp_path):
    path = tmp_path / "db"
    with connect(path,page_size=512) as first:
        first.ensure_identity_indexes()
        with connect(path,page_size=512) as second:
            tx = first.begin()
            try:
                assert prepare(first,tx)
                with second.begin() as writer:
                    writer.execute("CREATE NODE TABLE Winner(id INT64, PRIMARY KEY(id))")
                with pytest.raises(GrafxWriteConflict):
                    tx.commit()
                assert not activated(second)
            finally:
                tx.rollback()
            with first.begin() as fresh:
                assert prepare(first,fresh)
            assert activated(first)
    with connect(path,page_size=512) as db:
        assert db._catalog.read_from_pages().table("Winner").name == "Winner"
        assert activated(db)


def test_old_reader_catalog_refusal_and_recognizing_reader_roundtrip(tmp_path,monkeypatch):
    with connect(tmp_path / "db",page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            prepare(db,tx)
        image = db._catalog.read_from_pages().serialize()
        with monkeypatch.context() as patch:
            patch.setattr(catalog_module,"_KNOWN_CAPABILITY_BITS",
                          catalog_module._KNOWN_CAPABILITY_BITS & ~TEMPORAL_VALUES_CAPABILITY_BIT)
            with pytest.raises(GrafxSchemaVersionMismatch):
                catalog_module.Catalog.deserialize(image)
        assert catalog_module.Catalog.deserialize(image).requires_capability(TEMPORAL_VALUES_CAPABILITY)


def test_pre_append_failure_preserves_prepared_activation_for_retry(tmp_path,monkeypatch):
    with connect(tmp_path / "db",page_size=512) as db:
        db.ensure_identity_indexes()
        before = db._wal.last_lsn
        tx = db.begin()
        try:
            assert prepare(db,tx)
            def fail_plan(manager,records):
                raise GrafxTransactionBudgetExceeded("injected planning fault",field="test")
            with monkeypatch.context() as patch:
                patch.setattr(WalManager,"planned_terminal_lsn",fail_plan)
                with pytest.raises(GrafxTransactionBudgetExceeded):
                    tx.commit()
            assert tx.active and db._wal.last_lsn == before
            assert not activated(db)
            tx.commit()
            assert activated(db)
            assert not db._transactions._temporal_activation_plans
        finally:
            if tx.active:
                tx.rollback()


def test_durable_commit_apply_failure_recovers_exactly_one_activation(tmp_path,monkeypatch):
    path = tmp_path / "db"
    with connect(path,page_size=512) as db:
        db.ensure_identity_indexes()
        before = db._wal.last_lsn
        tx = db.begin()
        assert prepare(db,tx)
        def fail_apply(manager,images):
            assert manager is db._transactions
            raise RuntimeError("injected post-barrier apply cut")
        with monkeypatch.context() as patch:
            patch.setattr(TransactionManager,"_apply_images",fail_apply)
            with pytest.raises(GrafxTransactionStateError) as failure:
                tx.commit()
            assert failure.value.details["committed"] is True
            assert failure.value.details["durable"] is True
            committed = tx._context.commit_csn
    with connect(path,page_size=512) as db:
        assert activated(db)
        commits = [r.lsn for r in db._wal.read_from(before+1)
                   if r.record_type == int(WalRecordType.COMMIT)]
        assert commits == [committed]
        assert not db.verify("all").findings
