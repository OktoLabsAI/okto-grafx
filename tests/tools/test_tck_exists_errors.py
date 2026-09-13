"""EXISTS composition mapping uses native diagnostic fields, not messages."""

import pytest

from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import compile_error


@pytest.mark.parametrize("reason", ["existential_write", "existential_return_mismatch"])
def test_explicit_existential_composition_reason(reason):
    found = compile_error(GrafxPlanError("arbitrary", field="subquery", reason=reason, query_phase="planning"))
    assert (found.type,found.phase,found.detail) == ("SyntaxError","compile time","InvalidClauseComposition")
    for details in ({"field":"subquery", "reason":reason},
                    {"field":"other", "reason":reason, "query_phase":"planning"}):
        assert compile_error(GrafxPlanError("EXISTS subqueries cannot write", **details)).detail != "InvalidClauseComposition"
