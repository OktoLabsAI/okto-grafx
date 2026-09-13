"""SQ-13: UNION branches share rollback but execute private effects in order."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.fixture(params=[None, 8192], ids=["memory", "spill"])
def db(request, tmp_path):
    with connect(tmp_path / "db", query_memory_budget_bytes=request.param) as database:
        yield database


@pytest.mark.parametrize("query,expected,count", [
    ("CREATE(n:N {v:1}) RETURN n.v AS x UNION ALL MATCH(n:N) RETURN count(*) AS x", ((1,), (1,)), 1),
    ("MATCH(n:N) RETURN count(*) AS x UNION ALL CREATE(n:N) RETURN 1 AS x", ((0,), (1,)), 1),
    ("CREATE(n:N {v:1}) RETURN 1 AS x UNION CREATE(n:N {v:2}) RETURN 1 AS x", ((1,),), 2),
    ("CREATE(n:N) RETURN 1 AS x UNION ALL CREATE(n:N) RETURN 2 AS x UNION ALL MATCH(n:N) RETURN count(*) AS x", ((1,), (2,), (2,)), 2),
    ("CREATE(a:N) UNION ALL CREATE(b:N)", (), 2),
    ("CALL(){ CREATE(a:N) UNION CREATE(b:N) } RETURN 7", ((7,),), 2),
    ("UNWIND [1,2] AS i CALL(i){ CREATE(n:N {v:i}) RETURN i AS x UNION ALL MATCH(n:N) RETURN count(*) AS x } RETURN x", ((1,), (1,), (2,), (2,)), 2),
    ("CALL(){ CREATE(n:N) RETURN 1 AS x UNION ALL CREATE(n:N) RETURN 2 AS x } RETURN x LIMIT 0", (), 2),
    ("UNWIND [1,2] AS i CALL { WITH i CREATE(n:N {v:i}) UNION ALL WITH i CREATE(m:N {v:i+10}) } RETURN i", ((1,), (2,)), 4),
])
def test_branch_order_cardinality_and_private_visibility(db, query, expected, count):
    with db.begin("write") as tx:
        assert tx.execute(query).rows == expected
        assert tx.execute("MATCH(n:N) RETURN count(*)").rows == ((count,),)
    assert db.verify("all").findings == ()


def test_later_branch_read_phases_are_not_prepared_before_prior_writes(db):
    with db.begin("write") as tx:
        result = tx.execute("CREATE(a:N {v:1}) RETURN a.v AS x UNION ALL "
                            "MATCH(a:N) SET a.v=2 WITH a MATCH(b:N) RETURN b.v AS x")
        assert result.rows == ((1,), (2,))
        assert tx.execute("MATCH(n:N) RETURN n.v").rows == ((2,),)


@pytest.mark.parametrize("branches", [
    "CREATE(n:N {v:1}) RETURN n AS x UNION MATCH(n:N) RETURN n AS x",
    "CREATE(n:N {v:1}) WITH n AS m RETURN m AS x UNION MATCH(n:N) RETURN n AS x",
    "CREATE(n:N {v:1}) WITH collect(n) AS xs RETURN xs AS x UNION MATCH(n:N) RETURN collect(n) AS x",
    "CREATE(n:N {v:1}) RETURN {node:n} AS x UNION MATCH(n:N) RETURN {node:n} AS x",
])
def test_created_and_reread_native_values_have_one_distinct_identity(db, branches):
    with db.begin("write") as tx:
        assert tx.execute("CALL(){ " + branches + " } RETURN count(*)").rows == ((1,),)
        assert tx.execute("MATCH(n:N) RETURN count(*)").rows == ((1,),)


def test_lazy_schema_and_native_endpoints_cross_branches(db):
    with db.begin("write") as tx:
        result = tx.execute("CREATE(a {v:1}) RETURN 1 AS x UNION ALL "
                            "MATCH(a) CREATE(b:B {v:2}), (a)-[:R {v:3}]->(b) RETURN b.v AS x")
        assert result.rows == ((1,), (2,))
        assert tx.execute("MATCH(a)-[r:R]->(b) RETURN a.v,r.v,b.v").rows == ((1,3,2),)
    assert db.verify("all").findings == ()


@pytest.mark.parametrize("operator", ["UNION", "UNION ALL"])
def test_exported_nodes_keep_native_identity_for_downstream_writes(db, operator):
    with db.begin("write") as tx:
        result = tx.execute("CALL(){ CREATE(n:A {v:1}) RETURN n " + operator + " "
                            "CREATE(n:B {v:2}) RETURN n } SET n.v=n.v+10 RETURN n.v ORDER BY n.v")
        assert result.rows == ((11,), (12,))
        assert tx.execute("MATCH(n) RETURN n.v ORDER BY n.v").rows == ((11,), (12,))
    assert db.verify("all").findings == ()


@pytest.mark.parametrize("query", [
    "CREATE(n:N) RETURN 1 AS x UNION ALL CREATE(m:M {v:1/0}) RETURN 2 AS x",
    "CREATE(n:N) RETURN 1 AS x UNION ALL RETURN 1/0 AS x",
    "UNWIND [1,2] AS i CALL(i){ CREATE(n:N) RETURN i AS x UNION ALL RETURN 1/(2-i) AS x } RETURN x",
    "CREATE(n:N) UNION ALL CREATE(m:M {v:1/0})",
    "CALL(){ CREATE(n:N) RETURN 1 AS x UNION ALL RETURN 2 AS x } RETURN 1/(x-2)",
])
def test_late_branch_or_outer_failure_discards_whole_statement(db, query):
    with db.begin("write") as tx:
        tx.execute("CREATE(n:Prior {v:7})")
        with pytest.raises(GrafxError):
            tx.execute(query)
        assert tx.execute("MATCH(n) RETURN n.v").rows == ((7,),)
    assert {table.name for table in db.catalog.catalog.tables()} == {"Prior"}
    assert db.verify("all").findings == ()


@pytest.mark.parametrize("query", [
    "RETURN 1 AS x UNION ALL UNWIND [] AS i CREATE(n:N) RETURN 2 AS x",
    "CALL(){ RETURN 1 AS x UNION ALL CREATE(n:N) RETURN 2 AS x } RETURN x",
    "CREATE(n:N) UNION ALL CREATE(m:M)",
])
def test_read_transactions_and_cursors_refuse_before_effects(db, query):
    with db.begin("read") as tx, pytest.raises(GrafxError):
        tx.execute(query)
    with pytest.raises(GrafxError):
        with db.query(query).cursor(batch_size=1) as cursor:
            cursor.fetchmany()
    assert db.catalog.catalog.tables() == ()


@pytest.mark.parametrize("query", [
    "CREATE(n:N) RETURN 1 AS x UNION ALL CREATE(m:M)",
    "CREATE(n:N) RETURN 1 AS x UNION ALL CREATE(m:M) RETURN 2 AS y",
])
def test_mixed_unit_or_mismatched_columns_refuse_atomically(db, query):
    with db.begin("write") as tx, pytest.raises(GrafxError):
        tx.execute(query)
    assert db.catalog.catalog.tables() == ()


def test_statement_budget_is_shared_across_branches(tmp_path):
    with connect(tmp_path / "db", max_statement_writes=2) as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("CREATE(a:N),(b:N) RETURN 1 AS x UNION ALL CREATE(c:N) RETURN 2 AS x")
            assert tx.execute("MATCH(n) RETURN count(*)").rows == ((0,),)
        assert db.catalog.catalog.tables() == ()


@pytest.mark.parametrize("target", ["engine", "public"])
def test_public_result_conversion_failure_rolls_back_root_union(db, monkeypatch, target):
    from okto_grafx.errors import GrafxConfigurationError
    from okto_grafx.engine import database, query_engine
    module = query_engine if target == "engine" else database
    name = "_projected" if target == "engine" else "_query_result_view"
    original = getattr(module, name)
    with db.begin("write") as tx:
        tx.execute("CREATE(n:Prior {v:7})")
        def fail(*args, **kwargs):
            raise RuntimeError("injected public detachment failure")
        with monkeypatch.context() as scoped:
            scoped.setattr(module, name, fail)
            with pytest.raises(GrafxConfigurationError) as failure:
                tx.execute("CREATE(n:N) RETURN 1 AS x UNION ALL CREATE(n:M) RETURN 2 AS x")
            assert isinstance(failure.value.__cause__, RuntimeError)
            assert "injected" in str(failure.value.__cause__)
        assert getattr(module, name) is original
        assert tx.execute("MATCH(n) RETURN n.v").rows == ((7,),)
    assert {table.name for table in db.catalog.catalog.tables()} == {"Prior"}


@pytest.mark.parametrize("budget", [4096, 16384])
def test_distinct_native_output_writes_after_actual_disk_spill(tmp_path, monkeypatch, budget):
    from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory
    opened = []
    original = LocalQuerySpillFactory.open
    def record(factory, budget):
        workspace = original(factory, budget)
        opened.append(workspace)
        return workspace
    monkeypatch.setattr(LocalQuerySpillFactory, "open", record)
    with connect(tmp_path / "db", query_memory_budget_bytes=budget) as db:
        with db.begin("write") as tx:
            result = tx.execute("CALL(){ UNWIND range(1,40) AS i CREATE(n:N {v:i}) RETURN n "
                                "UNION MATCH(n:N) RETURN n } SET n.v=n.v+1 RETURN count(*)")
            assert result.rows == ((40,),)
        assert opened and any(workspace._next_run > 0 for workspace in opened)
        assert all(workspace._closed for workspace in opened)
        assert db.execute("MATCH(n:N) RETURN n.v ORDER BY n.v").rows == tuple((i,) for i in range(2,42))
        assert db.verify("all").findings == ()


def test_insufficient_entity_spill_budget_refuses_and_rolls_back(tmp_path):
    from okto_grafx.errors import GrafxQueryBudgetExceeded
    with connect(tmp_path / "db", query_memory_budget_bytes=512) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:Prior {v:7})")
            with pytest.raises(GrafxQueryBudgetExceeded):
                tx.execute("CALL(){ UNWIND range(1,40) AS i CREATE(n:N {v:i}) RETURN n "
                           "UNION MATCH(n:N) RETURN n } SET n.v=n.v+1 RETURN count(*)")
            assert tx.execute("MATCH(n) RETURN n.v").rows == ((7,),)
        assert {table.name for table in db.catalog.catalog.tables()} == {"Prior"}
        assert db.verify("all").findings == ()


def test_relationship_group_schema_is_published_between_branches(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(a:A)-[:R]->(b:B) RETURN 1 AS x UNION ALL "
                   "CREATE(c:C)-[:R]->(d:D) RETURN 2 AS x UNION ALL "
                   "MATCH(a)-[r:R]->(b) RETURN 3 AS x")
        assert tx.execute("MATCH(a)-[r:R]->(b) RETURN count(*)").rows == ((2,),)
    assert db.verify("all").findings == ()


def test_merge_and_delete_recreate_observe_branch_effects(db):
    with db.begin("write") as tx:
        assert tx.execute("MERGE(n:N {v:1}) RETURN n AS x UNION ALL "
                          "MERGE(n:N {v:1}) RETURN n AS x").rows[0] == tx.execute("MATCH(n:N) RETURN n").rows[0]
        assert tx.execute("MATCH(n:N) DELETE n RETURN 0 AS x UNION ALL "
                          "MERGE(n:N {v:2}) RETURN n.v AS x").rows == ((0,), (2,))
        assert tx.execute("MATCH(n:N) RETURN n.v").rows == ((2,),)
    assert db.verify("all").findings == ()


def test_original_snapshots_and_occ_survive_updating_union(tmp_path):
    from okto_grafx.errors import GrafxWriteConflict
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:N {v:0})")
        with connect(path) as peer, db.begin("read") as old:
            first, second = db.begin("write"), peer.begin("write")
            try:
                for tx, value in ((first, 1), (second, 2)):
                    tx.execute("MATCH(n:N) SET n.v=$v RETURN n.v AS x UNION ALL "
                               "MATCH(n:N) RETURN n.v AS x", {"v":value})
                first.commit()
                with pytest.raises(GrafxWriteConflict):
                    second.commit()
            finally:
                if first.active:
                    first.rollback()
                if second.active:
                    second.rollback()
            assert old.execute("MATCH(n:N) RETURN n.v").rows == ((0,),)
        assert db.execute("MATCH(n:N) RETURN n.v").rows == ((1,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interruption_after_private_publication_rolls_back_all_branches(db, monkeypatch, interrupt):
    from okto_grafx.engine.query_engine import _Context
    original = _Context.publish_phase
    published = []
    def stop(context):
        original(context)
        if context.published_refs:
            published.append(True)
            raise interrupt("injected after private publication")
    with db.begin("write") as tx:
        tx.execute("CREATE(n:Prior {v:7})")
        with monkeypatch.context() as scoped:
            scoped.setattr(_Context, "publish_phase", stop)
            with pytest.raises(interrupt, match="injected"):
                tx.execute("CREATE(n:N) RETURN 1 AS x UNION ALL CREATE(m:M) RETURN 2 AS x")
        assert published
        assert tx.execute("MATCH(n) RETURN n.v").rows == ((7,),)
    assert {table.name for table in db.catalog.catalog.tables()} == {"Prior"}


@pytest.mark.parametrize("option", ["cancel", "deadline"])
def test_public_read_controls_remain_refused_for_write_transactions(db, option):
    from okto_grafx import CancellationToken
    from okto_grafx.errors import GrafxUnsupportedOperation
    options = {"cancellation": CancellationToken()} if option == "cancel" else {"timeout_seconds":1}
    with db.begin("write") as tx:
        with pytest.raises(GrafxUnsupportedOperation):
            tx.execute("CREATE(n:N) UNION ALL CREATE(m:M)", **options)
        assert tx.execute("MATCH(n) RETURN count(*)").rows == ((0,),)
    assert db.catalog.catalog.tables() == ()
