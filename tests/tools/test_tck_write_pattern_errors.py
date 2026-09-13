"""Write-pattern diagnostics have explicit reasons and phases, not message matching."""

import pytest

from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import compile_error, native_error


@pytest.mark.parametrize("field,reason,detail", [
    ("types","no_single_relationship_type","NoSingleRelationshipType"),
    ("hops","creating_variable_length","CreatingVarLength"),
    ("direction","requires_directed_relationship","RequiresDirectedRelationship"),
])
def test_pattern_compile_error_requires_all_diagnostic_fields(field,reason,detail):
    error=GrafxPlanError("arbitrary text",field=field,reason=reason,query_phase="planning")
    found=compile_error(error)
    assert (found.type,found.phase,found.detail)==("SyntaxError","compile time",detail)
    for details in ({"field":field,"reason":reason}, {"field":"other","reason":reason,"query_phase":"planning"}):
        assert compile_error(GrafxPlanError("requires exactly one type",**details)).detail != detail


def test_merge_null_runtime_error_does_not_infer_phase_from_message():
    error=GrafxPlanError("arbitrary text",field="properties",reason="merge_null_property",query_phase="execution")
    found=native_error(error)
    assert (found.type,found.phase,found.detail)==("SemanticError","runtime","MergeReadOwnWrites")
    assert native_error(GrafxPlanError("MERGE cannot match a null property",field="properties")).phase=="unknown"
