"""Independent exact oracles for the FP-6 decimal foundation, not stored admission."""

from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from fractions import Fraction
import random

import pytest

from okto_grafx.domain.model.decimal_values import (
    DecimalValue, decimal_add, decimal_compare, decimal_divide, decimal_multiply,
    decimal_negate, decimal_absolute,
)
from okto_grafx.domain.model.errors import SchemaMismatchError


@pytest.mark.parametrize("args,reason", [
    ((1, 0, 0), "decimal_type_bounds"), ((1, 39, 0), "decimal_type_bounds"),
    ((1, 2, 3), "decimal_type_bounds"), ((1, 2, -1), "decimal_type_bounds"),
    ((True, 2, 0), "decimal_component_type"), ((1, True, 0), "decimal_component_type"),
    ((1, 2, False), "decimal_component_type"), ((1.0, 2, 0), "decimal_component_type"),
    ((100, 2, 0), "decimal_overflow"), ((-100, 2, 0), "decimal_overflow"),
])
def test_invalid_components(args, reason):
    with pytest.raises(SchemaMismatchError) as caught:
        DecimalValue(*args)
    assert caught.value.details["reason"] == reason


@pytest.mark.parametrize("coefficient,scale,text", [
    (0, 0, "0"), (0, 3, "0.000"), (1, 3, "0.001"), (-1, 3, "-0.001"),
    (12300, 3, "12.300"), (10**38-1, 0, "9"*38),
])
def test_lossless_text_and_immutable_shape(coefficient, scale, text):
    value = DecimalValue(coefficient, 38, scale)
    assert value.to_string() == text
    with pytest.raises(FrozenInstanceError):
        value.coefficient = 0


def test_numeric_identity_is_distinct_from_declared_shape():
    a, b = DecimalValue(120, 3, 2), DecimalValue(12, 2, 1)
    assert a != b
    assert a.numeric_key() == b.numeric_key() == (6, 5)
    assert DecimalValue(0, 38, 38).numeric_key() == (0, 1)
    assert decimal_compare(a, b) == 0
    assert decimal_compare(DecimalValue(1, 1, 1), 0.1) == -1
    assert decimal_compare(DecimalValue(5, 1, 1), 0.5) == 0
    assert decimal_compare(DecimalValue(2**53+1, 38, 0), float(2**53)) == 1


@pytest.mark.parametrize("value", [True, None, "1", Decimal("1"), float("nan"), float("inf")])
def test_no_implicit_host_coercion(value):
    with pytest.raises(SchemaMismatchError):
        decimal_compare(DecimalValue(1, 1, 0), value)


@pytest.mark.parametrize("mode,positive,negative", [
    ("HALF_EVEN", 12, -12), ("HALF_UP", 13, -13), ("DOWN", 12, -12),
    ("FLOOR", 12, -13), ("CEILING", 13, -12),
])
def test_explicit_rounding_sign_and_ties(mode, positive, negative):
    assert DecimalValue(125, 3, 2).rescale(3, 1, rounding=mode).coefficient == positive
    assert DecimalValue(-125, 3, 2).rescale(3, 1, rounding=mode).coefficient == negative
    assert DecimalValue(135, 3, 2).rescale(3, 1, rounding="HALF_EVEN").coefficient == 14


def test_exact_assignment_and_rounding_overflow():
    assert DecimalValue(120, 3, 2).rescale(2, 1) == DecimalValue(12, 2, 1)
    for action, reason in [
        (lambda: DecimalValue(121, 3, 2).rescale(2, 1), "decimal_inexact"),
        (lambda: DecimalValue(999, 3, 1).rescale(2, 0, rounding="HALF_UP"), "decimal_overflow"),
        (lambda: DecimalValue(1, 1, 0).rescale(1, 0, rounding="anything"), "decimal_rounding_mode"),
    ]:
        with pytest.raises(SchemaMismatchError) as caught:
            action()
        assert caught.value.details["reason"] == reason


def test_exact_arithmetic_and_zero_reduction():
    a, b = DecimalValue(120, 3, 2), DecimalValue(34, 2, 1)
    assert decimal_add(a, b).to_string() == "4.60"
    assert decimal_add(a, b, subtract=True).to_string() == "-2.20"
    assert decimal_multiply(a, b).to_string() == "4.080"
    assert decimal_multiply(DecimalValue(0, 38, 38), DecimalValue(1, 38, 38)).scale == 38
    assert decimal_multiply(DecimalValue(10**37, 38, 38), DecimalValue(10**37, 38, 38)).numeric_key() == (1, 100)
    for action in (
        lambda: decimal_add(DecimalValue(10**38-1, 38, 0), DecimalValue(1, 1, 0)),
        lambda: decimal_multiply(DecimalValue(1, 38, 38), DecimalValue(1, 38, 38)),
    ):
        with pytest.raises(SchemaMismatchError) as caught:
            action()
        assert caught.value.details["reason"] == "decimal_overflow"


def test_division_boundaries():
    assert decimal_divide(DecimalValue(1, 1, 0), DecimalValue(3, 1, 0)).to_string() == "0." + "3"*38
    assert decimal_divide(DecimalValue(-1, 1, 0), DecimalValue(2, 1, 0)).numeric_key() == (-1, 2)
    assert decimal_divide(DecimalValue(1, 38, 38), DecimalValue(2, 1, 0)).coefficient == 0
    assert decimal_divide(DecimalValue(3, 38, 38), DecimalValue(2, 1, 0)).coefficient == 2
    assert decimal_divide(DecimalValue(10**38-1, 38, 0), DecimalValue(1, 1, 0)).coefficient == 10**38-1
    for denominator, reason in [(DecimalValue(0, 1, 0), "decimal_division_by_zero"),
                                (DecimalValue(1, 38, 38), "decimal_overflow")]:
        with pytest.raises(SchemaMismatchError) as caught:
            decimal_divide(DecimalValue(10, 2, 0), denominator)
        assert caught.value.details["reason"] == reason


def test_exact_random_oracle_and_mutable_host_context_independence():
    randomizer = random.Random(606)
    with localcontext() as hostile:
        hostile.prec = 2
        for _ in range(250):
            a = DecimalValue(randomizer.randint(-999999, 999999), 12, randomizer.randrange(6))
            b = DecimalValue(randomizer.randint(1, 999999), 12, randomizer.randrange(6))
            fa, fb = Fraction(*a.numeric_key()), Fraction(*b.numeric_key())
            assert Fraction(*decimal_add(a, b).numeric_key()) == fa + fb
            assert Fraction(*decimal_multiply(a, b).numeric_key()) == fa * fb
            assert decimal_compare(a, b) == (fa > fb) - (fa < fb)
            result = decimal_divide(a, b)
            # Independent stdlib decimal oracle has its own ample context.
            with localcontext() as oracle:
                oracle.prec = 150
                expected = (Decimal(a.to_string()) / Decimal(b.to_string())).quantize(
                    Decimal(1).scaleb(-result.scale), rounding=ROUND_HALF_EVEN)
                assert Decimal(result.to_string()) == expected


@pytest.mark.parametrize("mode", ["HALF_EVEN", "HALF_UP", "DOWN", "FLOOR", "CEILING"])
def test_rescaling_against_independent_oracle(mode):
    randomizer = random.Random(6061)
    with localcontext() as context:
        context.prec = 120
        for _ in range(200):
            value = DecimalValue(randomizer.randint(-10**30, 10**30), 38, randomizer.randrange(39))
            target_scale = randomizer.randrange(39)
            expected = Decimal(value.to_string()).quantize(
                Decimal(1).scaleb(-target_scale), rounding="ROUND_" + mode)
            coefficient = int(expected.scaleb(target_scale))
            if abs(coefficient) >= 10**38:
                with pytest.raises(SchemaMismatchError) as caught:
                    value.rescale(38, target_scale, rounding=mode)
                assert caught.value.details["reason"] == "decimal_overflow"
            else:
                assert value.rescale(38, target_scale, rounding=mode).coefficient == coefficient


@pytest.mark.parametrize("scale", range(39))
def test_all_native_scale_boundaries(scale):
    value = DecimalValue(10**38-1, 38, scale)
    assert Fraction(*value.numeric_key()) == Fraction(10**38-1, 10**scale)
    assert value.rescale(38, scale) == value
    assert decimal_add(value, DecimalValue(0, 1, 0)) == value
    assert decimal_multiply(value, DecimalValue(1, 1, 0)) == value
    assert decimal_compare(value, value) == 0
    assert decimal_negate(value) == DecimalValue(-(10**38-1), 38, scale)
    assert decimal_absolute(decimal_negate(value)) == value


def test_full_width_division_and_overflow_oracle():
    randomizer = random.Random(6062)
    with localcontext() as oracle:
        oracle.prec = 200
        for _ in range(1000):
            a = DecimalValue(randomizer.randint(-10**38+1, 10**38-1), 38, randomizer.randrange(39))
            b = DecimalValue(randomizer.choice([-1, 1]) * randomizer.randint(1, 10**38-1), 38, randomizer.randrange(39))
            quotient = Decimal(a.to_string()) / Decimal(b.to_string())
            scale = 38 - max(0, quotient.copy_abs().adjusted() + 1)
            expected = None
            while scale >= 0:
                rounded = quotient.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_EVEN)
                if abs(int(rounded.scaleb(scale))) < 10**38:
                    expected = rounded
                    break
                scale -= 1
            if expected is None:
                with pytest.raises(SchemaMismatchError) as caught:
                    decimal_divide(a, b)
                assert caught.value.details["reason"] == "decimal_overflow"
            else:
                actual = decimal_divide(a, b)
                assert actual.scale == scale
                assert Decimal(actual.to_string()) == expected
