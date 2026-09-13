"""Procedure diagnostics require native field, reason and phase evidence."""

import pytest

from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import compile_error, native_error


@pytest.mark.parametrize("field,reason,kind,detail", (
    ("procedure", "procedure_not_found", "ProcedureError", "ProcedureNotFound"),
    ("procedure_arity", "procedure_arity", "SyntaxError", "InvalidNumberOfArguments"),
    ("procedure_arguments", "procedure_argument_mode", "SyntaxError", "InvalidArgumentPassingMode"),
    ("procedure_type", "procedure_argument_type", "SyntaxError", "InvalidArgumentType"),
    ("parameter", "missing_parameter", "ParameterMissing", "MissingParameter"),
    ("yield", "variable_already_bound", "SyntaxError", "VariableAlreadyBound"),
    ("yield", "procedure_yield_mode", "SyntaxError", "UnexpectedSyntax"),
))
def test_procedure_mapping_requires_all_native_evidence(field, reason, kind, detail):
    evidence = dict(field=field, reason=reason, query_phase="planning")
    error = GrafxPlanError("not an oracle", **evidence)
    found = native_error(error)
    assert (found.type, found.phase, found.detail) == (kind, "compile time", detail)
    assert compile_error(error) == found
    for key in evidence:
        forged = {name: value for name, value in evidence.items() if name != key}
        assert native_error(GrafxPlanError("Procedure not found", **forged)).phase == "unknown"
    assert native_error(GrafxPlanError("not an oracle", **(evidence | {"query_phase": "execution"}))).phase == "unknown"
