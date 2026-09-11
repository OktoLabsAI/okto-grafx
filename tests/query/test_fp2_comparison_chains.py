"""Adjacent-pair comparison semantics and native statement/cost boundaries."""

import itertools

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.parser import parse


@pytest.mark.parametrize("expression, expected", [
    ("1 < 2 <= 2", True), ("1 = 1 = true", False),
    ("(1 = 1) = true", True), ("1 = (1 = true)", False),
    ("1 < 3 > 2", True), ("3 < 2 < 1", False),
    ("1 < 2 = 2 <> 3 >= 3", True), ("3 != 2 != 1", True),
    ("1 < null < 3", None), ("null < 3 < 2", False),
    ("2 < 1 < null", False), ("null = null = null", None),
    ("'a' < 'b' <= 'c'", True), ("[1] < [2] <= [3]", True),
    ("NOT 1 < 2 < 3", False), ("1 < 2 < 3 AND 2 < 3 < 4", True),
    ("3 < 2 < 1 OR 1 < 2 < 3", True),
    ("true = 1 IN [1] = true", True),
    ("false = 'a' CONTAINS 'z' = false", True),
    ("true = null IS NULL = true", True),
    ("1 < 2 + 3 <= 6", True),
])
def test_exact_comparison_results(expression, expected):
    with connect(":memory:") as db:
        result = db.execute("RETURN " + expression)
        assert result.columns == (expression,)
        assert result.rows == ((expected,),)


def test_null_truth_table_matches_independent_adjacent_pair_oracle():
    def eq(a, b):
        return None if a is None or b is None else a == b

    values = tuple(itertools.product((None, 1, 2), repeat=3))
    expected = []
    for a, b, c in values:
        left, right = eq(a, b), eq(b, c)
        expected.append(False if left is False or right is False else
                        None if left is None or right is None else True)
    with connect(":memory:") as db:
        result = db.execute("UNWIND $triples AS t RETURN t[0] = t[1] = t[2] AS equal", {"triples": values})
    assert result.rows == tuple((value,) for value in expected)


def test_chain_short_circuit_and_failure_isolation():
    with connect(":memory:") as db:
        assert db.execute("RETURN 2 < 1 < 1 / 0 AS safe").rows == ((False,),)
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CREATE (:N {id:2}) RETURN null < 1 < 1 / 0")
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)


def test_filter_projection_and_scope_composition():
    with connect(":memory:") as db:
        result = db.execute("UNWIND [1,2,3,4,5] AS n WITH n WHERE 1 < n <= 4 "
                            "RETURN n, 2 <= n < 4 AS selected ORDER BY n")
        assert result.rows == ((2, True), (3, True), (4, False))
        db.explain("RETURN 1 < 2 < 3")


def test_expansion_is_structural_and_bounded_not_a_new_evaluator():
    assert parse("RETURN 1 < 2 <= 3 = 4") == parse("RETURN 1 < 2 AND 2 <= 3 AND 3 = 4")
    statement = parse("RETURN " + " < ".join("1" for _ in range(2000)))
    with pytest.raises(GrafxPlanError) as failure:
        analyze(statement)
    assert failure.value.details["field"] == "depth"
