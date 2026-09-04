"""Fast public snapshots for capability-free float sequences."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.model.value import MAX_VALUE_DEPTH
from okto_grafx.domain.query.limits import MAX_LIST_ELEMENTS
from okto_grafx.engine.public_views import _query_value_snapshot


def _snapshot(value: object, *, depth: int = 0):
    return _query_value_snapshot(
        value,
        field="parameters.vector",
        depth=depth,
        active=set(),
    )


@pytest.mark.parametrize("values", [(0.0, -0.0, 1.25), ()])
def test_exact_float_tuple_is_already_an_owned_capability_free_snapshot(
    values: tuple[float, ...],
) -> None:
    observed = _snapshot(values)

    assert observed is values


def test_exact_float_list_is_copied_once_and_never_retained() -> None:
    values = [0.0, -0.0, 1.25]

    observed = _snapshot(values)
    values.append(9.0)

    assert type(observed) is tuple
    assert observed == (0.0, -0.0, 1.25)


@pytest.mark.parametrize("foreign", [True, 1, 1.0])
def test_nonexact_float_components_keep_the_recursive_canonicalizer(
    foreign: object,
) -> None:
    class FloatSubclass(float):
        pass

    item = FloatSubclass(1.0) if type(foreign) is float else foreign
    values = (0.0, item, 2.0)

    observed = _snapshot(values)

    assert observed is not values
    assert type(observed) is tuple
    assert type(observed[1]) is type(foreign)


def test_exact_float_sequence_does_not_bypass_length_or_depth_bounds() -> None:
    values = (0.0,) * (MAX_LIST_ELEMENTS + 1)

    with pytest.raises(GrafxConfigurationError) as length_error:
        _snapshot(values)
    assert length_error.value.details == {
        "field": "parameters.vector",
        "value": MAX_LIST_ELEMENTS + 1,
        "limit": MAX_LIST_ELEMENTS,
    }

    with pytest.raises(GrafxConfigurationError) as depth_error:
        _snapshot((0.0,), depth=MAX_VALUE_DEPTH + 1)
    assert depth_error.value.details == {
        "field": "parameters.vector",
        "value": MAX_VALUE_DEPTH + 1,
        "limit": MAX_VALUE_DEPTH,
    }
