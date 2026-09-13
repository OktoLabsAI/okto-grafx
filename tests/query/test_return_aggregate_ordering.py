"""Grouped RETURN orders projected values without reopening discarded input rows."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import compile_error


@pytest.fixture(params=[None,32768])
def db(tmp_path, request):
    with connect(tmp_path / "db", query_memory_budget_bytes=request.param) as graph:
        with graph.begin("write") as tx:
            tx.execute("UNWIND [1,1,2,3,3,3] AS v CREATE(:N {v:v,other:100-v})")
        yield graph


@pytest.mark.parametrize("sort", ["n.v+count(*)", "v+count(*)", "$offset+v+count(*)"])
def test_composed_projected_aggregate_sorts_real_groups(db, sort):
    result = db.execute(f"MATCH(n:N) RETURN n.v AS v,count(*) AS c ORDER BY {sort} DESC,v", {"offset":7})
    assert result.rows == ((3,3),(1,2),(2,1))


@pytest.mark.parametrize("distinct", ["", "DISTINCT "])
def test_unaliased_aggregate_columns_and_shared_subexpressions(db, distinct):
    result = db.execute(f"MATCH(n:N) RETURN {distinct}n.v,count(*) ORDER BY n.v+count(*) DESC,n.v")
    assert result.columns == ("n.v", "count(*)")
    assert result.rows == ((3,3),(1,2),(2,1))


def test_empty_group_orders_null_aggregate_with_parameters(db):
    assert db.execute("MATCH(n:Missing) RETURN avg(n.v) AS a ORDER BY $x+avg(n.v)-1000", {"x":38}).rows == ((None,),)


def test_distinct_property_composition_does_not_need_input_bindings(db):
    assert db.execute("MATCH(n:N) RETURN DISTINCT n.v ORDER BY -n.v").rows == ((3,),(2,),(1,))


@pytest.mark.parametrize("projection,sort", [
    ("count(*) AS c", "n.other+count(*)"),
    ("DISTINCT n.v", "n.other"),
    ("n.v AS v,count(*) AS c", "n.other+count(*)"),
])
def test_discarded_input_property_has_undefined_variable_evidence(db, projection, sort):
    with pytest.raises(GrafxPlanError) as failure:
        db.execute(f"MATCH(n:N) RETURN {projection} ORDER BY {sort}")
    assert compile_error(failure.value).detail == "UndefinedVariable"


def test_exact_aggregate_projection_retains_shadowing_admission(db):
    assert db.execute("MATCH(n:N) RETURN DISTINCT count(n) AS n ORDER BY count(n)").rows == ((6,),)


def test_remaining_new_aggregate_is_not_silently_computed(db):
    with pytest.raises(GrafxPlanError) as failure:
        db.execute("MATCH(n:N) RETURN count(*) AS c ORDER BY sum(c)")
    assert compile_error(failure.value).detail == "InvalidAggregation"


def test_grouping_ambiguity_is_checked_before_projection_substitution(db):
    with pytest.raises(GrafxPlanError) as failure:
        db.execute("MATCH(n:N) RETURN n.v+n.other,count(*) AS c ORDER BY n.v+n.other+count(*)")
    assert compile_error(failure.value).detail == "AmbiguousAggregationExpression"


@pytest.mark.parametrize("distinct", ["", "DISTINCT "])
def test_alias_type_proof_uses_output_and_preserves_local_shadowing(db, distinct):
    result = db.execute(
        f"UNWIND [1,2,3] AS n RETURN {distinct}-n AS n "
        "ORDER BY n+reduce(n=0,x IN [1,2] | n+x)"
    )
    assert result.rows == ((-3,),(-2,),(-1,))


@pytest.mark.parametrize("distinct", ["", "DISTINCT "])
def test_map_alias_proof_does_not_reopen_input_scope(db, distinct):
    result = db.execute(
        f"MATCH(n:N) RETURN {distinct}{{v:n.v}} AS n ORDER BY -n.v"
    )
    assert [row[0]["v"] for row in result.rows] == ([3,2,1] if distinct else [3,3,3,2,1,1])


@pytest.mark.parametrize("distinct", ["", "DISTINCT "])
def test_type_proof_does_not_evaluate_projected_random_again(db, monkeypatch, distinct):
    from okto_grafx.api import assembly
    draws = []
    def draw():
        draws.append(1)
        return 0.5
    monkeypatch.setattr(assembly, "_new_query_random_source", lambda: draw)
    # The fixture handle predates the patch: create a handle with this random source.
    with connect(":memory:") as graph:
        query = f"RETURN {distinct}rand() AS r ORDER BY r+1"
        graph.explain(query)
        assert draws == []
        assert graph.execute(query).rows == ((0.5,),)
        assert draws == [1]


def test_late_grouped_sort_failure_rolls_back_only_current_statement(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(:Earlier)")
        with pytest.raises(GrafxPlanError) as failure:
            tx.execute(
                "UNWIND [1,2] AS v CREATE(:Aborted {v:v}) "
                "RETURN v,count(*) AS c ORDER BY c/(v-2)"
            )
        assert failure.value.details["field"] == "operator"
        tx.execute("CREATE(:Later)")
    assert db.execute("MATCH(n:Aborted) RETURN count(*)").rows == ((0,),)
    assert db.execute("MATCH(n:Earlier) RETURN count(*)").rows == ((1,),)
    assert db.execute("MATCH(n:Later) RETURN count(*)").rows == ((1,),)
    assert db.verify("all").findings == ()
