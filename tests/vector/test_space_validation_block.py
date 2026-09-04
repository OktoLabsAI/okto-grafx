"""The write door validates a vector as one block and answers exactly as it did (VEC-3).

``validate_components`` now asks its two guards -- every component finite, every component of a
float32 space inside the range a slot can hold -- of the whole vector at once, and rounds the
vector to the storage dtype in one ``struct`` round trip. What is held here: the stored tuple is
equal, component by component, to the per-component rule ``round_to_storage_dtype`` documents,
on ordinary corpora and on the values that sit at the edges of float32; a refusal names the
FIRST offending position with the reason it always named, whichever guard the offender trips and
wherever it sits; and the query door keeps its own rule, finiteness only.
"""

from __future__ import annotations

import math
import struct

import pytest

from okto_grafx.domain.errors import GrafxVectorValidationError
from okto_grafx.domain.model.schema import EmbeddingSpaceDef
from okto_grafx.domain.model.value import FLOAT32_OVERFLOW_THRESHOLD, MAX_FLOAT32
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.vector.space import (
    STORAGE_DTYPE_FLOAT32,
    STORAGE_DTYPE_FLOAT64,
    round_to_storage_dtype,
    validate_components,
    validate_query_components,
)

from .conftest import seeded_vectors

EDGE_VALUES: tuple[float, ...] = (
    0.0,
    -0.0,
    1.0,
    -1.0,
    1e-45,
    -1e-45,
    1.5e-45,
    1e-39,
    3.4e38,
    MAX_FLOAT32,
    -MAX_FLOAT32,
    math.nextafter(MAX_FLOAT32, math.inf),
    math.nextafter(FLOAT32_OVERFLOW_THRESHOLD, 0.0),
    -math.nextafter(FLOAT32_OVERFLOW_THRESHOLD, 0.0),
    0.1,
    1.0 / 3.0,
    123456789.123456789,
    2.0**-149,
    2.0**-150,
)
"""Values at the edges of float32: subnormals, the largest finite, and the band that rounds down."""


def _space(
    dimension: int, dtype: str, *, normalized: bool = False
) -> EmbeddingSpaceDef:
    return EmbeddingSpaceDef(
        space_id=1,
        name="block",
        dimension=dimension,
        metric=DistanceMetric.COSINE,
        normalized=normalized,
        storage_dtype=dtype,
    )


def _reference(
    space: EmbeddingSpaceDef, values: tuple[float, ...]
) -> tuple[float, ...]:
    """The per-component rule the module documents, applied component by component."""
    return tuple(
        round_to_storage_dtype(component, space.storage_dtype) for component in values
    )


def _expected_refusal(values: tuple[float, ...], single: bool) -> tuple[int, str]:
    """Return the position and reason the first offending component earns, as the rule reads."""
    for position, component in enumerate(values):
        if not math.isfinite(component):
            return position, "non_finite"
        if (
            single
            and not -FLOAT32_OVERFLOW_THRESHOLD < component < FLOAT32_OVERFLOW_THRESHOLD
        ):
            return position, "float32_range"
    raise AssertionError("the case has no offending component")


@pytest.mark.parametrize("dtype", [STORAGE_DTYPE_FLOAT32, STORAGE_DTYPE_FLOAT64])
@pytest.mark.parametrize("dimension", [1, 7, 48, 768])
def test_the_block_rounding_equals_the_per_component_rule_on_a_seeded_corpus(
    dtype: str, dimension: int
) -> None:
    space = _space(dimension, dtype)
    for values in seeded_vectors(25, dimension, 0x3B10C + dimension):
        scaled = tuple(component * 1e3 for component in values)
        for vector in (values, scaled):
            stored = validate_components(space, vector)
            assert stored == _reference(space, vector)
            assert all(isinstance(component, float) for component in stored)
            assert struct.pack(f"<{dimension}f", *stored) == struct.pack(
                f"<{dimension}f", *vector
            )


@pytest.mark.parametrize("dtype", [STORAGE_DTYPE_FLOAT32, STORAGE_DTYPE_FLOAT64])
def test_the_block_rounding_equals_the_per_component_rule_at_the_edges_of_float32(
    dtype: str,
) -> None:
    space = _space(len(EDGE_VALUES), dtype)
    stored = validate_components(space, EDGE_VALUES)
    expected = _reference(space, EDGE_VALUES)
    assert [repr(component) for component in stored] == [
        repr(component) for component in expected
    ]
    if dtype == STORAGE_DTYPE_FLOAT32:
        assert (
            stored[EDGE_VALUES.index(math.nextafter(MAX_FLOAT32, math.inf))]
            == MAX_FLOAT32
        )
        assert stored[EDGE_VALUES.index(2.0**-150)] == 0.0
        assert math.copysign(1.0, stored[1]) == -1.0


@pytest.mark.parametrize(
    ("values", "single"),
    [
        ((math.nan, 1.0, 1.0), True),
        ((1.0, math.inf, 1.0), True),
        ((1.0, 1.0, -math.inf), True),
        ((1e39, 1.0, 1.0), True),
        ((1.0, -1e39, 1.0), True),
        ((1.0, 1.0, FLOAT32_OVERFLOW_THRESHOLD), True),
        ((1e39, math.nan), True),
        ((math.nan, 1e39), True),
        ((-1e39, math.inf), True),
        ((math.nan, 1.0), False),
        ((1e300, math.inf), False),
        ((1.0, 1.0, 1.0, -math.inf), False),
    ],
)
def test_a_refusal_names_the_first_offending_position_and_its_reason(
    values: tuple[float, ...], single: bool
) -> None:
    dtype = STORAGE_DTYPE_FLOAT32 if single else STORAGE_DTYPE_FLOAT64
    position, reason = _expected_refusal(values, single)
    with pytest.raises(GrafxVectorValidationError) as failure:
        validate_components(_space(len(values), dtype), values)
    assert failure.value.details["reason"] == reason
    assert failure.value.details["position"] == position
    assert failure.value.details["value"] == repr(values[position])
    assert failure.value.details["space"] == "block"


def test_a_float64_space_accepts_what_a_float32_space_refuses_by_range() -> None:
    values = (1e300, -1e300, 1e39)
    assert validate_components(_space(3, STORAGE_DTYPE_FLOAT64), values) == values
    with pytest.raises(GrafxVectorValidationError) as failure:
        validate_components(_space(3, STORAGE_DTYPE_FLOAT32), values)
    assert failure.value.details["reason"] == "float32_range"
    assert failure.value.details["position"] == 0


def test_a_negative_overflow_is_refused_wherever_it_sits() -> None:
    for position in (0, 3, 7):
        values = [1.0] * 8
        values[position] = -1e39
        with pytest.raises(GrafxVectorValidationError) as failure:
            validate_components(_space(8, STORAGE_DTYPE_FLOAT32), tuple(values))
        assert failure.value.details["reason"] == "float32_range"
        assert failure.value.details["position"] == position


def test_the_normalized_rule_is_still_applied_to_the_rounded_vector() -> None:
    space = _space(4, STORAGE_DTYPE_FLOAT32, normalized=True)
    unit = (0.5, 0.5, 0.5, 0.5)
    assert validate_components(space, unit) == unit
    with pytest.raises(GrafxVectorValidationError) as failure:
        validate_components(space, (1.0, 1.0, 1.0, 1.0))
    assert failure.value.details["reason"] == "not_normalized"


def test_the_query_door_checks_finiteness_only_and_names_the_first_position() -> None:
    space = _space(3, STORAGE_DTYPE_FLOAT32)
    assert validate_query_components(space, (1e300, 0.5, -1e39)) == (1e300, 0.5, -1e39)
    for values, position in (
        ((math.nan, 1.0, 1.0), 0),
        ((1.0, math.inf, math.nan), 1),
        ((1.0, 1.0, -math.inf), 2),
    ):
        with pytest.raises(GrafxVectorValidationError) as failure:
            validate_query_components(space, values)
        assert failure.value.details["reason"] == "non_finite"
        assert failure.value.details["position"] == position
        assert failure.value.details["value"] == repr(values[position])


def test_the_query_door_keeps_the_query_in_double_precision() -> None:
    space = _space(2, STORAGE_DTYPE_FLOAT32)
    query = (0.1, 1.0 / 3.0)
    assert validate_query_components(space, query) == query
    assert validate_components(space, query) != query
