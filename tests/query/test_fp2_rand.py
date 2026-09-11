"""Controlled nondeterminism through public queries, never probabilistic expectations."""

import pytest

from okto_grafx import connect
from okto_grafx.api import assembly
from okto_grafx.domain.query.effects import is_deterministic
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.scalars import scalar_type, scalar_value
from okto_grafx.domain.model.value import ValueType
from okto_grafx.errors import GrafxError, GrafxPlanError, GrafxUnsupportedOperation


@pytest.fixture
def draws(monkeypatch):
    values = []
    observed = []

    def source():
        assert values, "unexpected random draw"
        value = values.pop(0)
        observed.append(value)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(assembly, "_new_query_random_source", lambda: source)
    return values, observed


def test_native_signature_and_pure_evaluator_boundary():
    assert scalar_type("RAND") is ValueType.DOUBLE
    with pytest.raises(GrafxPlanError, match="execution source"):
        scalar_value("RAND")
    assert not is_deterministic(parse("RETURN rand()"))
    assert not is_deterministic(parse("CALL { RETURN [rand()] AS xs } RETURN xs"))
    assert is_deterministic(parse("RETURN 'rand()' AS text"))


def test_per_expression_per_row_and_per_execution_not_cached(draws):
    values, observed = draws
    values.extend([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    with connect(":memory:") as db:
        query = "UNWIND [1,2] AS n RETURN rand() AS a, rand() AS b"
        db.explain(query)
        assert observed == []
        assert db.execute(query).rows == ((0.1, 0.2), (0.3, 0.4))
        assert db.execute("RETURN rand() AS v").rows == ((0.5,),)
        assert db.execute("RETURN rand() AS v").rows == ((0.6,),)
    assert values == []


def test_alias_reuses_value_without_reexecuting_expression(draws):
    values, observed = draws
    values.extend([0.9, 0.1, 0.5])
    with connect(":memory:") as db:
        result = db.execute("UNWIND [1,2,3] AS n WITH rand() AS r RETURN r, r AS same ORDER BY r")
        assert result.rows == ((0.1, 0.1), (0.5, 0.5), (0.9, 0.9))
    assert len(observed) == 3


def test_predicate_common_subexpressions_are_not_memoized(draws):
    values, observed = draws
    values.extend([0.1, 0.8, 0.9, 0.2])
    with connect(":memory:") as db:
        result = db.execute("UNWIND [1,2] AS n WITH n WHERE rand() < 0.5 OR rand() > 0.5 RETURN n")
        # Row 1 short-circuits after .1; row 2 compares .8 then .9.
        assert result.rows == ((1,), (2,))
        assert observed == [0.1, 0.8, 0.9]
        assert db.execute("RETURN rand() AS v").rows == ((0.2,),)


def test_lazy_branches_zero_rows_and_postfix_do_not_draw_during_binding(draws):
    values, observed = draws
    values.append(0.3)
    with connect(":memory:") as db:
        assert db.execute("RETURN CASE WHEN false THEN rand() ELSE 1 END, coalesce(1, rand())").rows == ((1, 1),)
        assert db.execute("UNWIND [] AS n RETURN rand()").rows == ()
        assert observed == []
        assert db.execute("RETURN coalesce(null, {a: [rand()]}).a[0]").rows == ((0.3,),)
    assert observed == [0.3]


def test_order_limit_aggregation_and_union_placement(draws):
    values, observed = draws
    values.extend([0.8, 0.1, 0.4, 0.25, 0.75, 0.2, 0.6])
    with connect(":memory:") as db:
        assert db.execute("UNWIND [1,2,3] AS n RETURN rand() AS r ORDER BY r LIMIT 1").rows == ((0.1,),)
        assert observed == [0.8, 0.1, 0.4]
        assert db.execute("UNWIND [1,2] AS n RETURN sum(rand()) AS total").rows == ((1.0,),)
        assert db.execute("RETURN rand() AS r UNION ALL RETURN rand() AS r").rows == ((0.2,), (0.6,))


def test_distinct_random_occurrences_remain_distinct_in_group_results(draws):
    values, observed = draws
    values.extend([0.1, 0.9])
    with connect(":memory:") as db:
        result = db.execute("UNWIND [1] AS n RETURN rand() AS a, rand() AS b, count(*) AS c")
        assert result.rows == ((0.1, 0.9, 1),)
    assert observed == [0.1, 0.9]


def test_separate_aggregate_calls_and_nested_groups_do_not_merge_draws(draws):
    values, observed = draws
    values.extend([0.125, 0.25, 0.5, 0.75, 0.125, 0.75])
    with connect(":memory:") as db:
        assert db.execute("UNWIND [1,2] AS n RETURN sum(rand()) AS a, sum(rand()) AS b").rows == ((0.625, 1.0),)
        assert db.execute("UNWIND [1] AS n RETURN coalesce(rand(),1) AS a, coalesce(rand(),1) AS b, count(*)").rows == ((0.125, 0.75, 1),)
    assert len(observed) == 6


@pytest.mark.parametrize("invalid", [None, True, 1, -0.1, 1.0, float("nan"), float("inf"), RuntimeError("bad source")])
def test_bad_source_preserves_statement_rollback(draws, invalid):
    values, observed = draws
    values.append(invalid)
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, v DOUBLE, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1, v:0.25})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CREATE (:N {id:2, v:rand()})")
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)


@pytest.mark.parametrize("query", ["RETURN rand()", "RETURN CASE WHEN false THEN rand() ELSE 1 END",
                                  "CALL { RETURN rand() AS r } RETURN r",
                                  "RETURN 1 AS r UNION ALL RETURN rand() AS r"])
def test_views_reject_nondeterminism_before_definition_effects(draws, query):
    with connect(":memory:") as db:
        db.views.prepare()
        before = db.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxUnsupportedOperation, match="deterministic"):
            db.views.create("random_view", query=query)
        assert db.transactions.published_state().last_committed_lsn == before
        assert db.views.get("random_view") is None
    assert draws[1] == []


@pytest.mark.parametrize("query", ["RETURN rand(1)", "RETURN rand(DISTINCT 1)", "RETURN rand(*)",
                                  "CREATE INDEX random_key FOR (n:N) ON (rand())"])
def test_invalid_signatures_and_expression_indexes_refuse_without_draws(draws, query):
    with connect(":memory:") as db:
        with pytest.raises(GrafxError):
            db.execute(query)
    assert draws[1] == []


def test_written_random_value_survives_restart_without_redraw(draws, tmp_path):
    draws[0].append(0.125)
    path = tmp_path / "graph"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, v DOUBLE, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1, v:rand()})")
    with connect(path) as db:
        assert db.execute("MATCH (n:N) RETURN n.v").rows == ((0.125,),)
    assert draws[1] == [0.125]


def test_production_source_has_the_exact_range_and_type():
    with connect(":memory:") as db:
        result = db.execute("UNWIND range(1,20) AS n RETURN rand() AS r")
    assert len(result.rows) == 20
    assert all(type(row[0]) is float and 0 <= row[0] < 1 for row in result.rows)
