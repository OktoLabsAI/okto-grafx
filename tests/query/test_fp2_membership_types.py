"""Static, bound and dynamic IN operand checks retain their actual phase."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError


@pytest.mark.parametrize("right", ["true", "123", "123.4", "'foo'", "{x: []}"])
@pytest.mark.parametrize("prefix", ["", "UNWIND [] AS empty "])
def test_known_non_list_operand_refuses_during_planning(right, prefix):
    with connect(":memory:") as db:
        for operation in (db.explain, db.execute):
            with pytest.raises(GrafxPlanError) as caught:
                operation(prefix + f"RETURN 1 IN {right}")
            assert caught.value.details == {
                "field": "operator", "value": "IN", "reason": "membership_operand_type", "query_phase": "planning",
            }


@pytest.mark.parametrize("value", [True, 123, 1.2, "foo", {"x": []}])
def test_parameter_types_refuse_before_empty_rows_or_statement_writes(value):
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:10})")
            for prefix in ("UNWIND [] AS empty", "CREATE (:N {id:1})"):
                with pytest.raises(GrafxPlanError) as caught:
                    tx.execute(prefix + " RETURN 1 IN $right", {"right": value})
                assert caught.value.details["query_phase"] == "execution"
                assert caught.value.details["reason"] == "membership_operand_type"
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((10,),)


def test_dynamic_failure_rolls_back_statement_through_reopen(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:10})")
            with pytest.raises(GrafxPlanError) as caught:
                tx.execute("UNWIND [{id:1,rhs:[1]}, {id:2,rhs:2}] AS x "
                           "CREATE (:N {id:x.id}) RETURN 1 IN x.rhs")
            assert caught.value.details["query_phase"] == "execution"
            assert caught.value.details["reason"] == "membership_operand_type"
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((10,),)
    with connect(path) as db:
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((10,),)


def test_membership_keeps_null_nested_equality_and_dynamic_short_circuit():
    with connect(":memory:") as db:
        assert db.execute("RETURN null IN [], null IN [1], 1 IN [null,1], "
                          "[1,null] IN [[1,null]], {a:1} IN [{a:1}], 1 IN null").rows == (
                              (False, None, True, None, True, None),)
        assert db.execute("UNWIND [[1],123] AS rhs RETURN false AND 1 IN rhs, true OR 1 IN rhs").rows == (
            (False, True), (False, True))
