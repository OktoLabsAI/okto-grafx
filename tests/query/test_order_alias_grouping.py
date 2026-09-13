"""Returned aliases and native grouping refusals, including empty input and rollback."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import compile_error


@pytest.mark.parametrize("distinct", ["", "DISTINCT "])
@pytest.mark.parametrize("budget", [None, 32768])
def test_return_entity_alias_properties_ordered_without_reopening_source(tmp_path, distinct, budget):
    with connect(tmp_path / "db", query_memory_budget_bytes=budget) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:A)-[:R {v:3}]->(:B),(:A)-[:R {v:1}]->(:B)")
        result = db.execute(f"MATCH()-[r:R]->() RETURN {distinct}r AS edge ORDER BY edge.v")
        assert tuple(row[0].properties["v"] for row in result.rows) == (1,3)


def test_return_scalar_alias_in_compound_sort_expression():
    with connect(":memory:") as db:
        assert db.execute("UNWIND [1,2,3] AS n RETURN n+10 AS x ORDER BY -x").rows == ((13,), (12,), (11,))
        assert db.execute("UNWIND [1,2,3] AS n RETURN -n AS n ORDER BY n+1").rows == ((-3,), (-2,), (-1,))


@pytest.mark.parametrize("query", [
    "MATCH(n:N) RETURN n.v+count(*)",
    "MATCH(a:N),(b:N) RETURN a.v+b.v, a.v+b.v+count(*)",
    "MATCH(a:N),(b:N) WITH a.v+b.v, count(*) AS n ORDER BY a.v+b.v+count(*) RETURN *",
    "MATCH(a:N),(b:N) RETURN a.v+b.v, count(*) AS n ORDER BY a.v+b.v+count(*)",
])
def test_ungrouped_leaves_have_proven_ambiguity_before_execution(query):
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(query)
        assert failure.value.details["reason"] == "ambiguous_aggregation_expression"
        assert compile_error(failure.value).detail == "AmbiguousAggregationExpression"


@pytest.mark.parametrize("query", [
    "UNWIND [1,1,2] AS n RETURN n,n+count(*) AS x ORDER BY n",
    "UNWIND [1,1,2] AS n WITH n,n+count(*) AS x RETURN n,x ORDER BY n",
    "UNWIND [1,1,2] AS n WITH *,n+count(*) AS x RETURN n,x ORDER BY n",
])
def test_explicit_grouping_leaf_is_allowed(query):
    with connect(":memory:") as db:
        assert db.execute(query).rows == ((1,3), (2,3))


def test_grouped_with_missing_input_precedes_new_aggregate_refusal():
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute("UNWIND [1,2] AS n WITH min(n) AS m ORDER BY sum(n) RETURN m")
        assert failure.value.details["reason"] == "undefined_variable"
        assert compile_error(failure.value).detail == "UndefinedVariable"


def test_nested_aggregate_native_cause_is_proven():
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute("RETURN count(count(*))")
        assert failure.value.details["reason"] == "nested_aggregation"
        assert compile_error(failure.value).detail == "NestedAggregation"


def test_ambiguity_rejection_preserves_earlier_writes(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Earlier)")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("CREATE(n:N {v:1}) RETURN n.v+count(*)")
            assert failure.value.details["reason"] == "ambiguous_aggregation_expression"
            tx.execute("CREATE(:Later)")
        assert db.execute("MATCH(n) RETURN labels(n) ORDER BY labels(n)").rows == ((("Earlier",),), (("Later",),))
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("overrides", [{"field":"item"}, {"reason":"other"}, {"query_phase":"execution"}, {"query_phase":None}])
def test_ambiguity_error_mapping_requires_proven_native_cause(overrides):
    fields = {"field":"aggregation", "reason":"ambiguous_aggregation_expression", "query_phase":"planning"}
    assert compile_error(GrafxPlanError("Refusal", **(fields | overrides))).detail != "AmbiguousAggregationExpression"
