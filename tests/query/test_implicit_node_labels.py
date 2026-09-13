"""Implicit single labels are native flexible tables, not fixture-only inferred DDL."""

import pytest
import os
from pathlib import Path
import subprocess
import sys

from okto_grafx import connect
from okto_grafx.errors import GrafxError


def test_labeled_creation_heterogeneous_values_and_reopen(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE(a:Person {v:'text'}),(b:Person {v:2}) RETURN a,b,labels(a)")
            a, b, labels = result.rows[0]
            assert labels == ("Person",) and a.label == b.label == "Person"
            assert dict(a.properties) == {"v": "text"} and dict(b.properties) == {"v": 2}
        table = db.catalog.catalog.table("Person")
        assert table.flexible_properties and not table.unlabeled and table.primary_key is None
        with db.begin("write") as tx:
            tx.execute("MATCH(n:Person {v:2}) SET n.v={nested:[true,null]}, n.extra=3")
        assert db.verify("all").findings == ()
    with connect(tmp_path / "db") as db:
        assert db.execute("MATCH(n:Person) RETURN count(n)").rows == ((2,),)
        assert db.execute("MATCH(n:Person) WHERE n.extra=3 RETURN n.v.nested[0]").rows == ((True,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("suffix,expected", [
    ("WITH * MATCH(n:B) RETURN n.v", ((2,),)),
    ("WITH * MATCH(n) RETURN count(n)", ((3,),)),
    ("WITH * MATCH(a)-[:R]->(b:B) RETURN b.v", ((2,),)),
    ("WITH * MATCH p=(a)-[:R*1..2]->(b:B) RETURN length(p)", ((1,),)),
    ("WITH * OPTIONAL MATCH(n:B {v:2}) RETURN n.v", ((2,),)),
    ("WITH * CALL() { MATCH(n:B) RETURN n.v AS v } RETURN v", ((2,),)),
    ("WITH a RETURN [(a)-[:R]->(n:B) | n.v]", (((2,),),)),
    ("WITH a MATCH p=(a)-[:R*0..0]->(b:A) RETURN length(p)", ((0,),)),
])
def test_interleaved_labels_edges_and_unlabeled_creation_use_actual_runtime_ids(tmp_path, suffix, expected):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            # ALLOC is allocated before B at runtime; prospective IDs must not
            # be mistaken for actual catalog/record authority.
            result = tx.execute("CREATE(a:A {v:1})-[:ALLOC]->(a) CREATE(b:B {v:2}) CREATE(c {v:3}) CREATE(a)-[:R]->(b) " + suffix)
            assert result.rows == expected
        assert db.verify("all").findings == ()


def test_merge_flexible_label_native_type_changes_and_multiplicity(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("UNWIND [1,1,'text'] AS v MERGE(n:Thing {v:v}) RETURN n")
            assert result.rows[0][0] == result.rows[1][0] != result.rows[2][0]
        assert db.execute("MATCH(n:Thing) RETURN count(n)").rows == ((2,),)


@pytest.mark.parametrize("query", [
    "UNWIND [] AS x CREATE(n:Unused {v:x})",
    "UNWIND [] AS x MERGE(n:Unused {v:x})",
    "UNWIND [] AS x CREATE(n:Unused) WITH count(n) AS c MATCH(m:Unused) RETURN m",
])
def test_no_input_creates_no_schema(tmp_path, query):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute(query)
        assert db.catalog.catalog.tables() == ()


def test_late_value_failure_rolls_back_all_implicit_schemas(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("CREATE(a:A)-[:R]->(b:B) CREATE(c:C {bad:0.0/0.0})")
            assert tx.execute("MATCH(n) RETURN count(n)").rows == ((0,),)
        assert db.catalog.catalog.tables() == ()
        assert db.verify("all").findings == ()


def test_explicit_typed_tables_are_not_widened(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
        original = db.catalog.catalog.table("Person")
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("CREATE(:Person {id:'text'})")
            with pytest.raises(GrafxError):
                tx.execute("CREATE(:Person {id:1, extra:2})")
        assert db.catalog.catalog.table("Person") == original


def test_read_doors_and_explain_cannot_create_implicit_labels(tmp_path):
    with connect(tmp_path / "db") as db:
        query = "CREATE(n:NeverCreated {v:1}) RETURN n"
        db.explain(query)
        with pytest.raises(GrafxError):
            db.execute(query)
        with pytest.raises(GrafxError):
            with db.query(query).cursor() as cursor:
                list(cursor)
        assert db.catalog.catalog.tables() == ()


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_labeled_models_transfer_copy_and_history_keep_labels_and_values(tmp_path, codec):
    from okto_grafx.catalog_copy import capture_copy, copy_graph, prepare_copy_target
    from okto_grafx.transfer import export_graph, import_graph
    with connect(tmp_path / "source", codec=codec) as source:
        with source.begin("write") as tx:
            tx.execute("CREATE(:Alpha {v:'text'})-[:LINK {weight:2}]->(:Beta {v:{nested:[true,null]}})")
        source.enable_commit_history()
        names = tuple(t.name for t in source.catalog.catalog.tables())
        source.enable_system_history(names)
        activated = source.commit_history().entries[-1].identity
        assert len(source.system_as_of(activated, tables=names).rows) == 3
        export_graph(source, tmp_path / "artifact", history="current-only")
        import_graph(tmp_path / "artifact", tmp_path / "target")
        with connect(tmp_path / "target", codec=codec) as target:
            assert target.execute("MATCH(a:Alpha)-[:LINK]->(b:Beta) RETURN a.v,b.v.nested[0]").rows == (("text",True),)
            with target.begin("write") as tx:
                tx.execute("MATCH(n) DETACH DELETE n")
            prepare_copy_target(target)
            target_names = tuple(t.name for t in target.catalog.catalog.tables() if not t.name.startswith("_grafx_"))
            target.enable_system_history(target_names)
            with source.begin("read") as tx:
                package = capture_copy(tx, tables=names, history="current-only")
            receipt = copy_graph(package, target, idempotency_key="labels")
            history = target.system_as_of(receipt.target_commit, tables=target_names)
            assert len(history.rows) == 3
            assert all(t.flexible_properties and not t.unlabeled for t in history.schemas)
            assert target.execute("MATCH(a:Alpha)-[:LINK]->(b:Beta) RETURN labels(a),labels(b)").rows == ((("Alpha",),("Beta",)),)
            assert target.verify("all").findings == ()


def test_label_and_type_names_are_independent(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a:R)-[:R]->(b:Other)")
        assert db.execute("MATCH(a:R)-[r:R]->(b:Other) RETURN labels(a),type(r),labels(b)").rows == (
            (("R",), "R", ("Other",)),)
        assert db.verify("all").findings == ()


def test_independent_snapshot_and_concurrent_label_creation_preserve_occ(tmp_path):
    with connect(tmp_path / "db") as first, connect(tmp_path / "db") as second:
        before = first.begin("read")
        writer = first.begin("write")
        try:
            writer.execute("CREATE(:Label {v:'first'})")
            with second.begin("write") as competing:
                competing.execute("CREATE(:Label {v:'second'})")
            with pytest.raises(GrafxError) as failure:
                writer.commit()
            assert failure.value.code == "write_conflict"
            assert before.execute("MATCH(n:Label) RETURN count(n)").rows == ((0,),)
        finally:
            if writer.active:
                writer.rollback()
            before.rollback()
        assert first.execute("MATCH(n:Label) RETURN n.v").rows == (("second",),)
        assert first.verify("all").findings == ()


def test_existing_v1_requires_explicit_upgrade_before_flexible_label(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Typed(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            with pytest.raises(GrafxError) as failure:
                tx.execute("CREATE(:Flexible {value:1})")
            assert failure.value.details["remedy"] == "maintenance.ensure_identity_indexes"
        assert not db.catalog.catalog.has_table("Flexible")
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE(:Flexible {value:1})")
        assert db.execute("MATCH(n:Flexible) RETURN n.value").rows == ((1,),)


def test_late_output_failure_preserves_earlier_transaction_and_zero_input_labels(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Earlier {v:1})")
            with pytest.raises(GrafxError):
                tx.execute("CREATE(:Failed {v:2}) RETURN 1/0")
            tx.execute("UNWIND [] AS x CREATE(:Unused) WITH count(*) AS c CREATE(:Actual {c:c})")
        assert {t.name for t in db.catalog.catalog.tables()} == {"Earlier", "Actual"}
        assert db.execute("MATCH(n:Actual) RETURN n.c").rows == ((0,),)
        assert db.verify("all").findings == ()


def test_failed_batch_removes_implicit_catalog_but_preserves_prior_schema(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Earlier {v:1})")
            with pytest.raises(GrafxError):
                tx.executemany("CREATE(:Batch {v:$v})", [{"v":1}, {"v":float("nan")}])
            tx.execute("CREATE(:After {v:2})")
        assert {t.name for t in db.catalog.catalog.tables()} == {"Earlier", "After"}
        assert db.verify("all").findings == ()


def test_public_result_failure_restores_schema_journal_as_well_as_rows(tmp_path, monkeypatch):
    import okto_grafx.engine.database as database
    from okto_grafx.errors import GrafxConfigurationError
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Earlier {v:1})")
            with monkeypatch.context() as patch:
                def refuse(*args, **kwargs):
                    raise GrafxConfigurationError("Injected public conversion failure.", field="result")
                patch.setattr(database, "_query_result_view", refuse)
                with pytest.raises(GrafxError):
                    tx.execute("CREATE(:Refused {v:2}) RETURN 1")
            tx.execute("CREATE(:After {v:3})")
        assert {t.name for t in db.catalog.catalog.tables()} == {"Earlier", "After"}
        assert db.verify("all").findings == ()


def test_unproven_schema_cleanup_aborts_instead_of_allowing_commit(tmp_path, monkeypatch):
    from okto_grafx.engine.query_engine import QueryEngine
    from okto_grafx.errors import GrafxConfigurationError
    with connect(tmp_path / "db") as db:
        tx = db.begin("write")
        tx.execute("CREATE(:Earlier {v:1})")
        original = QueryEngine._unwind_schema_statement
        def refuse(engine, effects, *, strict=False):
            if strict:
                raise GrafxConfigurationError("Injected cleanup refusal.", field="cleanup")
            return original(engine, effects)
        with monkeypatch.context() as patch:
            patch.setattr(QueryEngine, "_unwind_schema_statement", refuse)
            with pytest.raises(GrafxError):
                tx.execute("CREATE(:Refused)-[:R]->(:AlsoRefused) RETURN 1/0")
            assert not tx.active
            with pytest.raises(GrafxError):
                tx.commit()
        assert db.catalog.catalog.tables() == ()
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("cut,code,committed", [("before_commit",71,False),("before_apply",73,True),("after_commit",72,True)])
def test_implicit_labeled_schema_and_rows_survive_native_process_recovery(tmp_path, cut, code, committed):
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        db.checkpoint()
    worker = Path(__file__).resolve().parents[1] / "api" / "system_history_worker.py"
    result = subprocess.run([sys.executable, str(worker), str(path), "implicit_labels", cut],
                            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src")),
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == code, result.stderr
    for _ in range(2):
        with connect(path, page_size=512) as db:
            assert db.execute("MATCH(n) RETURN count(n)").rows == ((2 if committed else 0,),)
            if committed:
                assert db.execute("MATCH(a:A)-[r:R]->(b:B) RETURN a.v,b.v,type(r)").rows == (("text",2,"R"),)
            else:
                assert db.catalog.catalog.tables() == ()
            assert db.verify("all").findings == ()
            db.checkpoint()
