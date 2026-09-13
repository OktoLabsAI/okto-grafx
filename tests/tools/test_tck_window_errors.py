"""Window mappings require structured native cause and proven raise-site phase."""

import pytest

from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import compile_error, native_error


@pytest.mark.parametrize("field", ["skip", "limit"])
@pytest.mark.parametrize("reason,detail", [("window_argument_type", "InvalidArgumentType"),
                                          ("negative_window", "NegativeIntegerArgument"),
                                          ("non_constant_window", "NonConstantExpression")])
@pytest.mark.parametrize("phase", ["planning", "execution"])
def test_window_mapping_evidence(field, reason, detail, phase):
    error = GrafxPlanError("Window refusal", field=field, reason=reason, query_phase=phase)
    result = native_error(error)
    if reason == "non_constant_window" and phase == "execution":
        assert result.detail != detail
    else:
        assert (result.type, result.phase, result.detail) == ("SyntaxError", "compile time" if phase == "planning" else "runtime", detail)
    if phase == "planning":
        assert compile_error(error) == result


@pytest.mark.parametrize("overrides", [{"field":"expression"}, {"reason":"other"}, {"query_phase":None}])
@pytest.mark.parametrize("mapper", [compile_error, native_error])
def test_window_mapping_does_not_guess(mapper, overrides):
    fields = {"field":"skip", "reason":"negative_window", "query_phase":"planning"}
    assert mapper(GrafxPlanError("Window refusal", **(fields | overrides))).detail != "NegativeIntegerArgument"
