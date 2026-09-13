"""SQ-12: explicit globals and branch-local importing WITH are distinct contracts."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.fixture(params=[None, 8192], ids=["memory", "spill"])
def db(request, tmp_path):
    with connect(tmp_path / "db", query_memory_budget_bytes=request.param) as database:
        yield database


@pytest.mark.parametrize("query,expected", [
    ("WITH 2 AS x CALL(x) { WITH 3 AS y RETURN x+y AS z } RETURN z", ((5,),)),
    ("WITH 2 AS x CALL(*) { RETURN x+1 AS z } RETURN z", ((3,),)),
    ("WITH 2 AS x CALL { WITH x RETURN x+1 AS z } RETURN z", ((3,),)),
    ("WITH 99 AS gone WITH 2 AS x CALL(*) { RETURN x+1 AS z } RETURN z", ((3,),)),
    ("CALL(*) { RETURN 1 AS z } RETURN z", ((1,),)),
    ("WITH 2 AS x CALL { WITH * WITH x+1 AS v RETURN v AS z } RETURN z", ((3,),)),
    ("WITH 2 AS x CALL(x) { UNWIND [] AS i WITH count(*) AS c RETURN x,c } RETURN c", None),
    ("WITH 2 AS x CALL(x) { UNWIND [] AS i WITH count(*) AS c RETURN x+c AS z } RETURN z", ((2,),)),
    ("WITH 2 AS x CALL(x) { UNWIND [] AS i RETURN count(*)+x AS z } RETURN z", ((2,),)),
    ("WITH 2 AS x CALL(x) { UNWIND [1,1] AS i WITH DISTINCT i RETURN i+x AS z } RETURN z", ((3,),)),
    ("WITH 2 AS x CALL(x) { UNWIND [1,2] AS i WITH count(*)+x AS z RETURN z } RETURN z", ((4,),)),
    ("WITH 2 AS x CALL(x) { WITH 1 AS y WHERE x=2 CALL(*) { RETURN x+y AS z } RETURN z } RETURN z", ((3,),)),
    ("WITH 2 AS x CALL { WITH x WITH x+1 AS x RETURN x AS z } RETURN z", ((3,),)),
    ("WITH 2 AS x CALL { WITH 7 AS x RETURN x AS z } RETURN z", ((7,),)),
    ("WITH 1 AS x,2 AS y CALL { WITH x RETURN x AS z UNION ALL WITH y RETURN y AS z } RETURN z", ((1,), (2,))),
    ("WITH 2 AS x CALL(x) { WITH 1 AS z RETURN x+z AS v UNION ALL WITH 2 AS z RETURN x+z AS v } RETURN v", ((3,), (4,))),
    ("WITH 2 AS x CALL(x) { UNWIND [3,2,1] AS i RETURN i AS z ORDER BY z LIMIT x } RETURN z", ((1,), (2,))),
    ("WITH 1 AS x CALL(x) { UNWIND [1,2,3] AS i WITH i SKIP x LIMIT x+1 RETURN i AS z } RETURN z", ((2,), (3,))),
])
def test_scalar_scope_and_cardinality(db, query, expected):
    if expected is None:
        with pytest.raises(GrafxError):  # returning x collides with outer x
            db.execute(query)
    else:
        assert db.execute(query).rows == expected


@pytest.mark.parametrize("query", [
    "WITH 1 AS x CALL() { RETURN x AS z } RETURN z",
    "WITH 1 AS x CALL { RETURN x AS z } RETURN z",
    "WITH 1 AS x CALL { WITH x WITH 2 AS y RETURN x AS z } RETURN z",
    "WITH 1 AS x CALL(x) { WITH 2 AS x RETURN x AS z } RETURN z",
    "WITH 1 AS x CALL(*) { UNWIND [2] AS x RETURN x AS z } RETURN z",
    "WITH 1 AS x CALL { WITH x+1 AS y RETURN y AS z } RETURN z",
    "WITH 1 AS x CALL { WITH x AS y RETURN y AS z } RETURN z",
    "WITH 1 AS x CALL { WITH DISTINCT x RETURN x AS z } RETURN z",
    "WITH 1 AS x CALL { WITH x WHERE true RETURN x AS z } RETURN z",
    "WITH 1 AS x CALL { WITH x ORDER BY x RETURN x AS z } RETURN z",
    "WITH 1 AS x CALL { WITH x LIMIT 1 RETURN x AS z } RETURN z",
    "WITH 1 AS x CALL { WITH x RETURN x AS z UNION ALL RETURN x AS z } RETURN z",
    "WITH 1 AS x,2 AS y CALL { WITH x RETURN x AS z UNION ALL WITH y RETURN x AS z } RETURN z",
    "WITH 1 AS x CALL(*,x) { RETURN 1 AS z } RETURN z",
    "WITH 1 AS x CALL(x,x) { RETURN 1 AS z } RETURN z",
    "WITH 1 AS x CALL { WITH x UNWIND [1,2] AS i RETURN i AS z LIMIT x } RETURN z",
])
def test_refusals_do_not_create_schema(db, query):
    with db.begin("write") as tx:
        with pytest.raises(GrafxError):
            tx.execute(query)
    assert db.catalog.catalog.tables() == ()


def test_write_after_dropping_explicit_node_import_from_with(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(n:N {v:1})")
        assert tx.execute("MATCH(n:N) CALL(n) { WITH 2 AS v SET n.v=v } RETURN n.v").rows == ((2,),)
        assert tx.execute("MATCH(n:N) CALL { WITH n SET n.v=3 } RETURN n.v").rows == ((3,),)
    assert db.verify("all").findings == ()


def test_union_branch_local_name_must_not_be_correlated_to_other_branch_import(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(a:N {v:1}),(b:N {v:2})")
    result = db.execute("MATCH(a:N {v:1}) CALL { WITH a RETURN a.v AS z "
                        "UNION ALL MATCH(a:N) RETURN a.v AS z } RETURN z")
    assert sorted(result.rows) == [(1,), (1,), (2,)]


def test_leading_import_second_with_can_filter_and_aggregate(db):
    assert db.execute("UNWIND [1,2,3] AS x CALL { WITH x WITH x WHERE x>1 "
                      "RETURN x AS z } RETURN z").rows == ((2,), (3,))


def test_late_import_scope_call_failure_keeps_prior_statement(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(n:Prior {v:7})")
        with pytest.raises(GrafxError):
            tx.execute("UNWIND [1,2] AS i CALL(*) { WITH 1 AS unused CREATE(n:New {v:i}) "
                       "RETURN 1/(2-i) AS z } RETURN z")
        assert tx.execute("MATCH(n) RETURN n.v").rows == ((7,),)
    assert {table.name for table in db.catalog.catalog.tables()} == {"Prior"}


def test_global_entities_and_collections_survive_grouping_and_cursor_snapshots(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(a:N {v:1})-[:R]->(b:N {v:2})")
    query = ("MATCH p=(a:N)-[:R]->(b:N) WITH p,[a,b] AS xs CALL(*) { "
             "UNWIND [] AS none WITH count(*) AS total "
             "RETURN length(p) AS length,size(xs) AS size,total } RETURN length,size,total")
    assert db.execute(query).rows == ((1,2,0),)
    with db.query(query).cursor(batch_size=1) as cursor:
        assert cursor.fetchmany() == ((1,2,0),)
        assert cursor.fetchmany() == ()


def test_leading_branch_imports_are_native_entity_authority_not_shared_names(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(a:A {v:1}),(b:B {v:2})")
        result = tx.execute("MATCH(a:A),(b:B) CALL { WITH a RETURN a AS n "
                            "UNION ALL WITH b RETURN b AS n } SET n.v=n.v+10 RETURN n.v ORDER BY n.v")
        assert result.rows == ((11,), (12,))
    assert db.verify("all").findings == ()


def test_sequential_invocations_have_separate_global_row_windows(db):
    assert db.execute("UNWIND [1,2] AS take CALL(take) { UNWIND [1,2,3] AS j "
                      "RETURN j LIMIT take } RETURN take,j").rows == ((1,1), (2,1), (2,2))


def test_explicit_globals_do_not_let_nested_empty_imports_see_the_outer_scope(db):
    with pytest.raises(GrafxError):
        db.execute("WITH 1 AS x CALL(x) { WITH 2 AS y CALL() { RETURN x AS z } RETURN z } RETURN z")


def test_original_parameter_and_alias_visibility_is_not_global_by_accident(db):
    with pytest.raises(GrafxError):
        db.execute("WITH 1 AS x CALL(x) { WITH 2 AS y WITH 3 AS z RETURN y AS result } RETURN result")
    assert db.execute("WITH 1 AS x CALL(x) { WITH 2 AS y WITH 3 AS z "
                      "RETURN x+z AS result } RETURN result").rows == ((4,),)


def test_scope_metadata_is_not_unbound_authority():
    from dataclasses import replace
    from okto_grafx.domain.query.analysis import analyze
    from okto_grafx.domain.query.parser import parse
    query = parse("RETURN 1")
    for changed in (replace(query, scope_imports=("foreign",)),
                    replace(query, global_imports=("foreign",)),
                    replace(query, scope_imports=["foreign"])):
        with pytest.raises(GrafxError):
            analyze(changed)


@pytest.mark.parametrize("other", ["1", "{v:1}"])
def test_scalar_union_alternative_cannot_acquire_node_write_authority(db, other):
    with db.begin("write") as tx:
        tx.execute("CREATE(a:N {v:7})")
        with pytest.raises(GrafxError):
            tx.execute("MATCH(a:N) CALL { WITH a RETURN a AS n UNION ALL RETURN " + other +
                       " AS n } SET n.v=99 RETURN n")
        assert tx.execute("MATCH(a:N) RETURN a.v").rows == ((7,),)


def test_restore_operator_is_detached_in_the_public_plan(db):
    query = "WITH 2 AS x CALL(x) { WITH 3 AS y RETURN x+y AS z } RETURN z"
    result = db.execute(query)
    plan = result.plan
    restored = [node for node, _depth in plan.traverse() if node.label == "RestoreImports"]
    assert restored and all(node.details()["imports"] == ("x",) for node in restored)
    assert result.plan is plan  # one lazy materialization per public result
    sibling = db.execute(query).plan
    assert sibling is not plan
    assert all(left is not right for left, right in zip(plan.walk(), sibling.walk(), strict=True))
    object.__setattr__(restored[0], "names", ("tampered",))
    assert all(node.names == ("x",) for node in sibling.walk() if node.label == "RestoreImports")
    assert db.execute(query).rows == ((5,),)


@pytest.mark.parametrize("window,expected", [("", 25), ("SKIP 2 LIMIT 3", 5)])
def test_global_node_write_authority_survives_real_group_sort_spill(tmp_path, monkeypatch, window, expected):
    from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory
    opened = []
    original = LocalQuerySpillFactory.open
    def record(factory, budget):
        opened.append(True)
        return original(factory, budget)
    monkeypatch.setattr(LocalQuerySpillFactory, "open", record)
    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:N {v:0})")
            tx.execute("MATCH(n:N) CALL(n) { UNWIND range(1,25) AS i "
                       f"WITH DISTINCT i ORDER BY i {window} SET n.v=i }}")
        assert opened
        assert db.execute("MATCH(n:N) RETURN n.v").rows == ((expected,),)
        assert db.verify("all").findings == ()


def test_global_cursor_imports_retain_the_original_snapshot(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:N {v:7})")
        query = "MATCH(n:N) UNWIND [1,2] AS i CALL(n,i) { WITH 1 AS unused RETURN n.v AS v } RETURN i,v"
        with db.query(query).cursor(batch_size=1) as cursor:
            assert cursor.fetchmany() == ((1,7),)
            with connect(path) as peer, peer.begin("write") as tx:
                tx.execute("MATCH(n:N) SET n.v=9")
            assert cursor.fetchmany() == ((2,7),)
        assert db.execute("MATCH(n:N) RETURN n.v").rows == ((9,),)
