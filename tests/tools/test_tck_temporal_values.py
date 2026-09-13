"""TCK's quoted temporal notation is an output representation, not engine coercion."""

import pytest

from tools.tck_native import _reference_result_value
from tools.tck_values import reference_value
from tests.storage_core.test_temporal_admission import VALUES


@pytest.mark.parametrize("value", VALUES)
def test_exact_native_temporal_is_rendered_to_tck_text(value):
    text = value.isoformat()
    assert _reference_result_value(value) == reference_value(repr(text))
    assert _reference_result_value({"x": [value]}) == {"x": (text,)}


def test_no_arbitrary_host_formatting_or_expected_expression_execution():
    class Host:
        def isoformat(self):
            raise AssertionError("No duck-typed temporal formatter")
    host = Host()
    assert _reference_result_value(host) is host
    with pytest.raises(ValueError):
        reference_value("date('2000-01-01')")
