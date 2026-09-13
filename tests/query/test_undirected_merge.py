"""Undirected MERGE searches both orientations and creates only the written one."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


def write(db,query,parameters=None):
    with db.begin("write") as tx:
        return tx.execute(query,parameters)


def test_creates_left_to_right_and_endpoint_functions_keep_native_nodes(tmp_path):
    with connect(tmp_path / "db") as db:
        result=write(db,"CREATE(a:A {v:1}),(b:B {v:2}) MERGE p=(a)-[r:R]-(b) RETURN startNode(r).v,endNode(r).v,p")
        assert result.rows[0][:2] == (1,2)
        path=result.rows[0][2]
        assert path.relationships[0].source == path.nodes[0].identity
        assert path.relationships[0].target == path.nodes[1].identity


@pytest.mark.parametrize("typed", [False, True])
def test_reverse_match_does_not_install_forward_member(tmp_path,typed):
    with connect(tmp_path / "db") as db:
        if typed:
            db.ensure_identity_indexes()
            write(db,"CREATE NODE TABLE A(v INT64)")
            write(db,"CREATE NODE TABLE B(v INT64)")
            write(db,"CREATE REL TABLE R(FROM B TO A,v INT64)")
        write(db,"CREATE(a:A {v:1}),(b:B {v:2}),(b)-[:R {v:3}]->(a)")
        before=db.catalog.catalog.tables()
        result=write(db,"MATCH(a:A),(b:B) MERGE p=(a)-[r:R]-(b) ON MATCH SET r.v=r.v+1 RETURN r.v,startNode(r).v,endNode(r).v,p")
        assert result.rows[0][:3] == (4,2,1)
        assert db.catalog.catalog.tables() == before
        path=result.rows[0][3]
        assert path.relationships[0].source == path.nodes[1].identity
        assert path.relationships[0].target == path.nodes[0].identity
        assert db.verify("all").findings == ()


def test_both_orientations_duplicates_and_self_loop_actions(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(a:N {id:1}),(b:N {id:2}),(a)-[:R {v:1}]->(b),(b)-[:R {v:2}]->(a),(b)-[:R {v:3}]->(a),(a)-[:R {v:4}]->(a)")
        result=write(db,"MATCH(a:N {id:1}),(b:N {id:2}) MERGE(a)-[r:R]-(b) ON MATCH SET r.v=r.v+10 RETURN r.v ORDER BY r.v")
        assert result.rows == ((11,),(12,),(13,))
        assert write(db,"MATCH(a:N {id:1}) MERGE(a)-[r:R]-(a) ON MATCH SET r.v=r.v+10 RETURN r.v").rows == ((14,),)


def test_sorted_native_endpoints_keep_reverse_match_and_forward_creation(tmp_path):
    with connect(tmp_path / "db",query_memory_budget_bytes=8192) as db:
        write(db,"CREATE(a:N {id:1}),(b:N {id:2}),(b)-[:R {v:3}]->(a)")
        query="MATCH(a:N {id:1}),(b:N {id:2}) WITH a,b ORDER BY a.id "
        assert write(db,query+"MERGE(a)-[r:R]-(b) ON MATCH SET r.v=4 RETURN r.v").rows == ((4,),)
        write(db,query+"CREATE(a)-[:S]->(b)")
        assert db.execute("MATCH(a)-[r]->(b) RETURN a.id,type(r),b.id ORDER BY type(r)").rows == ((2,"R",1),(1,"S",2))
        assert db.verify("all").findings == ()


def test_typed_reverse_only_schema_refuses_creation_in_unsupported_forward_pair(tmp_path):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        write(db,"CREATE NODE TABLE A(v INT64)")
        write(db,"CREATE NODE TABLE B(v INT64)")
        write(db,"CREATE REL TABLE R(FROM B TO A)")
        write(db,"CREATE(:A {v:1}),(:B {v:2})")
        with pytest.raises(GrafxError):
            write(db,"MATCH(a:A),(b:B) MERGE(a)-[:R]-(b)")
        assert db.execute("MATCH()-[r]->() RETURN count(*)").rows == ((0,),)


def test_double_arrow_write_refuses_with_warm_plan_and_read_stays_undirected(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(a:N {id:1})-[:R]->(b:N {id:2})")
        query="MATCH(a:N {id:1}),(b:N {id:2}) MERGE(a)-[:R]-(b)"
        write(db,query)
        for keyword in ("CREATE","MERGE"):
            with pytest.raises(GrafxError) as failure:
                write(db,f"MATCH(a:N {{id:1}}),(b:N {{id:2}}) {keyword}(a)<-[:R]->(b)")
            assert failure.value.details["reason"] == "requires_directed_relationship"
        write(db,query)
        assert db.execute("MATCH(a)<-[:R]->(b) RETURN a.id,b.id ORDER BY a.id").rows == ((1,2),(2,1))


@pytest.mark.parametrize("expression", ["startNode(r)","endNode(r)","CASE WHEN true THEN startNode(r) ELSE endNode(r) END"])
def test_endpoint_node_alias_can_update_rematch_and_delete(tmp_path,expression):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(:A {v:1})-[:R]->(:B {v:2})")
        with db.begin("write") as tx:
            result=tx.execute(f"MATCH()-[r:R]->() WITH {expression} AS n SET n.v=9 WITH n MATCH(n) RETURN n.v,labels(n)")
            assert result.rows == ((9,("B" if expression=="endNode(r)" else "A",)),)
            tx.execute(f"MATCH()-[r:R]->() DETACH DELETE {expression}")
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((1,),)
        assert db.verify("all").findings == ()
