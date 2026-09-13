"""Conditional MERGE writes reuse SET and the enclosing statement boundary."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


def write(db, query, parameters=None):
    with db.begin("write") as tx:
        return tx.execute(query, parameters)


@pytest.mark.parametrize("typed", [False, True])
def test_create_match_ordered_actions_and_unselected_runtime_errors(tmp_path, typed):
    with connect(tmp_path / "db") as db:
        if typed:
            write(db,"CREATE NODE TABLE N(id INT64,v INT64,PRIMARY KEY(id))")
        created = write(db,"MERGE(n:N {id:1}) ON MATCH SET n.v=1/0 ON CREATE SET n.v=10 ON CREATE SET n.v=n.v+2 RETURN n.v")
        assert created.rows == ((12,),)
        matched = write(db,"MERGE(n:N {id:1}) ON CREATE SET n.v=1/0 ON MATCH SET n.v=n.v+3 RETURN n.v")
        assert matched.rows == ((15,),)
        assert db.verify("all").findings == ()


def test_each_match_is_updated_and_later_inputs_observe_previous_actions(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(:N {key:1,v:1}),(:N {key:1,v:2})")
        result = write(db,"UNWIND [1,1] AS i MERGE(n:N {key:i}) ON MATCH SET n.v=n.v+10 RETURN n.v ORDER BY n.v")
        assert result.rows == ((11,), (12,), (21,), (22,))
        assert db.execute("MATCH(n:N) RETURN n.v ORDER BY n.v").rows == ((21,), (22,))


@pytest.mark.parametrize("direction", ["out", "in"])
def test_bound_relationship_action_and_node_aliases(tmp_path, direction):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(:N {id:1}),(:N {id:2})")
        pattern = "(a)-[r:R]->(b)" if direction == "out" else "(b)<-[r:R]-(a)"
        query = ("MATCH(a:N {id:1}),(b:N {id:2}) MERGE " + pattern +
                 " ON CREATE SET r.v=3,a.created=true ON MATCH SET r.v=r.v+1,b.matched=true RETURN r.v")
        assert write(db,query).rows == ((3,),)
        assert write(db,query).rows == ((4,),)
        assert db.execute("MATCH(a:N {id:1})-[r:R]->(b) RETURN a.created,r.v,b.matched").rows == ((True,4,True),)


def test_subquery_imports_actions_and_pending_identity(tmp_path):
    with connect(tmp_path / "db") as db:
        result = write(db,"UNWIND [1,1,2] AS i CALL(i) { MERGE(n:N {id:i}) ON CREATE SET n.v=i ON MATCH SET n.v=n.v+10 RETURN n,n.v AS captured } RETURN n.id,n.v,captured ORDER BY n.id,captured")
        # Entity identity follows the later owner-visible update; the projected
        # scalar preserves the value captured by its individual invocation.
        assert result.rows == ((1,11,1), (1,11,11), (2,2,2))
        assert db.execute("MATCH(n:N) RETURN n.id,n.v ORDER BY n.id").rows == ((1,11), (2,2))


@pytest.mark.parametrize("branch", ["CREATE", "MATCH"])
def test_late_failure_restores_prior_statement_and_schema(tmp_path, branch):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Keep {id:9})")
            if branch == "MATCH":
                tx.execute("CREATE(:N {id:1,v:10}),(:N {id:2,v:20})")
            with pytest.raises(GrafxError):
                tx.execute("UNWIND [1,2] AS i MERGE(n:N {id:i}) ON " + branch +
                           " SET n.v=10/(2-i) RETURN n")
            assert tx.execute("MATCH(n:Keep) RETURN n.id").rows == ((9,),)
            assert tx.execute("MATCH(n:N) RETURN n.id,n.v ORDER BY n.id").rows == (
                ((1,10),(2,20)) if branch == "MATCH" else ())
        assert db.verify("all").findings == ()
    with connect(path) as db:
        assert db.execute("MATCH(n:Keep) RETURN n.id").rows == ((9,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("query", [
    "MERGE(n:N) ON CREATE SET other.v=1 RETURN n",
    "MERGE(n:N) ON MATCH SET n.v=sum(1) RETURN n",
    "MERGE(n:N) ON MATCH SET n.v=$missing RETURN n",
])
def test_invalid_inactive_actions_fail_before_any_effects(tmp_path, query):
    with connect(tmp_path / "db") as db:
        with pytest.raises(GrafxError):
            write(db,query)
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((0,),)


def test_read_only_refusal_and_pinned_reader(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        write(db,"CREATE(:N {id:1,v:1})")
        with db.begin("read") as reader, connect(path) as writer:
            with pytest.raises(GrafxError):
                reader.execute("MERGE(n:N {id:1}) ON MATCH SET n.v=2")
            assert write(writer,"MERGE(n:N {id:1}) ON MATCH SET n.v=2 RETURN n.v").rows == ((2,),)
            assert reader.execute("MATCH(n:N) RETURN n.v").rows == ((1,),)
        assert db.execute("MATCH(n:N) RETURN n.v").rows == ((2,),)


def test_every_matching_relationship_gets_its_action(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(a:A),(b:B),(a)-[:R {v:1}]->(b),(a)-[:R {v:2}]->(b)")
        result = write(db,"MATCH(a:A),(b:B) MERGE(a)-[r:R]->(b) ON MATCH SET r.v=r.v+10 RETURN r.v ORDER BY r.v")
        assert result.rows == ((11,), (12,))
        assert db.execute("MATCH()-[r:R]->() RETURN r.v ORDER BY r.v").rows == ((11,), (12,))


def test_conflicting_conditional_updates_preserve_occ(tmp_path):
    from okto_grafx.errors import GrafxWriteConflict
    path = tmp_path / "db"
    with connect(path) as left, connect(path) as right:
        write(left,"CREATE(:N {id:1,v:0})")
        first, second = left.begin("write"), right.begin("write")
        try:
            for tx in (first,second):
                assert tx.execute("MERGE(n:N {id:1}) ON MATCH SET n.v=n.v+1 RETURN n.v").rows == ((1,),)
            first.commit()
            with pytest.raises(GrafxWriteConflict):
                second.commit()
        finally:
            if first.active:
                first.rollback()
            if second.active:
                second.rollback()
        assert left.execute("MATCH(n:N) RETURN n.v").rows == ((1,),)
        assert left.verify("all").findings == ()


def test_action_structure_limits_and_description():
    from dataclasses import replace
    from okto_grafx.domain.query import parse, analyze
    from okto_grafx.domain.query.limits import MAX_CLAUSES
    query = parse("MERGE(n:N {id:1}) ON CREATE SET n.v=1 ON MATCH SET n.v=2 RETURN n.v")
    clause = query.updating_clauses[0]
    assert clause.describe() == "MERGE (n:N {id: 1}) ON CREATE SET n.v = 1 ON MATCH SET n.v = 2"
    for invalid in ([], (object(),), clause.on_create*(MAX_CLAUSES+1)):
        bad = replace(clause, on_create=invalid)
        with pytest.raises(GrafxError):
            analyze(replace(query,updating_clauses=(bad,),clause_pipeline=(bad,)))


def test_updating_union_actions_share_visibility_and_rollback(tmp_path):
    with connect(tmp_path / "db") as db:
        result = write(db,"MERGE(n:N {id:1}) ON CREATE SET n.v=1 RETURN n.v AS v UNION ALL MERGE(n:N {id:1}) ON MATCH SET n.v=n.v+10 RETURN n.v AS v")
        assert result.rows == ((1,), (11,))
        with pytest.raises(GrafxError):
            write(db,"MERGE(n:N {id:2}) ON CREATE SET n.v=2 UNION ALL MERGE(n:N {id:1}) ON MATCH SET n.v=1/0")
        assert db.execute("MATCH(n:N) RETURN n.id,n.v").rows == ((1,11),)


def test_pattern_nondeterminism_is_not_reevaluated_on_creation(tmp_path):
    with connect(tmp_path / "db") as db:
        assert write(db,"MERGE(n:N {v:rand()}) ON CREATE SET n.copy=n.v RETURN n.v=n.copy").rows == ((True,),)
        write(db,"CREATE(:A),(:B)")
        assert write(db,"MATCH(a:A),(b:B) MERGE(a)-[r:R {v:rand()}]->(b) ON CREATE SET r.copy=r.v RETURN r.v=r.copy").rows == ((True,),)
