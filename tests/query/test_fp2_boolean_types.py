"""No Python truthiness or silent UNKNOWN for invalid boolean operands."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError


@pytest.mark.parametrize("expression", [
    "123 AND true", "false AND 1", "true OR []", "null XOR {}", "NOT 'false'",
    "[true] OR null", "NOT {x: 1}", "(1 + 2) AND false", "NOT size([])",
])
def test_known_invalid_operands_fail_in_planning_even_in_lazy_branches(expression):
    with connect(":memory:") as db:
        for query in (f"RETURN {expression}", f"UNWIND [] AS x RETURN {expression}"):
            for operation in (db.explain, db.execute):
                with pytest.raises(GrafxPlanError) as caught:
                    operation(query)
                assert caught.value.details["reason"] == "boolean_operand_type"
                assert caught.value.details["query_phase"] == "planning"


@pytest.mark.parametrize("expression", ["$x AND false", "true OR $x", "NOT $x", "$x XOR null"])
@pytest.mark.parametrize("value", [1, 0.0, "true", [], {}, [False]])
def test_bound_operand_types_fail_before_empty_rows_or_writes(expression, value):
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})")
            for prefix in ("UNWIND [] AS empty", "CREATE (:N {id:2})"):
                with pytest.raises(GrafxPlanError) as caught:
                    tx.execute(f"{prefix} RETURN {expression}", {"x": value})
                assert caught.value.details["reason"] == "boolean_operand_type"
                assert caught.value.details["query_phase"] == "execution"
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)


def test_unknown_heterogeneous_row_type_refuses_late_and_rolls_back_statement(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:10})")
            with pytest.raises(GrafxPlanError) as caught:
                tx.execute("UNWIND [{id:1,v:true},{id:2,v:2}] AS x "
                           "CREATE (:N {id:x.id}) RETURN NOT x.v")
            assert caught.value.details["query_phase"] == "execution"
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((10,),)
    with connect(tmp_path / "db") as db:
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((10,),)


def test_valid_null_truth_table_and_dynamic_short_circuit():
    with connect(":memory:") as db:
        assert db.execute("RETURN true AND null, false AND null, true OR null, "
                          "false OR null, null XOR true, NOT null").rows == (
                              (None, False, True, None, None, None),)
        assert db.execute("UNWIND [true, 12] AS x RETURN false AND x, true OR x").rows == (
            (False, True), (False, True))


def test_scalar_literal_identity_cannot_collide_in_grouped_projection():
    from okto_grafx.domain.query.ast import Literal

    assert len({Literal(True), Literal(1), Literal(1.0)}) == 3
    assert len({Literal(False), Literal(0), Literal(0.0)}) == 3
    assert Literal(1) == Literal(1)
    with connect(":memory:") as db:
        row = db.execute("RETURN true AS t, 1 AS i, 1.0 AS d, count(*) AS n").rows[0]
        assert row == (True, 1, 1.0, 1)
        assert tuple(type(value) for value in row) == (bool, int, float, int)
        assert db.execute("RETURN 1 = 1.0, true = 1").rows == ((True, False),)
        assert db.execute("WITH true AS t, 1 AS i RETURN t AND true AS b, i, count(*)").rows == (
            (True, 1, 1),)
