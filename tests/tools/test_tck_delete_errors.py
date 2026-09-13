"""Only explicit native DELETE reason/phase contracts become reference errors."""

import pytest

from okto_grafx.errors import GrafxPlanError, GrafxQueryError
from tools.tck_errors import compile_error, native_error


@pytest.mark.parametrize("reason,phase,kind,detail", [
    ("delete_argument_type","planning","SyntaxError","InvalidArgumentType"),
    ("delete_argument_type","execution","TypeError","InvalidArgumentType"),
    ("invalid_delete","planning","SyntaxError","InvalidDelete"),
])
def test_delete_error_requires_native_reason_phase_and_field(reason, phase, kind, detail):
    error = GrafxPlanError("not a diagnostic oracle",field="target",reason=reason,query_phase=phase)
    found = native_error(error)
    assert (found.type,found.phase,found.detail) == (kind,"compile time" if phase == "planning" else "runtime",detail)
    if phase == "planning":
        assert compile_error(error) == found
    for details in ({"field":"target","reason":reason},
                    {"field":"not_target","reason":reason,"query_phase":phase},
                    {"field":"target","reason":"different","query_phase":phase}):
        assert native_error(GrafxPlanError("DELETE requires a node",**details)).phase == "unknown"


def test_connected_node_error_requires_runtime_and_both_tables():
    details = dict(reason="connected_node_delete",query_phase="execution",table="N",relationship_table="R")
    found = native_error(GrafxQueryError("arbitrary",**details))
    assert (found.type,found.phase,found.detail) == ("ConstraintVerificationFailed","runtime","DeleteConnectedNode")
    for key in details:
        assert native_error(GrafxQueryError("DELETE cannot remove",**{k:v for k,v in details.items() if k!=key})).phase == "unknown"
