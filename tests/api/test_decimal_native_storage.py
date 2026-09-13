"""DECIMAL typed/ANY storage, exact assignments and atomic native format admission."""

from pathlib import Path
import subprocess
import sys

import pytest

from okto_grafx import connect, DecimalValue
from okto_grafx.errors import GrafxError, GrafxParseError, GrafxTransactionStateError
from okto_grafx.domain.model.decimal_codec import DECIMAL_VALUES_CAPABILITY
from okto_grafx.domain.model.temporal_codec import TEMPORAL_VALUES_CAPABILITY
from okto_grafx.domain.model.temporal_values import DateValue
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.engine.txn_manager import TransactionManager


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("typed", [False, True])
def test_parameter_create_read_own_write_update_rollback_and_reopen(tmp_path, codec, typed):
    path = tmp_path / "db"
    offered = DecimalValue(123, 4, 2)
    stored = DecimalValue(1230, 10, 3) if typed else offered
    with connect(path, page_size=512, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute(f"CREATE NODE TABLE N(id INT64, val {'DECIMAL(10,3)' if typed else 'ANY'}, PRIMARY KEY(id))")
            assert tx.execute("CREATE (n:N {id:1, val:$v}) RETURN n.val", {"v": offered}).rows == ((stored,),)
            assert tx.execute("MATCH (n:N) RETURN n.val").rows == ((stored,),)
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((stored,),)
        assert db._catalog.catalog.requires_capability(DECIMAL_VALUES_CAPABILITY)
        assert not db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        tx = db.begin()
        tx.execute("MATCH (n:N) SET n.val=$v", {"v": DecimalValue(200, 3, 2)})
        tx.rollback()
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((stored,),)
        with db.begin() as tx:
            tx.execute("MATCH (n:N) SET n.val=$v", {"v": DecimalValue(-456, 4, 2)})
    expected = DecimalValue(-4560, 10, 3) if typed else DecimalValue(-456, 4, 2)
    with connect(path, page_size=512, codec=codec) as db:
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((expected,),)
        assert db.execute("MATCH (n:N) RETURN n").rows[0][0].properties["val"] == expected
        if typed:
            column = db.catalog.catalog.table("N").columns[1]
            assert (column.decimal_precision, column.decimal_scale) == (10, 3)
        assert not db.verify("all").findings


@pytest.mark.parametrize("declaration", ["DECIMAL", "DECIMAL(0,0)", "DECIMAL(39,0)",
    "DECIMAL(4,5)", "DECIMAL(4,-1)", "DECIMAL(4,1.0)", "DECIMAL(4,$s)"])
def test_invalid_ddl_has_no_catalog_effect(tmp_path, declaration):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            with pytest.raises(GrafxParseError):
                tx.execute(f"CREATE NODE TABLE N(val {declaration})")
        assert not db._catalog.catalog.has_table("N")
        assert not db._catalog.catalog.requires_capability(DECIMAL_VALUES_CAPABILITY)


def test_exact_rescale_failure_rolls_back_statement_not_prior_work(tmp_path):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val DECIMAL(5,2), PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:0, val:$v})", {"v": DecimalValue(123, 3, 2)})
            with pytest.raises(GrafxError):
                tx.execute("UNWIND $values AS v CREATE (:N {id:v.id, val:v.val})", {"values": [
                    {"id": 1, "val": DecimalValue(456, 3, 2)}, {"id": 2, "val": DecimalValue(1234, 4, 3)}]})
            assert tx.execute("MATCH (n:N) RETURN n.id, n.val").rows == ((0, DecimalValue(123, 5, 2)),)
        assert not db.verify("all").findings


def test_nested_decimal_temporal_and_private_schema_publish_together(tmp_path):
    value = {"nested": (DecimalValue(123, 3, 2), DateValue(2000))}
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
        tx = db.begin()
        tx.execute("CREATE (:N {id:1, val:$v})", {"v": value})
        tx.rollback()
        assert not db._catalog.catalog.requires_capability(DECIMAL_VALUES_CAPABILITY)
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:1, val:$v})", {"v": value})
            tx.execute("CREATE NODE TABLE Added(id INT64, PRIMARY KEY(id))")
        assert db._catalog.catalog.has_table("Added")
        assert db._catalog.catalog.requires_capability(DECIMAL_VALUES_CAPABILITY)
        assert db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((value,),)


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_durable_commit_recovery_restores_capability_and_native_value(tmp_path, monkeypatch, codec):
    path = tmp_path / "db"
    value = {"value": DecimalValue(10**38 - 1, 38, 19)}
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
        tx = db.begin()
        tx.execute("CREATE (:N {id:1, val:$v})", {"v": value})
        def fail(manager, images):
            raise RuntimeError("injected post-durable failure")
        with monkeypatch.context() as patch:
            patch.setattr(TransactionManager, "_apply_images", fail)
            with pytest.raises(GrafxTransactionStateError) as failure:
                tx.commit()
            assert failure.value.details["committed"] is True
            assert failure.value.details["durable"] is True
    with connect(path, codec=codec) as db:
        assert db._catalog.catalog.requires_capability(DECIMAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((value,),)
        assert not db.verify("all").findings


def test_nullable_column_metadata_and_old_rows_survive_reopen(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1})")
        db.add_nullable_column("N", ColumnDef("val", ValueType.DECIMAL, decimal_precision=12, decimal_scale=4))
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((None,),)
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:2, val:$v})", {"v": DecimalValue(12, 2, 1)})
    with connect(path) as db:
        assert db.execute("MATCH (n:N) RETURN n.val ORDER BY n.id").rows == ((None,), (DecimalValue(12000, 12, 4),))
        assert not db.verify("all").findings


def test_active_decimal_writer_does_not_reacquire_schema_lock(tmp_path, monkeypatch):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(val DECIMAL(5,2))")
        tx = db.begin()
        tx.execute("CREATE (:N {val:$v})", {"v": DecimalValue(123, 3, 2)})
        def forbidden(*args, **kwargs):
            raise AssertionError("No repeated schema admission lock")
        with monkeypatch.context() as patch:
            patch.setattr(TransactionManager, "schema_artifact_section", forbidden)
            tx.commit()
        assert not db.verify("all").findings


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("phase", ["statement", "commit"])
def test_abrupt_process_exit_commits_only_proven_values(tmp_path, codec, phase):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
    worker = Path(__file__).with_name("decimal_storage_worker.py")
    outcome = subprocess.run([sys.executable, str(worker), str(path), codec, phase],
                             capture_output=True, text=True, timeout=45)
    assert outcome.returncode == (71 if phase == "statement" else 72), outcome.stderr
    with connect(path, codec=codec) as db:
        expected = () if phase == "statement" else (({"amount": DecimalValue(-12345, 38, 4)},),)
        assert db.execute("MATCH (n:N) RETURN n.val").rows == expected
        assert db._catalog.catalog.requires_capability(DECIMAL_VALUES_CAPABILITY) == (phase == "commit")
        assert not db.verify("all").findings


def test_reader_snapshot_and_foreign_writer_keep_native_visibility(tmp_path):
    path = tmp_path / "db"
    with connect(path) as first:
        first.ensure_identity_indexes()
        with first.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1, val:'before'})")
        with connect(path) as second:
            with first.begin("read") as reader:
                assert reader.execute("MATCH (n:N) RETURN n.val").rows == (("before",),)
                with second.begin() as writer:
                    writer.execute("MATCH (n:N) SET n.val=$v", {"v": DecimalValue(123, 3, 2)})
                assert reader.execute("MATCH (n:N) RETURN n.val").rows == (("before",),)
            assert first.execute("MATCH (n:N) RETURN n.val").rows == ((DecimalValue(123, 3, 2),),)


def test_first_decimal_admission_preserves_original_schema_occ(tmp_path):
    from okto_grafx.errors import GrafxWriteConflict
    path = tmp_path / "db"
    with connect(path) as first:
        first.ensure_identity_indexes()
        with first.begin() as schema:
            schema.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
        with connect(path) as second:
            tx = first.begin()
            try:
                tx.execute("CREATE (:N {id:1, val:$v})", {"v": DecimalValue(123, 3, 2)})
                assert first._transactions._prepare_native_row_admission(tx._context)
                with second.begin() as winner:
                    winner.execute("CREATE NODE TABLE Winner(id INT64, PRIMARY KEY(id))")
                with pytest.raises(GrafxWriteConflict):
                    tx.commit()
            finally:
                tx.rollback()
    with connect(path) as db:
        assert db._catalog.catalog.has_table("Winner")
        assert not db._catalog.catalog.requires_capability(DECIMAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:N) RETURN count(n)").rows == ((0,),)


def test_typed_relationship_properties_and_detached_transport(tmp_path):
    import json
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N, amount DECIMAL(8,3))")
            result = tx.execute("CREATE (a:N {id:1}), (b:N {id:2}), (a)-[r:R {amount:$v}]->(b) RETURN r",
                                {"v": DecimalValue(12, 2, 1)})
            assert result.rows[0][0].properties["amount"] == DecimalValue(1200, 8, 3)
            rendered = json.dumps(result.rows[0][0].to_dict())
            assert '"coefficient": "1200"' in rendered
        assert db.execute("MATCH ()-[r:R]->() RETURN r.amount").rows == ((DecimalValue(1200, 8, 3),),)
        assert not db.verify("all").findings


def test_native_staged_intents_are_normalized_before_encoding_and_quota(tmp_path):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as schema:
            schema.execute("CREATE NODE TABLE N(id INT64, val DECIMAL(8,3), PRIMARY KEY(id))")
        table = db._catalog.catalog.table("N")
        with db.begin() as tx:
            ref = tx._context.stage_row_insert(table, (1, DecimalValue(12, 2, 1)))
            assert tx._context.row_intents[-1].values[1] == DecimalValue(1200, 8, 3)
            before = len(tx._context.row_intents), tx._context._staged_payload_bytes
            with pytest.raises(GrafxError):
                tx._context.stage_row_update(table, ref, (1, DecimalValue(12345, 5, 4)))
            assert (len(tx._context.row_intents), tx._context._staged_payload_bytes) == before
            tx._context.stage_row_update(table, ref, (1, DecimalValue(25, 2, 1)))
            assert tx._context.row_intents[-1].values[1] == DecimalValue(2500, 8, 3)
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((DecimalValue(2500, 8, 3),),)
