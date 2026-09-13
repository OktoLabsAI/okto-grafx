"""Expression-produced entities retain kind, qualified identity and lexical scope."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "bindings") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64)")
            tx.execute("CREATE NODE TABLE Other(id INT64)")
            tx.execute("CREATE REL TABLE R(FROM N TO N,k INT64)")
            tx.execute("CREATE REL TABLE S(FROM N TO Other,k INT64)")
            tx.execute("CREATE(a:N {id:1}), (b:N {id:2}), (c:N {id:3}), (d:Other {id:1}) "
                       "CREATE(a)-[:R {k:10}]->(b), (b)-[:R {k:20}]->(c), (a)-[:S {k:30}]->(d)")
        yield db


@pytest.mark.parametrize("expression", ["coalesce(null,a,b)", "CASE WHEN a.id=1 THEN a ELSE b END",
                                       "coalesce(CASE WHEN true THEN a END,b)"])
def test_node_expression_rematches_identity_instead_of_rescanning(graph, expression):
    query = f"MATCH(a:N {{id:1}}),(b:N {{id:2}}) WITH {expression} AS n MATCH(n)-[:R]->(m) RETURN n.id,m.id"
    assert graph.execute(query).rows == ((1,2),)
    with graph.query(query).cursor(batch_size=1) as cursor:
        assert tuple(cursor) == ((1,2),)


def test_null_coalesced_nodes_do_not_start_a_scan(graph):
    assert graph.execute("MATCH(a:N {id:1}) OPTIONAL MATCH(a)-->(b:Missing) OPTIONAL MATCH(a)-->(c:Missing) "
                         "WITH coalesce(b,c) AS x MATCH(x)-->(d) RETURN d").rows == ()
    assert graph.execute("WITH coalesce(null,null) AS x MATCH(x)-->() RETURN x").rows == ()
    assert graph.execute("MATCH(a:N {id:1}) WITH a,null AS absent WITH a,absent AS renamed "
                         "WITH coalesce(renamed,a) AS n MATCH(n) RETURN n.id").rows == ((1,),)
    assert graph.execute("MATCH(a:N)-[r:R]->() WITH r,null AS absent "
                         "WITH coalesce(absent,[r]) AS rs MATCH()-[rs*1]->() RETURN rs[0].k ORDER BY rs[0].k").rows == ((10,),(20,))


def test_cross_table_case_preserves_node_identity(graph):
    rows = graph.execute("MATCH(a:N {id:1}),(b:Other {id:1}) UNWIND [true,false] AS choose "
                         "WITH CASE WHEN choose THEN a ELSE b END AS n MATCH(n) RETURN n").rows
    assert len(rows) == 2
    assert {row[0].label for row in rows} == {"N","Other"}
    assert rows[0][0].identity != rows[1][0].identity


@pytest.mark.parametrize("expression", ["[r1,r2]", "coalesce(null,[r1,r2])", "CASE WHEN true THEN [r1,r2] ELSE [] END"])
def test_relationship_list_expression_restricts_order_and_endpoints(graph, expression):
    prefix = f"MATCH(a:N)-[r1:R]->()-[r2:R]->(b) WITH {expression} AS rs,a,b "
    assert graph.execute(prefix + "MATCH(first)-[rs*]->(second) RETURN first.id,second.id").rows == ((1,3),)
    assert graph.execute(prefix + "MATCH(a)-[rs*]->(b) RETURN a.id,b.id").rows == ((1,3),)
    assert graph.execute(prefix + "MATCH(b)-[rs*]->(a) RETURN a,b").rows == ()


def test_edge_coalesce_preserves_binding_and_list_is_not_single_edge(graph):
    assert graph.execute("MATCH()-[r:R]->() WITH coalesce(null,r) AS selected "
                         "MATCH(a)-[selected]->(b) RETURN selected.k ORDER BY selected.k").rows == ((10,),(20,))
    with pytest.raises(GrafxError):
        graph.execute("MATCH()-[r:R]->() WITH [r] AS rs MATCH()-[rs]->() RETURN rs")
    with pytest.raises(GrafxError):
        graph.execute("MATCH()-[r:R]->() MATCH()-[r*]->() RETURN r")


def test_range_list_import_and_empty_list(graph):
    assert graph.execute("MATCH(a:N)-[rs:R*2]->(b) CALL(rs) { MATCH(x)-[rs*]->(y) RETURN x.id AS id } RETURN id").rows == ((1,),)
    assert graph.execute("WITH [] AS rs MATCH(a)-[rs*0..0]->(b) RETURN a.id,labels(a),b.id ORDER BY labels(a),a.id").rows == (
        (1,('N',),1),(2,('N',),2),(3,('N',),3),(1,('Other',),1))


@pytest.mark.parametrize("prefix", [
    "WITH [] AS xs",
    "WITH [] AS original WITH original AS xs",
    "WITH [] AS original CALL(original) { RETURN original AS xs }",
    "WITH [] AS original CALL(original) { UNWIND original AS x RETURN length(x) AS unused } WITH [] AS xs",
    "WITH CASE WHEN true THEN [] ELSE null END AS xs",
])
def test_empty_collection_does_not_invent_an_unwind_entity_kind(graph, prefix):
    assert graph.execute(prefix + " UNWIND xs AS x RETURN length(x),labels(x),type(x)").rows == ()


def test_empty_collection_is_neutral_only_among_entity_lists(graph):
    assert graph.execute("MATCH(n:N {id:1}) WITH CASE WHEN true THEN [] ELSE [n] END AS xs "
                         "UNWIND xs AS x RETURN labels(x)").rows == ()
    assert graph.execute("MATCH(n:N {id:1}) WITH [] + [n] AS xs UNWIND xs AS x MATCH(x) RETURN x.id").rows == ((1,),)
    with pytest.raises(GrafxError):
        graph.execute("MATCH(n:N {id:1}) WITH CASE WHEN true THEN n ELSE [] END AS x MATCH(x) RETURN x")


def test_mixed_or_scalar_expressions_cannot_claim_entity_authority(graph):
    for expression in ("coalesce(a,1)", "CASE WHEN true THEN a ELSE 1 END", "[a]", "{id:1}"):
        with pytest.raises(GrafxError):
            graph.execute(f"MATCH(a:N) WITH {expression} AS x MATCH(x) RETURN x")


def test_entity_expression_write_and_late_error_preserve_atomicity(graph, tmp_path):
    with graph.begin("write") as tx:
        tx.execute("MATCH(a:N {id:3}) WITH coalesce(null,a) AS selected SET selected.id=30")
        with pytest.raises(GrafxError):
            tx.execute("MATCH(a:N {id:1}) UNWIND [1,'bad'] AS v WITH coalesce(null,a) AS n,v SET n.id=v")
        assert tx.execute("MATCH(n:N) RETURN n.id ORDER BY n.id").rows == ((1,),(2,),(30,))
    with connect(tmp_path / "bindings") as reader:
        assert reader.execute("MATCH(n:N) RETURN n.id ORDER BY n.id").rows == ((1,),(2,),(30,))
        assert reader.verify("all").findings == ()
