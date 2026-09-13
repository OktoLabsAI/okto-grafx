"""UNWIND retains native entity identity/authority instead of inventing a scan."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxWriteConflict


@pytest.fixture(params=[None,32768])
def graph(tmp_path, request):
    path = tmp_path / "graph"
    with connect(path, query_memory_budget_bytes=request.param) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a:A {id:1}),(b:A {id:2}),(c:B {id:1}) "
                       "CREATE(a)-[:R {v:1}]->(b),(a)-[:S {v:2}]->(c)")
        yield db, path


@pytest.mark.parametrize("transform", ["nodes", "nodes[..]", "[x IN nodes | x]",
    "[x IN nodes WHERE x.id=1 | x]", "coalesce(null,nodes)",
    "CASE WHEN true THEN nodes ELSE null END"])
def test_collected_polymorphic_nodes_can_be_written_and_rematched(graph, transform):
    db, path = graph
    with db.begin("write") as tx:
        result = tx.execute("MATCH(a) WHERE a.id=1 WITH collect(a) AS nodes "
                            f"WITH {transform} AS selected UNWIND selected AS n "
                            "SET n.changed=true WITH n MATCH(n) RETURN labels(n),n.id,n.changed ORDER BY labels(n)")
        assert result.rows == ((("A",),1,True),(("B",),1,True))
    with connect(path) as reader:
        assert reader.execute("MATCH(n) WHERE n.changed=true RETURN count(*)").rows == ((2,),)
        assert reader.execute("MATCH(n:A {id:2}) RETURN n.changed").rows == ((None,),)
        assert reader.verify("all").findings == ()


@pytest.mark.parametrize("transform", ["edges", "edges[..]", "[r IN edges | r]"])
def test_collected_edges_keep_actual_type_endpoints_and_support_delete(graph, transform):
    db, _ = graph
    with db.begin("write") as tx:
        result = tx.execute("MATCH()-[r]->() WITH collect(r) AS edges "
                            f"WITH {transform} AS selected UNWIND selected AS edge "
                            "SET edge.v=edge.v+10 WITH edge MATCH(a)-[edge]->(b) "
                            "RETURN type(edge),a.id,labels(b),edge.v ORDER BY type(edge)")
        assert result.rows == (("R",1,("A",),11),("S",1,("B",),12))
        tx.execute("MATCH()-[r:R]->() WITH collect(r) AS edges UNWIND edges AS edge DELETE edge")
    assert db.execute("MATCH()-[r]->() RETURN type(r),r.v").rows == (("S",12),)
    assert db.verify("all").findings == ()


def test_collection_snapshot_properties_and_live_unwound_node(graph):
    db, _ = graph
    with db.begin("write") as tx:
        result = tx.execute("MATCH(a:A) WITH collect(a) AS nodes "
                            "WITH nodes,[x IN nodes | x.id] AS oldIds "
                            "UNWIND nodes AS n SET n.id=n.id+10 RETURN n.id,oldIds ORDER BY n.id")
        assert {row[0] for row in result.rows} == {11,12}
        assert all(sorted(row[1]) == [1,2] for row in result.rows)


def test_repeated_entity_identity_is_not_deduplicated(graph):
    db, _ = graph
    with db.begin("write") as tx:
        result = tx.execute("MATCH(a:A {id:1}) UNWIND [a,a] AS n SET n.id=n.id+1 RETURN n.id")
        assert len(result.rows) == 2
    assert db.execute("MATCH(a:A) RETURN a.id ORDER BY a.id").rows == ((2,),(3,))


def test_comprehension_local_shadowing_and_outer_entity_capture(graph):
    db, _ = graph
    assert db.execute("MATCH(x:A {id:1}),(b:B) WITH x,[b] AS nodes "
                      "UNWIND [x IN nodes | x] AS n MATCH(n) RETURN labels(n)").rows == ((("B",),),)
    assert db.execute("MATCH(a:A {id:1}) UNWIND [x IN [1,2] | a] AS n MATCH(n) RETURN n.id").rows == ((1,),(1,))


@pytest.mark.parametrize("expression", ["[{id:1}]", "[a,1]", "$rows", "[x IN [1,2] | x]"])
def test_scalar_or_unproven_carriers_do_not_claim_write_authority(graph, expression):
    db, _ = graph
    with db.begin("write") as tx:
        with pytest.raises(GrafxError):
            tx.execute(f"MATCH(a:A) UNWIND {expression} AS n SET n.id=99", {"rows":[{"id":1}]})
    assert db.execute("MATCH(a:A) RETURN a.id ORDER BY a.id").rows == ((1,),(2,))


def test_late_invalid_write_rolls_back_all_unwound_entity_changes(graph):
    db, path = graph
    with db.begin("write") as tx:
        tx.execute("CREATE(:Before)")
        with pytest.raises(GrafxError):
            tx.execute("MATCH(a:A) WITH collect(a) AS nodes UNWIND nodes AS n "
                       "SET n.changed=true WITH n RETURN 1/(n.id-2)")
        tx.execute("CREATE(:After)")
    with connect(path) as reader:
        assert reader.execute("MATCH(n:A) RETURN n.changed ORDER BY n.id").rows == ((None,),(None,))
        assert reader.execute("MATCH(n) RETURN count(*)").rows == ((5,),)
        assert reader.verify("all").findings == ()


def test_new_pending_entity_repetition_keeps_insert_identity(graph):
    db, _ = graph
    with db.begin("write") as tx:
        assert len(tx.execute("CREATE(a:Fresh {v:0}) WITH a UNWIND [a,a] AS n SET n.v=n.v+1 RETURN n.v").rows) == 2
    assert db.execute("MATCH(n:Fresh) RETURN n.v").rows == ((2,),)


def test_deleted_entity_unwind_allows_count_but_not_resurrection(graph):
    db, _ = graph
    with db.begin("write") as tx:
        with pytest.raises(GrafxError) as failure:
            tx.execute("MATCH(a:A {id:2}) DETACH DELETE a WITH [a] AS nodes UNWIND nodes AS n SET n.id=9")
        assert failure.value.details["reason"] == "deleted_entity_access"
        assert tx.execute("MATCH(a:A {id:2}) DETACH DELETE a WITH [a] AS nodes UNWIND nodes AS n RETURN count(n)").rows == ((1,),)


def test_unwound_writes_preserve_other_reader_snapshot_and_writer_conflicts(graph):
    db, path = graph
    read_query = "MATCH(a:A {id:1}) WITH collect(a) AS nodes UNWIND nodes AS n RETURN n.id"
    write_query = "MATCH(a:A {id:1}) WITH collect(a) AS nodes UNWIND nodes AS n SET n.id=$id"
    with connect(path) as other:
        reader = db.begin("read")
        writer = db.begin("write")
        try:
            assert reader.execute(read_query).rows == ((1,),)
            writer.execute(write_query, {"id":7})
            with other.begin("write") as winner:
                winner.execute(write_query, {"id":8})
            assert reader.execute(read_query).rows == ((1,),)
            with pytest.raises(GrafxWriteConflict):
                writer.commit()
        finally:
            if writer.active:
                writer.rollback()
            reader.rollback()
    assert db.execute("MATCH(a:A) RETURN a.id ORDER BY a.id").rows == ((2,),(8,))


@pytest.mark.parametrize("budget", [None,32768])
def test_typed_entity_unwind_preserves_column_and_primary_key_constraints(tmp_path, budget):
    path = tmp_path / "typed"
    with connect(path, query_memory_budget_bytes=budget) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T(id INT64,v INT64,PRIMARY KEY(id))")
            tx.execute("CREATE(:T {id:1,v:1}),(:T {id:2,v:2})")
        with db.begin("write") as tx:
            assert tx.execute("MATCH(a:T) WITH collect(a) AS nodes UNWIND nodes AS n "
                              "SET n.v=n.v+10 RETURN n.id,n.v ORDER BY n.id").rows == ((1,11),(2,12))
            with pytest.raises(GrafxError):
                tx.execute("MATCH(a:T) WITH collect(a) AS nodes UNWIND nodes AS n SET n.id=0")
            with pytest.raises(GrafxError):
                tx.execute("MATCH(a:T) WITH collect(a) AS nodes UNWIND nodes AS n SET n.v='wrong type'")
            assert tx.execute("MATCH(n:T) RETURN n.id,n.v ORDER BY n.id").rows == ((1,11),(2,12))
    with connect(path) as db:
        assert db.execute("MATCH(n:T) RETURN n.id,n.v ORDER BY n.id").rows == ((1,11),(2,12))
        assert db.verify("all").findings == ()
