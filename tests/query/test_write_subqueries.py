"""SQ-01..11: native CALL effects share the outer statement and snapshot."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxWriteConflict


@pytest.mark.parametrize("unit", [False, True])
def test_correlated_creates_commit_reopen_and_unit_cardinality(tmp_path, unit):
    path = tmp_path / "db"
    body = "CREATE(n {v:i})" + ("" if unit else " RETURN n.v AS value")
    query = "UNWIND [1,2,3] AS i CALL (i) { " + body + " } RETURN i" + ("" if unit else ",value")
    with connect(path) as db:
        with db.begin("write") as tx:
            assert tx.execute(query).rows == tuple((i,) if unit else (i,i) for i in (1,2,3))
        assert db.verify("all").findings == ()
    with connect(path) as db:
        assert db.execute("MATCH(n) RETURN n.v ORDER BY n.v").rows == ((1,), (2,), (3,))


@pytest.mark.parametrize("query,expected", [
    ("UNWIND [1,2] AS i CALL(i) { MATCH(n {v:99}) SET n.v=i } RETURN i", ((1,), (2,))),
    ("UNWIND [1,2] AS i CALL(i) { CREATE(n {v:i}) WITH n WHERE false RETURN n AS x } RETURN i", ()),
    ("UNWIND [] AS i CALL(i) { CREATE(n {v:i}) } RETURN i", ()),
    ("UNWIND [1,2] AS i CALL(i) { UNWIND [3,4] AS j CREATE(n {v:i+j}) RETURN n.v AS v } RETURN v", ((4,), (5,), (5,), (6,))),
])
def test_zero_and_multiple_inner_rows(tmp_path, query, expected):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            assert tx.execute(query).rows == expected
        wanted = 2 if "WHERE false" in query else 4 if "[3,4]" in query else 0
        assert db.execute("MATCH(n) RETURN count(n)").rows == ((wanted,),)
        assert db.verify("all").findings == ()


def test_invocations_see_previous_updates_and_downstream_sees_final_state(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:Counter {v:0})")
            result = tx.execute("UNWIND [1,2,3] AS i CALL() { MATCH(n:Counter) SET n.v=n.v+1 RETURN n.v AS v } "
                                "MATCH(m:Counter) RETURN i,v,m.v")
            assert result.rows == ((1,1,3), (2,2,3), (3,3,3))


def test_nested_units_do_not_duplicate_prepared_phases(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("UNWIND [1,2] AS i CALL(i) { CALL(i) { CREATE(n {v:i}) } } "
                                "CALL(i) { CREATE(n {v:i+10}) } RETURN i LIMIT 1")
            assert result.rows == ((1,),)
            assert tx.execute("MATCH(n) RETURN n.v ORDER BY n.v").rows == ((1,), (2,), (11,), (12,))
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("suffix", ["RETURN i LIMIT 0", "RETURN i LIMIT 1", "RETURN i"])
def test_outer_window_does_not_limit_writes(tmp_path, suffix):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("UNWIND [1,2,3] AS i CALL(i) { CREATE(n {v:i}) } " + suffix)
            assert tx.execute("MATCH(n) RETURN count(n)").rows == ((3,),)


@pytest.mark.parametrize("query", [
    "UNWIND [1,2] AS i CALL(i) { CREATE(n:New {v:i}) RETURN 1/(2-i) AS x } RETURN x",
    "UNWIND [1,2] AS i CALL(i) { CREATE(n:New {v:i}) } RETURN 1/(2-i)",
    "CALL() { CREATE(a:New)-[:NEW_EDGE]->(b:Other) WITH a RETURN 1/0 AS x } RETURN x",
    "CALL() { CALL() { CREATE(n:New) } RETURN 1/0 AS x } RETURN x",
])
def test_late_failure_restores_whole_statement_but_retains_prior_work(tmp_path, query):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:Prior {v:7})")
            with pytest.raises(GrafxError):
                tx.execute(query)
            assert tx.execute("MATCH(n) RETURN n.v").rows == ((7,),)
        assert {t.name for t in db.catalog.catalog.tables()} == {"Prior"}
        assert db.verify("all").findings == ()
    with connect(path) as db:
        assert db.execute("MATCH(n) RETURN n.v").rows == ((7,),)


@pytest.mark.parametrize("query", [
    "CALL() { CREATE(n) }",
    "CALL() { CALL() { CREATE(n) } }",
    "UNWIND [] AS i CALL() { CREATE(n) } RETURN i",
    "CALL() { CREATE(n) RETURN 1 AS x } RETURN x",
])
def test_read_transaction_and_cursor_refuse_nested_effects(tmp_path, query):
    with connect(tmp_path / "db") as db:
        with db.begin("read") as tx:
            with pytest.raises(GrafxError):
                tx.execute(query)
        with pytest.raises(GrafxError):
            with db.query(query).cursor() as cursor:
                list(cursor)
        assert db.catalog.catalog.tables() == ()


def test_unit_standalone_and_exported_entity_authority(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            assert tx.execute("CALL() { CREATE(n {v:1}) }").columns == ()
            result = tx.execute("CALL() { CREATE(a:A {v:2}) RETURN a } "
                                "CALL(a) { CREATE(b:B {v:3}), (a)-[:R]->(b) } "
                                "MATCH(a)-[:R]->(b) RETURN a.v,b.v")
            assert result.rows == ((2,3),)
        assert db.verify("all").findings == ()


def test_conflicting_writer_and_snapshot_isolation(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n {v:0})")
        with connect(path) as peer, peer.begin("read") as old:
            writer = db.begin("write")
            try:
                writer.execute("CALL() { MATCH(n) SET n.v=1 }")
                assert old.execute("MATCH(n) RETURN n.v").rows == ((0,),)
                with peer.begin("write") as winner:
                    winner.execute("CALL() { MATCH(n) SET n.v=2 }")
                with pytest.raises(GrafxWriteConflict):
                    writer.commit()
            finally:
                if writer.active:
                    writer.rollback()
            assert old.execute("MATCH(n) RETURN n.v").rows == ((0,),)
        assert db.execute("MATCH(n) RETURN n.v").rows == ((2,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("target", ["engine", "public"])
def test_final_conversion_failure_rolls_back_all_calls(tmp_path, monkeypatch, target):
    import okto_grafx.engine.query_engine as engine
    import okto_grafx.engine.database as public
    from okto_grafx.errors import GrafxPlanError
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:Prior {v:7})")
            with monkeypatch.context() as patch:
                def refuse(*args, **kwargs):
                    raise GrafxPlanError("forced final conversion failure")
                patch.setattr(engine if target == "engine" else public,
                              "_projected" if target == "engine" else "_query_result_view", refuse)
                with pytest.raises(GrafxPlanError, match="forced final"):
                    tx.execute("UNWIND [1,2] AS i CALL(i) { CREATE(n:New {v:i}) RETURN n } RETURN n")
            assert tx.execute("MATCH(n) RETURN n.v").rows == ((7,),)
        assert {t.name for t in db.catalog.catalog.tables()} == {"Prior"}
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("options", [{"max_statement_writes": 2}, {"max_intermediate_rows": 2}])
def test_budgets_are_shared_across_invocations(tmp_path, options):
    with connect(tmp_path / "db", **options) as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("UNWIND [1,2,3] AS i CALL(i) { CREATE(n:New {v:i}) }")
            assert tx.execute("MATCH(n) RETURN count(n)").rows == ((0,),)
        assert db.catalog.catalog.tables() == ()


@pytest.mark.parametrize("query", [
    "WITH 1 AS i CALL() { RETURN i AS v } RETURN v",
    "WITH 1 AS i CALL(i) { RETURN 2 AS i } RETURN i",
    "WITH 1 AS i CALL(i,i) { CREATE(n {v:i}) } RETURN i",
])
def test_scope_refusals_precede_effects(tmp_path, query):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute(query)
        assert db.catalog.catalog.tables() == ()


def test_imported_node_updates_refresh_outer_values_and_inner_reads(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE(n:Counter {v:0}) WITH n UNWIND [1,2] AS i "
                                "CALL(n) { SET n.v=n.v+1 WITH n MATCH(m:Counter) RETURN m.v AS v } "
                                "RETURN i,v,n.v")
            assert result.rows == ((1,1,2), (2,2,2))
        assert db.verify("all").findings == ()


def test_private_merge_results_and_delete_recreate(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("UNWIND [1,1,2] AS i CALL(i) { MERGE(n:Item {id:i}) RETURN n } RETURN n")
            one, same, two = (row[0] for row in result.rows)
            assert one == same and one != two
            tx.execute("CALL() { MATCH(n:Item) DELETE n }")
            recreated = tx.execute("CALL() { CREATE(n:Item {id:1}) RETURN n } RETURN n").rows[0][0]
            assert recreated != one
        assert db.execute("MATCH(n) RETURN n.id").rows == ((1,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("budget", [None, 8192])
def test_nested_returning_calls_preserve_grouping_and_import_aliases(tmp_path, budget):
    with connect(tmp_path / "db", query_memory_budget_bytes=budget) as db:
        with db.begin("write") as tx:
            result = tx.execute("WITH 99 AS i WITH [1,2] AS xs UNWIND xs AS i "
                                "CALL(i) { CALL(i) { CREATE(n {v:i}) RETURN n } RETURN n.v AS v } "
                                "RETURN collect(v) AS vs")
            assert result.rows == (((1,2),),)
        assert db.verify("all").findings == ()


def test_import_refresh_is_bounded_and_preserves_scalar_dag_identity():
    from okto_grafx.engine.query_engine import _current_entity_value
    shared = [1,2]
    for _ in range(32):
        shared = [shared, shared]
    assert _current_entity_value(None, None, shared) is shared
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(GrafxError, match="acyclic"):
        _current_entity_value(None, None, cyclic)


def test_imported_collections_and_paths_refresh_native_entities(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a:N {v:0})-[:R]->(b:N {v:0})")
            result = tx.execute("MATCH p=(a:N)-[:R]->(b:N) WITH p,[a] AS xs "
                                "UNWIND [1,2] AS i CALL(xs) { UNWIND xs AS n SET n.v=n.v+1 } "
                                "RETURN i,nodes(p)[0].v,xs[0].v")
            assert result.rows == ((1,2,2), (2,2,2))


def test_pending_token_requires_the_exact_live_version_not_an_object_id():
    from dataclasses import replace
    from okto_grafx.domain.model.record import HeapVersion
    from okto_grafx.engine.query_engine import _Context
    context = _Context(engine=None, txn=None, parameters={}, analysis=None, statistics={},
                       coalesce_types={}, case_types={}, timestamp_values={})
    old = HeapVersion(0, 0, 0, (), None, 1, False, 1)
    fresh = replace(old)
    context.register_pending_token(old, 7)
    assert context.pending_tokens[id(old)][0] is old  # strong identity witness
    assert context.pending_token(old) == 7
    context.pending_tokens[id(fresh)] = (old, 7)  # deterministic stale-ID fault
    assert context.pending_token(fresh) is None
    context.register_pending_token(fresh, 8)
    assert context.pending_token(fresh) == 8
    assert context.pending_token(old) == 7
