"""Projected values, not their discarded source rows, survive grouping modifiers."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("UNWIND ['A','A','B','C','C'] AS name CREATE(:N {name:name, other:7})")
        yield db


@pytest.mark.parametrize("distinct", [False, True])
@pytest.mark.parametrize("key", ["a.name", "a.name + 'Z'", "toLower(a.name)"])
def test_ordering_uses_projected_property_after_group_or_distinct(graph, distinct, key):
    clause = "DISTINCT a.name AS name" if distinct else "a.name AS name, count(*) AS count"
    result = graph.execute(f"MATCH(a:N) WITH {clause} ORDER BY {key} DESC LIMIT 2 RETURN name")
    assert result.rows == (("C",), ("B",))


@pytest.mark.parametrize("distinct", [False, True])
def test_attached_filter_reuses_exact_projected_property(graph, distinct):
    clause = "DISTINCT a.name AS name" if distinct else "a.name AS name"
    result = graph.execute(f"MATCH(a:N) WITH {clause} WHERE a.name = 'B' RETURN *")
    assert result.columns == ("name",)
    assert result.rows == (("B",),)


@pytest.mark.parametrize("modifier", ["ORDER BY a.other", "WHERE a.other = 7"])
def test_distinct_property_does_not_reopen_entire_node(graph, modifier):
    with pytest.raises(GrafxPlanError):
        graph.execute(f"MATCH(a:N) WITH DISTINCT a.name AS name {modifier} RETURN name")


def test_projected_aggregate_reference_is_not_reaggregated(graph):
    result = graph.execute("MATCH(a:N) WITH a.name AS name, count(*) AS n ORDER BY count(*) DESC, name RETURN name,n")
    assert result.rows == (("A",2), ("C",2), ("B",1))


def test_expression_substitution_is_not_algebraic_equivalence():
    with connect(":memory:") as db:
        assert db.execute("UNWIND [1,2,3] AS n WITH DISTINCT n+1 AS x ORDER BY (n+1)*2 DESC RETURN x").rows == ((4,), (3,), (2,))
        with pytest.raises(GrafxPlanError):
            db.execute("UNWIND [1,2,3] AS n WITH DISTINCT n+1 AS x ORDER BY n+2 RETURN x")


def test_output_shadowing_does_not_substitute_old_expression():
    with connect(":memory:") as db:
        assert db.execute("UNWIND [1,2,3] AS n WITH n+1 AS n ORDER BY n+1 DESC RETURN n").rows == ((4,), (3,), (2,))
        assert db.execute("UNWIND [{v:1},{v:2}] AS n WITH n.v AS n ORDER BY n RETURN n").rows == ((1,), (2,))


def test_list_locals_do_not_alias_incoming_names():
    with connect(":memory:") as db:
        result = db.execute("UNWIND [1,2] AS n WITH DISTINCT n AS x WHERE any(n IN [2] WHERE n = 2) RETURN x")
        assert result.rows == ((1,), (2,))
        result = db.execute("UNWIND [1,2] AS n WITH DISTINCT n AS x WHERE any(y IN [n] WHERE y = 2) RETURN x")
        assert result.rows == ((2,),)


@pytest.mark.parametrize("budget", [None, 32768])
def test_projected_scope_survives_spill_without_source_capabilities(tmp_path, budget):
    with connect(tmp_path / "db", query_memory_budget_bytes=budget) as db:
        result = db.execute("UNWIND range(1,120) AS n WITH DISTINCT n+1 AS x ORDER BY n+1 DESC WHERE n+1>118 RETURN x")
        assert result.rows == ((121,), (120,), (119,))


@pytest.mark.parametrize("budget", [None, 32768])
def test_source_filter_survives_sort_spill_but_cannot_leak(tmp_path, budget, monkeypatch):
    from okto_grafx.engine import query_engine
    decoded = []
    original = query_engine._decode_sort_row
    def observe(payload, codec):
        row = original(payload, codec)
        decoded.append(row)
        return row
    monkeypatch.setattr(query_engine, "_decode_sort_row", observe)
    with connect(tmp_path / "db", query_memory_budget_bytes=budget) as db:
        with db.begin("write") as tx:
            tx.execute("UNWIND range(1,30) AS i CREATE(:N {id:i,other:i%2})")
        query = "MATCH(a:N) WITH a.id AS id ORDER BY a.id DESC WHERE a.other=0 RETURN *"
        result = db.execute(query)
        assert result.columns == ("id",)
        assert result.rows == tuple((i,) for i in range(30,0,-2))
        if budget is not None:
            assert decoded
        with pytest.raises(GrafxPlanError):
            db.execute("MATCH(a:N) WITH a.id AS id WHERE a.other=0 RETURN a")


def test_where_shadowing_and_local_names_do_not_capture_inputs():
    with connect(":memory:") as db:
        assert db.execute("UNWIND [1,2,3] AS n WITH n+1 AS n WHERE n=3 RETURN n").rows == ((3,),)
        assert db.execute("UNWIND [1,2,3] AS n WITH n+1 AS x WHERE any(n IN [2] WHERE n=2) AND n=3 RETURN x").rows == ((4,),)


def test_source_filter_failure_rolls_back_the_entire_instruction(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Earlier)")
            with pytest.raises(GrafxPlanError):
                tx.execute("CREATE(n:N {v:1}) WITH 1 AS x WHERE n.v / $zero > 0 RETURN x", {"zero":0})
            tx.execute("CREATE(:Later)")
        assert db.execute("MATCH(n) RETURN labels(n) ORDER BY labels(n)").rows == ((("Earlier",),), (("Later",),))
        assert db.verify("all").findings == ()
