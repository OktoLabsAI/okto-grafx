"""CREATE/MERGE capture native paths under the existing outer write boundary."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.mark.parametrize("keyword", ["CREATE", "MERGE"])
def test_capture_one_node_path(tmp_path, keyword):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute(f"{keyword} p=(n {{v:1}}) RETURN p,length(p),nodes(p)[0].v")
            assert result.rows[0][1:] == (0,1)
            assert len(result.rows[0][0].nodes) == 1


def test_merge_captures_anonymous_edge_and_actions_observe_path(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE(a:A),(b:B) MERGE p=(a)-[:R]->(b) ON CREATE SET a.hops=length(p) RETURN p,a.hops")
            assert result.rows[0][1] == 1
            assert len(result.rows[0][0].relationships) == 1


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_created_path_keeps_incoming_direction_cycle_and_deletion(tmp_path, codec):
    with connect(tmp_path / "db",codec=codec) as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE p=(a:A {id:1})<-[:R]-(b:B {id:2})-[:S]->(a) RETURN p,length(p)")
            path = result.rows[0][0]
            assert result.rows[0][1] == 2
            assert path.nodes[0].identity == path.nodes[2].identity
            assert path.relationships[0].source == path.nodes[1].identity
            assert path.relationships[0].target == path.nodes[0].identity
        with db.begin("write") as tx:
            with pytest.raises(GrafxError) as failure:
                tx.execute("MATCH(a:A),(b:B) MERGE p=(a)<-[:R]-(b) WITH p DELETE p,relationships(p)[0]")
            assert failure.value.details["reason"] == "connected_node_delete"
            assert tx.execute("MATCH()-[r]->() RETURN count(*)").rows == ((2,),)
        assert db.verify("all").findings == ()


def test_merge_named_path_duplicates_preserve_each_matching_edge(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a:A),(b:B),(a)-[:R {v:1}]->(b),(a)-[:R {v:2}]->(b)")
        with db.begin("write") as tx:
            result = tx.execute("MATCH(a:A),(b:B) MERGE p=(a)-[r:R]->(b) ON MATCH SET r.hops=length(p) RETURN p,r.v ORDER BY r.v")
            assert [row[1] for row in result.rows] == [1,2]
            assert result.rows[0][0].relationships[0].identity != result.rows[1][0].relationships[0].identity


def test_delete_then_merge_finishes_entire_prior_mutation(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:A {v:1}),(:A {v:2})")
        with db.begin("write") as tx:
            result = tx.execute("MATCH(a:A) DELETE a MERGE p=(n:A) RETURN n.v,p")
            assert tuple(row[0] for row in result.rows) == (None,None)
            assert result.rows[0][1].nodes[0].identity == result.rows[1][1].nodes[0].identity
        assert db.execute("MATCH(n:A) RETURN count(*)").rows == ((1,),)


def test_named_paths_compose_with_imported_calls_and_rollback(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Keep)")
            with pytest.raises(GrafxError):
                tx.execute("UNWIND [1,2] AS i CALL(i){ CREATE p=(:N {id:i})-[:R]->(:N) RETURN p,1/(2-i) AS v } RETURN p,v")
            assert tx.execute("MATCH(n:N) RETURN count(*)").rows == ((0,),)
        assert db.execute("MATCH(n:Keep) RETURN count(*)").rows == ((1,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("keyword", ["CREATE", "MERGE"])
@pytest.mark.parametrize("shape", ["(n)","(n:N)-[:R]->()","(n {})-[:R]->()"])
def test_bound_nodes_cannot_be_redeclared_even_on_empty_input(tmp_path, keyword, shape):
    with connect(tmp_path / "db") as db:
        with pytest.raises(GrafxError) as failure:
            with db.begin("write") as tx:
                tx.execute(f"MATCH(n:Absent) {keyword} {shape}")
        assert failure.value.details["reason"] == "variable_already_bound"
        assert failure.value.details["query_phase"] == "planning"
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((0,),)


@pytest.mark.parametrize("kind", ["node", "relationship"])
def test_merge_null_properties_refuse_at_runtime_and_restore_previous_statement(tmp_path, kind):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a:A),(b:B)")
            query = ("MERGE(:N {v:$value})" if kind == "node" else
                     "MATCH(a:A),(b:B) MERGE(a)-[:R {v:$value}]->(b)")
            with pytest.raises(GrafxError) as failure:
                tx.execute(query,{"value":None})
            assert failure.value.details["reason"] == "merge_null_property"
            assert failure.value.details["query_phase"] == "execution"
            assert tx.execute("MATCH(n) RETURN count(*)").rows == ((2,),)
        assert db.verify("all").findings == ()
