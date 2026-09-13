"""Native pattern-comprehension execution with independent result expectations."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.fixture
def graph():
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, name STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N, weight INT64)")
            tx.execute("CREATE (a:N {id:1,name:'a'}), (b:N {id:2,name:'b'}), (c:N {id:3,name:'c'}) "
                       "CREATE(a)-[:R {weight:10}]->(b), (b)-[:R {weight:20}]->(c)")
        yield db


@pytest.mark.parametrize("query,expected", [
    ("MATCH(n:N) RETURN n.id, [(n)-->(m) | m.name] AS xs ORDER BY n.id",
     ((1, ('b',)), (2, ('c',)), (3, ()))),
    ("MATCH(n:N) RETURN n.id, [(n)-[r:R]->() | r.weight] AS xs ORDER BY n.id",
     ((1, (10,)), (2, (20,)), (3, ()))),
    ("MATCH(n:N) RETURN n.id, [(n)-->(m) WHERE m.id>2 | m.id] AS xs ORDER BY n.id",
     ((1, ()), (2, (3,)), (3, ()))),
    ("MATCH(n:N {id:1}), (m:N {id:3}) RETURN [(n)-->(m) | m.id] AS xs", (((),),)),
    ("MATCH(n:N {id:1}) RETURN [(n)-->(m) | [(m)-->(z) | z.id]] AS xs", ((((3,),),),)),
    ("MATCH(n:N {id:1}) RETURN [p=(n)-[:R*0..2]->(m) | length(p)] AS xs", (((0,1,2),),)),
    ("MATCH p=(n:N {id:1})-->(m) RETURN [x IN nodes(p) | size([(x)-->() | 1])] AS xs", (((1,1),),)),
    ("MATCH(n:N) WITH [(n)-->(m) | m.id] AS xs, count(n) AS c RETURN xs,c",
     (((2,),1), ((3,),1), ((),1))),
    ("MATCH(n:N {id:1}) WITH 1 AS keep RETURN [(n:N)-->(m) | m.id] AS xs", (((2,3),),)),
    ("MATCH(n:N) RETURN count([(n)-->() | 1]) AS c", ((3,),)),
    ("MATCH(a:N)-[r:R]->(b) RETURN [(a)-[r]->(b) | r.weight] AS xs", (((10,),), ((20,),))),
    ("RETURN [(:Missing)-->() | 1] AS xs", (((),),)),
    ("MATCH(n:N {id:2}) RETURN [(n)<--(m) | m.id] AS xs", (((1,),),)),
    ("MATCH(n:N {id:2}) WITH [(n)--(m) | m.id] AS xs UNWIND xs AS id RETURN id ORDER BY id", ((1,), (3,))),
    ("MATCH(n:N {id:1}) RETURN [(n)-[:Missing|R]->(m) | m.id] AS xs", (((2,),),)),
    ("MATCH(n:N {id:1}) RETURN [(n)-->(m) WHERE (m)-->() | upper(m.name)] AS xs", ((('B',),),)),
    ("UNWIND [(:N)-->(m) | m] AS x RETURN x.id ORDER BY x.id", ((2,), (3,))),
    ("MATCH(n:N {id:1}) WITH [p=(n)-[:R*1..2]->() | p] AS ps "
     "UNWIND ps AS p RETURN length(p) AS len ORDER BY len", ((1,), (2,))),
])
def test_native_comprehension_shapes(graph, query, expected):
    assert graph.execute(query).rows == expected


def test_bound_scalar_is_not_reinterpreted_as_graph_scan(graph):
    with pytest.raises(GrafxError):
        graph.execute("RETURN [x IN [1] | [(x)-->() | 1]] AS xs")


def test_null_anchor_is_not_an_unbound_scan(graph):
    assert graph.execute("OPTIONAL MATCH(n:Missing) RETURN [(n)-->() | 1] AS xs").rows == ((None,),)


def test_nested_entity_results_are_detached(graph):
    result = graph.execute("MATCH(n:N {id:1}) RETURN [p=(n)-[:R*1..2]->() | p] AS paths")
    paths = result.rows[0][0]
    assert [len(path.relationships) for path in paths] == [1,2]
    assert [[node.properties['id'] for node in path.nodes] for path in paths] == [[1,2], [1,2,3]]
    assert result.plan is not None


@pytest.mark.parametrize("query,lengths", [
    ("MATCH(n) RETURN [p=(n)-->() | p] AS xs", [1,1,0]),
    ("MATCH(n:A) RETURN [p=(n)-->(:B) | p] AS xs", [1]),
])
def test_polymorphic_group_paths_without_primary_keys(query, lengths):
    with connect(":memory:") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            for label in ('A','B','C'):
                tx.execute(f"CREATE NODE TABLE {label}(v BOOL)")
            tx.execute("CREATE REL TABLE GROUP T(FROM A TO B, FROM B TO C)")
            tx.execute("CREATE(a:A), (b:B) CREATE(a)-[:T]->(b), (b)-[:T]->(:C)")
        result = db.execute(query)
        assert [len(row[0]) for row in result.rows] == lengths


def test_comprehension_where_optional_and_subquery_composition(graph):
    assert graph.execute("MATCH(n:N) WHERE size([(n)-->() | 1])>0 RETURN n.id ORDER BY n.id").rows == ((1,),(2,))
    assert graph.execute("MATCH(n:N) OPTIONAL MATCH(n)-->(m) WHERE size([(m)-->() | 1])>0 "
                         "RETURN n.id,m.id ORDER BY n.id").rows == ((1,2),(2,None),(3,None))
    assert graph.execute("MATCH(n:N) CALL(n) { RETURN [(n)-->(m) | m.id] AS xs } "
                         "RETURN n.id,xs ORDER BY n.id").rows == ((1,(2,)),(2,(3,)),(3,()))


def test_list_local_relationship_cannot_be_used_as_node(graph):
    with pytest.raises(GrafxError):
        graph.execute("MATCH p=(:N {id:1})-->() RETURN [x IN relationships(p) | [(x)-->() | 1]] AS xs")


@pytest.mark.parametrize("function", ["length", "nodes", "relationships"])
def test_dynamic_path_functions_validate_selected_value(graph, function):
    assert graph.execute(f"UNWIND [null] AS p RETURN {function}(p)").rows == ((None,),)
    assert graph.execute(f"WITH [null] AS xs UNWIND xs AS p RETURN {function}(p)").rows == ((None,),)
    assert graph.execute(f"UNWIND [] AS p RETURN {function}(p)").rows == ()
    with pytest.raises(GrafxError):
        graph.execute(f"UNWIND $xs AS p RETURN {function}(p)", {"xs":[1]})
    with pytest.raises(GrafxError):
        graph.execute(f"MATCH(n:N) RETURN {function}(n)")


@pytest.mark.parametrize("function", ["properties", "labels", "type"])
def test_all_null_collections_do_not_invent_an_entity_kind(graph, function):
    assert graph.execute(f"UNWIND [null,null] AS x RETURN {function}(x)").rows == ((None,), (None,))
    assert graph.execute(f"WITH [null] AS xs RETURN {function}(xs[0])").rows == ((None,),)


def test_element_limit_is_cumulative_and_resets_between_statements(graph, monkeypatch):
    from okto_grafx.engine import query_engine
    from okto_grafx.errors import GrafxQueryBudgetExceeded
    monkeypatch.setattr(query_engine, "MAX_GENERATED_LIST_ELEMENTS", 3)
    query = "MATCH(n:N) RETURN [(n)-->() | 1] AS xs"
    with pytest.raises(GrafxQueryBudgetExceeded):
        graph.execute("UNWIND [1,2] AS k MATCH(n:N) RETURN [(n)-->() | k] AS xs")
    assert graph.execute(query).rows == (((1,),), ((1,),), ((),))


def test_memory_budget_covers_nested_payloads_and_late_write_failure(tmp_path):
    from okto_grafx.errors import GrafxQueryBudgetExceeded
    path = tmp_path / "limited"
    with connect(path, query_memory_budget_bytes=1024) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N)")
        with db.begin("write") as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxQueryBudgetExceeded) as error:
                tx.execute("CREATE(a:N {id:1})-[:R]->(b:N {id:2}) "
                           "RETURN [(a)-->(b) | {large:$value}] AS xs", {"value":"x"*300})
            assert error.value.details["operator"] == "pattern_comprehension"
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
    with connect(path) as db:
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert not db.verify("all").findings


def test_cursor_close_and_independent_read_snapshot(graph):
    query = "MATCH(n:N) RETURN n.id, [(n)-->(m) | m.id] AS xs ORDER BY n.id"
    expected = ((1,(2,)), (2,(3,)), (3,()))
    with graph.begin("read") as reader:
        assert reader.execute(query).rows == expected
        with graph.begin("write") as writer:
            writer.execute("MATCH(a:N {id:3}),(b:N {id:1}) CREATE(a)-[:R]->(b)")
            assert writer.execute(query).rows[-1] == (3,(1,))
            assert reader.execute(query).rows == expected
        assert reader.execute(query).rows == expected
    with graph.query(query).cursor(batch_size=1) as cursor:
        assert next(cursor) == (1,(2,))
    assert graph.execute(query).rows[-1] == (3,(1,))


@pytest.mark.parametrize("cursor_mode", [False, True])
def test_cancellation_inside_materialization_releases_arguments(graph, monkeypatch, cursor_mode):
    from okto_grafx import CancellationToken
    from okto_grafx.engine.query_engine import _Context
    from okto_grafx.errors import GrafxQueryCancelled
    token = CancellationToken()
    original = _Context.count
    contexts = []
    def count(context, name, amount=1):
        if name == "list_iterations":
            contexts.append(context)
            token.cancel()
        return original(context, name, amount)
    query = "MATCH(n:N) RETURN [(n)-->() | 1] AS xs"
    with monkeypatch.context() as patch:
        patch.setattr(_Context, "count", count)
        with pytest.raises(GrafxQueryCancelled):
            if cursor_mode:
                with graph.query(query).cursor(cancellation=token) as cursor:
                    tuple(cursor)
            else:
                graph.execute(query, cancellation=token)
    assert contexts and all(not context.arguments for context in contexts)
    assert not graph._transactions._open
    assert graph.execute(query).rows == (((1,),), ((1,),), ((),))


@pytest.mark.parametrize("mode", ["start", "next_and_close", "close"])
def test_subplan_faults_preserve_primary_error_and_whole_statement_rollback(graph, monkeypatch, mode):
    from okto_grafx.engine.query_engine import QueryEngine
    from okto_grafx.errors import GrafxPlanError
    primary = GrafxPlanError("injected subplan failure")
    closing = GrafxPlanError("injected close failure")
    original = QueryEngine._rows
    contexts = []
    class BrokenStream:
        def __iter__(self):
            return self
        def __next__(self):
            if mode == "next_and_close":
                raise primary
            raise StopIteration
        def close(self):
            raise closing
    def rows(engine, node, context):
        if any(node is descriptor[1] for descriptor in context.pattern_comprehensions.values()):
            contexts.append(context)
            if mode == "start":
                raise primary
            return BrokenStream()
        return original(engine, node, context)
    with graph.begin("write") as tx:
        tx.execute("CREATE(:N {id:4})")
        with monkeypatch.context() as patch:
            patch.setattr(QueryEngine, "_rows", rows)
            with pytest.raises(GrafxPlanError) as error:
                tx.execute("CREATE(a:N {id:5})-[:R]->(b:N {id:6}) RETURN [(a)-->(b) | b] AS xs")
        assert error.value is (closing if mode == "close" else primary)
        if mode == "next_and_close":
            assert any("injected close failure" in note for note in error.value.__notes__)
        assert contexts and all(not context.arguments for context in contexts)
        assert tx.execute("MATCH(n:N) RETURN n.id ORDER BY n.id").rows == ((1,),(2,),(3,),(4,))
    assert graph.execute("MATCH(:N)-[r:R]->(:N) RETURN count(r)").rows == ((2,),)
