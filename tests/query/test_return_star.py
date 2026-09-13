"""RETURN wildcard expands visible lexical bindings, never heap fields or hidden IDs."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxPlanError, GrafxTransactionStateError
from okto_grafx.domain.query import analyze, parse
from okto_grafx.domain.query.limits import MAX_PROJECTION_ITEMS
from tools.tck_errors import compile_error


@pytest.mark.parametrize("query,columns,rows", [
    ("WITH 2 AS z,1 AS a RETURN *,3 AS extra", ("a","z","extra"), ((1,2,3),)),
    ("WITH 1 AS old,2 AS n WITH n+1 AS n RETURN *", ("n",), ((3,),)),
    ("WITH [x IN [1,2] | x+1] AS xs RETURN *", ("xs",), (((2,3),),)),
    ("UNWIND [2,1,2] AS x RETURN DISTINCT * ORDER BY x SKIP 1 LIMIT 1", ("x",), ((2,),)),
    ("UNWIND [] AS n RETURN *", ("n",), ()),
    ("WITH 3 AS n RETURN *,count(*) AS c", ("n","c"), ((3,1),)),
    ("CALL() { WITH 1 AS n RETURN * } RETURN *", ("n",), ((1,),)),
    ("CALL() { WITH 1 AS n RETURN * UNION WITH 2 AS n RETURN * } RETURN n", ("n",), ((1,),(2,))),
    ("WITH 1 AS n RETURN * UNION RETURN 2 AS n", ("n",), ((1,),(2,))),
    ("RETURN 2 AS n UNION WITH 1 AS n RETURN *", ("n",), ((2,),(1,))),
    ("WITH 1 AS n CALL(n) { RETURN n+1 AS x } RETURN *", ("n","x"), ((1,2),)),
    ("WITH 1 AS n CALL(n) { CALL(n) { RETURN n+1 AS x } RETURN x AS y } RETURN *", ("n","y"), ((1,2),)),
    ("WITH 1 AS `z z`,2 AS `a a` RETURN *", ("a a","z z"), ((2,1),)),
])
def test_scalar_scopes_aggregation_union_and_subqueries(tmp_path, query, columns, rows):
    parsed = parse(query)
    assert "*" in parsed.describe()
    with connect(tmp_path / "db") as db:
        db.explain(query)
        result = db.execute(query)
        assert result.columns == columns and result.rows == rows
        with db.query(query).cursor(batch_size=1) as cursor:
            assert tuple(cursor) == rows


@pytest.mark.parametrize("query", ["RETURN *", "MATCH() RETURN *", "RETURN *, 1 AS x"])
def test_no_visible_bindings_is_a_source_proven_compile_error(query):
    with pytest.raises(GrafxPlanError) as failure:
        analyze(parse(query))
    observed = compile_error(failure.value)
    assert (observed.type, observed.phase, observed.detail) == ("SyntaxError", "compile time", "NoVariablesInScope")


@pytest.mark.parametrize("query", [
    "WITH 1 AS n RETURN *,n",
    "WITH 1 AS n RETURN *,2 AS n",
    "WITH 1 AS n RETURN * UNION WITH 1 AS m RETURN *",
    "WITH 1 AS n CALL(n) { RETURN * } RETURN n",
    "WITH 1 AS n CALL() { RETURN * } RETURN n",
])
def test_conflicts_and_scope_leaks_are_refused(tmp_path, query):
    with connect(tmp_path / "db") as db:
        with pytest.raises(GrafxError):
            db.execute(query)


def test_entity_paths_optional_snapshot_and_writes_use_native_identities(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE(a:A {v:1})-[r:R {v:2}]->(b:B {v:3}) RETURN *")
            assert result.columns == ("a","b","r")
        query = "MATCH p=(a:A)-[r:R]->(b:B) RETURN *"
        result = db.execute(query)
        assert result.columns == ("a","b","p","r")
        a,b,p,r = result.rows[0]
        explicit = db.execute("MATCH p=(a:A)-[r:R]->(b:B) RETURN a,b,p,r")
        assert result == explicit
        with db.query(query).cursor(batch_size=1) as cursor:
            with db.begin("write") as tx:
                tx.execute("MATCH(n:A) SET n.v=5")
            assert cursor.fetchone() == (a,b,p,r)
        nulls = db.execute("OPTIONAL MATCH p=(a:Missing)-[r:R]->(b) RETURN *")
        assert nulls.columns == ("a","b","p","r") and nulls.rows == ((None,None,None,None),)
        assert db.verify("all").findings == ()


def test_projection_expansion_is_bounded_and_ast_flags_are_exact():
    names = ",".join(f"{i} AS n{i}" for i in range(MAX_PROJECTION_ITEMS))
    query = parse(f"WITH {names} RETURN *,1 AS extra")
    with pytest.raises(GrafxPlanError) as failure:
        analyze(query)
    assert failure.value.details["field"] == "items"
    query = parse("WITH 1 AS n RETURN *")
    with pytest.raises(GrafxPlanError) as failure:
        analyze(replace(query, return_clause=replace(query.return_clause, include_existing=1)))
    assert failure.value.details["reason"] == "invalid_ast_structure"


def test_write_read_doors_and_late_failure_preserve_statement_atomicity(tmp_path):
    with connect(tmp_path / "db") as db:
        with pytest.raises(GrafxTransactionStateError):
            db.execute("CREATE(a:A) RETURN *")
        with db.begin("write") as tx:
            tx.execute("CREATE(:Earlier)")
            with pytest.raises(GrafxError):
                tx.execute("CREATE(a:A)-[:R]->(b:B) RETURN *,1/0 AS bad")
            tx.execute("CREATE(:After)")
        assert {t.name for t in db.catalog.catalog.tables()} == {"Earlier", "After"}
        assert db.verify("all").findings == ()
