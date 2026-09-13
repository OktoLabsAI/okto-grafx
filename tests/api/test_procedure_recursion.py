"""Nested native CALL keeps authority, effects and budgets at the outer statement."""

from dataclasses import replace
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from okto_grafx import connect, CancellationToken
from okto_grafx.errors import (GrafxPlanError, GrafxQueryBudgetExceeded, GrafxTransactionBudgetExceeded,
                               GrafxTransactionStateError, GrafxConfigurationError, GrafxQueryCancelled)
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure


def procedure(name, callback, *, mode="read", columns=(("value","INT64"),), **options):
    """Declare native authority and one integer recursion argument."""
    return TabularProcedure(name, ("INT64",), columns, callback, mode=mode, graph_read=mode == "read",
                             required_permissions=frozenset({"graph"}), **options)


def registry(*procedures):
    """Grant only the test's graph permission on this handle."""
    return ExtensionRegistry(trusted=True, procedures=procedures, procedure_permissions=frozenset({"graph"}))


def seed(db):
    """Use declared schema to isolate recursion from schema-changing procedures."""
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE T (id INT64, value INT64, PRIMARY KEY(id))")


def test_recursive_read_factorial_and_cursor():
    def factorial(reader, n):
        value = 1 if n <= 1 else n * reader.query("CALL app.factorial($n)", {"n":n-1}).rows[0][0]
        return ((value,),)
    with connect(":memory:", extensions=registry(procedure("app.factorial", factorial))) as db:
        assert db.execute("CALL app.factorial(6)").rows == ((720,),)
        with db.query("CALL app.factorial(4)").cursor(batch_size=1) as cursor:
            assert tuple(cursor) == ((24,),)


@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("door", ("execute", "query"))
def test_recursive_writes_commit_once_and_reopen(tmp_path, codec, door):
    def write(writer, n):
        writer.execute("CREATE (:T {id:$n,value:$n})", {"n":n})
        if n > 1:
            getattr(writer, door)("CALL app.write($n)", {"n":n-1})
    path = tmp_path / "graph"
    with connect(path, codec=codec, max_statement_writes=4,
                 extensions=registry(procedure("app.write", write, mode="write", columns=()))) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CALL app.write(4)")
            assert tx.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,),(2,),(3,),(4,))
        db.verify()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:T) RETURN count(n)").rows == ((4,),)
        db.verify()


@pytest.mark.parametrize("limit", (1, 3, 8, 16))
def test_infinite_recursion_fails_at_declared_depth_not_python_recursion(limit):
    calls = []
    def recurse(reader, n):
        calls.append(n)
        return reader.query("CALL app.recurse($n)", {"n":n+1}).rows
    with connect(":memory:", extensions=registry(procedure("app.recurse", recurse, max_call_depth=limit))) as db:
        with pytest.raises(GrafxQueryBudgetExceeded) as failure:
            db.execute("CALL app.recurse(0)")
        assert failure.value.details["resource"] == "procedure_call_depth"
        assert failure.value.details["observed"] == limit + 1
        assert len(calls) == limit


def test_mutual_recursion_inherits_stricter_ancestor_limit():
    def a(reader, n):
        return reader.query("CALL app.b($n)", {"n":n}).rows
    def b(reader, n):
        return reader.query("CALL app.a($n)", {"n":n}).rows
    with connect(":memory:", extensions=registry(procedure("app.a", a, max_call_depth=2),
                                                 procedure("app.b", b, max_call_depth=16))) as db:
        with pytest.raises(GrafxQueryBudgetExceeded) as failure:
            db.execute("CALL app.a(0)")
        assert failure.value.details["limit"] == 2


@pytest.mark.parametrize("limits", ({"max_query_statements":2}, {"max_query_rows":2}, {"max_query_bytes":18}))
def test_recursive_query_budgets_do_not_reset_at_child_context(limits):
    def recurse(reader, n):
        return ((1,),) if n == 0 else reader.query("CALL app.r($n)", {"n":n-1}).rows
    with connect(":memory:", extensions=registry(procedure("app.r", recurse, **limits))) as db:
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("CALL app.r(3)")


def test_outer_native_write_budget_counts_deep_descendants():
    def write(writer, n):
        writer.execute("CREATE (:T {id:$n,value:$n})", {"n":n})
        if n:
            writer.query("CALL app.write($n)", {"n":n-1})
    with connect(":memory:", max_statement_writes=2,
                 extensions=registry(procedure("app.write", write, mode="write", columns=()))) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:99,value:99})")
            with pytest.raises(GrafxTransactionBudgetExceeded):
                tx.execute("CALL app.write(3)")
            assert tx.execute("MATCH (n:T) RETURN n.id").rows == ((99,),)


def test_read_authority_cannot_escalate_to_registered_writer_in_write_transaction():
    calls = []
    def reader(scope, n):
        return scope.query("CALL app.write($n)", {"n":n}).rows
    def writer(scope, n):
        calls.append(n)
        scope.execute("CREATE (:T {id:$n,value:$n})", {"n":n})
    with connect(":memory:", extensions=registry(procedure("app.read", reader),
                                                 procedure("app.write", writer, mode="write", columns=()))) as db:
        seed(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxTransactionStateError):
                tx.execute("CALL app.read(1)")
        assert not calls


def test_ungranted_nested_procedure_is_refused_before_callback():
    def outer(scope, n):
        return scope.query("CALL app.denied($n)", {"n":n}).rows
    denied = replace(procedure("app.denied", lambda *_: pytest.fail("Unauthorized callback")),
                     required_permissions=frozenset({"secret"}))
    with connect(":memory:", extensions=registry(procedure("app.outer", outer), denied)) as db:
        with pytest.raises(GrafxPlanError):
            db.execute("CALL app.outer(1)")


def test_nested_native_entity_output_retains_provisional_identity():
    def outer(writer, n):
        return writer.query("CALL app.inner($n)", {"n":n}).rows
    def inner(writer, n):
        return writer.query("CREATE (n:T {id:$id,value:1}) RETURN n", {"id":n}).rows
    extensions = registry(procedure("app.outer", outer, mode="write", columns=(("node","NODE"),)),
                          procedure("app.inner", inner, mode="write", columns=(("node","NODE"),)))
    with connect(":memory:", extensions=extensions) as db:
        seed(db)
        with db.begin("write") as tx:
            assert tx.execute("CALL app.outer(1) YIELD node SET node.value=9 RETURN node.value").rows == ((9,),)


def test_late_outer_failure_rolls_back_successful_deep_writes():
    def outer(writer, n):
        writer.query("CALL app.inner($n)", {"n":n})
        yield (n,)
        yield ("invalid",)
    def inner(writer, n):
        writer.execute("CREATE (:T {id:$n,value:1})", {"n":n})
        if n > 0:
            writer.query("CALL app.inner($n)", {"n":n-1})
    with connect(":memory:", extensions=registry(procedure("app.outer", outer, mode="write"),
                                                 procedure("app.inner", inner, mode="write", columns=()))) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:99,value:99})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.outer(2) YIELD value RETURN value LIMIT 1")
            assert tx.execute("MATCH (n:T) RETURN n.id").rows == ((99,),)


@pytest.mark.parametrize("options", ({"max_call_depth":0}, {"max_call_depth":17}, {"max_call_depth":True},
                                      {"deterministic":1}, {"deterministic":True,"mode":"write"}))
def test_invalid_depth_and_determinism_declarations(options):
    with pytest.raises(GrafxConfigurationError):
        procedure("app.invalid", lambda *_: (), **options)


def test_deterministic_reader_can_only_compose_deterministic_native_effects():
    def outer(scope, n):
        return scope.query("CALL app.inner($n)", {"n":n}).rows
    inner = procedure("app.inner", lambda scope,n: ((n,),))
    with connect(":memory:", extensions=registry(procedure("app.outer", outer, deterministic=True), inner)) as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute("CALL app.outer(1)")
        assert failure.value.details["field"] == "procedure_effect"
    with connect(":memory:", extensions=registry(procedure("app.outer", outer, deterministic=True),
                                                 replace(inner, deterministic=True))) as db:
        assert db.execute("CALL app.outer(1)").rows == ((1,),)


def test_recursive_cancellation_reaches_every_native_level():
    token = CancellationToken()
    def recurse(reader, n):
        if n == 0:
            token.cancel()
        return reader.query("CALL app.r($n)", {"n":n-1}).rows
    with connect(":memory:", extensions=registry(procedure("app.r", recurse))) as db:
        with pytest.raises(GrafxQueryCancelled):
            db.execute("CALL app.r(3)", cancellation=token)


def test_nested_callback_wait_keeps_independent_writer_and_original_snapshot(tmp_path):
    entered, resume = Event(), Event()
    def outer(reader, n):
        return reader.query("CALL app.inner($n)", {"n":n}).rows
    def inner(reader, n):
        first = reader.query("MATCH (n:T) RETURN n.value").rows
        entered.set()
        assert resume.wait(30)
        assert reader.query("MATCH (n:T) RETURN n.value").rows == first
        return first
    path = tmp_path / "concurrent"
    with connect(path, extensions=registry(procedure("app.outer", outer), procedure("app.inner", inner))) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:1,value:10})")
        with connect(path) as writer, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(db.execute, "CALL app.outer(1)")
            try:
                assert entered.wait(30)
                with writer.begin("write") as tx:
                    tx.execute("MATCH (n:T) SET n.value=11")
            finally:
                resume.set()
            assert future.result(timeout=30).rows == ((10,),)


def test_child_cannot_reenter_parent_capability_and_hide_failure():
    captured = []
    def outer(writer, n):
        captured.append(writer)
        writer.execute("CREATE (:T {id:1,value:1})")
        writer.query("CALL app.inner(1)")
    def inner(writer, n):
        try:
            captured[0].query("RETURN 1")
        except GrafxTransactionStateError:
            pass
    with connect(":memory:", extensions=registry(procedure("app.outer", outer, mode="write", columns=()),
                                                 procedure("app.inner", inner, mode="write", columns=()))) as db:
        seed(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxTransactionStateError):
                tx.execute("CALL app.outer(1)")
            assert tx.execute("MATCH (n:T) RETURN count(n)").rows == ((0,),)


def test_deep_delete_invalidates_outer_alias_and_restores_on_failure():
    def outer(writer, n):
        writer.query("CALL app.inner($n)", {"n":n})
    def inner(writer, n):
        writer.execute("MATCH (n:T {id:$n}) DELETE n", {"n":n})
    with connect(":memory:", extensions=registry(procedure("app.outer", outer, mode="write", columns=()),
                                                 procedure("app.inner", inner, mode="write", columns=()))) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:1,value:1})")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("MATCH (n:T) CALL app.outer(n.id) RETURN n.value")
            assert failure.value.details["reason"] == "deleted_entity_access"
            assert tx.execute("MATCH (n:T) RETURN n.value").rows == ((1,),)


@pytest.mark.parametrize("query", ("RETURN rand()", "RETURN datetime.realtime()"))
def test_deterministic_procedure_refuses_volatile_native_functions(query):
    def callback(reader, n):
        return reader.query(query).rows
    with connect(":memory:", extensions=registry(procedure("app.d", callback, deterministic=True))) as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute("CALL app.d(1)")
        assert failure.value.details["reason"] == "nondeterministic_query"


def test_registry_replaces_forged_ast_determinism_and_default_is_volatile():
    from okto_grafx.domain.query.parser import parse
    from okto_grafx.domain.query.procedure_resolution import resolve_procedure_calls
    from okto_grafx.domain.query.effects import is_deterministic
    callback = procedure("app.d", lambda reader,n: ((n,),))
    parsed = parse("CALL app.d(1) YIELD value RETURN value")
    forged = replace(parsed, clause_pipeline=(replace(parsed.clause_pipeline[0], deterministic=True),))
    assert not is_deterministic(resolve_procedure_calls(forged, {callback.name:callback}))
    assert is_deterministic(resolve_procedure_calls(parsed, {callback.name:replace(callback, deterministic=True)}))


def test_recursive_read_call_inside_exists_uses_native_scope():
    def recurse(reader, n):
        if n == 0:
            return ((1,),)
        result = reader.query("RETURN EXISTS { CALL app.r($n) YIELD value RETURN value }", {"n":n-1})
        assert result.rows == ((True,),)
        return ((1,),)
    with connect(":memory:", extensions=registry(procedure("app.r", recurse))) as db:
        assert db.execute("CALL app.r(3)").rows == ((1,),)


def test_native_exists_still_rejects_writing_call_before_child_callback():
    calls = []
    def outer(writer, n):
        return writer.query("RETURN EXISTS { CALL app.inner(1) }").rows
    def inner(writer, n):
        calls.append(n)
        writer.execute("CREATE (:T {id:1,value:1})")
    with connect(":memory:", extensions=registry(procedure("app.outer", outer, mode="write"),
                                                 procedure("app.inner", inner, mode="write", columns=()))) as db:
        seed(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.outer(1)")
        assert not calls


@pytest.mark.parametrize("option", ("max_traversal_expansions", "max_traversal_paths"))
def test_recursive_traversals_charge_the_root_statement_budget(option):
    def recurse(reader, n):
        reader.query("MATCH p=(a:T)-[:R*1..1]->(b:T) RETURN length(p)")
        return ((1,),) if n == 0 else reader.query("CALL app.r($n)", {"n":n-1}).rows
    with connect(":memory:", extensions=registry(procedure("app.r", recurse)), **{option:2}) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE REL TABLE R (FROM T TO T)")
            tx.execute("CREATE (a:T {id:1,value:1}), (b:T {id:2,value:2}), (a)-[:R]->(b)")
        with pytest.raises(GrafxQueryBudgetExceeded) as failure:
            db.execute("CALL app.r(2)")
        assert failure.value.details["field"] == option


def test_default_volatile_callback_keeps_distinct_exists_evaluations():
    calls = []
    def toggle(reader, n):
        calls.append(n)
        return ((len(calls),),)
    expression = "EXISTS { CALL app.toggle(0) YIELD value WHERE value=1 RETURN value }"
    with connect(":memory:", extensions=registry(procedure("app.toggle", toggle))) as db:
        assert db.execute(f"RETURN {expression} AS first, {expression} AS second").rows == ((True,False),)
        assert len(calls) == 2
