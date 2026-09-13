"""A shared embedding space must retain every physical vector owner."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxIndexError, GrafxUnsupportedOperation


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("threshold", [0, 128])
@pytest.mark.parametrize("peer_kind", ["node", "rel"])
def test_shared_space_search_keeps_physical_owners_across_reopen(tmp_path, codec, threshold, peer_kind):
    path = tmp_path / "db"
    with connect(path, codec=codec, vector_exact_scan_threshold=threshold) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE R(id INT64,embedding VECTOR(s),PRIMARY KEY(id))")
            if peer_kind == "node":
                tx.execute("CREATE NODE TABLE Other(id INT64,embedding VECTOR(s),PRIMARY KEY(id))")
            else:
                tx.execute("CREATE REL TABLE Other(FROM R TO R,id INT64,embedding VECTOR(s))")
            tx.execute("CREATE(:R {id:11,embedding:$v})", {"v":[1.0,0.0]})
            if peer_kind == "node":
                tx.execute("CREATE(:Other {id:22,embedding:$v})", {"v":[0.0,1.0]})
            else:
                tx.execute("MATCH(n:R) CREATE(n)-[:Other {id:22,embedding:$v}]->(n)", {"v":[0.0,1.0]})
        assert len(db.vectors.indexes()) == 2
        assert {index.definition.table_id for index in db._vectors.indexes()} == {1, 2}

    for _ in range(2):
        with connect(path, codec=codec, vector_exact_scan_threshold=threshold) as db:
            assert {index.definition.table_id for index in db._vectors.indexes()} == {1, 2}
            with pytest.raises(GrafxIndexError, match="multiple physical owners"):
                db.vectors.index("s")
            with db.begin("read") as tx:
                with pytest.raises(GrafxIndexError) as ambiguous:
                    db.search_vectors(tx, space="s", query=[1.0,0.0], k=1)
                assert ambiguous.value.details["reason"] == "ambiguous_vector_owner"
                for kind, name, expected_id, expected_score in (
                    ("node", "R", 11, 1.0), (peer_kind, "Other", 22, 0.0),
                ):
                    hits = db.search_vectors(tx, table=(kind,name), space="s", query=[1.0,0.0], k=1,
                                             timeout_seconds=10).hits
                    assert len(hits) == 1
                    assert hits[0].score == pytest.approx(expected_score)
                    pattern = f"(n:{name})" if kind == "node" else f"()-[n:{name}]->()"
                    result = tx.execute(
                        f"MATCH {pattern} WHERE similarity(n.embedding,$q,space=>'s') > -2.0 "
                        "RETURN n.id, similarity_score() AS score ORDER BY score DESC LIMIT 1",
                        {"q":[1.0,0.0]},
                    )
                    assert result.rows == ((expected_id, pytest.approx(expected_score)),)
            assert db.verify("all").findings == ()
            db.checkpoint()


def test_rollback_of_one_owner_keeps_committed_sibling(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE R(id INT64,embedding VECTOR(s),PRIMARY KEY(id))")
        original = db._vectors.index("s")
        tx = db.begin("write")
        try:
            tx.execute("CREATE NODE TABLE Other(id INT64,embedding VECTOR(s),PRIMARY KEY(id))")
            assert len(tuple(db._vectors.indexes())) == 2
        finally:
            tx.rollback()
        assert tuple(db._vectors.indexes()) == (original,)
        assert db._vectors.index("s") is original
        assert db._vectors._map_claims == {}
        assert db.verify("all").findings == ()


def test_shared_space_hybrid_and_clean_owner_reads_in_write_transaction(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            for name in ("R", "Other"):
                tx.execute(f"CREATE NODE TABLE {name}(id INT64,body STRING,embedding VECTOR(s),PRIMARY KEY(id))")
                tx.execute(f"CREATE(:{name} {{id:1,body:'durable',embedding:$v}})", {"v":[1.0,0.0]})
        db.create_text_index("text", "R", ("body",))
        with db.begin("read") as tx:
            result = db.search_hybrid(tx, table="R", index="text", query="durable", space="s",
                                      vector=[1.0,0.0], k=1)
            assert result.regime == "complete" and len(result.hits) == 1
        tx = db.begin("write")
        try:
            tx.execute("MATCH(n:Other) SET n.embedding=$v", {"v":[0.0,1.0]})
            assert db.search_vectors(tx, table=("node","R"), space="s", query=[1.0,0.0], k=1).hits
            with pytest.raises(GrafxUnsupportedOperation, match="staged"):
                db.search_vectors(tx, table=("node","Other"), space="s", query=[1.0,0.0], k=1)
        finally:
            tx.rollback()
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("threshold", [0, 128])
def test_foreign_writer_preserves_pinned_reader_and_owner_boundaries(tmp_path, threshold):
    path = tmp_path / "db"
    with connect(path, vector_exact_scan_threshold=threshold) as first:
        with first.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            for name in ("R", "Other"):
                tx.execute(f"CREATE NODE TABLE {name}(id INT64,embedding VECTOR(s),PRIMARY KEY(id))")
                tx.execute(f"CREATE(:{name} {{id:1,embedding:$v}})", {"v":[1.0,0.0]})
        with connect(path, vector_exact_scan_threshold=threshold) as second:
            with first.begin("read") as old:
                for name in ("R", "Other"):
                    assert first.search_vectors(old, table=name, space="s", query=[1.0,0.0], k=1).hits[0].score == 1.0
                with second.begin("write") as writer:
                    writer.execute("MATCH(n:Other) SET n.embedding=$v", {"v":[0.0,1.0]})
                assert first.search_vectors(old, table="Other", space="s", query=[1.0,0.0], k=1).hits[0].score == 1.0
                with first.begin("read") as fresh:
                    assert first.search_vectors(fresh, table="R", space="s", query=[1.0,0.0], k=1).hits[0].score == 1.0
                    assert first.search_vectors(fresh, table="Other", space="s", query=[1.0,0.0], k=1).hits[0].score == 0.0
        assert first.verify("all").findings == ()
