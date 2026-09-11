"""Native paths share entity identity, snapshot, spill and public admission."""

from dataclasses import FrozenInstanceError
from typing import get_type_hints

import pytest

from okto_grafx import PathValue, NodeValue, RelationshipValue, QueryResult, connect
from okto_grafx.cli.output import jsonable
from okto_grafx.errors import GrafxConfigurationError, GrafxCorruptionDetected


PREFIX = "MATCH p = (a:A)-[r:R]->(b:B) RETURN "
QUERY = PREFIX + "p"


@pytest.fixture(params=[None, 8192], ids=["memory", "spill"])
def graph(tmp_path, request):
    with connect(tmp_path / "db", query_memory_budget_bytes=request.param) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, name STRING, _ID STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, name STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM A TO B, weight INT64, _SRC STRING)")
            tx.execute("CREATE (:A {id:1,name:'alpha',_ID:'user'})-[:R {weight:7,_SRC:'prop'}]->(:B {id:1,name:'beta'})")
        yield db


def test_path_components_are_the_same_qualified_entities_as_direct_returns(graph):
    path, nodes, edges = graph.execute(PREFIX + "p,nodes(p),relationships(p)").rows[0]
    assert type(path) is PathValue and len(path) == 1
    assert path.nodes == nodes and path.relationships == edges
    direct = graph.execute("MATCH (a:A)-[r:R]->(b:B) RETURN a,r,b").rows[0]
    assert (nodes[0], edges[0], nodes[1]) == direct
    assert all(type(n) is NodeValue for n in nodes) and type(edges[0]) is RelationshipValue
    assert nodes[0].identity.record_id == nodes[1].identity.record_id == 1
    assert nodes[0].identity != nodes[1].identity
    assert nodes[0].provenance == direct[0].provenance
    assert edges[0].source == nodes[0].identity and edges[0].target == nodes[1].identity
    assert nodes[0].properties["_ID"] == "user" and edges[0].properties["_SRC"] == "prop"
    assert graph.execute(PREFIX + "nodes(p)[0].name,relationships(p)[0].weight").rows == (("alpha", 7),)


def test_path_union_spill_preserves_identity_properties_and_versions(graph):
    direct = graph.execute(QUERY).rows[0][0]
    assert graph.execute(f"{QUERY} UNION {QUERY}").rows == ((direct,),)
    repeated = graph.execute(f"{QUERY} UNION ALL {QUERY}").rows
    assert len(repeated) == 2 and repeated[0][0] == repeated[1][0] == direct
    for row in repeated:
        assert row[0].nodes[0].provenance == direct.nodes[0].provenance
        assert row[0].relationships[0].properties == direct.relationships[0].properties


def test_pending_path_identity_matches_returned_nodes_and_survives_spill(graph):
    with graph.begin("write") as tx:
        tx.execute("MATCH (n:A) DETACH DELETE n")
        a, edge, b = tx.execute("CREATE (a:A {id:2,name:'new'})-[r:R {weight:9}]->(b:B {id:2,name:'target'}) "
                               "RETURN a,r,b").rows[0]
        path = tx.execute(f"{QUERY} UNION {QUERY}").rows[0][0]
        assert path.nodes == (a, b) and path.relationships == (edge,)
        assert path.relationships[0].source == a.identity
        assert all(n.provenance.pending and not n.identity.committed for n in path.nodes)
        assert path == tx.execute(QUERY).rows[0][0]
    durable = graph.execute(QUERY).rows[0][0]
    assert durable != path and durable.nodes[0].identity.committed


def test_path_cursor_and_old_result_do_not_follow_later_commits(graph):
    with graph.query(f"{QUERY} UNION ALL {QUERY}").cursor(batch_size=1) as cursor:
        old = cursor.fetchone()[0]
        with graph.begin("write") as tx:
            tx.execute("MATCH (n:A) SET n.name='changed'")
        remaining = cursor.fetchone()[0]
        assert remaining == old and remaining.nodes[0].properties["name"] == "alpha"
        assert remaining.nodes[0].provenance == old.nodes[0].provenance
    new = graph.execute(QUERY).rows[0][0]
    assert new == old and hash(new) == hash(old)
    assert new.nodes[0].properties["name"] == "changed"
    assert old.nodes[0].properties["name"] == "alpha"


def test_path_json_is_owned_and_path_is_not_a_parameter_or_write_handle(graph):
    path = graph.execute(QUERY).rows[0][0]
    with pytest.raises(FrozenInstanceError):
        path.nodes = ()
    detached = jsonable(path)
    assert detached["format"] == "grafx.path.v1"
    detached["nodes"][0]["properties"]["name"] = "poisoned"
    assert path.nodes[0].properties["name"] == "alpha"
    with pytest.raises(GrafxConfigurationError):
        graph.execute("RETURN $path", {"path": path})
    assert "PathValue" in str(get_type_hints(QueryResult)["rows"])


@pytest.mark.parametrize("fault", ["future_version", "disconnected", "empty"])
def test_path_spill_rejects_invalid_paths(graph, monkeypatch, request, fault):
    from okto_grafx.engine import query_engine
    original = query_engine._SpillRowCodec._detach

    def future(codec, value):
        result = original(codec, value)
        if fault == "future_version" and type(value) is query_engine.RowBinding:
            return (*result[:7], query_engine._spill_pack_internal(codec._context.snapshot.read_lsn + 1), result[8])
        if type(value) is query_engine._PathValue:
            if fault == "disconnected":
                return (result[0], (result[1][0], result[1][0]), result[2])
            if fault == "empty":
                return (result[0], (), result[2])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(query_engine._SpillRowCodec, "_detach", future)
        if request.node.callspec.params["graph"] is None:
            assert len(graph.execute(f"{QUERY} UNION {QUERY}").rows) == 1
        else:
            with pytest.raises(GrafxCorruptionDetected):
                graph.execute(f"{QUERY} UNION {QUERY}")
    assert len(graph.execute(QUERY).rows) == 1


def test_cross_table_keys_are_independent_but_same_table_collision_rolls_back(tmp_path):
    from okto_grafx.errors import GrafxQueryError
    path = tmp_path / "keys"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(note STRING, id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM A TO B)")
            tx.execute("CREATE REL TABLE S(FROM A TO A)")
        with db.begin("write") as tx:
            tx.execute("CREATE (:A {id:1})-[:R]->(:B {id:1,note:'same key, different position'})")
            with pytest.raises(GrafxQueryError):
                tx.execute("CREATE (:A {id:2})-[:S]->(:A {id:2})")
            assert tx.execute("MATCH (n:A) RETURN n.id").rows == ((1,),)
            tx.execute("CREATE (:A {id:3})")
    with connect(path) as db:
        assert db.execute("MATCH (n:A) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
        assert db.execute("MATCH (a:A)-[r:R]->(b:B) RETURN a.id,b.id").rows == ((1, 1),)
        assert db.execute("MATCH (a:A)-[r:S]->(b:A) RETURN count(*)").rows == ((0,),)


def test_hostile_path_results_are_rebuilt_and_refused_without_entity_authority(graph):
    from okto_grafx.engine.public_views import _query_value_snapshot
    path = graph.execute(QUERY).rows[0][0]

    def snapshot(value):
        return _query_value_snapshot(value, field="result", depth=0, active=set(), allow_entities=True)

    rebuilt = snapshot(path)
    assert rebuilt == path and rebuilt is not path and rebuilt.nodes[0] is not path.nodes[0]
    for nodes, edges in [(path.nodes, ()), ((path.nodes[0], path.nodes[0]), path.relationships),
                         ((path,), ()), (("not an entity",), ())]:
        forged = object.__new__(PathValue)
        object.__setattr__(forged, "nodes", nodes)
        object.__setattr__(forged, "relationships", edges)
        with pytest.raises(GrafxConfigurationError):
            snapshot(forged)
    cyclic = object.__new__(PathValue)
    object.__setattr__(cyclic, "nodes", (cyclic,))
    object.__setattr__(cyclic, "relationships", ())
    with pytest.raises(GrafxConfigurationError):
        snapshot(cyclic)


def test_path_reference_oracle_compares_properties_and_direction_not_identity(graph):
    from dataclasses import replace
    from tools.tck_native import _reference_result_value
    from tools.tck_values import reference_key
    path = graph.execute(QUERY).rows[0][0]
    wrong = PathValue((replace(path.nodes[0], properties={"id": 100}), path.nodes[1]), path.relationships)
    assert wrong == path
    assert reference_key(_reference_result_value(wrong)) != reference_key(_reference_result_value(path))
    reverse = PathValue(path.nodes[::-1], path.relationships)
    assert _reference_result_value(reverse).directions == ("incoming",)
    assert _reference_result_value(path).directions == ("outgoing",)
