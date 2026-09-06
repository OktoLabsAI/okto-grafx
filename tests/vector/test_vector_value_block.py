"""A vector is encoded after one block judgement and decoded without a second one (VEC-3).

``_encode_vector`` asks its two guards of the whole vector at once and walks the components only
to name the FIRST offending position, exactly as before. The decoder builds the value through a
trusted door that skips the constructor's normalisation for fields a page already holds. What is
held here: every refusal keeps its position, its message and its details; an encoded vector
decodes to a value equal to one built through the public constructor; and the public
constructor still normalises and refuses what it always did.
"""

from __future__ import annotations

import math

import pytest

from okto_grafx.domain.errors import GrafxVectorValidationError
from okto_grafx.domain.model.value import (
    FLOAT32_OVERFLOW_THRESHOLD,
    MAX_FLOAT32,
    VectorValue,
    decode_values,
    encode_value,
)

from .conftest import seeded_vectors


def _first_offender(values: tuple[float, ...], single: bool) -> tuple[int, bool]:
    """Return the position of the first offending component and whether it is a range offender."""
    for position, component in enumerate(values):
        if not math.isfinite(component):
            return position, False
        if (
            single
            and not -FLOAT32_OVERFLOW_THRESHOLD < component < FLOAT32_OVERFLOW_THRESHOLD
        ):
            return position, True
    raise AssertionError("the case has no offending component")


@pytest.mark.parametrize("dtype", ["float32", "float64"])
@pytest.mark.parametrize("dimension", [1, 7, 768])
def test_an_encoded_vector_decodes_to_the_value_the_public_constructor_builds(
    dtype: str, dimension: int
) -> None:
    for values in seeded_vectors(5, dimension, 0xB10C + dimension):
        vector = VectorValue(values=values, space_ref=7, dtype=dtype)
        (decoded,), following = decode_values(encode_value(vector), 1)
        assert following == len(encode_value(vector))
        assert isinstance(decoded, VectorValue)
        assert type(decoded.values) is tuple
        assert all(type(component) is float for component in decoded.values)
        assert decoded.space_ref == 7 and decoded.dtype == dtype
        expected = VectorValue(values=decoded.values, space_ref=7, dtype=dtype)
        assert decoded == expected
        assert hash(decoded) == hash(expected)
        if dtype == "float64":
            assert decoded.values == values


def test_the_trusted_door_equals_the_public_constructor_on_decoded_fields() -> None:
    values = (0.5, -0.25, 3.0)
    assert VectorValue._from_decoded(values, 3, "float32") == VectorValue(
        values, 3, "float32"
    )
    assert VectorValue._from_decoded(values, 3, "float64") == VectorValue(
        values, 3, "float64"
    )
    assert VectorValue._from_decoded(values, 3, "float32") != VectorValue(
        values, 3, "float64"
    )


@pytest.mark.parametrize(
    ("values", "dtype"),
    [
        ((math.nan, 1.0, 1.0), "float32"),
        ((1.0, math.inf, 1.0), "float32"),
        ((1.0, 1.0, -math.inf), "float32"),
        ((1e39, 1.0, 1.0), "float32"),
        ((1.0, -1e39, 1.0), "float32"),
        ((1.0, 1.0, FLOAT32_OVERFLOW_THRESHOLD), "float32"),
        ((1e39, math.nan), "float32"),
        ((math.nan, 1e39), "float32"),
        ((-1e39, math.inf), "float32"),
        ((math.nan, 1.0), "float64"),
        ((1e300, math.inf), "float64"),
        ((1.0, 1.0, 1.0, -math.inf), "float64"),
    ],
)
def test_a_refusal_names_the_first_offending_position_with_its_details(
    values: tuple[float, ...], dtype: str
) -> None:
    position, by_range = _first_offender(values, dtype == "float32")
    with pytest.raises(GrafxVectorValidationError) as failure:
        encode_value(VectorValue(values=values, space_ref=1, dtype=dtype))
    details = failure.value.details
    assert details["field"] == "values"
    assert details["position"] == position
    assert details["value"] == repr(values[position])
    if by_range:
        assert details["limit"] == MAX_FLOAT32 and details["dtype"] == "float32"
        assert "float32 vector must be within" in str(failure.value)
    else:
        assert "limit" not in details
        assert "must be finite" in str(failure.value)


def test_a_float64_vector_stores_what_a_float32_vector_refuses_by_range() -> None:
    values = (1e300, -1e300, 1e39)
    encoded = encode_value(VectorValue(values=values, space_ref=1, dtype="float64"))
    assert decode_values(encoded, 1)[0][0].values == values
    with pytest.raises(GrafxVectorValidationError) as failure:
        encode_value(VectorValue(values=values, space_ref=1, dtype="float32"))
    assert failure.value.details["position"] == 0 and "limit" in failure.value.details


def test_a_negative_overflow_is_refused_wherever_it_sits() -> None:
    for position in (0, 3, 7):
        values = [1.0] * 8
        values[position] = -1e39
        with pytest.raises(GrafxVectorValidationError) as failure:
            encode_value(
                VectorValue(values=tuple(values), space_ref=1, dtype="float32")
            )
        assert failure.value.details["position"] == position
        assert failure.value.details["limit"] == MAX_FLOAT32


@pytest.mark.parametrize("dtype", ["float32", "float64"])
def test_an_empty_vector_is_refused_by_the_dimension_rule_before_the_block_guards(
    dtype: str,
) -> None:
    """Differential regression: the block guards never see an empty vector, in either dtype.

    The dimension rule answers first, exactly as it did before the guards were asked of the
    whole vector at once, so ``min`` and ``max`` are never asked about zero components.
    """
    with pytest.raises(GrafxVectorValidationError) as empty:
        encode_value(VectorValue(values=(), space_ref=1, dtype=dtype))
    assert empty.value.details["field"] == "dimension"
    assert empty.value.details["value"] == 0
    assert "at least one component" in str(empty.value)


def test_the_dimension_and_space_reference_rules_come_before_the_components() -> None:
    with pytest.raises(GrafxVectorValidationError) as unassigned:
        encode_value(VectorValue(values=(math.nan,), space_ref=0))
    assert unassigned.value.details["field"] == "space_ref"


def test_the_public_constructor_still_normalises_and_refuses_what_it_always_did() -> (
    None
):
    assert VectorValue(values=(1, 2, 3), space_ref=1).values == (1.0, 2.0, 3.0)
    assert all(
        type(c) is float for c in VectorValue(values=[1, 2.5], space_ref=1).values
    )
    assert VectorValue(values=range(3), space_ref=1).values == (0.0, 1.0, 2.0)
    with pytest.raises(GrafxVectorValidationError) as text:
        VectorValue(values="abc", space_ref=1)  # type: ignore[arg-type]
    assert text.value.details["field"] == "values"
    with pytest.raises(GrafxVectorValidationError) as letters:
        VectorValue(values=("a", "b"), space_ref=1)  # type: ignore[arg-type]
    assert letters.value.details["field"] == "values"
    with pytest.raises(GrafxVectorValidationError) as dtype:
        VectorValue(values=(1.0,), space_ref=1, dtype="float16")
    assert dtype.value.details["field"] == "dtype"
    with pytest.raises(GrafxVectorValidationError) as reference:
        VectorValue(values=(1.0,), space_ref=True)  # type: ignore[arg-type]
    assert reference.value.details["field"] == "space_ref"
