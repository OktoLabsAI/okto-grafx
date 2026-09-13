"""WITH * expands the incoming lexical scope, not table columns or stale bindings."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.limits import MAX_PROJECTION_ITEMS
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.scopes import lower_scopes
from okto_grafx.errors import GrafxParseError, GrafxPlanError


@pytest.mark.parametrize("query,expected", [
    ("WITH 1 AS x WITH * RETURN x", ((1,),)),
    ("WITH * RETURN 1", ((1,),)),
    ("WITH 1 AS x WITH *, x + 1 AS y RETURN x, y", ((1, 2),)),
    ("WITH 1 AS x WITH *, x + 1 AS y WHERE y = 2 RETURN x, y", ((1, 2),)),
    ("UNWIND [3,1,2] AS x WITH * ORDER BY x SKIP 1 LIMIT 1 RETURN x", ((2,),)),
    ("UNWIND [1,1,2] AS x WITH DISTINCT * RETURN x ORDER BY x", ((1,), (2,))),
    ("UNWIND [1,1,2] AS x WITH *, count(*) AS c RETURN x,c ORDER BY x", ((1, 2), (2, 1))),
    ("WITH 1 AS x WITH x + 1 AS x WITH * RETURN x", ((2,),)),
    ("WITH 1 AS x WITH 2 AS y WITH *, 3 AS x RETURN x,y", ((3, 2),)),
    ("WITH 1 AS x CALL (x) { WITH *, x + 1 AS y RETURN y } WITH * RETURN x,y", ((1, 2),)),
    ("WITH 1 AS x WITH * RETURN x UNION ALL WITH 2 AS x WITH * RETURN x", ((1,), (2,))),
    ("WITH 2 AS x WITH *, [x IN [1,3] | x+1] AS ys RETURN x,ys", ((2, (2, 4)),)),
])
def test_star_composes_with_existing_query_operators(query, expected):
    statement = parse(query)
    assert parse(statement.describe()) == statement
    with connect(":memory:") as db:
        assert db.execute(query).rows == expected
        # Cached execution must not carry prior rows or alter lexical identities.
        assert db.execute(query).rows == expected


@pytest.mark.parametrize("query", [
    "WITH 1 AS x WITH *, x RETURN x",
    "WITH 1 AS x WITH *, 2 AS x RETURN x",
    "WITH 1 AS x WITH *, 2 AS y, y + 1 AS z RETURN z",
    "WITH 1 AS x WITH 2 AS y WITH * RETURN x",
])
def test_star_does_not_hide_duplicates_same_stage_aliases_or_dropped_names(query):
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError):
            db.execute(query)


@pytest.mark.parametrize("query", ["WITH *, RETURN 1", "WITH *, * RETURN 1", "WITH 1 AS x, * RETURN x"])
def test_star_is_a_single_leading_projection_marker(query):
    with pytest.raises(GrafxParseError):
        parse(query)


def test_star_expansion_is_bounded_and_wrong_ast_flag_is_refused():
    source = ", ".join(f"{i} AS v{i}" for i in range(MAX_PROJECTION_ITEMS))
    with connect(":memory:") as db:
        assert db.execute(f"WITH {source} WITH * RETURN v0").rows == ((0,),)
        with pytest.raises(GrafxPlanError, match="after star expansion"):
            db.execute(f"WITH {source} WITH *, 1 AS extra RETURN extra")
    query = parse("WITH * RETURN 1")
    wrong = replace(query.with_clauses[0], include_existing=1)
    with pytest.raises(GrafxPlanError) as failure:
        analyze(replace(query, with_clauses=(wrong,)))
    assert failure.value.details["field"] == "ast"
    assert failure.value.details["reason"] == "invalid_ast_structure"


def test_lowering_expands_only_current_names_and_does_not_expose_internal_names():
    query = parse("WITH 1 AS x WITH x+1 AS x WITH *, 3 AS y RETURN x,y")
    analyze(query)
    lowered = lower_scopes(query)
    assert all(not clause.include_existing for clause in lowered.with_clauses)
    assert len(lowered.with_clauses[-1].items) == 2
    assert lowered.return_clause.column_names() == ("x", "y")
    assert len(analyze(lowered).bindings) == 2


def test_star_keeps_live_typed_entities_for_reads_writes_and_rollback(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (n:N {id:1,val:0}) WITH * SET n.val=2")
            assert tx.execute("MATCH (n:N) WITH *, n.val AS old SET n.val=old+1 RETURN n.val").rows == ((3,),)
            with pytest.raises(GrafxPlanError):
                tx.execute("MATCH (n:N) WITH * SET n.val=99 WITH * RETURN range(0,1,0)")
            assert tx.execute("MATCH (n:N) RETURN n.val").rows == ((3,),)
    with connect(path) as db:
        assert db.execute("MATCH (n:N) WITH * RETURN n.id,n.val").rows == ((1, 3),)


def test_star_cursor_early_close_releases_read_transaction():
    with connect(":memory:") as db:
        cursor = db.query("UNWIND range(1,100) AS n WITH *, n+1 AS next RETURN n,next").cursor(batch_size=1)
        assert cursor.fetchone() == (1, 2)
        cursor.close()
        assert db.transactions.open_transactions == 0


def test_star_carries_yield_aliases_and_only_explicit_subquery_imports():
    from okto_grafx.extensions import ExtensionRegistry, TabularProcedure

    procedure = TabularProcedure("app.next", ("INT64",), (("value", "INT64"),), lambda n: ((n + 1,),))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(procedure,))) as db:
        assert db.execute("WITH 1 AS x CALL app.next(x) YIELD value AS y WITH *, y+1 AS z RETURN x,y,z").rows == ((1, 2, 3),)
        with pytest.raises(GrafxPlanError):
            db.execute("WITH 1 AS x, 2 AS y CALL (x) { WITH * RETURN y AS z } RETURN z")


def test_star_keeps_optional_null_entity_bindings():
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N)")
            tx.execute("CREATE (:N {id:1})")
        assert db.execute("MATCH (n:N) OPTIONAL MATCH (n)-[r:R]->(m:N) "
                          "WITH * RETURN n.id,m.id").rows == ((1, None),)
