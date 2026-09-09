"""Batched sparse heads share barriers, never COMMIT authority or transactions."""

import os
import subprocess
import sys

import pytest

from okto_grafx import connect


def setup(path):
    with connect(path, page_size=512, buffer_budget_bytes=2048) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,v STRING,PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="sparse_hash", bucket_count=128)


def test_multi_insert_shares_head_barrier(tmp_path, monkeypatch):
    path = tmp_path / "db"
    setup(path)
    with connect(path, page_size=512, buffer_budget_bytes=2048) as db:
        store = db._indexes.active_index("v")
        cls = type(db._storage)
        original = cls.durable_barrier
        calls = []

        def barrier(self, *args, **kwargs):
            if args == (store.file,):
                calls.append(1)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "durable_barrier", barrier)
        with db.begin() as tx:
            for i in range(24):
                tx.execute("CREATE (:D {id:$i,v:$v})", {"i": i, "v": str(i)})
        assert len(tuple(store._populated_buckets())) >= 16
        assert len(calls) == 1
        assert not db.verify().findings


def test_batched_foreign_writer_preserves_old_reader(tmp_path):
    path = tmp_path / "db"
    setup(path)
    with connect(path, page_size=512) as db, db.begin("read") as old:
        assert old.execute("MATCH (d:D) RETURN count(d)").rows == ((0,),)
        with connect(path, page_size=512) as writer, writer.begin() as tx:
            for i in range(24):
                tx.execute("CREATE (:D {id:$i,v:$v})", {"i": i, "v": str(i)})
        assert old.execute("MATCH (d:D) RETURN count(d)").rows == ((0,),)
        assert old.execute("MATCH (d:D) WHERE d.v='5' RETURN d.id").rows == ()
        assert db.execute("MATCH (d:D) WHERE d.v='5' RETURN d.id").rows == ((5,),)


@pytest.mark.parametrize("cut", ["before_barrier", "after_barrier", "after_pointers"])
def test_batch_crash_replays_whole_transaction(tmp_path, cut):
    path = tmp_path / "db"
    setup(path)
    code = '''
import os, sys
from okto_grafx import connect
from okto_grafx.engine.sparse_hash import SparseHashIndex
with connect(sys.argv[1], page_size=512, buffer_budget_bytes=2048) as db:
    store = db._indexes.active_index('v')
    cls = type(db._storage)
    original = cls.durable_barrier
    def barrier(self, *args, **kwargs):
        target = args == (store.file,)
        if target and sys.argv[2] == 'before_barrier': os._exit(84)
        result = original(self, *args, **kwargs)
        if target and sys.argv[2] == 'after_barrier': os._exit(84)
        return result
    cls.durable_barrier = barrier
    prepare = SparseHashIndex._ensure_buckets
    def stop(self, buckets, lsn):
        result = prepare(self, buckets, lsn)
        if self.file == store.file and sys.argv[2] == 'after_pointers':
            self._pool.flush(self.file)
            os._exit(84)
        return result
    SparseHashIndex._ensure_buckets = stop
    with db.begin() as tx:
        for i in range(24):
            tx.execute('CREATE (:D {id:$i,v:$v})', {'i':i,'v':str(i)})
'''
    child = subprocess.run([sys.executable, "-c", code, str(path), cut],
                           env=os.environ.copy(), capture_output=True, text=True, timeout=60)
    assert child.returncode == 84, child.stderr
    for _ in range(2):
        with connect(path, page_size=512) as db:
            assert db.execute("MATCH (d:D) RETURN count(d)").rows == ((24,),)
            for i in range(24):
                assert db.execute("MATCH (d:D) WHERE d.v=$v RETURN d.id", {"v": str(i)}).rows == ((i,),)
            assert not db.verify().findings
            db.checkpoint()
