"""Native procedure writes own only the caller's statement, snapshot and budgets."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from pathlib import Path
import subprocess
import sys

import pytest

from okto_grafx import connect
from okto_grafx.errors import (GrafxConfigurationError, GrafxPlanError, GrafxQueryBudgetExceeded,
                               GrafxTransactionBudgetExceeded, GrafxTransactionStateError, GrafxUnsupportedOperation)
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure


def registry(callback, *, columns=(), granted=True, statements=128):
    """Build an explicitly permitted writer with a bounded mutation door."""
    return ExtensionRegistry(trusted=True, procedures=(TabularProcedure(
        "app.write", ("INT64",), columns, callback, mode="write",
        required_permissions=frozenset({"mutate"}), max_write_statements=statements,
    ),), procedure_permissions=frozenset({"mutate"}) if granted else frozenset())


def schema(db):
    """Declare the native typed schema before invoking a procedure."""
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE T (id INT64, value INT64, PRIMARY KEY(id))")


def insert(writer, value):
    """Create one row through the transaction-scoped capability."""
    writer.execute("CREATE (:T {id: $id, value: $id})", {"id": value})


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_native_write_call_cold_reopen_and_downstream_visibility(tmp_path, codec):
    path = tmp_path / "graph"
    with connect(path, codec=codec, extensions=registry(insert)) as db:
        schema(db)
        with db.begin("write") as tx:
            result = tx.execute("UNWIND [1, 2, 3] AS x CALL app.write(x) WITH x "
                                "MATCH (n:T {id:x}) RETURN n.id ORDER BY n.id LIMIT 1")
            assert result.rows == ((1,),)
            assert tx.execute("MATCH (n:T) RETURN count(n)").rows == ((3,),)
        db.verify()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (2,), (3,))
        db.verify()


def test_permissions_read_transaction_cursor_explain_and_empty_input():
    seen = []

    def callback(writer, value):
        seen.append(value)
        insert(writer, value)

    with connect(":memory:", extensions=registry(callback, granted=False)) as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.write(1)")
    with connect(":memory:", extensions=registry(callback)) as db:
        schema(db)
        for query in ("CALL app.write(1)", "UNWIND [] AS x CALL app.write(x)"):
            with pytest.raises(GrafxTransactionStateError):
                db.execute(query)
        with pytest.raises(GrafxUnsupportedOperation):
            with db.query("CALL app.write(1)").cursor() as cursor:
                list(cursor)
        nodes = [node for node in db.explain("CALL app.write(1)").walk() if node.label == "ProcedureRows"]
        assert nodes[0].details()["access"] == "transaction_write"
        with db.begin("write") as tx:
            tx.execute("UNWIND [] AS x CALL app.write(x)")
        assert seen == []


@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("failure_kind", ("callback", "output", "cleanup", "caught"))
def test_late_failures_restore_only_calling_statement(tmp_path, codec, failure_kind):
    seen = []

    def callback(writer, value):
        insert(writer, value)
        seen.append(value)
        if failure_kind == "callback":
            raise RuntimeError("callback failure")
        if failure_kind == "caught":
            try:
                writer.execute("RETURN 1")
            except GrafxPlanError:
                pass
        try:
            yield (value, "bad" if failure_kind == "output" else value)
        finally:
            if failure_kind == "cleanup":
                raise RuntimeError("cleanup failure")

    path = tmp_path / "graph"
    with connect(path, codec=codec, extensions=registry(callback, columns=(("value", "INT64"), ("hidden", "INT64")))) as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id: 1, value: 1})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.write(2) YIELD value RETURN value")
            tx.execute("CREATE (:T {id: 3, value: 3})")
        assert seen == [2]
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
        db.verify()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
        db.verify()


def test_authority_expiry_foreign_thread_and_no_commit_door():
    captured = []

    def callback(writer, value):
        captured.append(writer)
        assert not hasattr(writer, "commit") and not hasattr(writer, "begin")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(insert, writer, value + 100)
            with pytest.raises(GrafxTransactionStateError):
                future.result(timeout=5)
        insert(writer, value)

    with connect(":memory:", extensions=registry(callback)) as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CALL app.write(1)")
            with pytest.raises(GrafxTransactionStateError):
                insert(captured[0], 2)
            assert tx.execute("MATCH (n:T) RETURN n.id").rows == ((1,),)
        with pytest.raises(GrafxTransactionStateError):
            insert(captured[0], 3)


@pytest.mark.parametrize("query", (
    "RETURN 1", "CREATE NODE TABLE X (id INT64, PRIMARY KEY(id))",
    "CREATE (:Missing {id: 1})", "CREATE (:T {id: 1, value: 1}) RETURN 1",
    "CREATE (a:T {id:1, value:1}), (b:T {id:2, value:2}), (a)-[:Missing]->(b)",
))
def test_refused_capability_operations_have_no_effects(query):
    def callback(writer, value):
        writer.execute(query)

    with connect(":memory:", extensions=registry(callback)) as db:
        schema(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.write(1)")
            assert tx.execute("MATCH (n:T) RETURN count(n)").rows == ((0,),)


def test_statement_budget_is_shared_across_rows():
    with connect(":memory:", extensions=registry(insert, statements=2)) as db:
        schema(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxQueryBudgetExceeded) as failure:
                tx.execute("UNWIND [1,2,3] AS x CALL app.write(x)")
            assert failure.value.details["resource"] == "procedure_statements"
            assert tx.execute("MATCH (n:T) RETURN count(n)").rows == ((0,),)
            tx.execute("CALL app.write(4)")  # A new outer statement owns a fresh budget.


def test_native_write_budget_is_shared_with_outer_statement():
    with connect(":memory:", extensions=registry(insert), max_statement_writes=2) as db:
        schema(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxTransactionBudgetExceeded) as failure:
                tx.execute("CREATE (:T {id: 9, value: 9}) WITH 1 AS ignored "
                           "UNWIND [1,2] AS x CALL app.write(x)")
            actual = failure.value
            assert isinstance(actual, GrafxTransactionBudgetExceeded)
            assert actual.details["field"] == "max_statement_writes"
            assert tx.execute("MATCH (n:T) RETURN count(n)").rows == ((0,),)


@pytest.mark.parametrize("options", ({"mode": "WRITE"}, {"mode": True}, {"max_write_statements": 0},
                                     {"max_write_statements": True}, {"max_write_statements": 1025}))
def test_invalid_write_registration(options):
    with pytest.raises(GrafxConfigurationError):
        TabularProcedure("app.write", (), (), lambda: None, **options)


def test_write_registration_requires_permissions_and_direct_invocation_requires_authority():
    with pytest.raises(GrafxConfigurationError):
        TabularProcedure("app.write", (), (), lambda: None, mode="write")
    with pytest.raises(GrafxPlanError):
        tuple(registry(insert).procedures[0].invoke((1,)))


@pytest.mark.parametrize("forged", (1, object(), {}))
def test_read_procedure_never_receives_a_forged_writer_argument(forged):
    seen = []
    procedure = TabularProcedure("app.read", (), (), lambda *args: seen.append(args))
    with pytest.raises(GrafxPlanError) as failure:
        tuple(procedure.invoke((), writer=forged))
    assert failure.value.details["field"] == "procedure_authority"
    assert seen == []


def test_public_publication_failure_retains_previous_transaction_statements(tmp_path, monkeypatch):
    from okto_grafx.engine import database

    path = tmp_path / "publication"
    with connect(path, extensions=registry(insert)) as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id: 1, value: 1})")
            with monkeypatch.context() as patch:
                def refuse(*args, **kwargs):
                    raise GrafxPlanError("Injected public result failure.", field="result")
                patch.setattr(database, "_query_result_view", refuse)
                with pytest.raises(GrafxPlanError, match="Injected"):
                    tx.execute("CALL app.write(2)")
            tx.execute("CREATE (:T {id: 3, value: 3})")
        db.verify()
    with connect(path) as db:
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))


def test_updates_merge_delete_and_outer_entity_refresh():
    def callback(writer, value):
        writer.execute("MATCH (n:T {id:$id}) SET n.value = n.value + 1", {"id": value})
        writer.execute("MERGE (:T {id: 2, value: 20})")
        writer.execute("MATCH (n:T {id: 3}) DELETE n")

    with connect(":memory:", extensions=registry(callback)) as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("UNWIND [1,3] AS id CREATE (:T {id:id, value:10})")
            result = tx.execute("MATCH (n:T {id:1}) CALL app.write(n.id) RETURN n.value")
            assert result.rows == ((11,),)
            assert tx.execute("MATCH (n:T) RETURN n.id, n.value ORDER BY n.id").rows == ((1,11), (2,20))


@pytest.mark.parametrize("query", (
    "CALL () { CALL app.write(1) } RETURN 1 AS value",
    "CALL app.write(1) RETURN 1 AS value UNION ALL CALL app.write(2) RETURN 2 AS value",
))
def test_outer_subqueries_and_union_classify_procedure_effects(query):
    with connect(":memory:", extensions=registry(insert)) as db:
        schema(db)
        with pytest.raises(GrafxTransactionStateError):
            db.execute(query)
        with db.begin("write") as tx:
            result = tx.execute(query)
            assert result.rows == (((1,), (2,)) if "UNION" in query else ((1,),))
            assert tx.execute("MATCH (n:T) RETURN count(n)").rows == ((len(result.rows),),)


@pytest.mark.parametrize("budget", ("rows", "bytes"))
def test_output_budgets_are_shared_across_invocations(budget):
    def callback(writer, value):
        insert(writer, value)
        return ((value,),)

    base = registry(callback, columns=(("value", "INT64"),))
    procedure = replace(base.procedures[0], **({"max_rows": 2} if budget == "rows" else {"max_result_bytes": 18}))
    with connect(":memory:", extensions=replace(base, procedures=(procedure,))) as db:
        schema(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxQueryBudgetExceeded) as failure:
                tx.execute("UNWIND [1,2,3] AS x CALL app.write(x) YIELD value RETURN value")
            assert failure.value.details["resource"] == "procedure_outputs"
            assert tx.execute("MATCH (n:T) RETURN count(n)").rows == ((0,),)


def test_generator_cleanup_can_write_before_authority_expires():
    captured = []

    def callback(writer, value):
        captured.append(writer)
        try:
            insert(writer, value)
            yield (value,)
        finally:
            insert(writer, value + 1)

    with connect(":memory:", extensions=registry(callback, columns=(("value", "INT64"),))) as db:
        schema(db)
        with db.begin("write") as tx:
            assert tx.execute("CALL app.write(1) YIELD value RETURN value LIMIT 0").rows == ()
            assert tx.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (2,))
            with pytest.raises(GrafxTransactionStateError):
                insert(captured[0], 3)


def test_other_participants_can_read_and_write_while_callback_is_active(tmp_path):
    path = tmp_path / "concurrent"
    ready, finish = Event(), Event()

    def callback(writer, value):
        insert(writer, value)
        ready.set()
        assert finish.wait(timeout=30)

    def run_writer():
        with connect(path, extensions=registry(callback)) as db:
            with db.begin("write") as tx:
                tx.execute("CALL app.write(1)")

    with connect(path) as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Other (id INT64, PRIMARY KEY(id))")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_writer)
            try:
                assert ready.wait(timeout=30)
                assert db.execute("MATCH (n:T) RETURN count(n)").rows == ((0,),)
                with db.begin("write") as tx:
                    tx.execute("CREATE (:Other {id: 1})")
            finally:
                finish.set()
            future.result(timeout=30)
        assert db.execute("MATCH (n:T) RETURN n.id").rows == ((1,),)
        assert db.execute("MATCH (n:Other) RETURN n.id").rows == ((1,),)
        db.verify()


def test_ast_effect_hint_cannot_suppress_registered_write_authority():
    from okto_grafx.domain.query import parse
    from okto_grafx.domain.query.procedure_resolution import resolve_procedure_calls

    procedure = registry(insert).procedures[0]
    parsed = parse("CALL app.write(1)")
    assert not parsed.writes  # Syntax alone has no registry authority.
    resolved = resolve_procedure_calls(parsed, {procedure.name: procedure})
    assert resolved.writes
    forged = replace(resolved, clause_pipeline=(replace(resolved.ordered_clauses()[0], writes=False),))
    assert resolve_procedure_calls(forged, {procedure.name: procedure}).writes
    read = replace(procedure, mode="read")
    assert not resolve_procedure_calls(resolved, {read.name: read}).writes


@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("phase,exit_code", (("callback", 71), ("statement", 72), ("commit", 73)))
def test_abrupt_exit_requires_durable_commit_for_procedure_effects(tmp_path, codec, phase, exit_code):
    path = tmp_path / "crash"
    with connect(path, codec=codec) as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id: 1, value: 1})")
    worker = Path(__file__).with_name("writing_procedure_worker.py")
    completed = subprocess.run([sys.executable, str(worker), str(path), codec, phase],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == exit_code, completed.stdout + completed.stderr
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == (
            ((1,), (2,)) if phase == "commit" else ((1,),)
        )
        db.verify()


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_relationship_mutations_and_rollback_share_native_endpoint_authority(tmp_path, codec):
    def callback(writer, value):
        if value == 1:
            writer.execute("MATCH (a:T {id:1}), (b:T {id:2}) CREATE (a)-[:R {value:10}]->(b)")
            writer.execute("MATCH (:T)-[r:R]->(:T) SET r.value = r.value + 1")
        else:
            writer.execute("MATCH (:T)-[r:R]->(:T) DELETE r")
            if value == 2:
                raise RuntimeError("refused deletion")

    path = tmp_path / "edges"
    with connect(path, codec=codec, extensions=registry(callback)) as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE REL TABLE R (FROM T TO T, value INT64)")
            tx.execute("UNWIND [1,2] AS id CREATE (:T {id:id, value:id})")
        with db.begin("write") as tx:
            tx.execute("CALL app.write(1)")
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.write(2)")
            assert tx.execute("MATCH (:T)-[r:R]->(:T) RETURN r.value").rows == ((11,),)
        db.verify()
    with connect(path, codec=codec, extensions=registry(callback)) as db:
        assert db.execute("MATCH (:T)-[r:R]->(:T) RETURN r.value").rows == ((11,),)
        with db.begin("write") as tx:
            tx.execute("CALL app.write(3)")
        assert db.execute("MATCH (:T)-[r:R]->(:T) RETURN count(r)").rows == ((0,),)
        db.verify()
