"""Exact canonical JSON decimal components, independent of mutable host contexts."""

from decimal import localcontext, InvalidOperation
import json

import pytest

from okto_grafx import DecimalValue
from okto_grafx.domain.model.decimal_interchange import decimal_json_value, decimal_from_json_value
from okto_grafx.domain.model.errors import SchemaMismatchError


@pytest.mark.parametrize("value", (DecimalValue(0, 1, 0), DecimalValue(0, 38, 38),
    DecimalValue(10**38-1, 38, 0), DecimalValue(-(10**38-1), 38, 38), DecimalValue(12300, 12, 4)))
def test_canonical_tag_retains_all_coordinates_without_host_decimal_context(value):
    with localcontext() as context:
        context.prec = 1
        context.traps[InvalidOperation] = True
        tagged = decimal_json_value(value)
        assert tagged == {"type": "decimal", "coefficient": str(value.coefficient),
                          "precision": value.precision, "scale": value.scale}
        decoded = decimal_from_json_value(json.loads(json.dumps(tagged)))
        assert decoded == value and decoded is not value
        assert type(tagged["coefficient"]) is str


@pytest.mark.parametrize("change", (
    {"type": "DECIMAL"}, {"type": "double"}, {"extra": 1}, {"coefficient": 123},
    {"coefficient": True}, {"coefficient": None}, {"coefficient": "-0"}, {"coefficient": "+1"},
    {"coefficient": "01"}, {"coefficient": "1.0"}, {"coefficient": "1e2"}, {"coefficient": " 1"},
    {"coefficient": "١"}, {"coefficient": "9" * 10000}, {"precision": True}, {"precision": 0},
    {"precision": 39}, {"precision": 1.0}, {"precision": "12"}, {"precision": None},
    {"scale": True}, {"scale": -1}, {"scale": 13}, {"scale": 1.0},
    {"coefficient": "1" + "0" * 12}, {"coefficient": {"x": 1}},
))
def test_rejects_noncanonical_or_lossy_tagged_fields(change):
    value = {"type": "decimal", "coefficient": "12300", "precision": 12, "scale": 4, **change}
    with pytest.raises(SchemaMismatchError) as error:
        decimal_from_json_value(value)
    assert error.value.details["reason"] == "decimal_transport"


@pytest.mark.parametrize("value", (None, [], 1.23, "1.23", {}, {"type": "decimal"}))
def test_no_inference_from_untyped_shapes(value):
    with pytest.raises(SchemaMismatchError):
        decimal_from_json_value(value)


def test_forged_native_value_refuses_json_encoding():
    value = DecimalValue(1, 3, 0)
    object.__setattr__(value, "coefficient", 10**38)
    with pytest.raises(SchemaMismatchError):
        decimal_json_value(value)
