"""Native existential query scopes, cardinality, authority and rollback."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxPlanError


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a {v:1})-[:R]->(b {v:2}), (c {v:3})")
        yield db


@pytest.mark.parametrize("body", [
    "(n)-->()", "MATCH(n)-->()", "MATCH(n)-->(m) RETURN m.v",
    "MATCH(n)-->(m) WITH count(*) AS c WHERE c>0 RETURN c",
    "MATCH(m) WHERE exists{(n)-->(m)} RETURN true",
    "WITH 2 AS target MATCH(n)-->(m) WHERE m.v=target",
])
def test_correlated_body_variants(graph, body):
    assert graph.execute(f"MATCH(n) WHERE exists{{{body}}} RETURN n.v").rows == ((1,),)


@pytest.mark.parametrize(("body", "expected"), [
    ("UNWIND [] AS x RETURN x", False),
    ("UNWIND [] AS x RETURN count(*)", True),
    ("RETURN null", True),
    ("RETURN true LIMIT 0", False),
    ("MATCH(n {v:99}) UNION MATCH(n {v:1})", True),
    ("RETURN true AS x LIMIT 0 UNION ALL RETURN false AS x", True),
])
def test_cardinality_not_truthiness(graph, body, expected):
    assert graph.execute(f"RETURN EXISTS {{{body}}} AS result").rows == ((expected,),)


def test_scalar_imports_with_shadowed_outer_spelling(graph):
    assert graph.execute("WITH 1 AS x WITH 2 AS x RETURN EXISTS{MATCH(n {v:x})}, x").rows == ((True, 2),)
    assert graph.execute("WITH 2 AS x RETURN EXISTS{WITH 1 AS y MATCH(n {v:x}) RETURN y}").rows == ((True,),)


@pytest.mark.parametrize("query", [
    "MATCH(n) WHERE false RETURN EXISTS{CREATE(x)}",
    "RETURN EXISTS{MATCH(n) SET n.v=0}",
    "RETURN EXISTS{CALL(){CREATE(n)} RETURN true}",
])
def test_writes_refused_during_planning_even_if_unreachable(graph, query):
    with pytest.raises(GrafxPlanError) as failure:
        graph.execute(query)
    assert failure.value.details["reason"] == "existential_write"
    assert failure.value.details["query_phase"] == "planning"
    assert graph.execute("MATCH(n) RETURN n.v ORDER BY n.v").rows == ((1,), (2,), (3,))


@pytest.mark.parametrize("query", [
    "WITH 1 AS x RETURN EXISTS{WITH 2 AS x RETURN x}",
    "RETURN EXISTS{MATCH(m)}, m",
    "RETURN EXISTS{MATCH(n) UNION RETURN true}",
    "RETURN EXISTS{}",
])
def test_scope_and_composition_errors(graph, query):
    with pytest.raises(GrafxError):
        graph.execute(query)


def test_lazy_evaluation_and_repeated_invocations(graph):
    assert graph.execute("RETURN true OR EXISTS{RETURN 1/0}").rows == ((True,),)
    assert graph.execute("UNWIND [1,99,2] AS x RETURN EXISTS{MATCH(n {v:x})}").rows == ((True,), (False,), (True,))
    assert graph.execute("RETURN [x IN [1,99,2] | EXISTS{MATCH(n {v:x})}]").rows == (((True,False,True),),)


def test_same_statement_read_after_create_and_late_failure(graph):
    with graph.begin("write") as tx:
        assert tx.execute("CREATE(n {v:4}) RETURN EXISTS{MATCH(m {v:4})}").rows == ((True,),)
        with pytest.raises(GrafxError):
            tx.execute("CREATE(n {v:5}) RETURN EXISTS{RETURN 1/0}")
        assert tx.execute("MATCH(n {v:5}) RETURN count(*)").rows == ((0,),)
    assert graph.verify("all").findings == ()


@pytest.mark.parametrize(("query", "expected"), [
    ("MATCH p=(a)-->(b) RETURN [n IN nodes(p) | EXISTS{(n)-->()}]", (((True,False),),)),
    ("MATCH(a) RETURN [(a)-->(b) | EXISTS{(b)<--()}] AS xs ORDER BY a.v", (((True,),), ((),), ((),))),
    ("MATCH(n) RETURN EXISTS{MATCH(m {v:1})},count(*)", ((True,3),)),
    ("MATCH(n) WITH EXISTS{(n)-->()} AS has,count(*) AS c RETURN has,c ORDER BY has", ((False,2),(True,1))),
    ("OPTIONAL MATCH(n:Missing) RETURN EXISTS{(n)-->()}, EXISTS{RETURN n}", ((False,True),)),
    ("MATCH p=(a)-->(b) RETURN EXISTS{RETURN length(p)}, EXISTS{MATCH(a)-[r]->(b) RETURN r}", ((True,True),)),
    ("MATCH(a)-[r]->(b) RETURN EXISTS{MATCH(a)-[r]->(b)}", ((True,),)),
    ("RETURN EXISTS{CALL(){RETURN 1 AS x} WITH x WHERE x=1 RETURN x}", ((True,),)),
    ("WITH 1 AS x RETURN [x IN [2,99] | EXISTS{WITH x AS y MATCH(n {v:y})}]", (((True,False),),)),
    ("WITH 1 AS x RETURN EXISTS{RETURN *}", ((True,),)),
    ("WITH 1 AS x RETURN EXISTS{WITH * RETURN x}", ((True,),)),
    ("RETURN EXISTS{p=(a)-[*1..2]->(b) WHERE length(p)=1}", ((True,),)),
    ("MATCH p=(a)-->(b) RETURN [n IN nodes(p) | [m IN [n] | EXISTS{(m)-->()}]]", ((((True,),(False,)),),)),
    ("RETURN [n IN [null] | EXISTS{(n)-->()}], [n IN [] | EXISTS{(n)-->()}]", (((False,),()),)),
    ("MATCH(n) RETURN count(*) + size(CASE WHEN EXISTS{RETURN [n IN [1] | n]} THEN [1] ELSE [] END)", ((4,),)),
])
def test_composed_scopes_and_values(graph, query, expected):
    assert graph.execute(query).rows == expected


def test_inner_parameters_are_required_and_bound(graph):
    query = "RETURN EXISTS{MATCH(n {v:$value}) WHERE EXISTS{RETURN $inner}}"
    assert graph.execute(query, {"value":1,"inner":None}).rows == ((True,),)
    assert graph.execute(query, {"value":99,"inner":True}).rows == ((False,),)
    with pytest.raises(GrafxError):
        graph.execute(query, {"value":99})


def test_inner_null_or_scalar_is_never_an_unbound_entity(graph):
    for source in ("[1]", "[{v:1}]"):
        with pytest.raises(GrafxError):
            graph.execute(f"RETURN [n IN {source} | EXISTS{{(n)-->()}}]")


def test_snapshot_and_two_participants(tmp_path):
    path = tmp_path / "snapshots"
    with connect(path) as db, connect(path) as other:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:N {v:1})")
        with db.begin("read") as reader:
            assert reader.execute("RETURN EXISTS{MATCH(n:N {v:2})}").rows == ((False,),)
            with other.begin("write") as writer:
                writer.execute("CREATE(n:N {v:2})")
            assert reader.execute("RETURN EXISTS{MATCH(n:N {v:2})}").rows == ((False,),)
        assert db.execute("RETURN EXISTS{MATCH(n:N {v:2})}").rows == ((True,),)


def test_shared_traversal_budget_and_short_circuit(tmp_path):
    from okto_grafx.errors import GrafxQueryBudgetExceeded
    path = tmp_path / "bounded"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a:N {v:1})-[:R]->(b:N {v:2})-[:R]->(c:N {v:3})")
    with connect(path, max_traversal_expansions=1) as db:
        prefix = "MATCH(n:N) WITH n ORDER BY n.v LIMIT 1 RETURN "
        assert db.execute(prefix + "EXISTS{(n)-->()}").rows == ((True,),)
        assert db.execute(prefix + "true OR EXISTS{(n)-[:R*2]->()}").rows == ((True,),)
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute(prefix + "EXISTS{(n)-[:R*2]->()}")
        assert db.execute(prefix + "EXISTS{(n)-->()}").rows == ((True,),)


@pytest.mark.parametrize("cursor", [False, True])
def test_cancel_inside_body_releases_arguments_and_snapshot(graph, monkeypatch, cursor):
    from okto_grafx import CancellationToken
    from okto_grafx.errors import GrafxQueryCancelled
    from okto_grafx.engine.query_engine import QueryEngine
    token = CancellationToken()
    original = QueryEngine._rows
    observed = []

    def rows(engine, node, context):
        if any(node is descriptor[1] for descriptor in context.existential_queries.values()):
            observed.append(context)
            token.cancel()
        yield from original(engine, node, context)

    monkeypatch.setattr(QueryEngine, "_rows", rows)
    query = "RETURN EXISTS{UNWIND range(1,1000) AS x RETURN x ORDER BY x DESC}"
    with pytest.raises(GrafxQueryCancelled):
        if cursor:
            stream = graph.query(query).cursor(cancellation=token)
            tuple(stream)
        else:
            graph.execute(query, cancellation=token)
    assert observed and all(not context.arguments for context in observed)
    assert not graph._transactions._open


def test_body_stops_at_first_row_and_closes_on_failure(graph, monkeypatch):
    from okto_grafx.engine.query_engine import QueryEngine
    original = QueryEngine._rows
    closed = []
    produced = []
    contexts = []

    def rows(engine, node, context):
        inner = any(node is descriptor[1] for descriptor in context.existential_queries.values())
        stream = original(engine, node, context)
        try:
            for row in stream:
                if inner:
                    produced.append(row)
                yield row
        finally:
            stream.close()
            if inner:
                closed.append(node)
                contexts.append(context)

    monkeypatch.setattr(QueryEngine, "_rows", rows)
    assert graph.execute("RETURN EXISTS{UNWIND range(1,1000) AS x RETURN x}").rows == ((True,),)
    assert len(produced) == len(closed) == 1
    with pytest.raises(GrafxError):
        graph.execute("RETURN EXISTS{RETURN 1/0}")
    assert len(closed) == 2 and all(not context.arguments for context in contexts)


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_spill_and_repeated_cached_execution(tmp_path, codec):
    with connect(tmp_path / codec, codec=codec, query_memory_budget_bytes=8192) as db:
        query = "WITH $minimum AS bound RETURN EXISTS{UNWIND range(1,600) AS x WITH x ORDER BY x DESC SKIP bound RETURN x}"
        for bound, expected in ((599, True), (600, False), (599, True)):
            assert db.execute(query, {"minimum":bound}).rows == ((expected,),)
        assert not db._transactions._open
        assert db.verify("all").findings == ()
