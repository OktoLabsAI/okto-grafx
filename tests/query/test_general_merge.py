"""Whole-pattern MERGE must never reuse a merely partial unbound match."""

import pytest

from okto_grafx import connect
from okto_grafx.api import assembly
from okto_grafx.errors import GrafxError, GrafxWriteConflict
from okto_grafx.domain.errors import GrafxQueryBudgetExceeded


def write(db, query, parameters=None):
    with db.begin("write") as tx:
        return tx.execute(query, parameters)


@pytest.fixture(params=["pure", "numpy"])
def db(tmp_path, request):
    with connect(tmp_path / "db", codec=request.param) as database:
        yield database
        assert database.verify("all").findings == ()


def test_unbound_single_edge_is_idempotent_across_inputs(db):
    query = "UNWIND [1,1,1] AS i MERGE p=(a:A {v:i})-[r:R]->(b:B {v:2}) RETURN p"
    paths = write(db, query).rows
    assert len(paths) == 3
    assert len({row[0].nodes[0].identity for row in paths}) == 1
    assert db.execute("MATCH(n) RETURN count(*)").rows == ((2,),)
    assert db.execute("MATCH()-[r]->() RETURN count(*)").rows == ((1,),)


def test_partial_unbound_match_creates_the_whole_pattern(db):
    write(db, "CREATE(:A {v:1})-[:R]->(:B {v:2})")
    query = "MERGE p=(a:A {v:1})-[:R]->(b:B {v:2})-[:S]->(c:C {v:3}) RETURN p"
    write(db, query)
    assert db.execute("MATCH(n) RETURN count(*)").rows == ((5,),)
    assert db.execute("MATCH()-[r]->() RETURN count(*)").rows == ((3,),)
    write(db, query)
    assert db.execute("MATCH(n) RETURN count(*)").rows == ((5,),)


def test_bound_anchor_reused_but_unbound_middle_not_reused(db):
    write(db, "CREATE(a:A {v:1})-[:R]->(:B {v:2})")
    query = "MATCH(a:A) MERGE p=(a)-[:R]->(b:B {v:2})-[:S]->(c:C) RETURN p"
    write(db, query)
    assert db.execute("MATCH(n) RETURN count(*)").rows == ((4,),)
    write(db, query)
    assert db.execute("MATCH(n) RETURN count(*)").rows == ((4,),)


def test_multiple_complete_matches_actions_and_repeated_node_cycle(db):
    write(db, "CREATE(a:N {v:1})-[:R {v:1}]->(b:N {v:2}),"
          "(a)-[:R {v:2}]->(b),(b)-[:S]->(a)")
    result = write(db, "MERGE p=(a:N {v:1})-[r:R]->(b:N {v:2})-[:S]->(a) "
                   "ON MATCH SET r.hit=true ON CREATE SET a.created=true RETURN r.v,p ORDER BY r.v")
    assert tuple(row[0] for row in result.rows) == (1, 2)
    for _, path in result.rows:
        assert path.nodes[0].identity == path.nodes[-1].identity
    assert db.execute("MATCH(n) RETURN count(*)").rows == ((2,),)
    assert db.execute("MATCH()-[r:R]->() RETURN r.hit").rows == ((True,), (True,))


def test_incoming_and_undirected_general_pattern(db):
    query = "MERGE p=(a:A {v:1})<-[:R]-(b:B {v:2})-[:S]-(c:C {v:3}) RETURN p"
    write(db, query)
    first = db.execute("MATCH p=(:A)<-[:R]-(:B)-[:S]-(:C) RETURN p").rows[0][0]
    second = write(db, query).rows[0][0]
    assert tuple(n.identity for n in first.nodes) == tuple(n.identity for n in second.nodes)
    assert first.relationships[0].source == first.nodes[1].identity
    assert first.relationships[1].source == first.nodes[1].identity


def test_late_null_and_action_error_restore_all_statement_effects(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(:Keep)")
        for query, reason in (
            ("UNWIND [1,null] AS i MERGE(:A {v:i})-[:R]->(:B)-[:S]->(:C)", "merge_null_property"),
            ("UNWIND [1,2] AS i MERGE(a:A {v:i})-[:R]->(:B) ON CREATE SET a.v=1/(2-i)", None),
        ):
            with pytest.raises(GrafxError) as failure:
                tx.execute(query)
            if reason is not None:
                assert failure.value.details["reason"] == reason
            assert tx.execute("MATCH(n) RETURN count(*)").rows == ((1,),)
        assert tx.execute("MATCH(n:Keep) RETURN count(*)").rows == ((1,),)


def test_unlabeled_match_keeps_actual_labels_and_homonymous_relationship(db):
    write(db, "CREATE(:R {v:1})-[:R]->(:B {v:2})")
    result = write(db, "MERGE(a {v:1})-[r:R]->(b {v:2}) RETURN labels(a),labels(b),type(r)")
    assert result.rows == ((("R",), ("B",), "R"),)
    assert db.execute("MATCH(n) RETURN count(*)").rows == ((2,),)


def test_entire_match_set_is_fixed_before_conditional_actions(db):
    write(db, "CREATE(a:A {v:1})-[:R]->(b:B),(a)-[:R]->(b)")
    result = write(db, "MERGE(a:A {v:1})-[:R]->(b:B) ON MATCH SET a.v=a.v+1 RETURN a.v")
    assert result.rows == ((2,), (3,))


def test_relationship_is_not_reused_within_a_whole_pattern(db):
    write(db, "CREATE(a:A)-[:R]->(a)")
    write(db, "MATCH(a:A) MERGE(a)-[:R]->(a)-[:R]->(a)")
    assert db.execute("MATCH()-[r]->() RETURN count(*)").rows == ((3,),)


def test_native_merge_in_imported_writing_subquery(db):
    query = ("UNWIND [1,1,2] AS i CALL(i){ MERGE p=(a:A {v:i})-[:R]->(:B)-[:S]->(:C) "
             "ON CREATE SET a.hops=length(p) RETURN a } RETURN a.v,a.hops")
    assert write(db, query).rows == ((1,2), (1,2), (2,2))
    assert db.execute("MATCH(n) RETURN count(*)").rows == ((6,),)


def test_typed_primary_key_prevents_partial_reuse_and_rolls_back(db):
    db.ensure_identity_indexes()
    write(db, "CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
    write(db, "CREATE REL TABLE R(FROM N TO N)")
    write(db, "CREATE(:N {id:1})")
    with pytest.raises(GrafxError):
        write(db, "MERGE(:N {id:1})-[:R]->(:N {id:2})")
    assert db.execute("MATCH(n:N) RETURN n.id").rows == ((1,),)
    assert db.execute("MATCH()-[r:R]->() RETURN count(*)").rows == ((0,),)
    assert write(db, "MATCH(a:N {id:1}) MERGE(a)-[:R]->(b:N {id:2}) RETURN b.id").rows == ((2,),)


def test_match_memory_refusal_precedes_actions_and_preserves_prior_statement(tmp_path):
    path = tmp_path / "budget"
    with connect(path) as db:
        write(db, "CREATE(a:A {v:1}),(b:B) WITH a,b UNWIND range(1,40) AS i CREATE(a)-[:R]->(b)")
    with connect(path, query_memory_budget_bytes=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Keep)")
            with pytest.raises(GrafxQueryBudgetExceeded) as failure:
                tx.execute("MERGE(a:A {v:1})-[:R]->(:B) ON MATCH SET a.changed=true")
            assert failure.value.details["operator"] == "merge_matches"
            assert tx.execute("MATCH(a:A) RETURN a.changed").rows == ((None,),)
        assert db.execute("MATCH(n:Keep) RETURN count(*)").rows == ((1,),)
        assert db.verify("all").findings == ()


def test_properties_are_evaluated_once_for_match_and_creation(tmp_path, monkeypatch):
    samples = iter((0.25, 0.75, 0.25, 0.75))
    observed = []

    def draw():
        value = next(samples)
        observed.append(value)
        return value

    monkeypatch.setattr(assembly, "_new_query_random_source", lambda: draw)
    with connect(tmp_path / "db") as db:
        query = "MERGE(a:A {v:rand()})-[r:R {v:rand()}]->(:B) RETURN a.v,r.v"
        assert write(db, query).rows == ((0.25, 0.75),)
        assert write(db, query).rows == ((0.25, 0.75),)
        assert observed == [0.25, 0.75, 0.25, 0.75]
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((2,),)


def test_conflicting_whole_pattern_actions_preserve_occ(tmp_path):
    path = tmp_path / "db"
    with connect(path) as left, connect(path) as right:
        write(left, "CREATE(:A {v:0})-[:R]->(:B)-[:S]->(:C)")
        first, second = left.begin("write"), right.begin("write")
        try:
            for tx in (first, second):
                assert tx.execute("MERGE(a:A)-[:R]->(:B)-[:S]->(:C) ON MATCH SET a.v=a.v+1 RETURN a.v").rows == ((1,),)
            first.commit()
            with pytest.raises(GrafxWriteConflict):
                second.commit()
        finally:
            if first.active:
                first.rollback()
            if second.active:
                second.rollback()
        assert left.execute("MATCH(a:A) RETURN a.v").rows == ((1,),)
        assert left.verify("all").findings == ()


def test_read_only_door_refuses_without_schema_effects(db):
    before = db.catalog.catalog.tables()
    with pytest.raises(GrafxError):
        db.execute("MERGE(:A)-[:R]->(:B)-[:S]->(:C)")
    assert db.catalog.catalog.tables() == before


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_reopen_and_independent_reader_visibility(tmp_path, codec):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        write(db, "CREATE(:Keep)")
        with connect(path, codec=codec) as peer, peer.begin("read") as reader:
            assert reader.execute("MATCH(n) RETURN count(*)").rows == ((1,),)
            write(db, "MERGE(:A)-[:R]->(:B)-[:S]->(:C)")
            assert reader.execute("MATCH(n) RETURN count(*)").rows == ((1,),)
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((4,),)
    for _ in range(2):
        with connect(path, codec=codec) as db:
            write(db, "MERGE(:A)-[:R]->(:B)-[:S]->(:C)")
            assert db.execute("MATCH(n) RETURN count(*)").rows == ((4,),)
            assert db.verify("all").findings == ()
