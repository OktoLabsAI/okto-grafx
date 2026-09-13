"""WITH ordering temporarily sees input bindings; following clauses never inherit them."""

import pytest

from okto_grafx import connect
from okto_grafx.api import assembly
from okto_grafx.errors import GrafxPlanError


def test_with_orders_by_unprojected_property_and_keeps_only_output(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("UNWIND range(0,15) AS i CREATE(:N {v:i})")
        assert db.execute("MATCH(a:N) WITH a.v AS value ORDER BY a.v SKIP 10 LIMIT 10 RETURN *").rows == tuple((i,) for i in range(10,16))
        for suffix in ("RETURN a", "RETURN a.v"):
            with pytest.raises(GrafxPlanError):
                db.execute("MATCH(a:N) WITH a.v AS value ORDER BY a.v " + suffix)


@pytest.mark.parametrize("budget", [None, 32768])
def test_input_ordering_survives_bounded_sort_and_topk_spill(tmp_path, budget, monkeypatch):
    from okto_grafx.engine import query_engine
    decoded = []
    original = query_engine._decode_sort_row
    def observe(payload, codec):
        decoded.append(1)
        return original(payload, codec)
    monkeypatch.setattr(query_engine, "_decode_sort_row", observe)
    with connect(tmp_path / "db", query_memory_budget_bytes=budget) as db:
        query = "UNWIND range(1,120) AS i WITH 121-i AS x ORDER BY i DESC"
        assert db.execute(query + " RETURN x").rows == tuple((i,) for i in range(1,121))
        assert db.execute(query + " SKIP 10 LIMIT 20 RETURN x").rows == tuple((i,) for i in range(11,31))
        if budget is not None:
            assert decoded, "Exercise real private spill restoration, not just a small configured budget"


def test_output_alias_shadows_input_only_in_ordering():
    with connect(":memory:") as db:
        assert db.execute("UNWIND [1,2,3] AS n WITH -n AS n ORDER BY n RETURN n").rows == ((-3,),(-2,),(-1,))
        assert db.execute("UNWIND [1,2,3] AS n WITH n+10 AS out ORDER BY n DESC LIMIT 2 RETURN out").rows == ((13,),(12,))


@pytest.mark.parametrize("projection", ["DISTINCT n+1 AS out", "count(n) AS out"])
def test_distinct_or_grouping_cannot_reopen_discarded_input(projection):
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError):
            db.execute(f"UNWIND [1,2,3] AS n WITH {projection} ORDER BY n RETURN out")


def test_sort_projection_is_not_evaluated_twice(monkeypatch):
    calls = []
    def draw():
        calls.append(1)
        return 0.25
    monkeypatch.setattr(assembly, "_new_query_random_source", lambda: draw)
    with connect(":memory:") as db:
        assert db.execute("UNWIND [1,2,3] AS n WITH rand() AS r ORDER BY n DESC LIMIT 2 RETURN r").rows == ((0.25,),(0.25,))
        assert len(calls) == 3


def test_repeated_shadowing_and_nested_subquery_ordering():
    with connect(":memory:") as db:
        result = db.execute("UNWIND [1,2] AS x CALL(x) { UNWIND [1,2,3] AS n WITH n+x AS y ORDER BY n DESC LIMIT 1 RETURN y } WITH y AS x ORDER BY y RETURN x")
        assert result.rows == ((4,), (5,))


def test_attached_where_can_use_projected_alias_after_ordering():
    with connect(":memory:") as db:
        assert db.execute("UNWIND [1,2] AS n WITH n+1 AS x ORDER BY n WHERE x>2 RETURN x").rows == ((3,),)
