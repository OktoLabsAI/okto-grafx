"""Native decimal query semantics, with independent finite numeric oracles."""

from fractions import Fraction
from itertools import permutations
import math

import pytest

from okto_grafx import connect, DecimalValue
from okto_grafx.errors import GrafxPlanError
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.query.decimal_numeric import decimal_group_key, DecimalOrderKey
from okto_grafx.domain.model.decimal_values import decimal_from_text
from tests.query.stack import build_query_stack


def evaluate(text, **parameters):
    stack = build_query_stack()
    return stack.engine.execute(text, stack.transaction(), parameters).rows


@pytest.mark.parametrize("left,right,expected", [
    (DecimalValue(123, 3, 2), DecimalValue(1230, 8, 3), 0),
    (DecimalValue(100, 3, 2), 1, 0), (DecimalValue(125, 3, 2), 1.25, 0),
    (DecimalValue(1, 1, 1), 0.1, -1),
    (DecimalValue(100000000000000000001, 21, 0), 1e20, 1),
    (DecimalValue(-1, 38, 38), 0, -1),
    (DecimalValue(10**38 - 1, 38, 0), float("inf"), -1),
    (DecimalValue(-10**38 + 1, 38, 0), float("-inf"), 1),
])
def test_exact_comparisons_both_directions_and_membership(left, right, expected):
    for lhs, rhs, sign in ((left, right, expected), (right, left, -expected)):
        result = evaluate("RETURN $a=$b, $a<>$b, $a<$b, $a<=$b, $a>$b, $a>=$b, $a IN [$b], $a IN $list",
                          a=lhs, b=rhs, list=[rhs])
        assert result == ((sign == 0, sign != 0, sign < 0, sign <= 0, sign > 0, sign >= 0, sign == 0, sign == 0),)


@pytest.mark.parametrize("other", [True, "1", None, float("nan")])
def test_non_numeric_and_null_nan_predicates(other):
    value = DecimalValue(100, 3, 2)
    result = evaluate("RETURN $a=$b, $a<$b, $b<$a", a=value, b=other)
    if other is None:
        assert result == ((None, None, None),)
    elif type(other) is float:
        assert result == ((False, False, False),)
    else:
        assert result == ((False, None, None),)


@pytest.mark.parametrize("expression,expected", [
    ("decimal('1.23',5,2)", DecimalValue(123, 5, 2)),
    ("decimal('1.235',5,2,'HALF_EVEN')", DecimalValue(124, 5, 2)),
    ("decimal('1.225',5,2,'HALF_EVEN')", DecimalValue(122, 5, 2)),
    ("decimal(0.1,5,2,'HALF_UP')", DecimalValue(10, 5, 2)),
    ("decimal(12,5,2)", DecimalValue(1200, 5, 2)),
    ("decimal(decimal('1.2',2,1),8,3)", DecimalValue(1200, 8, 3)),
    ("decimal(null,5,2)", None),
    ("decimal('1.2',3,1)+2", DecimalValue(32, 38, 1)),
    ("2-decimal('1.2',3,1)", DecimalValue(8, 38, 1)),
    ("decimal('1.2',3,1)*decimal('2.5',3,1)", DecimalValue(300, 38, 2)),
    ("decimal('1',1,0)/3", DecimalValue(int('3'*38), 38, 38)),
    ("-decimal('1.2',3,1)", DecimalValue(-12, 3, 1)),
    ("abs(decimal('-1.2',3,1))", DecimalValue(12, 3, 1)),
    ("sign(decimal('-1.2',3,1))", -1),
    ("toString(decimal('-1.20',3,2))", "-1.20"),
    ("toInteger(decimal('-1.99',3,2))", -1),
    ("toInteger(decimal('999999999999999999999',21,0))", None),
    ("toFloat(decimal('1.25',3,2))", 1.25),
    ("CASE WHEN decimal('1.0',2,1)=1 THEN decimal('2.0',2,1)+1 ELSE null END", DecimalValue(30, 38, 1)),
])
def test_explicit_cast_arithmetic_and_scalars(expression, expected):
    assert evaluate("RETURN " + expression + " AS value") == ((expected,),)


@pytest.mark.parametrize("expression", ["decimal('1.23',2,1)", "decimal('100',2,0)",
    "decimal(0.1,2,1)", "decimal('NaN',3,0)", "decimal('1',0,0)", "decimal('1',3,4)",
    "decimal('1',3,0,'UNKNOWN')", "decimal('1',3,0)/0", "decimal('1',3,0)+0.5",
    "0.5*decimal('1',3,0)", "sqrt(decimal('1',3,0))", "toBoolean(decimal('1',3,0))"])
def test_inexact_overflow_nonfinite_and_implicit_double_refuse(expression):
    with pytest.raises(GrafxPlanError):
        evaluate("RETURN " + expression)


@pytest.mark.parametrize("budget", [None, 4096])
@pytest.mark.parametrize("name", ["sum", "avg", "sum(DISTINCT", "avg(DISTINCT"])
def test_mixed_decimal_double_aggregate_refusal_is_order_and_spill_independent(budget, name):
    expression = f"{name} x)" if "(" in name else f"{name}(x)"
    for values in ((DecimalValue(10, 2, 1), 1.0), (1.0, DecimalValue(10, 2, 1))):
        stack = build_query_stack(query_memory_budget_bytes=budget)
        with pytest.raises(GrafxPlanError) as failure:
            stack.engine.execute("UNWIND $xs AS x RETURN " + expression, stack.transaction(), {"xs": values})
        assert failure.value.details["reason"] == "decimal_operand_type"


@pytest.mark.parametrize("budget", [None, 4096])
def test_exact_sum_and_average_cancellation_without_intermediate_overflow(budget):
    maximum = DecimalValue(10**38 - 1, 38, 0)
    negative = DecimalValue(1 - 10**38, 38, 0)
    stack = build_query_stack(query_memory_budget_bytes=budget)
    for values in set(permutations((maximum, maximum, negative))):
        assert stack.engine.execute("UNWIND $xs AS x RETURN sum(x)", stack.transaction(), {"xs": values}).rows == ((maximum,),)
    assert stack.engine.execute("UNWIND $xs AS x RETURN avg(x)", stack.transaction(), {"xs": [maximum, maximum]}).rows == ((maximum,),)
    value = stack.engine.execute("UNWIND $xs AS x RETURN sum(x),avg(x)", stack.transaction(),
                                {"xs": [1, DecimalValue(2, 2, 1), None, -1]}).rows[0]
    assert value[0] == DecimalValue(2, 38, 1)
    assert Fraction(*value[1].as_integer_ratio()) == Fraction(6666666666666666666666666666666666667, 10**38)


@pytest.mark.parametrize("budget", [None, 4096])
def test_aggregate_overflow_refuses_only_at_finalization(budget):
    stack = build_query_stack(query_memory_budget_bytes=budget)
    with pytest.raises(GrafxPlanError) as failure:
        stack.engine.execute("UNWIND $xs AS x RETURN sum(x)", stack.transaction(),
                             {"xs": [DecimalValue(10**38 - 1, 38, 0)] * 2})
    assert failure.value.details["reason"] == "decimal_overflow"


def test_group_and_order_key_agree_with_independent_fraction_oracle():
    values = [DecimalValue(1, 38, 38), DecimalValue(1, 1, 1), DecimalValue(10, 4, 2),
              DecimalValue(125, 3, 2), DecimalValue(10**38 - 1, 38, 0),
              DecimalValue(100000000000000000001, 21, 0), 0.1, 1.25, 1e20, 0, -1]
    def ratio(value):
        return Fraction(*value.as_integer_ratio()) if type(value) in (float, DecimalValue) else Fraction(value)
    for left in values:
        for right in values:
            if type(left) is DecimalValue:
                left_key = decimal_group_key(left)
                right_key = decimal_group_key(right) if type(right) is DecimalValue else ("number", int(right) if type(right) is float and right.is_integer() else right)
                assert (left_key == right_key) == (ratio(left) == ratio(right))
                assert (DecimalOrderKey(left) < (DecimalOrderKey(right) if type(right) is DecimalValue else right)) == (ratio(left) < ratio(right))


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_typed_native_query_update_reopen_and_case(tmp_path, codec):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, amount DECIMAL(10,3), PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1,amount:decimal('1.23',4,2)})")
            assert tx.execute("MATCH (n:N) WHERE n.amount=decimal('1.2300',8,4) RETURN n.id").rows == ((1,),)
            tx.execute("MATCH (n:N) SET n.amount=n.amount+decimal('2.1',3,1)")
            assert tx.execute("MATCH (n:N) RETURN n.amount").rows == ((DecimalValue(3330, 10, 3),),)
            with pytest.raises(SchemaMismatchError) as failure:
                tx.execute("MATCH (n:N) SET n.amount=n.amount+decimal('0.12345',5,5)")
            assert failure.value.details["reason"] == "decimal_inexact"
            assert tx.execute("MATCH (n:N) RETURN n.amount").rows == ((DecimalValue(3330, 10, 3),),)
        assert not db.verify("all").findings
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:N) RETURN sum(n.amount),avg(n.amount)").rows[0][0] == DecimalValue(3330, 38, 3)
        assert db.execute("MATCH (n:N) WHERE n.amount>3.0 RETURN n.id").rows == ((1,),)


def test_expression_nonfinite_does_not_enter_decimal_cast():
    assert evaluate("RETURN decimal('1',1,0)=(0.0/0.0)") == ((False,),)
    with pytest.raises(GrafxPlanError):
        evaluate("RETURN decimal(0.0/0.0,5,2)")
    assert math.isnan(evaluate("RETURN 0.0/0.0")[0][0])


def test_decimal_result_rounding_matches_exact_constructor():
    value = evaluate("RETURN decimal('1',1,0)/6")[0][0]
    assert value == decimal_from_text("0.16666666666666666666666666666666666667", 38, 38)


@pytest.mark.parametrize("budget", [None, 4096])
def test_distinct_aggregate_result_type_is_independent_of_numeric_representative(budget):
    for values in ((1, DecimalValue(100, 3, 2)), (DecimalValue(100, 3, 2), 1)):
        stack = build_query_stack(query_memory_budget_bytes=budget)
        result = stack.engine.execute("UNWIND $xs AS x RETURN sum(DISTINCT x),avg(DISTINCT x)",
                                      stack.transaction(), {"xs": values}).rows[0]
        assert result == (DecimalValue(100, 38, 2), DecimalValue(10**37, 38, 37))


@pytest.mark.parametrize("budget", [None, 4096])
def test_seventy_six_digit_intermediate_is_not_prematurely_rounded(budget):
    maximum, tiny = DecimalValue(10**38 - 1, 38, 0), DecimalValue(1, 38, 38)
    negative = DecimalValue(1 - 10**38, 38, 0)
    for values in permutations((maximum, tiny, negative)):
        stack = build_query_stack(query_memory_budget_bytes=budget)
        assert stack.engine.execute("UNWIND $xs AS x RETURN sum(x)", stack.transaction(), {"xs": values}).rows == ((tiny,),)


def test_expression_only_decimal_does_not_publish_storage_capability(tmp_path):
    with connect(tmp_path / "db") as db:
        before = db._catalog.read_from_pages().serialize()
        assert db.execute("RETURN decimal('1.2',3,1)+2").rows == ((DecimalValue(32, 38, 1),),)
        assert db._catalog.read_from_pages().serialize() == before
