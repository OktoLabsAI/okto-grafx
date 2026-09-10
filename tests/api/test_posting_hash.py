"""Dictionary postings retain native candidates, snapshots and replay contracts."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxCorruptionDetected, GrafxSchemaVersionMismatch


def write(db, query, parameters=None):
    with db.begin() as tx:
        tx.execute(query, parameters)


@pytest.mark.parametrize("layout", ["hash", "posting_hash"])
def test_repeated_postings_and_snapshot(tmp_path, layout):
    path = tmp_path / layout
    with connect(path, page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout=layout, bucket_count=1)
        with db.begin() as tx:
            tx.executemany("CREATE (:D {id:$id,v:$v})", [{"id": n, "v": "same" * 20} for n in range(60)])
        distribution = db.index_distribution("v")
        assert distribution.entries == 60
        print(layout, distribution.pages)
        if layout == "posting_hash":
            assert distribution.pages <= 5
            assert "posting_hash_v1" in db._catalog.catalog.required_capabilities()
        else:
            assert distribution.pages >= 15
        with db.begin("read") as old:
            write(db, "MATCH (d:D {id:1}) SET d.v='different'")
            assert len(old.execute("MATCH (d:D) WHERE d.v=$v RETURN d.id", {"v": "same" * 20}).rows) == 60
            assert len(db.execute("MATCH (d:D) WHERE d.v=$v RETURN d.id", {"v": "same" * 20}).rows) == 59
        write(db, "MATCH (d:D {id:2}) DELETE d")
        assert not db.verify("all").findings
    with connect(path, page_size=512) as db:
        assert len(db.execute("MATCH (d:D) WHERE d.v=$v RETURN d.id", {"v": "same" * 20}).rows) == 58
        assert not db.verify("all").findings


def test_dictionary_corruption_and_old_reader(tmp_path, monkeypatch):
    import okto_grafx.domain.model.catalog as catalog_module

    with connect(tmp_path / "db", page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="posting_hash", bucket_count=1)
        write(db, "CREATE (:D {id:1,v:'same'})")
        raw = db._catalog.catalog.serialize()
        with monkeypatch.context() as scoped:
            scoped.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 14))
            with pytest.raises(GrafxSchemaVersionMismatch):
                catalog_module.Catalog.deserialize(raw)
        store = db._indexes.active_index("v")
        with store._pool.pinned(store.file, 1) as page:
            clone = type(page).from_bytes(page.to_bytes())
        clone.free_slot(0)
        with pytest.raises(GrafxCorruptionDetected):
            tuple(store._entry_images(clone))


def test_same_key_batch_validates_membership_once(tmp_path, monkeypatch):
    from okto_grafx.engine.posting_hash import PostingHashIndex

    with connect(tmp_path / "db", page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="posting_hash", bucket_count=1)
        calls = []
        original = PostingHashIndex._scan_bucket
        def counted(self, *args, **kwargs):
            calls.append(1)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(PostingHashIndex, "_scan_bucket", counted)
        with db.begin() as tx:
            tx.executemany("CREATE (:D {id:$id,v:'same'})", [{"id": n} for n in range(120)])
        assert len(calls) == 1
        assert len(db.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 120


@pytest.mark.parametrize("damage", ["tag", "short", "reference", "provisional", "duplicate_key"])
def test_corrupt_postings_refuse_before_candidates(tmp_path, damage):
    import struct
    from okto_grafx.domain.ids import PROVISIONAL_CSN

    with connect(tmp_path / "db", page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="posting_hash", bucket_count=1)
        write(db, "CREATE (:D {id:1,v:'same'})")
        store = db._indexes.active_index("v")
        with db._pool.pinned(store.file, 1) as page:
            clone = type(page).from_bytes(page.to_bytes())
        raw = bytearray(clone.read_slot(1))
        if damage == "tag":
            raw[0] = 255
        elif damage == "short":
            raw.pop()
        elif damage == "reference":
            struct.pack_into("<Q", raw, 3, 0)
        elif damage == "provisional":
            struct.pack_into("<Q", raw, 11, PROVISIONAL_CSN)
        else:
            clone.insert_slot(clone.read_slot(0))
        clone.update_slot(1, raw)
        with pytest.raises(GrafxCorruptionDetected):
            tuple(store._posting_matches(clone, b"absent", None))


def test_independent_handles_keep_old_reader_and_new_writer(tmp_path):
    path = tmp_path / "db"
    with connect(path, page_size=512) as first:
        write(first, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        first.create_index("v", "D", ("v",), layout="posting_hash", bucket_count=1)
        write(first, "CREATE (:D {id:1,v:'same'})")
        with connect(path, page_size=512) as second, first.begin("read") as old:
            assert old.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows == ((1,),)
            write(second, "CREATE (:D {id:2,v:'same'})")
            assert old.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows == ((1,),)
            assert len(first.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 2
        assert not first.verify("all").findings


def test_vacuum_reclaims_postings_then_reuses_cleared_pages(tmp_path):
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="posting_hash", bucket_count=1)
        with db.begin() as tx:
            tx.executemany("CREATE (:D {id:$id,v:'same'})", [{"id": n} for n in range(25)])
        write(db, "MATCH (d:D) DELETE d")
        report = db.maintenance.vacuum(confirm_quiescent=True)
        assert report.complete and report.index_entries_removed >= 25
        assert db.index_distribution("v").entries == 0
        write(db, "CREATE (:D {id:99,v:'new'})")
        assert db.execute("MATCH (d:D) WHERE d.v='new' RETURN d.id").rows == ((99,),)
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(path, page_size=512, read_only=True) as db:
        assert db.execute("MATCH (d:D) WHERE d.v='new' RETURN d.id").rows == ((99,),)


def test_maintenance_backup_transfer(tmp_path):
    from okto_grafx.backup import create_backup, restore_backup
    from okto_grafx.transfer import export_graph, import_graph

    with connect(tmp_path / "db", page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        with db.begin() as tx:
            tx.executemany("CREATE (:D {id:$id,v:'same'})", [{"id": n} for n in range(25)])
        db.create_index("v", "D", ("v",), layout="posting_hash", bucket_count=1)
        db.rehash_index("v", bucket_count=4)
        db.rebuild_index("v")
        assert len(db.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 25
        assert not db.verify("all").findings
        export_graph(db, tmp_path / "logical")
        create_backup(db, tmp_path / "physical")
    restore_backup(tmp_path / "physical", tmp_path / "restored", confirm_original_offline=True)
    import_graph(tmp_path / "logical", tmp_path / "imported")
    for name in ("restored", "imported"):
        with connect(tmp_path / name, page_size=512 if name == "restored" else 8192) as db:
            assert db._indexes.active_index("v").definition.layout.value == "posting_hash"
            assert len(db.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 25
            assert not db.verify("all").findings


@pytest.mark.parametrize("cut", ["before_posting", "after_posting", "after_commit"])
def test_durable_commit_crash_and_repeated_recovery(tmp_path, cut):
    import os
    import subprocess
    import sys
    from pathlib import Path

    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="posting_hash", bucket_count=1)
    code = '''
import os, sys
from okto_grafx import connect
from okto_grafx.engine.posting_hash import PostingHashIndex
original = PostingHashIndex._place
def stop(self, pages, entry, lsn):
    if sys.argv[2] == 'before_posting': os._exit(73)
    result = original(self, pages, entry, lsn)
    if sys.argv[2] == 'after_posting':
        self._pool.flush(self.file)
        os._exit(73)
    return result
PostingHashIndex._place = stop
with connect(sys.argv[1], page_size=512) as db:
    with db.begin() as tx:
        tx.executemany("CREATE (:D {id:$id,v:'same'})", [{"id": n} for n in range(30)])
    os._exit(73)
'''
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    result = subprocess.run([sys.executable, "-c", code, str(path), cut],
                            env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 73, result.stderr
    for _ in range(2):
        with connect(path, page_size=512) as db:
            assert len(db.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 30
            assert not db.verify("all").findings
            db.checkpoint()
