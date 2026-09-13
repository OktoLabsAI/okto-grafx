"""Procedure DDL is permissioned and remains inside the outer schema/data journal."""

from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxConfigurationError, GrafxParseError, GrafxPlanError, GrafxQueryBudgetExceeded, GrafxTransactionStateError, GrafxWriteConflict
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure


def procedure(callback, *, name="app.schema", columns=(), **options):
    """Declare schema authority separately from ordinary graph-write permission."""
    return TabularProcedure(name, (), columns, callback, mode="write", schema_write=True,
                             required_permissions=frozenset({"schema"}), **options)


def registry(*procedures, granted=True):
    """Supply only the explicitly requested connection-local permission."""
    return ExtensionRegistry(trusted=True, procedures=procedures,
                             procedure_permissions=frozenset({"schema"}) if granted else frozenset())


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_schema_and_data_commit_together_with_cold_reopen(tmp_path, codec):
    def callback(writer):
        writer.schema("CREATE NODE TABLE T (id INT64, value INT64, PRIMARY KEY(id))")
        writer.schema("CREATE REL TABLE R (FROM T TO T, value INT64)")
        writer.execute("CREATE (a:T {id:1,value:10}), (b:T {id:2,value:20}), (a)-[:R {value:30}]->(b)")
        assert writer.query("MATCH ()-[r:R]->() RETURN r.value").rows == ((30,),)
    path = tmp_path / "graph"
    with connect(path, codec=codec, extensions=registry(procedure(callback))) as db:
        with db.begin("write") as tx:
            tx.execute("CALL app.schema()")
            assert tx.execute("MATCH (n:T) RETURN count(n)").rows == ((2,),)
        db.verify()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH ()-[r:R]->() RETURN r.value").rows == ((30,),)
        db.verify()


def test_new_schema_entity_can_be_returned_and_updated_in_outer_statement():
    def callback(writer):
        writer.schema("CREATE NODE TABLE New (id INT64, value INT64, PRIMARY KEY(id))")
        return writer.query("CREATE (n:New {id:1,value:2}) RETURN n").rows
    with connect(":memory:", extensions=registry(procedure(callback, columns=(("node","NODE"),)))) as db:
        with db.begin("write") as tx:
            assert tx.execute("CALL app.schema() YIELD node SET node.value=3 RETURN node.value").rows == ((3,),)


@pytest.mark.parametrize("failure_kind", ("callback", "late_row", "cleanup"))
def test_schema_refusal_removes_only_current_statement_artifacts(failure_kind):
    def callback(writer):
        try:
            writer.schema("CREATE NODE TABLE Ghost (id INT64, PRIMARY KEY(id))")
            writer.execute("CREATE (:Ghost {id:1})")
            if failure_kind == "callback":
                raise RuntimeError("Injected callback failure")
            yield (1,)
            if failure_kind == "late_row":
                yield ("wrong",)
        finally:
            if failure_kind == "cleanup":
                raise RuntimeError("Injected cleanup failure")
    with connect(":memory:", extensions=registry(procedure(callback, columns=(("value","INT64"),)))) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Earlier (id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:Earlier {id:9})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.schema() YIELD value RETURN value LIMIT 1")
            assert tx.execute("MATCH (n:Earlier) RETURN n.id").rows == ((9,),)
            tx.execute("CREATE NODE TABLE Ghost (id INT64, PRIMARY KEY(id))")
        assert db.execute("MATCH (n:Ghost) RETURN count(n)").rows == ((0,),)
        db.verify()


def test_permission_refusal_read_transaction_and_expiry():
    called, retained = [], []
    def callback(writer):
        called.append(True)
        retained.append(writer)
        writer.schema("CREATE NODE TABLE T (id INT64, PRIMARY KEY(id))")
    with connect(":memory:", extensions=registry(procedure(callback), granted=False)) as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.schema()")
        assert not called
    with connect(":memory:", extensions=registry(procedure(callback))) as db:
        with pytest.raises(GrafxTransactionStateError):
            db.execute("CALL app.schema()")
        assert not called
        with db.begin("write") as tx:
            tx.execute("CALL app.schema()")
        with pytest.raises(GrafxTransactionStateError):
            retained[0].schema("CREATE NODE TABLE X (id INT64, PRIMARY KEY(id))")


@pytest.mark.parametrize("options", ({"schema_write":1}, {"schema_write":True},
    {"schema_write":True,"mode":"write","required_permissions":frozenset({"graph"})},
    {"max_schema_statements":0}, {"max_schema_statements":True}, {"max_schema_statements":1025}))
def test_schema_descriptor_admission(options):
    with pytest.raises(GrafxConfigurationError):
        TabularProcedure("app.invalid", (), (), lambda:None, **options)


def test_schema_budget_and_shared_write_budget_rollback_catalog():
    def callback(writer):
        writer.schema("CREATE NODE TABLE A (id INT64, PRIMARY KEY(id))")
        writer.schema("CREATE NODE TABLE B (id INT64, PRIMARY KEY(id))")
    for option in ("max_schema_statements", "max_write_statements"):
        with connect(":memory:", extensions=registry(procedure(callback, **{option:1}))) as db:
            with db.begin("write") as tx:
                with pytest.raises(GrafxQueryBudgetExceeded):
                    tx.execute("CALL app.schema()")
                tx.execute("CREATE NODE TABLE A (id INT64, PRIMARY KEY(id))")
            assert len(db.catalog.catalog.tables()) == 1


def test_dml_parent_cannot_escalate_via_schema_child():
    called = []
    def outer(writer):
        writer.query("CALL app.inner()")
    def inner(writer):
        called.append(True)
        writer.schema("CREATE NODE TABLE T (id INT64, PRIMARY KEY(id))")
    parent = replace(procedure(outer), schema_write=False)
    with connect(":memory:", extensions=registry(parent, procedure(inner, name="app.inner"))) as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxTransactionStateError):
                tx.execute("CALL app.schema()")
        assert not called


def test_nested_schema_adoption_and_outer_failure_roll_back_all_levels():
    def outer(writer):
        writer.schema("CREATE NODE TABLE A (id INT64, PRIMARY KEY(id))")
        writer.query("CALL app.inner()")
        writer.execute("CREATE (:B {id:1})")
        raise RuntimeError("Rollback all levels")
    def inner(writer):
        writer.schema("CREATE NODE TABLE B (id INT64, PRIMARY KEY(id))")
    with connect(":memory:", extensions=registry(procedure(outer), procedure(inner, name="app.inner"))) as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.schema()")
            tx.execute("CREATE NODE TABLE A (id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B (id INT64, PRIMARY KEY(id))")
        db.verify()


@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("fail", (False, True))
def test_vector_space_and_custom_index_follow_schema_journal(tmp_path, codec, fail):
    def callback(writer):
        writer.schema("CREATE VECTOR SPACE s {dimension:2, metric:'cosine'}")
        writer.schema("CREATE NODE TABLE V (id INT64, score INT64, embedding VECTOR(s), PRIMARY KEY(id))")
        writer.execute("CREATE (:V {id:1,score:10,embedding:[1.0,0.0]})")
        writer.schema("CREATE INDEX by_score FOR (v:V) ON (v.score) OPTIONS bucket_count=8")
        if fail:
            raise RuntimeError("Injected late DDL failure")
    path = tmp_path / "schema"
    with connect(path, codec=codec, extensions=registry(procedure(callback))) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            if fail:
                with pytest.raises(GrafxPlanError):
                    tx.execute("CALL app.schema()")
            else:
                tx.execute("CALL app.schema()")
        if fail:
            assert not db.catalog.catalog.tables()
            assert not db.catalog.catalog.spaces()
            assert "by_score" not in db.attached_indexes
        else:
            assert db.execute("MATCH (n:V {score:10}) RETURN n.id").rows == ((1,),)
            assert db.indexes.index("by_score").columns == ("score",)
        db.verify()
    with connect(path, codec=codec) as db:
        assert bool(db.catalog.catalog.tables()) is not fail
        db.verify()


@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("phase,exit_code", (("callback",71), ("statement",72), ("commit",73)))
def test_schema_effects_require_durable_commit_after_process_exit(tmp_path, codec, phase, exit_code):
    path = tmp_path / "crash"
    with connect(path, codec=codec):
        pass
    child = subprocess.run([sys.executable, str(Path(__file__).with_name("schema_procedure_worker.py")),
                            str(path), codec, phase], capture_output=True, text=True, timeout=60)
    assert child.returncode == exit_code, child.stderr
    with connect(path, codec=codec) as db:
        if phase == "commit":
            assert db.execute("MATCH (n:New) RETURN n.id").rows == ((7,),)
        else:
            assert not db.catalog.catalog.has_table("New")
        db.verify()


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_post_durable_apply_failure_recovers_schema_and_data(tmp_path, codec, monkeypatch):
    from okto_grafx.engine.txn_manager import TransactionManager
    def callback(writer):
        writer.schema("CREATE NODE TABLE New (id INT64, PRIMARY KEY(id))")
        writer.execute("CREATE (:New {id:1})")
    path = tmp_path / "recover"
    with connect(path, codec=codec, extensions=registry(procedure(callback))) as db:
        tx = db.begin("write")
        tx.execute("CALL app.schema()")
        def fail(manager, images):
            raise RuntimeError("Injected durable apply failure")
        with monkeypatch.context() as patch:
            patch.setattr(TransactionManager, "_apply_images", fail)
            with pytest.raises(GrafxTransactionStateError) as failure:
                tx.commit()
            assert failure.value.details["committed"] is True
            assert failure.value.details["durable"] is True
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:New) RETURN n.id").rows == ((1,),)
        db.verify()


def test_speculative_schema_is_hidden_and_loser_does_not_remove_winner(tmp_path):
    def callback(writer):
        writer.schema("CREATE NODE TABLE T (id INT64, PRIMARY KEY(id))")
        writer.execute("CREATE (:T {id:1})")
    path = tmp_path / "concurrent"
    with connect(path, extensions=registry(procedure(callback))) as db, connect(path) as other:
        loser = db.begin("write")
        loser.execute("CALL app.schema()")
        assert other.execute("MATCH (n:T) RETURN count(n)").rows == ((0,),)
        with other.begin("write") as winner:
            winner.execute("CREATE NODE TABLE T (id INT64, PRIMARY KEY(id))")
            winner.execute("CREATE (:T {id:2})")
        with pytest.raises(GrafxWriteConflict):
            loser.commit()
        assert other.execute("MATCH (n:T) RETURN n.id").rows == ((2,),)
    with connect(path) as db:
        assert db.execute("MATCH (n:T) RETURN n.id").rows == ((2,),)
        db.verify()


def test_schema_budget_spans_repeated_invocations():
    calls = []
    def callback(writer):
        calls.append(1)
        writer.schema(f"CREATE NODE TABLE T{len(calls)} (id INT64, PRIMARY KEY(id))")
    with connect(":memory:", extensions=registry(procedure(callback, max_schema_statements=1))) as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxQueryBudgetExceeded):
                tx.execute("UNWIND [1,2] AS x CALL app.schema()")
        assert not db.catalog.catalog.tables()


def test_outer_scan_keeps_compiled_catalog_and_next_statement_sees_new_schema():
    def callback(writer):
        writer.schema("CREATE NODE TABLE New (id INT64, PRIMARY KEY(id))")
        writer.execute("CREATE (:New {id:1})")
        assert writer.query("MATCH (n:New) RETURN n.id").rows == ((1,),)
    with connect(":memory:", extensions=registry(procedure(callback))) as db:
        with db.begin("write") as tx:
            assert tx.execute("CALL app.schema() WITH 1 AS ignored MATCH (n:New) RETURN n.id").rows == ()
            assert tx.execute("MATCH (n:New) RETURN n.id").rows == ((1,),)


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_implicit_schema_and_heterogeneous_properties_share_native_authority(tmp_path, codec):
    def callback(writer):
        result = writer.query("CREATE p=(a {value:'text'})-[:R {values:[1,'two']}]->(b:Named {value:2}) RETURN p")
        assert writer.query("MATCH (a)-[r:R]->(b) RETURN a.value, r.values, b.value").rows == (("text", [1,"two"], 2),)
        return result.rows
    path = tmp_path / "implicit"
    with connect(path, codec=codec, extensions=registry(procedure(callback, columns=(("path", "PATH"),)))) as db:
        with db.begin("write") as tx:
            result = tx.execute("CALL app.schema() YIELD path RETURN path")
            assert len(result.rows[0][0].nodes) == 2
        assert db.verify("all").findings == ()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (a)-[r:R]->(b) RETURN a.value, r.values, b.value").rows == (("text", (1,"two"), 2),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("operation", ("query", "execute"))
@pytest.mark.parametrize("limit", (1, 2))
def test_implicit_schema_is_charged_and_rolled_back_before_quota_overrun(operation, limit):
    def callback(writer):
        getattr(writer, operation)("CREATE (a)-[:R]->(b:Named)")
    with connect(":memory:", extensions=registry(procedure(callback, max_schema_statements=limit))) as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxQueryBudgetExceeded) as failure:
                tx.execute("CALL app.schema()")
            assert failure.value.details["resource"] == "procedure_schema_statements"
            assert tx.execute("MATCH (n) RETURN count(n)").rows == ((0,),)
        assert not db.catalog.catalog.tables()
        assert db.verify("all").findings == ()


def test_nested_implicit_schema_returns_entities_without_leaking_authority():
    def inner(writer):
        return writer.query("CREATE (n {value:'inner'}) RETURN n").rows
    def outer(writer):
        return writer.query("CALL app.inner() YIELD node RETURN node").rows
    procedures = (procedure(outer, columns=(("node","NODE"),)),
                  procedure(inner, name="app.inner", columns=(("node","NODE"),)))
    with connect(":memory:", extensions=registry(*procedures)) as db:
        with db.begin("write") as tx:
            assert tx.execute("CALL app.schema() YIELD node SET node.value=42 RETURN node.value").rows == ((42,),)
        assert db.execute("MATCH (n) RETURN n.value").rows == ((42,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("new_table", (False, True))
def test_composable_index_covers_rows_before_and_after_ddl(tmp_path, codec, new_table):
    from okto_grafx.domain.query.plan import IndexSeek, plan_nodes
    def callback(writer):
        writer.execute("MATCH (n:T {id:1}) SET n.value=11")
        writer.execute("MATCH (n:T {id:2}) DELETE n")
        writer.execute("CREATE (:T {id:3,value:30})")
        writer.schema("CREATE INDEX by_value FOR (n:T) ON (n.value) OPTIONS bucket_count=8")
        assert writer.query("MATCH (n:T {value:11}) RETURN n.id").rows == ((1,),)
        writer.execute("MATCH (n:T {id:3}) SET n.value=31")
        writer.execute("CREATE (:T {id:4,value:40})")
        assert writer.query("MATCH (n:T {value:31}) RETURN n.id").rows == ((3,),)
    path = tmp_path / "mixed"
    with connect(path, codec=codec, extensions=registry(procedure(callback))) as db:
        db.ensure_identity_indexes()
        tx = db.begin("write")
        tx.execute("CREATE NODE TABLE T (id INT64, value INT64, PRIMARY KEY(id))")
        tx.execute("CREATE (:T {id:1,value:10}), (:T {id:2,value:20})")
        if not new_table:
            tx.commit()
            tx = db.begin("write")
        tx.execute("CALL app.schema()")
        tx.execute("CREATE (:T {id:5,value:50})")
        tx.commit()
        assert any(isinstance(node, IndexSeek) for node in plan_nodes(
            db.explain("MATCH (n:T {value:31}) RETURN n.id")
        ))
        assert db.verify("all").findings == ()
    with connect(path, codec=codec) as db:
        assert any(isinstance(node, IndexSeek) for node in plan_nodes(
            db.explain("MATCH (n:T {value:31}) RETURN n.id")
        ))
        for value, expected in ((10,()), (20,()), (30,()), (11,((1,),)), (31,((3,),)), (40,((4,),)), (50,((5,),))):
            assert db.execute("MATCH (n:T {value:$value}) RETURN n.id", {"value":value}).rows == expected
        assert not db.indexes.index("by_value").automatic
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("foreign_write", ("before", "after"))
def test_index_base_is_fenced_against_foreign_writes(tmp_path, foreign_write):
    def callback(writer):
        writer.schema("CREATE INDEX by_value FOR (n:T) ON (n.value) OPTIONS bucket_count=8")
    path = tmp_path / "conflict"
    with connect(path, extensions=registry(procedure(callback))) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as seed:
            seed.execute("CREATE NODE TABLE T (id INT64, value INT64, PRIMARY KEY(id))")
            seed.execute("CREATE (:T {id:1,value:10})")
        with connect(path) as other:
            loser = db.begin("write")
            if foreign_write == "after":
                loser.execute("CALL app.schema()")
            with other.begin("write") as winner:
                winner.execute("MATCH (n:T {id:1}) SET n.value=20")
            if foreign_write == "before":
                loser.execute("CALL app.schema()")
            assert not other._catalog.catalog.has_index_definition("by_value")
            with pytest.raises(GrafxWriteConflict):
                loser.commit()
        assert db.execute("MATCH (n:T {value:20}) RETURN n.id").rows == ((1,),)
        assert not db._catalog.catalog.has_index_definition("by_value")
        assert db.verify("all").findings == ()


def test_failed_existing_table_index_unwinds_then_same_name_can_be_retried():
    def callback(writer):
        writer.schema("CREATE INDEX by_value FOR (n:T) ON (n.value) OPTIONS bucket_count=8")
        writer.execute("MATCH (n:T {id:1}) SET n.value=999")
        raise RuntimeError("Abort new index")
    with connect(":memory:", extensions=registry(procedure(callback))) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as seed:
            seed.execute("CREATE NODE TABLE T (id INT64, value INT64, PRIMARY KEY(id))")
            seed.execute("CREATE (:T {id:1,value:10})")
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:2,value:20})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.schema()")
            tx.execute("CREATE INDEX by_value FOR (n:T) ON (n.value) OPTIONS bucket_count=8")
            assert tx.execute("MATCH (n:T {value:10}) RETURN n.id").rows == ((1,),)
            assert tx.execute("MATCH (n:T {value:20}) RETURN n.id").rows == ((2,),)
        assert db.execute("MATCH (n:T {value:999}) RETURN n.id").rows == ()
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("phase,exit_code", (("callback",71), ("statement",72), ("commit",73)))
def test_detached_index_and_own_row_deltas_require_same_durable_commit(tmp_path, codec, phase, exit_code):
    path = tmp_path / "index-crash"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Existing (id INT64, value INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:Existing {id:1,value:10})")
    child = subprocess.run([sys.executable, str(Path(__file__).with_name("schema_procedure_worker.py")),
                            str(path), codec, phase, "yes"], capture_output=True, text=True, timeout=60)
    assert child.returncode == exit_code, child.stderr
    with connect(path, codec=codec) as db:
        assert db._catalog.catalog.has_index_definition("by_value") is (phase == "commit")
        if phase == "commit":
            assert db.execute("MATCH (n:Existing {value:20}) RETURN n.id").rows == ((1,),)
            assert db.execute("MATCH (n:Existing {value:30}) RETURN n.id").rows == ((2,),)
            assert db.execute("MATCH (n:Existing {value:10}) RETURN n.id").rows == ()
        else:
            assert db.execute("MATCH (n:Existing) RETURN n.id,n.value").rows == ((1,10),)
            assert not db.catalog.catalog.has_table("New")
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("layout", ("hash", "sparse_hash", "posting_hash", "ordered"))
@pytest.mark.parametrize("new_table", (False, True))
def test_composable_index_preserves_every_textual_layout(tmp_path, layout, new_table):
    from okto_grafx import Timestamp
    def callback(writer):
        writer.schema(f"CREATE INDEX by_time FOR (n:T) ON (n.time,n.id) OPTIONS layout={layout}")
        writer.query("CREATE (:T {id:'b',time:$time}) RETURN 1", {"time":Timestamp(20)})
    path = tmp_path / "layout"
    with connect(path, extensions=registry(procedure(callback))) as db:
        db.ensure_identity_indexes()
        tx = db.begin("write")
        tx.execute("CREATE NODE TABLE T (id STRING, time TIMESTAMP, PRIMARY KEY(id))")
        tx.execute("CREATE (:T {id:'a',time:$time})", {"time":Timestamp(10)})
        if not new_table:
            tx.commit()
            tx = db.begin("write")
        tx.execute("CALL app.schema()")
        tx.commit()
        assert db.verify("all").findings == ()
    with connect(path) as db:
        assert db.indexes.index("by_time").layout.value == layout
        for name, micros in (("a",10), ("b",20)):
            assert db.execute("MATCH (n:T {id:$id,time:$time}) RETURN n.id", {"id":name,"time":Timestamp(micros)}).rows == ((name,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("query,error", (("RETURN 1",GrafxPlanError), ("CREATE (:T)",GrafxPlanError), ("DROP TABLE T",GrafxParseError)))
def test_invalid_schema_calls_poison_even_when_callback_catches(query, error):
    def callback(writer):
        writer.schema("CREATE NODE TABLE T (id INT64, PRIMARY KEY(id))")
        try:
            writer.schema(query)
        except Exception:
            pass
    with connect(":memory:", extensions=registry(procedure(callback))) as db:
        with db.begin("write") as tx:
            with pytest.raises(error):
                tx.execute("CALL app.schema()")
        assert not db.catalog.catalog.tables()
        assert db.verify("all").findings == ()
