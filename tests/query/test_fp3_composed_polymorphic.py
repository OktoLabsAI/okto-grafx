"""Composed polymorphic reads retain multiplicity, qualified bindings and one snapshot."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.query.analysis import analyze, polymorphic_node
from okto_grafx.domain.query.parser import parse
from okto_grafx.errors import GrafxQueryBudgetExceeded


@pytest.fixture
def db(tmp_path):
    with connect(tmp_path / "db") as handle:
        with handle.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, name STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM A TO B)")
            tx.execute("CREATE (:A {id:1,name:'one'}), (:A {id:2,name:'two'}), (:B {id:1})")
        yield handle


@pytest.mark.parametrize("query,expected", [
    ("MATCH () RETURN count(*)", ((3,),)),
    ("MATCH (a:A {id:1}) WITH * MATCH (n) RETURN n.id ORDER BY n.id", ((1,), (1,), (2,))),
    ("MATCH (a), (b) RETURN count(*)", ((9,),)),
    ("UNWIND [1,2] AS id MATCH (n {id:id}) RETURN id,label(n) AS kind ORDER BY id,kind",
     ((1,"A"), (1,"B"), (2,"A"))),
    ("MATCH (n {name:'one'}) RETURN label(n),n.id", (("A",1),)),
    ("MATCH (n {missing:1}) RETURN count(*)", ((0,),)),
    ("MATCH (n) MATCH (n) RETURN count(*)", ((3,),)),
    ("MATCH (n) WITH n AS m MATCH (m) RETURN count(*)", ((3,),)),
    ("MATCH (n) WITH DISTINCT n RETURN count(*)", ((3,),)),
    ("MATCH (n) WITH n, count(*) AS c RETURN label(n),n.id,c ORDER BY n.id,label(n)",
     (("A",1,1),("B",1,1),("A",2,1))),
    ("MATCH (n) MATCH (n:A) RETURN n.id ORDER BY n.id", ((1,), (2,))),
    ("MATCH (n) MATCH (n {id:1}) RETURN count(*)", ((2,),)),
    ("UNWIND [1,3] AS id OPTIONAL MATCH (n {id:id}) RETURN id,n.id ORDER BY id", ((1,1),(1,1),(3,None))),
    ("OPTIONAL MATCH (n {id:99}) MATCH (n) RETURN count(*)", ((0,),)),
    ("OPTIONAL MATCH (n {id:99}) WITH n AS m MATCH (m:A) RETURN count(*)", ((0,),)),
    ("MATCH (n) WITH n.id AS id MATCH (n) RETURN count(*)", ((9,),)),
    ("MATCH (a:A {id:1}) OPTIONAL MATCH (n {id:99}), (b:B) RETURN a.id,n.id,b.id", ((1,None,None),)),
    ("MATCH (n) CALL (n) { MATCH (n) RETURN n.id AS id } RETURN id ORDER BY id", ((1,),(1,),(2,))),
    ("CALL { MATCH (n) RETURN n } WITH n AS m MATCH (m) RETURN m.id ORDER BY m.id", ((1,),(1,),(2,))),
    ("MATCH (n) CALL (n) { WITH * RETURN n.name AS name } RETURN name ORDER BY name", (("one",),("two",),(None,))),
])
def test_composed_reads_return_exact_rows(db, query, expected):
    assert db.execute(query).rows == expected
    assert db.execute(query).rows == expected


def test_rematch_does_not_add_a_second_all_nodes_scan(db):
    plan = db.explain("MATCH (n) MATCH (n) RETURN n.id")
    assert sum(node.label == "AllNodesScan" for node in plan.walk()) == 1


def test_composition_does_not_remove_an_available_typed_anchor_seek(db):
    plan = db.explain("MATCH (a:A {id:1}) WITH * MATCH (n) RETURN n.id")
    assert any(node.label == "IndexSeek" for node in plan.walk())


def test_scope_discard_rebinds_a_real_node_before_polymorphic_write():
    query = parse("MATCH (n:A) WITH n.id AS id MATCH (n) SET n.id=2")
    assert polymorphic_node(query) is not None
    assert next(binding for binding in analyze(query).bindings if binding.name == "n").entity == "node"


def test_different_property_types_do_not_refuse_empty_or_filtered_reads(db):
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE C(id INT64, name INT64, PRIMARY KEY(id))")
    assert db.execute("UNWIND [] AS i MATCH (n) RETURN n.name").rows == ()
    assert db.execute("MATCH (n {name:'one'}) RETURN n.id").rows == ((1,),)


def test_composed_reads_see_private_overlay_without_exposing_it_to_other_readers(db):
    with db.begin("read") as old:
        assert old.execute("UNWIND [0] AS x MATCH (n) RETURN count(*)").rows == ((3,),)
        with db.begin("write") as writer:
            writer.execute("CREATE (:B {id:3})")
            writer.execute("MATCH (n:A {id:2}) DELETE n")
            writer.execute("MATCH (n:A {id:1}) SET n.name='changed'")
            assert writer.execute("UNWIND [1] AS id MATCH (n {id:id}) RETURN n.name ORDER BY n.name").rows == (("changed",),(None,))
            assert old.execute("UNWIND [0] AS x MATCH (n) RETURN count(*)").rows == ((3,),)
            assert old.execute("MATCH (n {id:2}) RETURN n.id").rows == ((2,),)
        assert old.execute("MATCH (n {id:2}) RETURN n.id").rows == ((2,),)
    assert db.execute("UNWIND [0] AS x MATCH (n) RETURN n.id ORDER BY n.id").rows == ((1,),(1,),(3,))


def test_multiple_scan_cursor_close_and_budget_failures_release_transactions(db):
    cursor = db.query("MATCH (n), (m) RETURN n.id,m.id").cursor(batch_size=1)
    assert cursor.fetchone() is not None
    cursor.close()
    assert db.transactions.open_transactions == 0
    # A composed cross product is bounded globally, not independently per table.
    with connect(":memory:", max_intermediate_rows=3) as limited:
        with limited.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1}), (:N {id:2})")
        with pytest.raises(GrafxQueryBudgetExceeded):
            limited.execute("MATCH (n), (m) RETURN n.id,m.id")
        assert limited.transactions.open_transactions == 0
