"""Projected maps retain native postfix access without reevaluating their source."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError


@pytest.fixture(params=[None,32768])
def db(tmp_path, request):
    with connect(tmp_path / "db", query_memory_budget_bytes=request.param) as graph:
        yield graph


@pytest.mark.parametrize("query,expected", [
    ("WITH {name:{name2:'baz'}} AS nestedMap RETURN nestedMap.name.name2", (("baz",),)),
    ("WITH {a:{b:3}} AS a WITH a AS b RETURN b.a.b", ((3,),)),
    ("WITH $m AS m RETURN m.a.b", ((3,),)),
    ("WITH CASE WHEN true THEN {a:{b:3}} ELSE null END AS m RETURN m.a.b", ((3,),)),
    ("WITH null AS m RETURN m.a.b", ((None,),)),
    ("WITH {} AS m RETURN m.a.b", ((None,),)),
    ("WITH {a:{b:3}} AS m CALL(m) { RETURN m.a.b AS value } RETURN value", ((3,),)),
    ("CALL { RETURN {a:{b:3}} AS m } RETURN m.a.b", ((3,),)),
    ("WITH {a:{b:3}} AS m WITH {a:{b:4}} AS m RETURN m.a.b", ((4,),)),
    ("WITH {b:3} AS a WITH {a:a} AS b WITH {b:b} AS c RETURN c.b.a.b", ((3,),)),
    ("WITH [{a:{b:3}}] AS xs RETURN xs[0].a.b", ((3,),)),
    ("WITH {a:{b:3}} AS m RETURN m['a']['b']", ((3,),)),
    ("WITH {a:{b:'X'}} AS m RETURN lower(m.a.b)", (("x",),)),
    ("WITH {a:{b:3}} AS m RETURN m.a.b AS x UNION RETURN 4 AS x", ((3,),(4,))),
])
def test_projected_map_forms(db, query, expected):
    assert db.execute(query, {"m":{"a":{"b":3}}}).rows == expected


def test_projected_map_sort_and_group_spill(db):
    query = ("UNWIND range(1,120) AS n WITH {a:{b:n%3}} AS m "
             "WITH DISTINCT m ORDER BY m.a.b DESC RETURN m.a.b")
    assert db.execute(query).rows == ((2,),(1,),(0,))


def test_projection_source_is_not_reexecuted_during_type_proof(tmp_path, monkeypatch):
    from okto_grafx.api import assembly
    calls = []
    def draw():
        calls.append(1)
        return 0.25
    monkeypatch.setattr(assembly, "_new_query_random_source", lambda: draw)
    with connect(tmp_path / "db") as db:
        query = "WITH {a:{b:rand()}} AS m RETURN m.a.b,m.a.b+1"
        db.explain(query)
        assert calls == []
        assert db.execute(query).rows == ((0.25,1.25),)
        assert calls == [1]


@pytest.mark.parametrize("query", [
    "WITH 1 AS m RETURN m.a.b",
    "WITH {a:1} AS m RETURN m.a.b",
    "WITH {a:{b:1}} AS m RETURN lower(m.a.b)",
])
def test_known_nonmap_and_wrong_leaf_types_still_refuse(db, query):
    with pytest.raises(GrafxPlanError):
        db.explain(query)


def test_unknown_projected_subject_is_checked_at_use_and_rolls_back(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(:Earlier)")
        with pytest.raises(GrafxPlanError) as failure:
            tx.execute("UNWIND [{a:{b:1}},{a:2}] AS v CREATE(:N) WITH v AS m RETURN m.a.b")
        assert failure.value.details["reason"] == "property_subject_type"
        assert failure.value.details["query_phase"] == "execution"
        tx.execute("CREATE(:Later)")
    assert db.execute("MATCH(n) RETURN labels(n) ORDER BY labels(n)").rows == ((("Earlier",),), (("Later",),))
    assert db.verify("all").findings == ()


def test_projected_parameter_map_rebinds_without_stale_values(db):
    query = "WITH $m AS m RETURN m.a.b"
    for value, expected in [({"a":{"b":1}},1), ({"a":None},None), ({},None)]:
        assert db.execute(query, {"m":value}).rows == ((expected,),)
    with pytest.raises(GrafxPlanError) as failure:
        db.execute(query, {"m":{"a":5}})
    assert failure.value.details["reason"] == "property_subject_type"
    assert db.execute(query, {"m":{"a":{"b":3}}}).rows == ((3,),)


def test_deep_shared_alias_proof_does_not_duplicate_source_evaluation(monkeypatch):
    from okto_grafx.api import assembly
    draws = []
    def draw():
        draws.append(1)
        return 0.5
    monkeypatch.setattr(assembly, "_new_query_random_source", lambda: draw)
    query = "WITH {a:{b:rand()}} AS m" + " WITH {a:m.a,other:m.a} AS m" * 18 + " RETURN m.a.b,m.other.b"
    with connect(":memory:") as db:
        db.explain(query)
        assert draws == []
        assert db.execute(query).rows == ((0.5,0.5),)
        assert draws == [1]
