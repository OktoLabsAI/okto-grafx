"""List-local scopes, predicate truth tables and reduction through the public API."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.mark.parametrize(("expression", "expected"), [
    ("[x IN [1, 2, 3] WHERE x > 1 | x * 2]", (4, 6)),
    ("[x IN [1, 2, NULL] WHERE x > 1]", (2,)),
    ("[x IN NULL | x]", None),
    ("[x IN [] | 1 / 0]", ()),
    ("[x IN [1] WHERE false | 1 / 0]", ()),
    ("[x IN [{a: 1}, {a: 2}] | x.a]", (1, 2)),
    ("all(x IN [] WHERE x)", True),
    ("any(x IN [] WHERE x)", False),
    ("none(x IN [] WHERE x)", True),
    ("single(x IN [] WHERE x)", False),
    ("all(x IN [true, NULL] WHERE x)", None),
    ("all(x IN [NULL, false] WHERE x)", False),
    ("any(x IN [NULL, true] WHERE x)", True),
    ("none(x IN [NULL, true] WHERE x)", False),
    ("single(x IN [true, NULL] WHERE x)", None),
    ("single(x IN [true, true, NULL] WHERE x)", False),
    ("single(x IN [false, true] WHERE x)", True),
    ("reduce(total = 10, x IN [1, 2, 3] | total + x)", 16),
    ("reduce(total = 10, x IN [] | total + x)", 10),
    ("reduce(total = 10, x IN NULL | total + x)", None),
    ("[x IN [1, 2] | [x IN [3, 4] | x]]", ((3, 4), (3, 4))),
    ("[1, 2] + [3]", (1, 2, 3)),
    ("0 + [1, 2]", (0, 1, 2)),
    ("[true] + false", (True, False)),
    ("[] + {a: 1}", ({"a": 1},)),
    ("NULL + [1]", None),
    ("reduce(a = [], x IN [1, 2] | a + x)", (1, 2)),
])
def test_list_iteration(expression, expected):
    with connect(":memory:") as db:
        assert db.execute(f"RETURN {expression} AS value").rows == ((expected,),)
        with db.query(f"RETURN {expression} AS value").cursor(batch_size=1) as cursor:
            assert tuple(cursor) == ((expected,),)


def test_local_shadowing_preserves_outer_scopes_and_column_names():
    with connect(":memory:") as db:
        assert db.execute("WITH 99 AS x RETURN [x IN [1, 2] | x], x").rows == (((1, 2), 99),)
        result = db.execute("RETURN [x IN [1] | x]")
        assert result.columns == ("[x IN [1] | x]",)
        assert db.execute("WITH [1, 2] AS x WITH [x IN x | x + 1] AS x UNWIND x AS y RETURN y").rows == ((2,), (3,))
        assert db.execute("RETURN [x IN [1] | x] AS v UNION RETURN [y IN [1.0] | y] AS v").rows == (((1,),),)


@pytest.mark.parametrize("query", [
    "RETURN [x IN [] | missing]",
    "RETURN [x IN [1] | x], x",
    "RETURN [x IN 1 | x]",
    "RETURN any(x IN [] WHERE 1)",
    "RETURN reduce(x = 0, x IN [1] | x)",
    "RETURN [x IN [1] | sum(x)]",
    "UNWIND [] AS ignored RETURN [x IN $bad | x]",
    "UNWIND [] AS ignored RETURN any(x IN [] WHERE $bad)",
])
def test_invalid_list_scopes_and_prebind(query):
    with connect(":memory:") as db:
        with pytest.raises(GrafxError):
            db.execute(query, {"bad": 1})


def test_nested_iteration_budget_is_shared_and_allows_next_query():
    from okto_grafx.errors import GrafxQueryBudgetExceeded
    with connect(":memory:") as db:
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("RETURN [x IN range(1, 400) | [y IN range(1, 400) | y]]")
        assert db.execute("RETURN [x IN [1] | x]").rows == (((1,),),)


def test_locals_across_match_with_and_subquery(tmp_path):
    with connect(tmp_path / "locals") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:T {id:1})")
        query = (
            "WITH [1,2] AS x MATCH (n:T) WHERE n.id IN [x IN x | x] "
            "WITH n, [x IN x | x+1] AS x "
            "CALL (x) { RETURN [x IN x | x+1] AS y } RETURN n.id,x,y"
        )
        assert db.execute(query).rows == ((1, (2,3), (3,4)),)
        with db.query(query).cursor(batch_size=1) as cursor:
            assert tuple(cursor) == ((1, (2,3), (3,4)),)


@pytest.mark.parametrize(("query", "expected"), [
    ("RETURN [2,0] > [2] AS v", True),
    ("RETURN [2,NULL] > [2] AS v", True),
    ("RETURN [2,4] < [3,NULL] AS v", True),
    ("RETURN [2,4] >= [2,NULL] AS v", None),
    ("RETURN [[2,4]] >= [[3,NULL]] AS v", False),
    ("RETURN false = true IS NULL AS v", True),
    ("RETURN (false = true) IS NULL AS v", False),
    ("RETURN false = 1+2 IS NULL AS v", True),
    ("RETURN 2^3^2 AS v", 64.0),
    ("RETURN -3^2 AS v", 9.0),
    ("WITH 3 AS n RETURN -n^2 AS v", 9.0),
    ("UNWIND [[2], [2,3], [1]] AS x RETURN max(x) AS v", (2,3)),
    ("UNWIND [2, 'a', [1]] AS x RETURN min(x) AS v", (1,)),
    ("UNWIND [2, 'a', [1]] AS x RETURN max(x) AS v", 2),
])
def test_ordering_and_null_predicate_precedence(query, expected):
    with connect(":memory:") as db:
        assert db.execute(query).rows == ((expected,),)


@pytest.mark.parametrize("budget", (None, 4096))
def test_sum_preserves_int64_precision_and_empty_identity(budget):
    with connect(":memory:", query_memory_budget_bytes=budget) as db:
        result = db.execute("UNWIND [9007199254740993,2] AS x RETURN sum(x)")
        assert result.rows == ((9007199254740995,),)
        assert type(result.rows[0][0]) is int
        assert db.execute("UNWIND [] AS x RETURN sum(x),avg(x)").rows == ((0,None),)
        assert db.execute("UNWIND [NULL] AS x RETURN sum(x),avg(x)").rows == ((0,None),)
        for values in ("[true]", "['x']", "[1,'x']"):
            with pytest.raises(GrafxError):
                db.execute(f"UNWIND {values} AS x RETURN sum(x)")
        with pytest.raises(GrafxError):
            db.execute("UNWIND [] AS x RETURN sum($bad)", {"bad": True})
