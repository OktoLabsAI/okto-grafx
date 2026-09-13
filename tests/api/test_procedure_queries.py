"""Restricted same-transaction procedure queries own results, budgets and authority."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from okto_grafx import connect, NodeValue, CancellationToken
from okto_grafx.errors import GrafxPlanError, GrafxQueryBudgetExceeded, GrafxTransactionStateError, GrafxConfigurationError, GrafxQueryCancelled
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure, ProcedureReader, ProcedureResult


def registry(callback, *, mode="read", columns=(("value","INT64"),), granted=True, **options):
    """Create only explicitly granted graph access for the callback under test."""
    return ExtensionRegistry(trusted=True, procedures=(TabularProcedure(
        "app.graph", (), columns, callback, mode=mode, graph_read=mode == "read",
        required_permissions=frozenset({"graph"}), **options,
    ),), procedure_permissions=frozenset({"graph"}) if granted else frozenset())


def seed(db):
    """Use declared schema to isolate result authority from future DDL capabilities."""
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE T (id INT64, value INT64, PRIMARY KEY(id))")
        tx.execute("CREATE (:T {id:1,value:10})")


def test_reader_native_query_results_and_aggregate():
    seen = []
    def callback(reader):
        assert type(reader) is ProcedureReader
        result = reader.query("MATCH (n:T) RETURN sum(n.value) AS total")
        assert type(result) is ProcedureResult and result.columns == ("total",)
        assert not hasattr(result, "plan") and not hasattr(reader, "execute")
        seen.append(reader)
        return result.rows
    with connect(":memory:", extensions=registry(callback)) as db:
        seed(db)
        assert db.execute("CALL app.graph()").rows == ((10,),)
        with db.query("CALL app.graph()").cursor(batch_size=1) as cursor:
            assert tuple(cursor) == ((10,),)
    for reader in seen:
        with pytest.raises(GrafxTransactionStateError):
            reader.query("RETURN 1")


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_writer_returning_query_new_entity_and_cold_reopen(tmp_path, codec):
    retained = []
    def callback(writer):
        result = writer.query("CREATE (n:T {id:2,value:20}) RETURN n")
        node = result.rows[0][0]
        assert type(node) is NodeValue and node.provenance.pending
        retained.append(writer)
        return ((node,),)
    path = tmp_path / "graph"
    with connect(path, codec=codec, extensions=registry(callback, mode="write", columns=(("node","NODE"),))) as db:
        seed(db)
        with db.begin("write") as tx:
            assert tx.execute("CALL app.graph() YIELD node SET node.value=21 RETURN node.id,node.value").rows == ((2,21),)
        db.verify()
    with pytest.raises(GrafxTransactionStateError):
        retained[0].query("RETURN 1")
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:T) RETURN n.value ORDER BY n.id").rows == ((10,), (21,))
        db.verify()


def test_reader_query_entity_is_valid_native_output_and_sees_prior_private_writes():
    def callback(reader):
        return reader.query("MATCH (n:T {id:2}) RETURN n").rows
    with connect(":memory:", extensions=registry(callback, columns=(("node","NODE"),))) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:2,value:20})")
            assert tx.execute("CALL app.graph() YIELD node SET node.value=22 RETURN node.value").rows == ((22,),)


@pytest.mark.parametrize("query", ("CREATE (:T {id:2,value:20})", "MATCH (n:T) WHERE false SET n.value=9 RETURN n",
                                    "RETURN 1 AS value UNION CREATE (:T {id:2,value:20}) RETURN 2 AS value"))
def test_reader_refuses_writes_before_effects_and_caught_failure_stays_poisoned(query):
    def callback(reader):
        try:
            reader.query(query)
        except GrafxTransactionStateError:
            pass
        return ((1,),)
    with connect(":memory:", extensions=registry(callback)) as db:
        seed(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxTransactionStateError):
                tx.execute("CALL app.graph() YIELD value RETURN value LIMIT 1")
            assert tx.execute("MATCH (n:T) RETURN n.value").rows == ((10,),)


@pytest.mark.parametrize("query", ("CREATE NODE TABLE Other (id INT64, PRIMARY KEY(id))",))
def test_ddl_remains_explicitly_refused(query):
    def callback(writer):
        writer.query(query)
        return ((1,),)
    with connect(":memory:", extensions=registry(callback, mode="write")) as db:
        seed(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.graph()")
        assert len(db.catalog.catalog.tables()) == 1


@pytest.mark.parametrize("budget,query", (({"max_query_rows":1}, "UNWIND [1,2] AS x RETURN x"),
                                         ({"max_query_bytes":1}, "RETURN 1"),
                                         ({"max_value_bytes":2}, "RETURN 'long'")))
def test_query_result_budgets_rollback_all_callback_effects(budget, query):
    def callback(writer):
        writer.execute("CREATE (:T {id:2,value:20})")
        return writer.query(query).rows
    with connect(":memory:", extensions=registry(callback, mode="write", **budget)) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:3,value:30})")
            with pytest.raises(GrafxQueryBudgetExceeded):
                tx.execute("CALL app.graph()")
            assert tx.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))


@pytest.mark.parametrize("limits", ({"max_query_statements":1}, {"max_query_rows":1}, {"max_query_bytes":9}))
def test_query_budgets_span_input_row_invocations(limits):
    def callback(reader):
        return reader.query("RETURN 1").rows
    with connect(":memory:", extensions=registry(callback, **limits)) as db:
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("UNWIND [1,2] AS x CALL app.graph() YIELD value RETURN value")


def test_returning_write_queries_and_execute_share_write_budget():
    def callback(writer):
        writer.execute("CREATE (:T {id:2,value:20})")
        return writer.query("CREATE (:T {id:3,value:30}) RETURN 3").rows
    with connect(":memory:", extensions=registry(callback, mode="write", max_write_statements=1)) as db:
        seed(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxQueryBudgetExceeded):
                tx.execute("CALL app.graph()")
        assert db.execute("MATCH (n:T) RETURN count(n)").rows == ((1,),)


def test_reader_permission_and_foreign_thread_refusal():
    called = []
    def callback(reader):
        called.append(True)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reader.query, "RETURN 1")
            with pytest.raises(GrafxTransactionStateError):
                future.result(timeout=10)
        return reader.query("RETURN 2").rows
    with connect(":memory:", extensions=registry(callback, granted=False)) as db:
        with pytest.raises(GrafxPlanError):
            db.execute("CALL app.graph()")
        assert not called
    with connect(":memory:", extensions=registry(callback)) as db:
        assert db.execute("CALL app.graph()").rows == ((2,),)


@pytest.mark.parametrize("options", ({"graph_read":1}, {"graph_read":True}, {"max_query_rows":0},
                                      {"max_query_bytes":True}, {"max_query_statements":1025}))
def test_invalid_read_authority_and_budget_descriptors_are_refused(options):
    with pytest.raises(GrafxConfigurationError):
        TabularProcedure("app.invalid", (), (), lambda: None, **options)


def test_unit_reader_cannot_hide_poison_behind_downstream_limit():
    def callback(reader):
        try:
            reader.query("CREATE (:T {id:2,value:20})")
        except GrafxTransactionStateError:
            pass
    with connect(":memory:", extensions=registry(callback, columns=())) as db:
        seed(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxTransactionStateError):
                tx.execute("CALL app.graph() RETURN 1 LIMIT 1")
            assert tx.execute("MATCH (n:T) RETURN n.value").rows == ((10,),)


def test_late_output_failure_rolls_back_returning_child_query():
    def callback(writer):
        yield from writer.query("CREATE (:T {id:2,value:20}) RETURN 2").rows
        yield ("wrong",)
    with connect(":memory:", extensions=registry(callback, mode="write")) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:3,value:30})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.graph() YIELD value RETURN value LIMIT 1")
            assert tx.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))


def test_query_snapshots_and_independent_writer_survive_callback_wait(tmp_path):
    entered, resume = Event(), Event()
    def callback(reader):
        first = reader.query("MATCH (n:T) RETURN n.value").rows
        entered.set()
        assert resume.wait(30)
        assert reader.query("MATCH (n:T) RETURN n.value").rows == first
        return first
    path = tmp_path / "graph"
    with connect(path, extensions=registry(callback)) as db:
        seed(db)
        with connect(path) as writer, connect(path) as other, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(db.execute, "CALL app.graph()")
            try:
                assert entered.wait(30)
                with writer.begin("write") as tx:
                    tx.execute("MATCH (n:T) SET n.value=11")
                assert other.execute("MATCH (n:T) RETURN n.value").rows == ((11,),)
            finally:
                resume.set()
            assert future.result(timeout=30).rows == ((10,),)


def test_read_cursor_close_revokes_query_capability_and_closes_callback():
    retained, closed = [], []
    def callback(reader):
        retained.append(reader)
        try:
            yield from reader.query("UNWIND [1,2,3] AS x RETURN x").rows
        finally:
            closed.append(True)
    with connect(":memory:", extensions=registry(callback)) as db:
        with db.query("CALL app.graph()").cursor(batch_size=1) as cursor:
            assert next(iter(cursor)) == (1,)
        assert closed == [True]
        with pytest.raises(GrafxTransactionStateError):
            retained[0].query("RETURN 2")


def test_returning_write_union_and_created_path_components_are_native():
    def callback(writer):
        result = writer.query("CREATE (a:T {id:2,value:20}), (b:T {id:3,value:30}), "
                              "p=(a)-[:R]->(b) RETURN p")
        path = result.rows[0][0]
        assert len(path.nodes) == 2
        assert writer.query("RETURN 1 AS x UNION ALL RETURN 2 AS x").rows == ((1,), (2,))
        return ((path.nodes[0],),)
    with connect(":memory:", extensions=registry(callback, mode="write", columns=(("node","NODE"),))) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE REL TABLE R (FROM T TO T)")
            assert tx.execute("CALL app.graph() YIELD node SET node.value=25 RETURN node.id,node.value").rows == ((2,25),)


def test_query_result_map_copy_does_not_modify_stored_property():
    def callback(reader):
        result = reader.query("MATCH (n:T) RETURN {value:n.value,items:[1,2]}")
        result.rows[0][0]["items"].append(3)
        result.rows[0][0]["value"] = 99
        assert reader.query("MATCH (n:T) RETURN n.value").rows == ((10,),)
        return ((99,),)
    with connect(":memory:", extensions=registry(callback)) as db:
        seed(db)
        assert db.execute("CALL app.graph()").rows == ((99,),)


@pytest.mark.parametrize("door", ("execute", "cursor"))
def test_child_query_uses_outer_cancellation_and_expires(door):
    token, retained = CancellationToken(), []
    def callback(reader):
        retained.append(reader)
        assert reader.query("RETURN 1").rows == ((1,),)
        token.cancel()
        return reader.query("RETURN 2").rows
    with connect(":memory:", extensions=registry(callback)) as db:
        with pytest.raises(GrafxQueryCancelled):
            if door == "execute":
                db.execute("CALL app.graph()", cancellation=token)
            else:
                with db.query("CALL app.graph()").cursor(cancellation=token) as cursor:
                    tuple(cursor)
        with pytest.raises(GrafxTransactionStateError):
            retained[0].query("RETURN 3")


def test_child_query_preserves_outer_statement_clock():
    def callback(reader):
        return reader.query("RETURN datetime.statement()").rows
    with connect(":memory:", extensions=registry(callback, columns=(("value","DATETIME"),))) as db:
        assert db.execute("WITH datetime.statement() AS started CALL app.graph() YIELD value "
                          "RETURN started=value").rows == ((True,),)


def test_caught_reader_failure_during_early_stream_cleanup_rolls_back_outer_statement():
    def callback(reader):
        try:
            yield (1,)
            yield (2,)
        finally:
            try:
                reader.query("MATCH (n:T) DELETE n")
            except GrafxTransactionStateError:
                pass
    with connect(":memory:", extensions=registry(callback)) as db:
        seed(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxTransactionStateError):
                tx.execute("CREATE (n:T {id:2,value:20}) WITH n CALL app.graph() YIELD value RETURN value LIMIT 1")
            assert tx.execute("MATCH (n:T) RETURN n.id").rows == ((1,),)
