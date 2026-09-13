"""All typed SET forms must stage the admitted VectorValue, not its input list."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("kind", ["node", "rel"])
@pytest.mark.parametrize("assignment", ["n.embedding=$v", "n += {embedding:$v}", "n = {id:1,embedding:$v}"])
def test_set_vector_forms_admit_components_and_rollback_bad_vectors(tmp_path, codec, kind, assignment):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            if kind == "node":
                tx.execute("CREATE NODE TABLE R(id INT64,embedding VECTOR(s),PRIMARY KEY(id))")
                tx.execute("CREATE(:R {id:1,embedding:$v})", {"v":[1.0,0.0]})
            else:
                tx.execute("CREATE NODE TABLE Anchor(id INT64,PRIMARY KEY(id))")
                tx.execute("CREATE REL TABLE R(FROM Anchor TO Anchor,id INT64,embedding VECTOR(s))")
                tx.execute("CREATE(a:Anchor {id:1})-[:R {id:1,embedding:$v}]->(a)", {"v":[1.0,0.0]})
        pattern = "(n:R)" if kind == "node" else "()-[n:R]->()"
        with db.begin("write") as tx:
            tx.execute(f"MATCH {pattern} SET {assignment}", {"v":[0.0,1.0]})
        for invalid in ([1.0], [float("nan"),1.0], [float("inf"),1.0]):
            with pytest.raises(GrafxError):
                with db.begin("write") as tx:
                    tx.execute(f"MATCH {pattern} SET {assignment}", {"v":invalid})
        with db.begin("read") as reader:
            assert db.search_vectors(reader, table=(kind,"R"), space="s", query=[1.0,0.0], k=1).hits[0].score == 0.0
        db.checkpoint()
    with connect(path, codec=codec) as db:
        with db.begin("read") as reader:
            assert db.search_vectors(reader, table=(kind,"R"), space="s", query=[1.0,0.0], k=1).hits[0].score == 0.0
        with db.begin("write") as tx:
            tx.execute(f"MATCH {pattern} SET {assignment}", {"v":None})
        with db.begin("read") as reader:
            assert db.search_vectors(reader, table=(kind,"R"), space="s", query=[1.0,0.0], k=1).hits == ()
        assert db.verify("all").findings == ()
