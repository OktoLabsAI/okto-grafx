"""Missing accelerators remain absent on read and require explicit owned rebuild."""

import hashlib

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxIndexError


def data_tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()
            and p.relative_to(root).parts[0] != "control"}


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("v2", [False, True])
@pytest.mark.parametrize("kind,name", [("node", "N"), ("rel", "E")])
def test_missing_vector_is_not_recreated_by_read_and_repairs_only_selected_owner(tmp_path, codec, v2, kind, name):
    root = tmp_path / "db"
    with connect(root, codec=codec) as db:
        if v2:
            db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE N(id INT64,v VECTOR(s),PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM N TO N,v VECTOR(s))")
            tx.execute("CREATE(:N {id:1,v:$v})", {"v": [1.0, 0.0]})
            tx.execute("MATCH(n:N) CREATE(n)-[:E {v:$v}]->(n)", {"v": [0.0, 1.0]})
        selected_id = db.catalog.catalog.table(name, kind=kind).table_id
        missing = db.vectors.index("s", table_id=selected_id)
        sibling = next(index for index in db.vectors.indexes() if index.table_id != selected_id)
        db.checkpoint()
    (root / missing.file).unlink()  # Only this test's freshly created accelerator.
    before = data_tree(root)
    with connect(root, codec=codec, read_only=True) as db:
        assert db.execute("MATCH(:N)-[e:E]->(:N) RETURN count(e)").rows == ((1,),)
        assert not (root / missing.file).exists()
        with db.begin("read") as reader:
            with pytest.raises(GrafxIndexError):
                db.search_vectors(reader, space="s", table=(kind, name), query=[1.0, 0.0], k=1)
    assert data_tree(root) == before
    with connect(root, codec=codec) as db:
        assert db.vectors.index("s", table_id=selected_id).stale
        with db.begin("read") as reader:
            with pytest.raises(GrafxIndexError):
                db.search_vectors(reader, space="s", table=(kind, name), query=[1.0, 0.0], k=1)
        repaired = db.rebuild_vector_index("s", table=(kind, name))
        assert repaired.table_id == selected_id and repaired.name == missing.name
        assert repaired.stale is False
        assert db.vectors.index("s", table_id=sibling.table_id).name == sibling.name
        assert db.verify("all").findings == ()
        db.checkpoint()
    for _ in range(2):
        with connect(root, codec=codec, read_only=True) as db:
            with db.begin("read") as reader:
                for target, expected in ((("node", "N"), 1.0), (("rel", "E"), 0.0)):
                    hits = db.search_vectors(reader, space="s", table=target, query=[1.0, 0.0], k=1).hits
                    assert len(hits) == 1 and hits[0].score == pytest.approx(expected)
            assert db.verify("all").findings == ()
