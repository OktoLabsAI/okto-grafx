"""Entity/map metadata functions use native bindings and explicit type phases."""

import pytest

import okto_grafx
from okto_grafx.errors import GrafxParseError, GrafxPlanError


@pytest.fixture(params=[None, 8192], ids=["memory", "spill-budget"])
def graph(tmp_path, request):
    with okto_grafx.connect(tmp_path / "scalars", query_memory_budget_bytes=request.param) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, value INT64, note STRING, _ID STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE Q(id INT64, name STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM P TO P, value INT64, _SRC STRING)")
            tx.execute("CREATE (:P {id:1,value:7,_ID:'user'}), (:P {id:2,note:'text'}), (:Q {id:1,name:'other'})")
            tx.execute("MATCH (a:P {id:1}), (b:P {id:2}) CREATE (a)-[:E {value:9,_SRC:'user'}]->(b)")
        yield db


def test_properties_on_nodes_edges_and_maps_have_no_structural_metadata(graph):
    row = graph.execute("MATCH (a:P {id:1})-[r:E]->(b:P) "
                        "RETURN properties(a),properties(r),properties(b),labels(a),type(r)").rows[0]
    assert row == ({"id": 1, "value": 7, "_ID": "user"},
                   {"value": 9, "_SRC": "user"}, {"id": 2, "note": "text"}, ("P",), "E")
    assert graph.execute("RETURN properties({key:null, nested:[1,{x:2}]})").rows == (
        ({"key": None, "nested": (1, {"x": 2})},),)


def test_nulls_and_optional_absence_are_preserved(graph):
    assert graph.execute("RETURN properties(null),labels(null),type(null)").rows == ((None, None, None),)
    assert graph.execute("OPTIONAL MATCH (n:P {id:99}) RETURN properties(n),labels(n)").rows == ((None, None),)
    assert graph.execute("MATCH (a:P {id:2}) OPTIONAL MATCH (a)-[r:E]->(b:P) "
                         "RETURN properties(r),type(r)").rows == ((None, None),)


def test_polymorphic_nodes_and_composed_path_components(graph):
    rows = graph.execute("MATCH (n) RETURN labels(n),properties(n) ORDER BY labels(n),n.id").rows
    assert len(rows) == 3
    assert rows[-1] == (("Q",), {"id": 1, "name": "other"})
    row = graph.execute("MATCH p=(a:P {id:1})-[r:E]->(b:P) WITH p "
                        "RETURN properties(nodes(p)[0]),labels(nodes(p)[1]),type(relationships(p)[0])").rows[0]
    assert row == ({"id": 1, "value": 7, "_ID": "user"}, ("P",), "E")


def test_union_subquery_and_aggregation_preserve_result_types(graph):
    rows = graph.execute("CALL () { MATCH (n:P {id:1}) RETURN n UNION MATCH (n:Q {id:1}) RETURN n } "
                         "RETURN labels(n),properties(n)").rows
    assert {row[0] for row in rows} == {("P",), ("Q",)}
    assert len(graph.execute("MATCH (n:P) RETURN collect(properties(n))").rows[0][0]) == 2


def test_own_set_and_remove_nulls_are_visible_without_changing_other_reader(graph):
    before = graph.execute("MATCH (n:P {id:1}) RETURN properties(n)").rows
    with graph.begin("write") as tx:
        row = tx.execute("MATCH p=(n:P {id:1}) SET n.value=11,n.note='new',n._ID=null "
                         "RETURN properties(n),properties(nodes(p)[0])").rows[0]
        assert row == ({"id": 1, "value": 11, "note": "new"},) * 2
        assert graph.execute("MATCH (n:P {id:1}) RETURN properties(n)").rows == before
    assert graph.execute("MATCH (n:P {id:1}) RETURN properties(n)").rows == (row[:1],)


def test_pending_insert_and_relationship_properties(graph):
    with graph.begin("write") as tx:
        row = tx.execute("CREATE (n:P {id:99,value:3}) RETURN properties(n),labels(n)").rows[0]
        assert row == ({"id": 99, "value": 3}, ("P",))
        row = tx.execute("MATCH (a:P {id:1}), (b:P {id:99}) CREATE (a)-[r:E {value:4}]->(b) "
                         "RETURN properties(r),type(r)").rows[0]
        assert row == ({"value": 4}, "E")


def test_invalid_dynamic_value_after_writes_rolls_back_whole_statement(graph, monkeypatch):
    import okto_grafx.engine.query_engine as engine

    original = engine._write_assignments
    applied = []

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        applied.append(True)
        return result

    monkeypatch.setattr(engine, "_write_assignments", observed)
    with graph.begin("write") as tx:
        tx.execute("CREATE (:Q {id:99})")
        with pytest.raises(GrafxPlanError) as raised:
            tx.execute("MATCH (n:P {id:1}) UNWIND [n,1] AS item SET n.value=8 RETURN properties(item)")
        assert applied
        assert raised.value.details["query_phase"] == "execution"
        assert tx.execute("MATCH (n:P {id:1}) RETURN n.value").rows == ((7,),)
        assert tx.execute("MATCH (n:Q {id:99}) RETURN n.id").rows == ((99,),)


def test_map_parameter_is_owned_and_short_circuit_does_not_call_bad_branch(graph):
    parameter = {"nested": [{"x": 1}], "empty": None}
    result = graph.execute("RETURN properties($value)", {"value": parameter}).rows[0][0]
    result["nested"][0]["x"] = 9
    assert parameter["nested"][0]["x"] == 1
    assert graph.execute("RETURN CASE WHEN true THEN {} ELSE properties($bad) END", {"bad": 1}).rows == (({},),)
    assert graph.execute("MATCH (n:P {id:99}) RETURN properties($bad)", {"bad": 1}).rows == ()
    with pytest.raises(GrafxPlanError) as raised:
        graph.execute("RETURN properties($bad)", {"bad": 1})
    assert raised.value.details["query_phase"] == "execution"


def test_dynamic_nodes_edges_and_nulls_are_not_conflated(graph):
    rows = graph.execute("MATCH (a:P {id:1})-[r:E]->(b:P) UNWIND [a,r,null] AS item "
                         "RETURN properties(item)").rows
    assert rows == (({"id": 1, "value": 7, "_ID": "user"},),
                    ({"value": 9, "_SRC": "user"},), (None,))


def test_entity_scalars_consume_list_selections_and_case_results(graph):
    row = graph.execute("MATCH (n:P {id:1}) WITH [n,1] AS items "
                        "RETURN labels(items[0]),properties(items[0])").rows[0]
    assert row == (("P",), {"id": 1, "value": 7, "_ID": "user"})
    assert graph.execute("MATCH (n:P {id:1}) RETURN properties(CASE WHEN $selected THEN n ELSE null END)",
                         {"selected": True}).rows == ((row[1],),)
    with pytest.raises(GrafxPlanError) as raised:
        graph.execute("MATCH (n:P {id:1}) WITH [n,1] AS items RETURN labels(items[1])")
    # LIST<ANY> remains dynamically typed for entity scalar admission even when
    # a literal index names the invalid element (original Graph3 #0009).
    assert raised.value.details["query_phase"] == "execution"
    assert raised.value.details["reason"] == "entity_function_argument_type"
    with pytest.raises(GrafxPlanError) as raised:
        graph.execute("MATCH (n:P {id:1}) WITH [n,1] AS items RETURN labels(items[$slot])", {"slot": 1})
    assert raised.value.details["query_phase"] == "execution"


def test_full_properties_materializes_vector_landing(tmp_path):
    with okto_grafx.connect(tmp_path / "vectors") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE emb {dimension:3, metric:'cosine'}")
            tx.execute("CREATE NODE TABLE N(id INT64, v VECTOR(emb), PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N)")
            tx.execute("CREATE (:N {id:1,v:[1.0,2.0,3.0]}), (:N {id:2,v:[3.0,2.0,1.0]})")
            tx.execute("MATCH (a:N {id:1}), (b:N {id:2}) CREATE (a)-[:R]->(b)")
        direct = db.execute("MATCH (n:N {id:2}) RETURN n.v").rows[0][0]
        row = db.execute("MATCH (a:N {id:1})-[:R]->(b:N) RETURN properties(b),labels(b)").rows[0]
        assert row[0]["v"] == direct
        assert row[0]["id"] == 2 and row[1] == ("N",)


def test_properties_cursor_keeps_original_snapshot_and_releases_reader(graph):
    token = okto_grafx.CancellationToken()
    with graph.query("MATCH (n:P) RETURN properties(n) ORDER BY n.id").cursor(
            batch_size=1, cancellation=token) as cursor:
        assert cursor.fetchone()[0]["value"] == 7
        with graph.begin("write") as tx:
            tx.execute("MATCH (n:P {id:2}) SET n.note='changed'")
        assert cursor.fetchone()[0]["note"] == "text"
    assert graph.transactions.open_transactions == 0


@pytest.mark.parametrize("expression", ["properties(1)", "properties('x')", "properties([true,false])",
                                        "labels({})", "type([])"])
def test_statically_invalid_types_are_planning_errors(graph, expression):
    with pytest.raises(GrafxPlanError) as raised:
        graph.execute(f"RETURN {expression}")
    assert raised.value.details["reason"] == "entity_function_argument_type"
    assert raised.value.details["query_phase"] == "planning"


@pytest.mark.parametrize("query", [
    "MATCH (n:P) RETURN type(n)",
    "MATCH (a:P)-[r:E]->(b:P) RETURN labels(r)",
    "MATCH p=(n:P) RETURN properties(p)",
    "MATCH p=(n:P) RETURN labels(p)",
    "MATCH (a:P)-[r:E*1..1]->(b:P) RETURN type(r)",
])
def test_wrong_entity_kind_is_refused_before_reading_rows(graph, query):
    with pytest.raises(GrafxPlanError) as raised:
        graph.execute(query)
    assert raised.value.details["query_phase"] == "planning"


@pytest.mark.parametrize("function", ["properties", "labels", "type"])
def test_dynamic_invalid_argument_has_execution_phase(graph, function):
    with pytest.raises(GrafxPlanError) as raised:
        graph.execute(f"UNWIND [null,1] AS item RETURN {function}(item)")
    assert raised.value.details["reason"] == "entity_function_argument_type"
    assert raised.value.details["query_phase"] == "execution"


@pytest.mark.parametrize("expression,error", [("properties()", GrafxPlanError), ("properties(1,2)", GrafxPlanError),
                                             ("labels(*)", GrafxParseError), ("type(DISTINCT null)", GrafxParseError)])
def test_entity_functions_have_one_positional_argument(graph, expression, error):
    with pytest.raises(error):
        graph.execute(f"RETURN {expression}")
