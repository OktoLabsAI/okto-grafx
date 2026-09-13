"""Canonical primitive temporal coordinates, independently of Arrow/host clocks."""

import pytest

from okto_grafx.domain.model.temporal_interchange import (
    temporal_components, temporal_from_components, temporal_json_value, temporal_from_json_value,
)
from okto_grafx.errors import GrafxError
from tests.api.test_temporal_transfer import TYPES, VALUES


@pytest.mark.parametrize("name,value", zip(TYPES, VALUES))
def test_temporal_components_exact_owned_roundtrip(name, value):
    fields = temporal_components(value)
    decoded = temporal_from_components(name, fields)
    assert decoded == value and type(decoded) is type(value)
    fields["extra"] = "mutation"
    assert "extra" not in temporal_components(value)
    assert temporal_json_value(value)["type"] == name.lower()


@pytest.mark.parametrize("name,value", zip(TYPES, VALUES))
@pytest.mark.parametrize("fault", ["missing", "extra", "null", "bool", "float", "string"])
def test_temporal_coordinates_refuse_implicit_coercion_and_unknown_fields(name, value, fault):
    fields = temporal_components(value)
    first = next(iter(fields))
    if fault == "missing":
        fields.pop(first)
    elif fault == "extra":
        fields["extra"] = 1
    else:
        fields[first] = {"null": None, "bool": True, "float": 1., "string": "1"}[fault]
    with pytest.raises(GrafxError):
        temporal_from_components(name, fields)


@pytest.mark.parametrize("nanoseconds", [-1, 10**9, 2**31-1])
def test_duration_transport_requires_normalized_components(nanoseconds):
    with pytest.raises(GrafxError):
        temporal_from_components("DURATION", {"months": 0, "days": 0, "seconds": 1, "nanoseconds": nanoseconds})


@pytest.mark.parametrize("name,value", zip(TYPES, VALUES))
def test_tagged_json_roundtrip(name, value):
    assert temporal_from_json_value(name, temporal_json_value(value)) == value


@pytest.mark.parametrize("coordinate", ["-0", "00", "+1", " 1", "1.0", "1e1", "9" * 20, 1, True, None, "١"])
def test_tagged_json_wide_coordinate_must_be_canonical(coordinate):
    with pytest.raises(GrafxError):
        temporal_from_json_value("DATE", {"type": "date", "epoch_day": coordinate})
