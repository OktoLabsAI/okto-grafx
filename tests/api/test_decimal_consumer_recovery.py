"""Recovery, resume and historical DECIMAL schema evolution consumer contracts."""

from pathlib import Path
import subprocess
import sys

import pytest

from okto_grafx import connect, CommitId, DecimalValue, TemporalLimits
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.transfer import export_graph, import_graph, TransferLimits
from tests.api.test_decimal_transfer import MODELS, seed_decimals, picture, decimal_declarations


WORKER = Path(__file__).with_name("decimal_consumer_worker.py")


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("cut", ("after_wal_barrier", "after_promotion"))
def test_resume_durable_batch_or_lost_promotion_ack_without_duplicates(tmp_path, model, cut):
    with connect(tmp_path / "source") as db:
        seed_decimals(db, model, indexed=model in ("typed", "namespace"))
        expected = picture(db)
        declarations = decimal_declarations(db.catalog.catalog.tables())
        export_graph(db, tmp_path / "artifact")
    proc = subprocess.run([sys.executable, str(WORKER), "import", str(tmp_path), cut],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 73, proc.stderr
    assert (tmp_path / "target").exists() == (cut == "after_promotion")
    options = {"resume_directory": tmp_path / "work", "limits": TransferLimits(batch_rows=1)}
    report = import_graph(tmp_path / "artifact", tmp_path / "target", **options)
    assert report.rows == 3 and len(report.record_id_mapping) == 3
    assert import_graph(tmp_path / "artifact", tmp_path / "target", **options) == report
    with connect(tmp_path / "target") as db:
        assert picture(db) == expected
        assert decimal_declarations(db.catalog.catalog.tables()) == declarations
        assert db.execute("MATCH(n) RETURN count(n)").rows == ((2,),)
        assert not db.verify("all").findings


@pytest.mark.parametrize("model", ("typed", "flexible"))
@pytest.mark.parametrize("cut", ("before_commit", "before_apply"))
@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_recovery_data_and_history_publish_one_decimal_outcome(tmp_path, model, cut, codec):
    limits = TemporalLimits(access_path="index")
    with connect(tmp_path / "source", codec=codec) as db:
        names = seed_decimals(db, model)
        db.enable_commit_history()
        db.enable_system_history(names)
        db.enable_system_history_index()
        before = db.commit_history().entries[-1].identity
        original = db.system_as_of(before, tables=names, limits=limits)
        expected = picture(db)
        db.checkpoint()
    proc = subprocess.run([sys.executable, str(WORKER), "history", str(tmp_path), cut, codec, model],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == (71 if cut == "before_commit" else 73), proc.stderr
    for _ in range(2):
        with connect(tmp_path / "source", codec=codec) as db:
            assert db.system_as_of(before, tables=names, limits=limits).rows == original.rows
            latest = db.commit_history().entries[-1].identity
            history = db.system_as_of(latest, tables=names, limits=limits)
            assert (history.rows == original.rows) == (cut == "before_commit")
            assert (picture(db) == expected) == (cut == "before_commit")
            if cut == "before_apply":
                prop = "v1" if model == "typed" else "bag"
                assert db.execute(f"MATCH(n)-[r]->() RETURN n.{prop},r.{prop}").rows == (
                    (DecimalValue(90000, 12, 4), DecimalValue(80000, 12, 4)),)
                assert len(db.system_diff(before, latest, tables=names, limits=limits).rows) == 2
            assert not db.verify("all").findings
            db.checkpoint()


@pytest.mark.parametrize("indexed", (False, True))
def test_nullable_decimal_append_retains_historical_schema_and_current_transfer(tmp_path, indexed):
    limits = TemporalLimits(access_path="index" if indexed else "scan")
    with connect(tmp_path / "source") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:1})")
        db.enable_commit_history()
        db.enable_system_history(("N",))
        if indexed:
            db.enable_system_history_index()
        before = db.commit_history().entries[-1].identity
        original = db.system_as_of(before, tables=("N",), limits=limits)
        db.add_nullable_column("N", ColumnDef("v", ValueType.DECIMAL, decimal_precision=12, decimal_scale=4))
        appended = db.commit_history().entries[-1].identity
        older = db.system_as_of(appended, tables=("N",), limits=limits)
        assert older.rows[0].values == (1, None)
        assert older.schemas[0].columns[1].decimal_precision == 12
        assert older.schemas[0].columns[1].decimal_scale == 4
        with db.begin() as tx:
            tx.execute("MATCH(n:N) SET n.v=decimal('1.23',3,2)")
        changed = CommitId(db.identity.database_uuid, tx.report.csn)
        current = db.system_as_of(changed, tables=("N",), limits=limits)
        assert current.rows[0].values == (1, DecimalValue(12300, 12, 4))
        assert db.system_as_of(before, tables=("N",), limits=limits).schemas == original.schemas
        export_graph(db, tmp_path / "artifact", history="current-only")
        assert not db.verify("all").findings
    import_graph(tmp_path / "artifact", tmp_path / "target")
    with connect(tmp_path / "target") as db:
        assert db.execute("MATCH(n:N) RETURN n.id,n.v").rows == ((1, DecimalValue(12300, 12, 4)),)
        assert not db.verify("all").findings
    with connect(tmp_path / "source") as db:
        for identity, expected in ((before, original), (appended, older), (changed, current)):
            actual = db.system_as_of(identity, tables=("N",), limits=limits)
            assert actual.rows == expected.rows and actual.schemas == expected.schemas
