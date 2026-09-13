"""Arithmetic type refusals retain planning versus evaluated runtime evidence."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import compile_error, native_error


@pytest.mark.parametrize("quantifier", ["all", "any", "none", "single"])
@pytest.mark.parametrize("items", ["['Clara']", "[false,true]", "['Clara','Bob']"])
def test_known_quantifier_arithmetic_type_failure_is_planning(quantifier, items):
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(f"RETURN {quantifier}(x IN {items} WHERE x % 2 = 0)")
        assert failure.value.details["reason"] == "arithmetic_operand_type"
        assert failure.value.details["query_phase"] == "planning"
        observed = compile_error(failure.value)
        assert (observed.type, observed.phase, observed.detail) == ("SyntaxError", "compile time", "InvalidArgumentType")


@pytest.mark.parametrize("operator", ["+", "-", "*", "/", "%", "^"])
def test_dynamic_arithmetic_failure_has_runtime_evidence(operator):
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(f"UNWIND [1,'text'] AS v RETURN v {operator} 2")
        assert failure.value.details["reason"] == "arithmetic_operand_type"
        observed = native_error(failure.value)
        assert (observed.type, observed.phase, observed.detail) == ("TypeError", "runtime", "InvalidArgumentType")
        assert db.execute(f"UNWIND [null] AS v RETURN v {operator} 2").rows == ((None,),)


def test_late_arithmetic_type_error_rolls_back_before_reuse(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Earlier)")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("UNWIND [1,'text'] AS v CREATE(:N {v:v}) RETURN v % 2")
            assert failure.value.details["reason"] == "arithmetic_operand_type"
            tx.execute("CREATE(:Later)")
        assert db.execute("MATCH(n) RETURN labels(n) ORDER BY labels(n)").rows == ((("Earlier",),), (("Later",),))
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("override", [
    {"field":"other"}, {"value":"AND"}, {"reason":"other"},
    {"query_phase":"planning"}, {"query_phase":None},
])
def test_mapper_does_not_guess_arithmetic_evidence(override):
    fields = {"field":"operator", "value":"%", "reason":"arithmetic_operand_type", "query_phase":"execution"}
    assert native_error(GrafxPlanError("refused", **(fields | override))).detail != "InvalidArgumentType"


@pytest.mark.parametrize("query,expected", [
    ("RETURN [x IN [1,2] | x+1] AS a,[x IN ['a'] | x+'b'] AS b", (((2,3),("ab",)),)),
    ("WITH 7 AS x RETURN x AS a,[x IN ['a'] | [x IN [1] | x+1]] AS b,x AS c", ((7,((2,),),7),)),
    ("RETURN [x IN [1] | [y IN [x] | y+1]]", ((((2,),),),)),
    ("RETURN [x IN [1,null] | x+1]", (((2,None),),)),
    ("RETURN reduce(acc=0,x IN [1,2] | acc+x)", ((3,),)),
])
def test_local_type_inference_respects_lexical_identity_and_unknowns(query, expected):
    with connect(":memory:") as db:
        assert db.execute(query).rows == expected


def test_parameter_list_type_is_not_invented_at_planning():
    with connect(":memory:") as db:
        query = "RETURN [x IN $values | x%2]"
        assert db.execute(query, {"values":[1,2]}).rows == (((1,0),),)
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(query, {"values":[1,'text']})
        assert failure.value.details["query_phase"] == "execution"
