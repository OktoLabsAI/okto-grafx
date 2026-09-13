"""Aggregate inputs are stable expressions; WITH may materialize volatile row values."""

import pytest

from okto_grafx import connect
from okto_grafx.api import assembly
from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import compile_error


@pytest.mark.parametrize("aggregate", ["count", "sum", "avg", "min", "max", "collect"])
@pytest.mark.parametrize("argument", ["rand()", "1+rand()", "coalesce(1,rand())", "CASE WHEN false THEN rand() ELSE 1 END"])
def test_volatile_aggregate_argument_is_refused_before_random_draw(monkeypatch, aggregate, argument):
    def forbidden():
        pytest.fail("Planning an illegal aggregate must not consume randomness")
    monkeypatch.setattr(assembly, "_new_query_random_source", lambda: forbidden)
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(f"RETURN {aggregate}({argument})")
        assert failure.value.details["reason"] == "non_deterministic_aggregate_argument"
        assert failure.value.details["query_phase"] == "planning"
        assert compile_error(failure.value).detail == "NonConstantExpression"


def test_volatile_argument_refusal_preserves_earlier_instructions(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Earlier)")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("CREATE(:Discarded) RETURN count(rand())")
            assert failure.value.details["reason"] == "non_deterministic_aggregate_argument"
            tx.execute("CREATE(:Later)")
        assert db.execute("MATCH(n) RETURN labels(n) ORDER BY labels(n)").rows == ((("Earlier",),), (("Later",),))
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("overrides", [{"field":"function"}, {"reason":"other"}, {"query_phase":None}, {"query_phase":"execution"}])
def test_mapping_requires_native_planning_evidence(overrides):
    fields = {"field":"aggregation", "reason":"non_deterministic_aggregate_argument", "query_phase":"planning"}
    assert compile_error(GrafxPlanError("Aggregate refusal", **(fields | overrides))).detail != "NonConstantExpression"


def test_parameters_and_materialized_values_remain_legal():
    with connect(":memory:") as db:
        assert db.execute("UNWIND $values AS v RETURN sum(v)", {"values":[1,2,3]}).rows == ((6,),)
        assert db.execute("UNWIND [1,2] AS v WITH v+1 AS r RETURN collect(r)").rows == (((2,3),),)
