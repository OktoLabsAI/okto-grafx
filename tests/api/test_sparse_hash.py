"""Opt-in compact directories retain ordinary exact/MVCC index semantics."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxSchemaVersionMismatch


def write(db, query, parameters=None):
    with db.begin() as tx:
        tx.execute(query, parameters)


def test_empty_large_directory_and_lifecycle(tmp_path, monkeypatch):
    import okto_grafx.domain.model.catalog as catalog_module

    path = tmp_path / "db"
    with connect(tmp_path / "wide", page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("wide", "D", ("v",), layout="sparse_hash", bucket_count=65536)
        wide = db._indexes.active_index("wide")
        assert db._storage.page_count(wide.file) == 571
    with connect(path, page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="sparse_hash", bucket_count=128)
        store = db._indexes.active_index("v")
        assert db._storage.page_count(store.file) == 3
        raw = db._catalog.catalog.serialize()
        with monkeypatch.context() as scoped:
            scoped.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 10))
            with pytest.raises(GrafxSchemaVersionMismatch):
                catalog_module.Catalog.deserialize(raw)
        write(db, "CREATE (:D {id:1,v:'needle'})")
        assert db._storage.page_count(store.file) == 4
        assert db.execute("MATCH (d:D) WHERE d.v='needle' RETURN d.id").rows == ((1,),)
        assert not db.verify("all").findings
    with connect(path, page_size=512) as db:
        with db.begin("read") as old:
            write(db, "MATCH (d:D {id:1}) SET d.v='new'")
            assert old.execute("MATCH (d:D) WHERE d.v='needle' RETURN d.id").rows == ((1,),)
            assert db.execute("MATCH (d:D) WHERE d.v='new' RETURN d.id").rows == ((1,),)
        assert not db.verify("all").findings


@pytest.mark.parametrize("cut", ["before_head", "after_head", "after_pointer", "after_commit"])
def test_first_sparse_bucket_crash_replays_complete_commit(tmp_path, cut):
    import os
    import subprocess
    import sys

    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="sparse_hash", bucket_count=128)
    code = '''
import os, sys
from okto_grafx import connect
from okto_grafx.engine.sparse_hash import SparseHashIndex
from okto_grafx.engine.buffer_pool import BufferPool
original = SparseHashIndex._ensure_bucket
def stop(self, bucket, lsn):
    if sys.argv[2] == 'before_head': os._exit(73)
    original(self, bucket, lsn)
    if sys.argv[2] == 'after_pointer':
        self._pool.flush(self.file)
        os._exit(73)
SparseHashIndex._ensure_bucket = stop
flush = BufferPool.flush
def cut_flush(self, file=None):
    result = flush(self, file)
    if sys.argv[2] == 'after_head' and file and file.startswith('index/g_'):
        os._exit(73)
    return result
with connect(sys.argv[1], page_size=512, buffer_budget_bytes=2048) as db:
    BufferPool.flush = cut_flush
    with db.begin() as tx:
        tx.execute("CREATE (:D {id:1,v:'value'})")
    os._exit(73)
'''
    process = subprocess.run([sys.executable, "-c", code, str(path), cut], env=os.environ.copy(),
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == 73, process.stderr
    for _ in range(2):
        with connect(path, page_size=512) as db:
            assert db.execute("MATCH (d:D) WHERE d.v='value' RETURN d.id").rows == ((1,),)
            assert not db.verify("all").findings
            db.checkpoint()


def test_sparse_physical_backup_and_logical_transfer(tmp_path):
    from okto_grafx.backup import create_backup, restore_backup
    from okto_grafx.transfer import export_graph, import_graph

    with connect(tmp_path / "db") as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="sparse_hash", bucket_count=128)
        write(db, "CREATE (:D {id:1,v:'value'})")
        export_graph(db, tmp_path / "logical")
        create_backup(db, tmp_path / "physical")
    restore_backup(tmp_path / "physical", tmp_path / "restored", confirm_original_offline=True)
    import_graph(tmp_path / "logical", tmp_path / "imported")
    for name in ("restored", "imported"):
        with connect(tmp_path / name) as db:
            assert db._indexes.active_index("v").definition.layout.value == "sparse_hash"
            assert db.execute("MATCH (d:D) WHERE d.v='value' RETURN d.id").rows == ((1,),)
            assert not db.verify("all").findings
def test_sparse_rebuild_rehash_delete_and_distribution(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        for i in range(20):
            write(db, "CREATE (:D {id:$i,v:'same'})", {"i": i})
        db.create_index("v", "D", ("v",), layout="sparse_hash", bucket_count=64)
        assert db.index_distribution("v").entries == 20
        assert db.index_distribution("v").overflow_pages > 0
        db.rehash_index("v", bucket_count=128)
        db.rebuild_index("v")
        assert len(db.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 20
        write(db, "MATCH (d:D {id:1}) DELETE d")
        assert len(db.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 19
        assert not db.verify("all").findings


@pytest.mark.parametrize("damage", ["flags", "pointer", "identity"])
def test_sparse_directory_revalidates_changed_images(tmp_path, damage):
    import struct
    from okto_grafx.errors import GrafxCorruptionDetected

    with connect(tmp_path / "db", page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64,v STRING,PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="sparse_hash", bucket_count=128)
        store = db._indexes.active_index("v")
        store._bucket_head(0)  # seed the exact-byte decode memo
        with db._pool.pinned(store.file, 1) as page:
            raw, flags = page.read_slot(0), page.flags
            changed = bytearray(raw)
            if damage == "flags":
                page.flags = 1
            elif damage == "pointer":
                struct.pack_into("<I", changed, 16, 999999)
                page.update_slot(0, changed)
            else:
                changed[0] ^= 1
                page.update_slot(0, changed)
        try:
            with pytest.raises(GrafxCorruptionDetected):
                store._bucket_head(0)
        finally:
            with db._pool.pinned(store.file, 1) as page:
                page.flags = flags
                page.update_slot(0, raw)
