"""Durable scalar snapshots: real WAL replay, foreign participants and census oracle."""

import os
import subprocess
import sys

import pytest

from okto_grafx import connect, TextIndexOptions
from okto_grafx.errors import GrafxCorruptionDetected


def seed(path):
    db = connect(path)
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE D(id INT64, a STRING, b STRING, PRIMARY KEY(id))")
        for i in range(20):
            tx.execute("CREATE (:D {id:$i,a:$a,b:$b})",
                       {"i": i, "a": f"common word{i}", "b": "common" if i % 2 else None})
    db.create_text_index("text", "D", ("a", "b"),
                         options=TextIndexOptions(field_weights=(2.0, 0.5), statistics_mode="durable"))
    return db


def test_durable_cold_old_snapshot_foreign_write_census(tmp_path, monkeypatch):
    import okto_grafx.engine.fulltext as fts

    with seed(tmp_path / "db") as db, db.begin("read") as old:
        before = db.search_text(old, index="text", query="word1")
        assert before.statistics_regime == "durable_summary"
        with connect(tmp_path / "db") as writer, writer.begin() as tx:
            tx.execute("MATCH (d:D {id:1}) SET d.a='word1 word1 longer'")
            tx.execute("MATCH (d:D {id:2}) DELETE d")
            tx.execute("CREATE (:D {id:99,a:'word1 new',b:''})")
        current = db.search_text(index="text", query="word1")
        assert current.statistics_regime == "durable_summary"
        assert current.corpus_documents == 20
        db._text_stats_cache.clear()
        assert db.search_text(old, index="text", query="word1").hits == before.hits
        db._text_stats_cache.clear()
        with monkeypatch.context() as scoped:
            scoped.setattr(fts, "snapshot_statistics", lambda *a: None)
            scoped.setattr(fts, "advance_statistics", lambda *a, **k: None)
            census = db.search_text(index="text", query="word1")
        assert census.hits == current.hits
        assert current.postings_visited < census.postings_visited
        assert not db.verify("all").findings
    with connect(tmp_path / "db") as db:
        db.checkpoint()
    with connect(tmp_path / "db", read_only=True) as db:
        cold = db.search_text(index="text", query="word1")
        assert cold.statistics_regime == "durable_summary"
        assert cold.hits == current.hits


@pytest.mark.parametrize("cut", ["before_summary", "after_summary"])
def test_summary_crash_complete_commit_recovery(tmp_path, cut):
    with seed(tmp_path / "db"):
        pass
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
        tx.execute("CREATE (:D {id:99,a:'crashterm common',b:null})")
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "db"), cut],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 73, result.stderr
    for _ in range(2):
        with connect(tmp_path / "db") as db:
            result = db.search_text(index="text", query="crashterm")
            assert result.corpus_documents == 21 and len(result.hits) == 1
            assert result.statistics_regime == "durable_summary"
            assert not db.verify("all").findings
            db.checkpoint()


def test_summary_corruption_refuses_and_verifies(tmp_path):
    import okto_grafx.engine.fulltext_durable as stats

    with seed(tmp_path / "db") as db:
        store = db._indexes.active_index("text")
        with store._pool.pinned(store.file, 0) as page:
            fields = list(stats._FORMAT.unpack(page.read_slot(2)))
            fields[2] = 2**63
            page.update_slot(2, stats._FORMAT.pack(*fields))
        store._pool.flush(store.file)
        db._text_stats_cache.clear()
        with pytest.raises(GrafxCorruptionDetected):
            db.search_text(index="text", query="common")
        assert db.verify("all").findings


def test_required_capability_old_reader_refuses_and_procedure_reports_summary(tmp_path, monkeypatch):
    import okto_grafx.domain.model.catalog as catalog_module
    from okto_grafx.errors import GrafxSchemaVersionMismatch

    with seed(tmp_path / "db") as db:
        raw = db._catalog.catalog.serialize()
        with monkeypatch.context() as scoped:
            scoped.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 6))
            with pytest.raises(GrafxSchemaVersionMismatch):
                catalog_module.Catalog.deserialize(raw)
        result = db.execute("CALL grafx.search_text('text', 'word1', 10)")
        assert result.statistics["statistics_from_durable_summary"] == 1


def test_semantic_summary_mismatch_with_valid_page_checksum_is_a_finding(tmp_path):
    import okto_grafx.engine.fulltext_durable as stats

    with seed(tmp_path / "db") as db:
        store = db._indexes.active_index("text")
        with store._pool.pinned(store.file, 0) as page:
            fields = list(stats._FORMAT.unpack(page.read_slot(2)))
            fields[3] += 1
            page.update_slot(2, stats._FORMAT.pack(*fields))
        store._pool.flush(store.file)
        findings = db.verify("all").findings
        assert any("differ from the heap census" in finding.detail for finding in findings)


def test_verification_reads_durable_summary_from_device_not_a_warm_pool(tmp_path):
    import okto_grafx.engine.fulltext_durable as stats

    with seed(tmp_path / "db") as db:
        store = db._indexes.active_index("text")
        stats.read_statistics(store)  # Intentionally leave an intact copy cached.
        original = db._storage.read_page(store.file, 0)
        page = db._pool.codec.decode_page(original)
        fields = list(stats._FORMAT.unpack(page.read_slot(2)))
        fields[3] += 1
        page.update_slot(2, stats._FORMAT.pack(*fields))
        db._storage.write_page(store.file, 0, db._pool.codec.encode_page(page))
        try:
            assert any("differ from the heap census" in finding.detail for finding in db.verify("all").findings)
        finally:
            db._storage.write_page(store.file, 0, original)


def test_durable_rebuild_rollback_empty_and_transfer(tmp_path):
    from okto_grafx.transfer import export_graph, import_graph

    with seed(tmp_path / "db") as db:
        tx = db.begin()
        tx.execute("CREATE (:D {id:99,a:'rolled back',b:null})")
        tx.rollback()
        db.rebuild_index("text")
        assert db.search_text(index="text", query="common").corpus_documents == 20
        export_graph(db, tmp_path / "artifact")
        with db.begin() as tx:
            tx.execute("MATCH (d:D) DELETE d")
        assert db.search_text(index="text", query="common").corpus_documents == 0
        assert not db.verify("all").findings
    import_graph(tmp_path / "artifact", tmp_path / "copy")
    with connect(tmp_path / "copy") as copy:
        result = copy.search_text(index="text", query="common")
        assert result.corpus_documents == 20
        assert result.statistics_regime == "durable_summary"
