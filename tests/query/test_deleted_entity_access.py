"""Owner deletion invalidates live content reads, not independent snapshot observations."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:N {id:1,v:7}),(:N {id:2,v:9})")
            tx.execute("CREATE(:A {v:11})-[:R {v:13}]->(:B {v:17})")
        yield db


@pytest.mark.parametrize("query", [
    "MATCH(n:N {id:1}) DELETE n RETURN n.v",
    "MATCH(n:N {id:1}) DELETE n RETURN n.missing",
    "MATCH(n:N {id:1}) DELETE n RETURN labels(n)",
    "MATCH(n:N {id:1}) DELETE n RETURN properties(n)",
    "MATCH(n:N {id:1}) DELETE n RETURN n:N",
    "MATCH(n:N {id:1}) WITH n AS x DELETE x RETURN coalesce(x,x).v",
    "MATCH(n:N {id:1}) DELETE n RETURN [n][0].v",
    "MATCH(n:N {id:1}) DELETE n WITH n RETURN n.v",
    "MATCH(n:N {id:1}) DELETE n WITH n WHERE n.v>0 RETURN 1",
    "MATCH(n:N {id:1}) WITH n,n.v AS saved DELETE n RETURN saved,n.v",
    "MATCH()-[r:R]->() DELETE r RETURN r.v",
    "MATCH()-[r:R]->() DELETE r RETURN properties(r)",
    "MATCH(n:A)-[r:R]->() DETACH DELETE n RETURN r.v",
    "MATCH(n:A)-[r:R]->() DETACH DELETE n WITH r RETURN properties(r)",
])
def test_deleted_content_refuses_and_rolls_back_whole_statement(graph, query):
    with graph.begin("write") as tx:
        tx.execute("CREATE(:Earlier {v:1})")
        with pytest.raises(GrafxPlanError) as failure:
            tx.execute(query)
        assert failure.value.details["reason"] == "deleted_entity_access"
        assert failure.value.details["query_phase"] == "execution"
        tx.execute("CREATE(:After {v:2})")
    assert graph.execute("MATCH(n:N) RETURN n.v ORDER BY n.v").rows == ((7,),(9,))
    assert graph.execute("MATCH()-[r:R]->() RETURN r.v").rows == ((13,),)
    assert graph.execute("MATCH(n:Earlier) RETURN count(n)").rows == ((1,),)
    assert graph.execute("MATCH(n:After) RETURN count(n)").rows == ((1,),)
    assert graph.verify("all").findings == ()


@pytest.mark.parametrize("prefix", ["", "WITH n "])
@pytest.mark.parametrize("expression", ["n.v", "labels(n)", "properties(n)"])
def test_created_then_deleted_rows_also_refuse_content(tmp_path, prefix, expression):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute(f"CREATE(n:N {{v:1}}) {prefix}DELETE n RETURN {expression}")
            assert failure.value.details["reason"] == "deleted_entity_access"
        assert db.catalog.catalog.tables() == ()
        assert db.verify("all").findings == ()


def test_pending_prior_statement_is_restored_after_failed_delete(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:N {v:3})")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("MATCH(n:N) DELETE n RETURN n.v")
            assert failure.value.details["reason"] == "deleted_entity_access"
        assert db.execute("MATCH(n:N) RETURN n.v").rows == ((3,),)


@pytest.mark.parametrize("budget", [None,32768])
@pytest.mark.parametrize("suffix", [
    "WITH DISTINCT n RETURN n.v",
    "WITH collect(n) AS ns UNWIND ns AS x RETURN properties(x)",
])
def test_blocking_spill_cannot_resurrect_deleted_content(tmp_path, budget, suffix, monkeypatch):
    from okto_grafx.engine.query_engine import _SpillRowCodec
    restored = []
    original = _SpillRowCodec._restore_binding
    def observe(codec, value):
        binding = original(codec, value)
        restored.append(binding)
        return binding
    monkeypatch.setattr(_SpillRowCodec, "_restore_binding", observe)
    count = 16 if "collect" in suffix else 80
    with connect(tmp_path / "db", query_memory_budget_bytes=budget) as db:
        with db.begin("write") as tx:
            tx.execute("UNWIND range(1,$count) AS i CREATE(:N {id:i,v:'payload'})", {"count":count})
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("MATCH(n:N) DELETE n " + suffix)
            assert failure.value.details["reason"] == "deleted_entity_access"
        if budget is not None:
            assert restored, "The deletion guard must be exercised after actual private spill decoding"
        assert db.execute("MATCH(n:N) RETURN count(n)").rows == ((count,),)
        assert db.verify("all").findings == ()


def test_detached_observations_scalars_and_immutable_relationship_type_remain_usable(graph):
    before = graph.execute("MATCH(n:N {id:1}) RETURN n").rows[0][0]
    with graph.begin("write") as tx:
        result = tx.execute("MATCH(n:N {id:1}) WITH n,n.v AS saved DELETE n RETURN saved,n IS NULL,n")
        assert result.rows[0][:2] == (7,False)
        assert result.rows[0][2] == before
        assert tx.execute("MATCH()-[r:R]->() DELETE r RETURN type(r)").rows == (("R",),)
    assert before.properties["v"] == 7
    assert graph.execute("MATCH(n:N) RETURN count(n)").rows == ((1,),)
    assert graph.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((0,),)


def test_independent_old_snapshot_survives_committed_delete(graph):
    with connect(graph.path) as reader:
        with reader.begin("read") as old:
            assert old.execute("MATCH(n:N {id:1}) RETURN n.v,labels(n)").rows == ((7,("N",)),)
            with graph.begin("write") as tx:
                tx.execute("MATCH(n:N {id:1}) DELETE n")
            assert old.execute("MATCH(n:N {id:1}) RETURN n.v,labels(n)").rows == ((7,("N",)),)
        assert reader.execute("MATCH(n:N {id:1}) RETURN n.v").rows == ()


def test_typed_deleted_property_reads_do_not_depend_on_flexible_bags(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v STRING, PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:1,v:'a'})")
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("MATCH(n:N) DELETE n RETURN n.v")
            assert failure.value.details["reason"] == "deleted_entity_access"
        assert db.execute("MATCH(n:N) RETURN n.v").rows == (("a",),)


def test_content_reads_without_current_deletion_do_not_scan_transaction_intents(graph, monkeypatch):
    from okto_grafx.engine.query_engine import _Context
    def forbidden(*args):
        pytest.fail("A normal content read must not scan the transaction's delete intents")
    monkeypatch.setattr(_Context, "already_ended", forbidden)
    assert graph.execute("MATCH(n:N) RETURN n.v,labels(n),properties(n)").rows
    with graph.begin("write") as tx:
        tx.execute("CREATE(:N {id:3,v:15})")
        assert tx.execute("MATCH(n:N) RETURN n.v,labels(n),properties(n)").rows


def test_delete_then_count_preserves_pulse_removal_receipts(graph):
    with graph.begin("write") as tx:
        assert tx.execute("MATCH(n:N) DETACH DELETE n WITH count(n) AS removed RETURN removed").rows == ((2,),)
        assert tx.execute("MATCH()-[r:R]->() DELETE r WITH count(r) AS removed RETURN removed").rows == ((1,),)
    assert graph.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)
    assert graph.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((0,),)
