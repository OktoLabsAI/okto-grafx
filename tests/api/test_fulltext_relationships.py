"""Relationship FTS preserves physical edge identities, endpoint offsets and MVCC."""

import pytest
import os
import subprocess
import sys

from okto_grafx import connect, TextIndexOptions
from okto_grafx.errors import GrafxSchemaVersionMismatch


def seed(db):
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE D(id INT64,PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM D TO D,body STRING)")
        tx.execute("CREATE (:D {id:1})")
        tx.execute("CREATE (:D {id:2})")
        for _ in range(2):
            tx.execute("MATCH (a:D {id:1}),(b:D {id:2}) CREATE (a)-[:R {body:'graph evidence'}]->(b)")
        tx.execute("MATCH (a:D {id:1}) CREATE (a)-[:R {body:'graph loop'}]->(a)")
    db.create_text_index("rels", "R", ("body",), options=TextIndexOptions(
        statistics_mode="durable", statistics_history_entries=2, prefix_max_characters=8))


def test_parallel_edges_self_loop_update_delete_and_old_reader(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        seed(db)
        hits = db.search_text(index="rels", query="graph").hits
        assert len(hits) == len({hit.record_id for hit in hits}) == 3
        assert all(hit.matched_fields == ("body",) for hit in hits)
        assert db.search_text(index="rels", query="gra", prefix=True).hits == hits
        assert len(db.execute("CALL grafx.search_text('rels', 'graph', 10)").rows) == 3
        with db.begin("read") as old:
            with connect(path) as writer, writer.begin() as tx:
                tx.execute("MATCH (:D)-[r:R]->(:D) WHERE r.body='graph evidence' SET r.body='changed'")
            assert len(db.search_text(old, index="rels", query="graph").hits) == 3
            assert len(db.search_text(index="rels", query="graph").hits) == 1
        with db.begin() as tx:
            tx.execute("MATCH (:D)-[r:R]->(:D) WHERE r.body='graph loop' DELETE r")
        assert not db.search_text(index="rels", query="graph").hits
        db.rebuild_index("rels")
        assert len(db.search_text(index="rels", query="changed").hits) == 2
        assert not db.verify().findings
    with connect(path) as db:
        assert len(db.search_text(index="rels", query="changed").hits) == 2


def test_relationship_fts_backup_transfer_and_older_reader_refusal(tmp_path, monkeypatch):
    from okto_grafx.backup import create_backup, restore_backup
    from okto_grafx.transfer import export_graph, import_graph
    import okto_grafx.domain.model.catalog as model
    with connect(tmp_path / "db") as db:
        seed(db)
        raw = db._catalog.catalog.serialize()
        with monkeypatch.context() as scoped:
            scoped.setattr(model, "_KNOWN_CAPABILITY_BITS", model._KNOWN_CAPABILITY_BITS & ~(1 << 12))
            with pytest.raises(GrafxSchemaVersionMismatch):
                model.Catalog.deserialize(raw)
        create_backup(db, tmp_path / "backup")
        export_graph(db, tmp_path / "export")
    restore_backup(tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True)
    import_graph(tmp_path / "export", tmp_path / "imported")
    for target in ("restored", "imported"):
        with connect(tmp_path / target) as db:
            assert len(db.search_text(index="rels", query="gra", prefix=True).hits) == 3
            assert not db.verify().findings


@pytest.mark.parametrize("cut", ["before_summary", "after_summary"])
def test_relationship_prefix_crash_replay(tmp_path, cut):
    path = tmp_path / "db"
    with connect(path) as db:
        seed(db)
    code = """
import os, sys
from okto_grafx import connect
import okto_grafx.engine.fulltext_durable as stats
original = stats.finish_statistics
def finish(store, changes, csn):
    if sys.argv[2] == 'after_summary':
        original(store, changes, csn)
        store._pool.flush(store.file)
    os._exit(73)
with connect(sys.argv[1]) as db:
    stats.finish_statistics = finish
    with db.begin() as tx:
        tx.execute("MATCH (a:D {id:1}),(b:D {id:2}) CREATE (a)-[:R {body:'crashterm graph'}]->(b)")
"""
    process = subprocess.run([sys.executable, "-c", code, str(path), cut],
                             env=os.environ.copy(), capture_output=True, text=True, timeout=45)
    assert process.returncode == 73, process.stderr
    for _ in range(2):
        with connect(path) as db:
            result = db.search_text(index="rels", query="crash", prefix=True)
            assert len(result.hits) == 1
            assert result.corpus_documents == 4
            assert not db.verify().findings
            db.checkpoint()


@pytest.mark.parametrize("capability", ["fulltext_prefixes_v1", "fulltext_relationships_v1"])
def test_missing_required_capability_refuses_serialization(tmp_path, capability):
    import struct
    from okto_grafx.domain.page import crc32c
    from okto_grafx.domain.model.catalog import Catalog
    from okto_grafx.errors import GrafxConfigurationError, GrafxCorruptionDetected
    with connect(tmp_path / "db") as db:
        seed(db)
        catalog = db._catalog.catalog.copy()
        raw = bytearray(catalog.serialize())
        bits = struct.unpack_from("<Q", raw, 28)[0]
        bit = 11 if capability == "fulltext_prefixes_v1" else 12
        struct.pack_into("<Q", raw, 28, bits & ~(1 << bit))
        struct.pack_into("<I", raw, len(raw) - 4, crc32c(bytes(raw[:-4])))
        with pytest.raises(GrafxCorruptionDetected):
            Catalog.deserialize(bytes(raw))
        catalog._required_capabilities = catalog._required_capabilities - {capability}
        catalog._serialized_memo = None  # Hostile model injection bypasses sanctioned invalidators.
        with pytest.raises((GrafxConfigurationError, GrafxSchemaVersionMismatch)):
            catalog.serialize()
