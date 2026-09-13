"""Projection/list/path static refusals carry native cause and phase evidence."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import compile_error, native_error


@pytest.mark.parametrize("query,reason,detail", [
    ("RETURN 1 AS a,2 AS a", "column_name_conflict", "ColumnNameConflict"),
    ("RETURN 1,1", "column_name_conflict", "ColumnNameConflict"),
    ("RETURN 1 AS `2`,2", "column_name_conflict", "ColumnNameConflict"),
    ("WITH 1 AS a,2 AS a RETURN a", "column_name_conflict", "ColumnNameConflict"),
    ("WITH 1 AS a WITH *,2 AS a RETURN a", "column_name_conflict", "ColumnNameConflict"),
    ("MATCH(n) WITH n,count(*) RETURN n", "no_expression_alias", "NoExpressionAlias"),
    ("WITH 1+2 RETURN 1", "no_expression_alias", "NoExpressionAlias"),
    ("MATCH(n) RETURN [x IN [1,2] | count(*)]", "invalid_aggregation_context", "InvalidAggregation"),
    ("RETURN [x IN [1,2] WHERE count(*)>0 | x]", "invalid_aggregation_context", "InvalidAggregation"),
    ("RETURN reduce(a=0,x IN [1,2] | a+count(*))", "invalid_aggregation_context", "InvalidAggregation"),
    ("MATCH p=()-[*]->() RETURN size(p)", "size_path_argument_type", "InvalidArgumentType"),
    ("MATCH p=()-[:R*1..2]->() WITH p AS q RETURN size(q)", "size_path_argument_type", "InvalidArgumentType"),
])
def test_static_refusal_precedes_writes_and_transaction_remains_usable(tmp_path, query, reason, detail):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Before)")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("CREATE(:MustNotExist) WITH 1 AS barrier " + query)
            assert failure.value.details["reason"] == reason
            assert failure.value.details["query_phase"] == "planning"
            mapped = compile_error(failure.value)
            assert (mapped.type, mapped.phase, mapped.detail) == ("SyntaxError", "compile time", detail)
            tx.execute("CREATE(:After)")
        assert db.execute("MATCH(n:MustNotExist) RETURN count(*)").rows == ((0,),)
        assert db.verify("all").findings == ()
    with connect(path) as db:
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((2,),)


@pytest.mark.parametrize("query,expected", [
    ("RETURN 1 AS a,2 AS b", ((1,2),)),
    ("WITH 1 AS a WITH a RETURN a", ((1,),)),
    ("RETURN size('abc'),size([1,2]),size(null)", ((3,2,None),)),
    ("UNWIND [1,2] AS n RETURN [x IN collect(n) | x+1] AS values", (((2,3),),)),
])
def test_valid_neighboring_operations_are_unchanged(query, expected):
    with connect(":memory:") as db:
        assert db.execute(query).rows == expected


@pytest.mark.parametrize("field,value,reason", [
    ("alias","a","column_name_conflict"),
    ("item","count(*)","no_expression_alias"),
    ("iteration",None,"invalid_aggregation_context"),
    ("function","SIZE","size_path_argument_type"),
])
@pytest.mark.parametrize("mutation", ["missing_phase", "wrong_phase", "wrong_field", "wrong_reason"])
def test_mapping_does_not_invent_static_evidence(field, value, reason, mutation):
    details = dict(field=field,value=value,reason=reason,query_phase="planning")
    if mutation == "missing_phase":
        details.pop("query_phase")
    elif mutation == "wrong_phase":
        details["query_phase"] = "execution"
    elif mutation == "wrong_field":
        details["field"] = "unrelated"
    else:
        details["reason"] = "unrelated"
    assert compile_error(GrafxPlanError("Refused", **details)).detail == "plan_error"
    assert native_error(GrafxPlanError("Refused", **details)).detail == "plan_error"


@pytest.mark.parametrize("query,detail", [
    ("RETURN 1 AS a,2 AS a", "ColumnNameConflict"),
    ("WITH 1 AS a,2 AS a RETURN a", "ColumnNameConflict"),
    ("MATCH(n) WITH n,count(*) RETURN n", "NoExpressionAlias"),
    ("MATCH(n) RETURN [x IN [1,2] | count(*)]", "InvalidAggregation"),
    ("MATCH p=()-[*]->() RETURN size(p)", "InvalidArgumentType"),
])
def test_original_refusal_crosses_native_backend_with_phase_intact(query, detail):
    from tools.tck_native import NativeScenarioBackend
    backend = NativeScenarioBackend()
    try:
        backend.admit({"steps":[]})
        observed = backend.execute(query, {}, control=False)
        assert (observed.error.type, observed.error.phase, observed.error.detail) == (
            "SyntaxError", "compile time", detail)
    finally:
        backend.close()
