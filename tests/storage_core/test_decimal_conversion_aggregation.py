"""FP-6 explicit conversion and exact aggregate finalization, independent of storage."""

from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import permutations
import random

import pytest

from okto_grafx.domain.model.decimal_values import (
    DecimalValue, decimal_from_text, decimal_from_number, decimal_sum, decimal_average,
)
from okto_grafx.domain.model.errors import SchemaMismatchError


@pytest.mark.parametrize("text,expected", [
    ("1.2300", "1.2300"), ("+1.23", "1.2300"), (".12", "0.1200"),
    ("1.", "1.0000"), ("-0e999999999999999", "0.0000"),
    ("1.23E+2", "123.0000"), ("123e-2", "1.2300"),
    ("000000001.23000", "1.2300"), ("-1e-4", "-0.0001"),
])
def test_text_syntax_and_scale(text, expected):
    assert decimal_from_text(text, 38, 4).to_string() == expected


@pytest.mark.parametrize("text", ["", " 1", "1 ", "１", "NaN", "Infinity", "1_000", ".", "1e", "--1", "1.2.3", "1\n", "1"*1025, None, 1])
def test_refuse_non_native_or_malformed_text(text):
    with pytest.raises(SchemaMismatchError):
        decimal_from_text(text, 38, 0)


@pytest.mark.parametrize("sign", ["", "-"])
@pytest.mark.parametrize("mode", ["EXACT", "HALF_EVEN", "HALF_UP", "DOWN", "FLOOR", "CEILING"])
def test_extreme_exponents_are_bounded_and_do_not_allocate_by_exponent(sign, mode):
    with pytest.raises(SchemaMismatchError) as overflow:
        decimal_from_text(sign + "1e" + "9"*900, 38, 0, rounding=mode)
    assert overflow.value.details["reason"] == "decimal_overflow"
    text = sign + "1e-" + "9"*900
    if mode == "EXACT":
        with pytest.raises(SchemaMismatchError) as inexact:
            decimal_from_text(text, 38, 38, rounding=mode)
        assert inexact.value.details["reason"] == "decimal_inexact"
    else:
        expected = -1 if sign and mode == "FLOOR" else 1 if not sign and mode == "CEILING" else 0
        assert decimal_from_text(text, 38, 38, rounding=mode).coefficient == expected


def test_numeric_conversion_preserves_binary_value_not_its_display():
    assert decimal_from_number(2**63-1, 38, 0).coefficient == 2**63-1
    assert decimal_from_number(0.5, 3, 1).numeric_key() == (1, 2)
    with pytest.raises(SchemaMismatchError) as inexact:
        decimal_from_number(0.1, 38, 1)
    assert inexact.value.details["reason"] == "decimal_inexact"
    assert decimal_from_number(0.1, 38, 1, rounding="HALF_EVEN") == decimal_from_text("0.1", 38, 1)
    for value in [True, Decimal("1"), float("nan"), float("inf"), float("-inf"), "1"]:
        with pytest.raises(SchemaMismatchError):
            decimal_from_number(value, 38, 0)


def test_sum_fits_only_after_cancellation_and_is_order_independent():
    maximum = DecimalValue(10**38-1, 38, 0)
    negative = DecimalValue(-maximum.coefficient, 38, 0)
    for values in permutations([maximum, maximum, negative]):
        assert decimal_sum(values) == maximum
    with pytest.raises(SchemaMismatchError) as overflow:
        decimal_sum([maximum, maximum])
    assert overflow.value.details["reason"] == "decimal_overflow"
    assert decimal_average([maximum, maximum]) == maximum


def test_nulls_scale_and_late_invalid_values():
    assert decimal_sum([None, None]).numeric_key() == (0, 1)
    assert decimal_sum([]).numeric_key() == (0, 1)
    assert decimal_average([None]) is None
    assert decimal_average([]) is None
    a, b = decimal_from_text("1.20", 3, 2), decimal_from_text("3.4", 2, 1)
    assert decimal_sum([None, a, b]).to_string() == "4.60"
    assert decimal_average([None, a, b]).numeric_key() == (23, 10)
    for function in [decimal_sum, decimal_average]:
        with pytest.raises(SchemaMismatchError):
            function(iter([a, None, object()]))


def test_average_rounds_once_not_per_row():
    tiny = DecimalValue(1, 38, 38)
    assert decimal_average([tiny, tiny, DecimalValue(0, 38, 38)]) == tiny


def test_parsing_and_aggregate_independent_oracles():
    rng = random.Random(6063)
    with localcontext() as context:
        context.prec = 160
        for _ in range(200):
            values = [DecimalValue(rng.randint(-10**15, 10**15), 38, rng.randrange(19)) for _ in range(12)]
            total = sum((Fraction(*v.numeric_key()) for v in values), Fraction(0))
            assert Fraction(*decimal_sum(values).numeric_key()) == total
            mean = decimal_average(values)
            expected = (Decimal(total.numerator) / Decimal(total.denominator) / len(values)).quantize(
                Decimal(1).scaleb(-mean.scale), rounding="ROUND_HALF_EVEN")
            assert Decimal(mean.to_string()) == expected
            for value in values:
                assert decimal_from_text(value.to_string(), 38, value.scale) == value
