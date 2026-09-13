"""Native multi-label creation and owner-wide MERGE, without query rewriting."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_create_complete_label_sets_without_duplicating_nodes(tmp_path, codec):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        with db.begin("write") as tx:
            rows = tx.execute("CREATE (a:A:B {id:1}), (b:B:A {id:2}), (c:A:A {id:3}) RETURN a,b,c").rows
            a,b,c = rows[0]
            assert a.labels == b.labels == ("A", "B") and c.labels == ("A",)
            assert len({a.identity,b.identity,c.identity}) == 3
            assert tx.execute("MATCH (n:A:B) RETURN n.id ORDER BY n.id").rows == ((1,), (2,))
            assert tx.execute("MATCH (n) RETURN count(n)").rows == ((3,),)
        assert len(db.catalog.catalog.tables()) == 2
    with connect(path) as db:
        assert db.execute("MATCH (n:B) RETURN n.id ORDER BY n.id").rows == ((1,), (2,))
        assert not db.verify("all").findings


@pytest.mark.parametrize("label", ["São Paulo", "x.y", "line\nbreak", "nul\x00name", "λ"])
def test_nonphysical_label_names_keep_identity_and_native_properties(tmp_path, label):
    escaped = "`" + label.replace("`", "``") + "`"
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            value = tx.execute(f"CREATE (n:{escaped}:Shared {{id:1}}) RETURN n").rows[0][0]
            assert value.labels == tuple(sorted((label,"Shared")))
            assert tx.execute(f"MATCH (n:{escaped}) RETURN n.id").rows == ((1,),)
        assert db.execute("MATCH (n:Shared) RETURN count(n)").rows == ((1,),)
        assert not db.verify("all").findings


def test_typed_owner_keeps_constraints_and_failed_statement_leaves_no_candidates(tmp_path):
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("CREATE (:N:Discarded {id:'bad'})")
            assert tx.execute("MATCH (n) RETURN count(n)").rows == ((0,),)
            assert "Discarded" not in db._queries._working_catalog(tx._context).table("N").node_label_candidates
            tx.execute("CREATE (:N:Good {id:1})")
            with pytest.raises(GrafxError):
                tx.execute("CREATE (:N:Other {id:1})")
        assert db.execute("MATCH (n) RETURN labels(n)").rows == ((("Good", "N"),),)


@pytest.mark.parametrize("setup", ["CREATE (:A:B {id:1})", "CREATE (n:C {id:1}) SET n:A:B"])
def test_merge_reuses_membership_not_a_physical_table_name(tmp_path, setup):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute(setup)
        existing = db.execute("MATCH (n) RETURN n").rows[0][0]
        with db.begin("write") as tx:
            result = tx.execute("MERGE (n:B:A {id:1}) ON MATCH SET n.hit=true RETURN n")
            assert result.rows[0][0].identity == existing.identity
        assert db.execute("MATCH (n) RETURN count(n)").rows == ((1,),)
        assert not db.catalog.catalog.has_table("B", kind="node")


def test_merge_complete_membership_and_same_statement_pending_identity(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("UNWIND [1,1,2] AS id MERGE (n:A:B {id:id}) RETURN n")
            assert result.rows[0][0].identity == result.rows[1][0].identity
            assert result.rows[0][0].identity != result.rows[2][0].identity
            tx.execute("MATCH (n) WHERE n.id=1 REMOVE n:B")
            result = tx.execute("MERGE (n:B:A {id:1}) RETURN labels(n)")
            assert result.rows == ((("A", "B"),),)
            assert tx.execute("MATCH (n) RETURN count(n)").rows == ((3,),)
        assert not db.verify("all").findings


def test_merge_freezes_membership_before_on_match_changes_another_candidate(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:A:B {id:1}), (:A {id:2})")
        with db.begin("write") as tx:
            result = tx.execute("MATCH (m:A {id:2}) MERGE (n:B) ON MATCH SET m:B RETURN n.id")
            assert result.rows == ((1,),)
            assert tx.execute("MATCH (n:B) RETURN n.id ORDER BY n.id").rows == ((1,), (2,))


@pytest.mark.parametrize("suffix,expected", [
    ("WITH a MATCH (n:B:A) RETURN n.id", ((1,),)),
    ("WITH a CALL() { MATCH (n:B) RETURN n.id AS id } RETURN id", ((1,),)),
    ("WITH a RETURN EXISTS { MATCH (n:B) }", ((True,),)),
    ("WITH a RETURN [(a)-[:R]->(n:B) | n.id]", (((1,),),)),
    ("WITH a MATCH p=(a)-[:R*0..1]->(n:B) RETURN length(p) ORDER BY length(p)", ((0,), (1,))),
])
def test_new_labels_are_visible_to_nested_read_shapes(tmp_path, suffix, expected):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE (a:A:B {id:1}) CREATE (a)-[:R]->(a) " + suffix)
            assert result.rows == expected


@pytest.mark.parametrize("verb", ["CREATE", "MERGE"])
def test_same_owner_endpoints_with_different_labels_remain_one_physical_pair(tmp_path, verb):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            rows = tx.execute(f"{verb} p=(a:N:B {{id:1}})-[:R]->(b:N:C {{id:2}}) RETURN p").rows
            assert rows[0][0].nodes[0].labels == ("B", "N")
            assert rows[0][0].nodes[1].labels == ("C", "N")
    with connect(path) as db:
        assert db.execute("MATCH (a:B)-[:R]->(b:C) RETURN a.id,b.id").rows == ((1,2),)
        assert not db.verify("all").findings


def test_empty_input_never_installs_creation_owner_or_label_candidates(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("UNWIND [] AS x CREATE (:A:B)")
            tx.execute("UNWIND [] AS x MERGE (:C:D)")
        assert db.catalog.catalog.tables() == ()


def test_late_merge_failure_unwinds_private_phases_but_preserves_prior_statement(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:Safe {id:0})")
            with pytest.raises(GrafxError):
                tx.execute("UNWIND [1,2,null] AS id MERGE (n:N:B {id:id}) RETURN n")
            assert tx.execute("MATCH (n) RETURN n.id,labels(n)").rows == ((0,("Safe",)),)
            assert not db._queries._working_catalog(tx._context).has_table("N", kind="node")
        assert db.execute("MATCH (n) RETURN n.id").rows == ((0,),)
        assert not db.verify("all").findings


def test_legacy_catalog_label_admission_refuses_with_explicit_remedy(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1})")
            with pytest.raises(GrafxError) as failure:
                tx.execute("CREATE (:N:B {id:2})")
            assert failure.value.details["remedy"] == "maintenance.ensure_identity_indexes"
            assert tx.execute("MATCH (n) RETURN n.id,labels(n)").rows == ((1,("N",)),)
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE (:N:B {id:2})")
        assert db.execute("MATCH (n:B) RETURN n.id").rows == ((2,),)


def test_whole_path_merge_matches_label_sets_without_partial_reuse(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:A:B {id:1})-[:R]->(:C:B {id:2})")
        with db.begin("write") as tx:
            result = tx.execute("MERGE p=(a:B:A {id:1})-[:R]->(b:B:C {id:2}) RETURN p")
            assert len(result.rows) == 1
            assert result.rows[0][0].nodes[0].labels == ("A", "B")
            assert result.rows[0][0].nodes[1].labels == ("B", "C")
            assert tx.execute("MATCH (n) RETURN count(n)").rows == ((2,),)
