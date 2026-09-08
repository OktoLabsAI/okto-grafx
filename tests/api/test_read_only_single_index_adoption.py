"""Read-only admission adopts artifacts once, after consistency and catalog loading."""

from collections import Counter
import hashlib

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.index import IndexVisibility
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.recovery_manager import RecoveryManager
from okto_grafx.engine.txn_manager import TransactionManager


def _data_tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*")
            if p.is_file() and not p.relative_to(root).as_posix().startswith("control/")}


def _seed(root, managed):
    with connect(root, page_size=512) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, title STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM A TO B)")
            tx.execute("CREATE (:A {id:1,title:'before'})")
            tx.execute("CREATE (:B {id:2})")
            tx.execute("MATCH (a:A),(b:B) CREATE (a)-[:E]->(b)")
        if managed:
            db.ensure_identity_indexes()
        definitions = db._catalog.catalog.active_index_definitions()
        expected = {d.name: d.file for d in definitions if d.visibility is IndexVisibility.EXACT}
        db.checkpoint()
    return expected


@pytest.mark.parametrize("managed", [False, True])
def test_each_exact_artifact_is_adopted_once_after_consistency(tmp_path, monkeypatch, managed):
    root = tmp_path / "db"
    expected = _seed(root, managed)
    before = _data_tree(root)
    calls = Counter()
    events = []
    original_adopt = IndexManager.adopt_committed
    original_consistency = RecoveryManager.require_read_only_consistent

    def consistent(self):
        result = original_consistency(self)
        events.append("consistent")
        return result

    def adopt(self, index, **kwargs):
        assert events and events[0] == "consistent"
        calls[index.name] += 1
        return original_adopt(self, index, **kwargs)

    monkeypatch.setattr(RecoveryManager, "require_read_only_consistent", consistent)
    monkeypatch.setattr(IndexManager, "adopt_committed", adopt)
    with connect(root, page_size=512, read_only=True) as reader:
        assert calls == Counter({name: 1 for name in expected})
        assert reader.execute("MATCH (a:A)-[:E]->(b:B) RETURN a.title,b.id").rows == (("before", 2),)
    assert _data_tree(root) == before


@pytest.mark.parametrize("damage", ["missing", "torn"])
def test_final_admission_still_refuses_active_artifact_damage(tmp_path, monkeypatch, damage):
    root = tmp_path / "db"
    definitions = _seed(root, True)
    target = root / definitions["pk_A"]
    assert target.name.startswith("g_") and target.is_file()
    original_init = TransactionManager.__init__
    after_damage = {}

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if kwargs.get("writable") is False:
            # Isolated fault injection at the boundary between catalog and final
            # artifact admission: removing the redundant pass must not move the
            # remaining proof earlier or turn it into remembered authority.
            if damage == "missing":
                target.unlink()
            else:
                target.write_bytes(b"")
            after_damage.update(_data_tree(root))

    monkeypatch.setattr(TransactionManager, "__init__", initialize)
    with pytest.raises(GrafxError):
        with connect(root, page_size=512, read_only=True):
            pass
    assert after_damage
    assert _data_tree(root) == after_damage


def test_read_only_snapshots_still_follow_independent_writer_commits(tmp_path):
    root = tmp_path / "db"
    _seed(root, True)
    query = "MATCH (a:A {id:1}) RETURN a.title"
    with connect(root, page_size=512) as writer:
        with connect(root, page_size=512, read_only=True) as reader:
            with reader.begin("read") as snapshot:
                assert snapshot.execute(query).rows == (("before",),)
                with writer.begin("write") as tx:
                    tx.execute("MATCH (a:A {id:1}) SET a.title='after'")
                assert snapshot.execute(query).rows == (("before",),)
            assert reader.execute(query).rows == (("after",),)


@pytest.mark.parametrize("managed", [False, True])
def test_read_only_vector_registry_is_ready_after_single_admission(tmp_path, managed):
    root = tmp_path / "vectors"
    with connect(root, page_size=512) as writer:
        with writer.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:4,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE V(id INT64, v VECTOR(s), PRIMARY KEY(id))")
            tx.execute("CREATE (:V {id:1,v:[1.0,0.0,0.0,0.0]})")
        if managed:
            writer.ensure_identity_indexes()
        writer.checkpoint()
    before = _data_tree(root)
    with connect(root, page_size=512, read_only=True) as reader:
        with reader.begin("read") as tx:
            result = reader.search_vectors(tx, space="s", query=(1.0, 0.0, 0.0, 0.0), k=1)
            assert result.achieved_k == 1
            assert result.hits[0].record_id == 1
    assert _data_tree(root) == before


def test_later_foreign_ddl_still_synchronizes_the_reader_registry(tmp_path):
    root = tmp_path / "db"
    _seed(root, True)
    with connect(root, page_size=512) as writer:
        with connect(root, page_size=512, read_only=True) as reader:
            assert reader.execute("MATCH (a:A) RETURN count(a)").rows == ((1,),)
            with writer.begin("write") as tx:
                tx.execute("CREATE NODE TABLE Later(id INT64, PRIMARY KEY(id))")
                tx.execute("CREATE (:Later {id:9})")
            assert reader.execute("MATCH (n:Later {id:9}) RETURN n.id").rows == ((9,),)
