"""Percentiles preserve numeric identity, grouping, bounded spill and rollback."""

import math

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import native_error


@pytest.fixture(params=[None, 65536])
def db(tmp_path, request):
    with connect(tmp_path / "db", query_memory_budget_bytes=request.param) as graph:
        yield graph


@pytest.mark.parametrize("fraction,disc,cont", [(0,10,10.0), (0.25,10,15.0), (0.5,20,20.0), (0.9,30,28.0), (1,30,30.0)])
def test_percentile_endpoints_and_interpolation(db, fraction, disc, cont):
    result = db.execute("UNWIND [30,10,null,20] AS v RETURN percentileDisc(v,$p),percentileCont(v,$p)", {"p":fraction})
    assert result.rows == ((disc,cont),)
    assert type(result.rows[0][0]) is int
    assert type(result.rows[0][1]) is float


def test_distinct_grouping_and_composed_results(db):
    assert db.execute("UNWIND [1,1,2,10] AS v RETURN percentileDisc(DISTINCT v,0.5),percentileCont(DISTINCT v,0.5)").rows == ((2,2.0),)
    assert db.execute("UNWIND [1,1,2,10] AS v RETURN percentileCont(v,0.5)").rows == ((1.5,),)
    assert db.execute("UNWIND [1,2,3,4] AS v WITH v%2 AS g,percentileCont(v,0.5) AS p RETURN g,p ORDER BY g").rows == ((0,3.0),(1,2.0))
    assert db.execute("CALL { UNWIND [1,2,3] AS v RETURN percentileCont(v,0.5) AS p } RETURN p+1").rows == ((3.0,),)


def test_repeated_percentile_expression_does_not_duplicate_samples(db):
    assert db.execute("UNWIND [10,20,30] AS v RETURN percentileCont(v,0.25) AS a,percentileCont(v,0.25)+1 AS b").rows == ((15.0,16.0),)
    assert db.execute("UNWIND [1,2,3] AS v RETURN sum(v) AS a,sum(v)+1 AS b,count(*) AS c,count(*)+1 AS d").rows == ((6,7,3,4),)
    assert db.execute("UNWIND [1,2,3] AS v WITH sum(v) AS a,sum(v)+1 AS b RETURN a,b").rows == ((6,7),)


@pytest.mark.parametrize("values", [[], [None], [None,None]])
def test_empty_and_null_samples_return_null(db, values):
    assert db.execute("UNWIND $vs AS v RETURN percentileDisc(v,0.5),percentileCont(v,0.5)", {"vs":values}).rows == ((None,None),)


@pytest.mark.parametrize("value", [-1,1.1,1000,float("nan")])
def test_fraction_bounds_have_runtime_evidence(db, value):
    with pytest.raises(GrafxPlanError) as failure:
        db.execute("RETURN percentileCont(1,$p)", {"p":value})
    assert failure.value.details["reason"] == "percentile_argument_bounds"
    assert native_error(failure.value).detail == "NumberOutOfRange"


@pytest.mark.parametrize("value", [None, True, "0.5", [0.5]])
def test_fraction_wrong_types_refuse(db, value):
    with pytest.raises(GrafxPlanError) as failure:
        db.execute("RETURN percentileDisc(1,$p)", {"p":value})
    assert failure.value.details["reason"] == "percentile_argument_type"


def test_arithmetic_extremes_and_nan_samples(db):
    assert db.execute("UNWIND [-1e308,1e308] AS v RETURN percentileCont(v,0.5)").rows == ((0.0,),)
    assert db.execute("UNWIND [9223372036854775806,9223372036854775807] AS v RETURN percentileDisc(v,1)").rows == ((9223372036854775807,),)
    result = db.execute("UNWIND [1.0,0.0/0.0] AS v RETURN percentileDisc(v,0),percentileCont(v,1)").rows[0]
    assert result[0] == 1.0 and math.isnan(result[1])


def test_late_invalid_fraction_rolls_back_all_statement_groups(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(:Earlier)")
        with pytest.raises(GrafxPlanError) as failure:
            tx.execute("UNWIND [0.5,2] AS p CREATE(:N {v:p}) RETURN p,percentileDisc(1,p)")
        assert failure.value.details["reason"] == "percentile_argument_bounds"
        tx.execute("CREATE(:Later)")
    assert db.execute("MATCH(n) RETURN labels(n) ORDER BY labels(n)").rows == ((("Earlier",),), (("Later",),))
    assert db.verify("all").findings == ()


def test_large_bounded_sample_uses_external_percentile_sort(tmp_path, monkeypatch):
    from okto_grafx.engine import query_engine
    sizes = []
    original = query_engine._SpilledAggregateState._fold
    def observe(state, index, value, sort_key):
        result = original(state,index,value,sort_key)
        if state._accumulators[index]._function in query_engine.PERCENTILE_FUNCTIONS:
            sizes.append(len(state._accumulators[index]._values))
        return result
    monkeypatch.setattr(query_engine._SpilledAggregateState, "_fold", observe)
    with connect(tmp_path / "db", query_memory_budget_bytes=32768) as graph:
        assert graph.execute("UNWIND range(1,500) AS v RETURN percentileDisc(v,0.5),percentileCont(v,0.5)").rows == ((250,250.5),)
    assert sizes and max(sizes) == 0


@pytest.mark.parametrize("call", ["percentileCont(1)", "percentileDisc(1,0.5,2)", "percentileCont(*)",
                                  "percentileDisc(1,0.5, extra => 1)", "percentileCont(rand(),0.5)", "percentileDisc(1,rand())"])
def test_percentile_signature_and_effect_refusals(call):
    with connect(":memory:") as graph:
        with pytest.raises(GrafxPlanError):
            graph.execute("RETURN " + call)


@pytest.mark.parametrize("overrides", [{"field":"other"}, {"reason":"other"}, {"query_phase":"planning"}, {"query_phase":None}])
def test_percentile_mapper_does_not_guess_missing_evidence(overrides):
    fields = {"field":"percentile", "reason":"percentile_argument_bounds", "query_phase":"execution"}
    assert native_error(GrafxPlanError("Percentile refusal", **(fields | overrides))).detail != "NumberOutOfRange"


def test_fraction_comes_from_first_non_null_sample_in_each_group(db):
    rows = [{"v":None,"p":9}, {"v":10,"p":0.5}, {"v":20,"p":1}, {"v":30,"p":0}]
    assert db.execute("UNWIND $rows AS row RETURN percentileCont(row.v,row.p)", {"rows":rows}).rows == ((20.0,),)


@pytest.mark.parametrize("sample", [True, "text", [1], {"x":1}])
def test_dynamic_nonnumeric_sample_refuses_with_runtime_type_evidence(db, sample):
    with pytest.raises(GrafxPlanError) as failure:
        db.execute("UNWIND $rows AS row RETURN percentileDisc(row.v,0.5)", {"rows":[{"v":sample}]})
    assert failure.value.details["reason"] == "percentile_sample_type"
    assert native_error(failure.value).detail == "InvalidArgumentType"


def test_percentile_failure_preserves_cause_and_cleans_all_sorters(tmp_path, monkeypatch):
    from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory
    from okto_grafx.engine import query_engine
    from okto_grafx.errors import GrafxCorruptionDetected
    spill_root = tmp_path / "spill"
    spill_root.mkdir()
    monkeypatch.setattr("okto_grafx.api.assembly.LocalQuerySpillFactory", lambda: LocalQuerySpillFactory(str(spill_root)))
    original = query_engine._SpilledAggregateState.finish
    closed = []
    class Faulty:
        def __init__(self, index, sorter):
            self.index, self.sorter = index, sorter
        def records(self):
            raise GrafxCorruptionDetected("primary-percentile-read", field="query_spill.record")
        def close(self):
            self.sorter.close()
            closed.append(self.index)
            if self.index == 0:
                raise RuntimeError("secondary-percentile-close")
    def finish(state):
        for index, sorter in tuple(state._percentiles.items()):
            state._percentiles[index] = Faulty(index, sorter)
        return original(state)
    monkeypatch.setattr(query_engine._SpilledAggregateState, "finish", finish)
    with connect(tmp_path / "db", query_memory_budget_bytes=32768) as graph:
        with graph.begin("write") as tx:
            tx.execute("CREATE(:Earlier)")
            with pytest.raises(GrafxCorruptionDetected, match="primary-percentile-read") as failure:
                tx.execute("UNWIND range(1,10) AS v CREATE(:N {v:v}) RETURN percentileDisc(v,0),percentileCont(v,1)")
            assert any("secondary-percentile-close" in note for note in getattr(failure.value, "__notes__", ()))
            tx.execute("CREATE(:Later)")
        assert sorted(closed) == [0,1]
        assert tuple(spill_root.iterdir()) == ()
        assert graph.execute("MATCH(n) RETURN labels(n) ORDER BY labels(n)").rows == ((("Earlier",),), (("Later",),))
        assert graph.verify("all").findings == ()


def test_truncated_percentile_run_is_detected_and_rolls_back(tmp_path, monkeypatch):
    from okto_grafx.engine import query_engine
    from okto_grafx.errors import GrafxCorruptionDetected
    original = query_engine._SpilledAggregateState.finish
    class Truncated:
        def __init__(self, sorter):
            self.sorter = sorter
        def records(self):
            records = self.sorter.records()
            pending = None
            try:
                for record in records:
                    if pending is not None:
                        yield pending
                    pending = record
            finally:
                records.close()
        def close(self):
            self.sorter.close()
    def finish(state):
        for index, sorter in tuple(state._percentiles.items()):
            state._percentiles[index] = Truncated(sorter)
        return original(state)
    monkeypatch.setattr(query_engine._SpilledAggregateState, "finish", finish)
    with connect(tmp_path / "db", query_memory_budget_bytes=32768) as graph:
        with graph.begin("write") as tx:
            tx.execute("CREATE(:Earlier)")
            with pytest.raises(GrafxCorruptionDetected) as failure:
                tx.execute("UNWIND range(1,10) AS v CREATE(:N {v:v}) RETURN percentileDisc(v,0)")
            assert failure.value.details["value"] == "percentile_count"
            tx.execute("CREATE(:Later)")
        assert graph.execute("MATCH(n) RETURN labels(n) ORDER BY labels(n)").rows == ((("Earlier",),), (("Later",),))
        assert graph.verify("all").findings == ()
