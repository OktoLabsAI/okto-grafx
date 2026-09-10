"""Bounded persisted FTS history versus independent snapshot corpus oracles."""

from contextlib import ExitStack
import os
import subprocess
import sys

import pytest

from okto_grafx import TextIndexOptions, connect
from okto_grafx.domain.index.fulltext import decode_options
from okto_grafx.engine.fulltext_durable import read_history_from_device
from okto_grafx.errors import GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch


def seed(db, capacity=2):
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE D(id INT64, body STRING, PRIMARY KEY(id))")
        tx.execute("CREATE NODE TABLE Other(id INT64, PRIMARY KEY(id))")
        for i in range(10):
            tx.execute("CREATE (:D {id:$i,body:'common text'})", {"i": i})
    db.create_text_index("text", "D", ("body",), options=TextIndexOptions(
        statistics_mode="durable", statistics_history_entries=capacity))


@pytest.mark.parametrize("value", [True, -1, 33, 1.0, "2"])
def test_history_option_bounds(value):
    with pytest.raises(GrafxConfigurationError):
        TextIndexOptions(statistics_mode="durable", statistics_history_entries=value)


def test_history_requires_durable_and_default_identity_unchanged():
    with pytest.raises(GrafxConfigurationError):
        TextIndexOptions(statistics_history_entries=1)
    for mode, prefix in [("wal", "fulltext_v1_"), ("durable", "fulltext_v2_")]:
        options = TextIndexOptions(statistics_mode=mode)
        assert options.derivation().startswith(prefix)
        assert decode_options(options.derivation()) == options
    for capacity in (1, 2, 32):
        options = TextIndexOptions(statistics_mode="durable", statistics_history_entries=capacity)
        assert options.derivation().startswith("fulltext_v3_")
        assert decode_options(options.derivation()) == options


def test_history_page_capacity_refuses_without_catalog_publication(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,body STRING,PRIMARY KEY(id))")
        previous = db.catalog.catalog
        with pytest.raises(GrafxConfigurationError) as raised:
            db.create_text_index("text", "D", ("body",), options=TextIndexOptions(
                statistics_mode="durable", statistics_history_entries=32))
        assert raised.value.details["field"] == "statistics_history_entries"
        assert db.catalog.catalog == previous


def test_historical_interval_eviction_and_foreign_writer_oracle(tmp_path, monkeypatch):
    import okto_grafx.engine.fulltext as fts

    path = tmp_path / "db"
    with connect(path) as db, ExitStack() as readers:
        seed(db)
        pinned = []
        for i in range(4):
            # Unrelated commits leave genuine gaps between summary markers.
            with db.begin() as tx:
                tx.execute("CREATE (:Other {id:$i})", {"i": i})
            reader = readers.enter_context(db.begin("read"))
            pinned.append(reader)
            with connect(path) as writer, writer.begin() as tx:
                tx.execute("MATCH (d:D {id:$i}) SET d.body=$body",
                           {"i": i, "body": "common " * (i + 3)})
        for ordinal, reader in enumerate(pinned):
            db._text_stats_cache.clear()
            result = db.search_text(reader, index="text", query="common")
            if ordinal >= 2:
                assert result.statistics_regime == "durable_summary"
            else:
                assert result.statistics_regime != "durable_summary"
            db._text_stats_cache.clear()
            with monkeypatch.context() as scope:
                scope.setattr(fts, "snapshot_statistics", lambda *args: None)
                scope.setattr(fts, "advance_statistics", lambda *args, **kwargs: None)
                oracle = db.search_text(reader, index="text", query="common")
            assert result.hits == oracle.hits
            assert result.corpus_documents == oracle.corpus_documents == 10
        assert not db.verify("all").findings
        records = read_history_from_device(db._indexes.index("text"))
        assert len(records) == 3
    with connect(path) as db:
        assert len(read_history_from_device(db._indexes.index("text"))) == 3
        assert not db.verify("all").findings


def test_history_updates_delete_null_rollback_and_rebuild(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        seed(db, 2)
        with db.begin("read") as reader:
            before = db.search_text(reader, index="text", query="common")
            with db.begin() as tx:
                tx.execute("MATCH (d:D {id:0}) DELETE d")
                tx.execute("MATCH (d:D {id:1}) SET d.body=NULL")
            db._text_stats_cache.clear()
            old = db.search_text(reader, index="text", query="common")
            assert old.hits == before.hits and old.statistics_regime == "durable_summary"
            committed = read_history_from_device(db._indexes.index("text"))
            with db.begin() as tx:
                tx.execute("MATCH (d:D {id:2}) DELETE d")
                tx.rollback()
            assert read_history_from_device(db._indexes.index("text")) == committed
        assert not db.verify("all").findings
        db.rehash_index("text", bucket_count=128)
        assert len(read_history_from_device(db._indexes.index("text"))) == 1
        assert db.search_text(index="text", query="common").corpus_documents == 9
        assert not db.verify("all").findings


@pytest.mark.parametrize("cut", ["before", "after"])
def test_history_crash_replays_once_at_complete_commit(tmp_path, cut):
    path = tmp_path / "db"
    with connect(path) as db:
        seed(db)
    code = '''
import os, sys
from okto_grafx import connect
import okto_grafx.engine.fulltext_durable as stats
original = stats.finish_statistics
def finish(store, changes, csn):
    if sys.argv[2] == 'after':
        original(store, changes, csn)
        store._pool.flush(store.file)
    os._exit(73)
with connect(sys.argv[1]) as db:
    stats.finish_statistics = finish
    with db.begin() as tx:
        tx.execute("CREATE (:D {id:99,body:'crashterm common'})")
'''
    result = subprocess.run([sys.executable, "-c", code, str(path), cut],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 73, result.stderr
    previous = None
    for _ in range(2):
        with connect(path) as db:
            found = db.search_text(index="text", query="crashterm")
            assert found.corpus_documents == 11 and len(found.hits) == 1
            series = read_history_from_device(db._indexes.index("text"))
            assert len(series) == 2
            assert previous is None or previous == series
            previous = series
            assert not db.verify("all").findings
            db.checkpoint()


@pytest.mark.parametrize("damage", ["future", "duplicate", "reserved", "unused", "count"])
def test_history_corruption_is_not_a_census_fallback(tmp_path, damage):
    import okto_grafx.engine.fulltext_durable as stats

    with connect(tmp_path / "db") as db:
        seed(db, 3)
        with db.begin() as tx:
            tx.execute("MATCH (d:D {id:0}) SET d.body='common longer'")
        store = db._indexes.index("text")
        original = db._storage.read_page(store.file, 0)
        page = db._pool.codec.decode_page(original)
        raw = bytearray(page.read_slot(2))
        if damage in ("future", "duplicate", "count"):
            values = list(stats._FORMAT.unpack(raw[16:80]))
            if damage == "future":
                values[2] = 2**63
            elif damage == "duplicate":
                values[2] = stats._FORMAT.unpack(raw[80:144])[2]
            else:
                values[3] += 1
            raw[16:80] = stats._FORMAT.pack(*values)
        elif damage == "reserved":
            raw[10] = 1
        else:
            raw[-1] = 1
        page.update_slot(2, raw)
        db._storage.write_page(store.file, 0, db._pool.codec.encode_page(page))
        try:
            # A warm pool must not hide even checksum-valid device corruption.
            assert any("text statistics" in f.detail for f in db.verify("all").findings)
            if damage != "count":
                with pytest.raises(GrafxCorruptionDetected):
                    read_history_from_device(store)
        finally:
            db._storage.write_page(store.file, 0, original)


def test_history_capability_refusal_and_transfer_backup_contracts(tmp_path, monkeypatch):
    import okto_grafx.domain.model.catalog as catalog_module
    from okto_grafx.backup import create_backup, restore_backup
    from okto_grafx.transfer import export_graph, import_graph

    with connect(tmp_path / "db") as db:
        seed(db, 32)
        with db.begin() as tx:
            tx.execute("MATCH (d:D {id:0}) SET d.body='common longer'")
        before = read_history_from_device(db._indexes.index("text"))
        raw = db._catalog.catalog.serialize()
        with monkeypatch.context() as scoped:
            scoped.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 8))
            with pytest.raises(GrafxSchemaVersionMismatch):
                catalog_module.Catalog.deserialize(raw)
        export_graph(db, tmp_path / "logical")
        create_backup(db, tmp_path / "physical")
    restore_backup(tmp_path / "physical", tmp_path / "restored", confirm_original_offline=True)
    import_graph(tmp_path / "logical", tmp_path / "imported")
    with connect(tmp_path / "restored") as db:
        assert read_history_from_device(db._indexes.index("text")) == before
        assert not db.verify("all").findings
    with connect(tmp_path / "imported") as db:
        store = db._indexes.index("text")
        assert decode_options(store.definition.key_derivation).statistics_history_entries == 32
        # New-identity imports build their own corpus history, never source markers.
        assert read_history_from_device(store)[-1][1] == 10
        assert not db.verify("all").findings
