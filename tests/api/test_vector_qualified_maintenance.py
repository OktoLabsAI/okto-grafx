"""Qualified vector observations and repair never select a same-space sibling."""

from dataclasses import FrozenInstanceError, replace

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError, GrafxIndexError


def seed(db):
    db.ensure_identity_indexes()
    with db.begin("write") as tx:
        tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
        tx.execute("CREATE NODE TABLE R(id INT64,v VECTOR(s),PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM R TO R,v VECTOR(s))")
        tx.execute("CREATE NODE TABLE Plain(id INT64,PRIMARY KEY(id))")
        tx.execute("CREATE(:R {id:11,v:$v})", {"v": [1.0, 0.0]})
        tx.execute("MATCH(n:R) CREATE(n)-[:R {v:$v}]->(n)", {"v": [0.0, 1.0]})
    db.checkpoint()


def assert_search(db, *, kinds=("node", "rel")):
    with db.begin("read") as tx:
        for kind, score in (("node", 1.0), ("rel", 0.0)):
            if kind not in kinds:
                continue
            result = db.search_vectors(tx, table=(kind, "R"), space="s",
                                       query=[1.0, 0.0], k=1)
            assert len(result.hits) == 1
            assert result.hits[0].score == pytest.approx(score)


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("kind", ["node", "rel"])
def test_qualified_rebuild_preserves_sibling_and_reopens(tmp_path, codec, kind):
    path = tmp_path / "db"
    with connect(path, codec=codec, vector_exact_scan_threshold=0) as db:
        seed(db)
        target_id = db.catalog.catalog.table("R", kind=kind).table_id
        sibling_id = db.catalog.catalog.table("R", kind="rel" if kind == "node" else "node").table_id
        snapshot = db.vectors
        target = snapshot.index("s", table_id=target_id)
        sibling = snapshot.index("s", table_id=sibling_id)
        sibling_bytes = (path / sibling.file).read_bytes()
        sibling_header = db._vectors.index("s", table_id=sibling_id).header
        with pytest.raises(FrozenInstanceError):
            target.table_id = sibling_id
        assert_search(db)
        assert db.vector_memory_usage("s", table=(kind, "R")).cached_entries == 1
        if kind == "node":
            result = db.rebuild_vector_index("s", table=(kind, "R"))
        else:
            result = db.maintenance.rebuild_vector_index("s", table=(kind, "R"))
        assert result.name == target.name and result.table_id == target_id
        assert result.stale is False and result.built_through_lsn is not None
        # Existing contract: this live handle may not invent coverage above the
        # rebuild scan. Only the selected owner keeps that fence, not its sibling.
        with db.begin("read") as tx:
            with pytest.raises(GrafxIndexError) as fenced:
                db.search_vectors(tx, table=(kind, "R"), space="s", query=[1.0, 0.0], k=1)
            assert fenced.value.retryable
        assert_search(db, kinds=("rel" if kind == "node" else "node",))
        assert db.verify("all").findings == ()
        db.checkpoint()
        # Checkpoint may advance certified coverage on every index. It must not
        # reset the sibling generation, change its identity, or rewrite entries.
        after_header = db._vectors.index("s", table_id=sibling_id).header
        assert after_header.built_through_lsn >= sibling_header.built_through_lsn
        assert replace(after_header, built_through_lsn=sibling_header.built_through_lsn) == sibling_header
        page_size = db._identity.page_size
        assert (path / sibling.file).read_bytes()[page_size:] == sibling_bytes[page_size:]
    with connect(path, codec=codec, vector_exact_scan_threshold=0) as db:
        assert snapshot.index("s", table_id=target_id) == target
        assert len(db.vectors.indexes()) == 2
        assert_search(db)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("selector", [None, "R", ("rel", "Missing"), ("node", "Plain"), []])
def test_refusal_precedes_rebuild_claim(tmp_path, monkeypatch, selector):
    with connect(tmp_path / "db") as db:
        seed(db)
        before = db.vectors

        def forbidden(*args, **kwargs):
            raise AssertionError("invalid/ambiguous owner must not claim a durable rebuild")

        monkeypatch.setattr(type(db._transactions), "checkpoint_and_claim_index_rebuild", forbidden)
        with pytest.raises(GrafxError):
            db.rebuild_vector_index("s", table=selector)
        with pytest.raises(GrafxError):
            db.vector_memory_usage("s", table=selector)
        assert db.vectors == before
        assert_search(db)


@pytest.mark.parametrize("identity", [True, 0, -1, "1", 999])
def test_detached_selection_refuses_invalid_or_unknown_owner(tmp_path, identity):
    with connect(tmp_path / "db") as db:
        seed(db)
        view = db.vectors
    with pytest.raises(GrafxIndexError):
        view.index("s", table_id=identity)


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_failed_qualified_rebuild_leaves_only_selected_owner_stale(tmp_path, monkeypatch, codec):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        seed(db)
        node_id = db.catalog.catalog.table("R", kind="node").table_id
        rel_id = db.catalog.catalog.table("R", kind="rel").table_id

        def fail(*args, **kwargs):
            raise GrafxIndexError("injected before rebuild COMMIT", field="test")

        with monkeypatch.context() as scoped:
            scoped.setattr(type(db._indexes), "rebuild", fail)
            with pytest.raises(GrafxIndexError, match="injected"):
                db.rebuild_vector_index("s", table=("rel", "R"))
        assert db.vectors.index("s", table_id=rel_id).stale
        assert not db.vectors.index("s", table_id=node_id).stale
        db.checkpoint()
    with connect(path, codec=codec) as db:
        assert db.vectors.index("s", table_id=rel_id).stale
        assert not db.vectors.index("s", table_id=node_id).stale
        assert db.rebuild_vector_index("s", table=("rel", "R")).stale is False
        assert_search(db, kinds=("node",))
        assert db.verify("all").findings == ()
    with connect(path, codec=codec) as db:
        assert_search(db)
        assert db.verify("all").findings == ()
