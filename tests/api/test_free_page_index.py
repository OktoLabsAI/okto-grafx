"""Indexed retired-page discovery retains native rollback and writer isolation."""

from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxCorruptionDetected


def write(db, query, parameters=None):
    with db.begin() as tx:
        tx.execute(query, parameters)


def prepare(path):
    with connect(path, page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64,value STRING,PRIMARY KEY(id))")
        db.ensure_identity_indexes()
        for i in range(3):
            write(db, "CREATE (:D {id:$id,value:$v})", {"id": i, "v": "old" * 1800})
            write(db, "MATCH (d:D {id:$id}) SET d.value='small'", {"id": i})
        report = db.maintenance.vacuum(confirm_quiescent=True, index_free_pages=True)
        assert report.reclaimed_overflow_pages > 20
        assert not db.verify("all").findings
        return db._storage.page_count("heap.dat")


def test_index_reopen_reuse_without_legacy_scan_or_growth(tmp_path):
    path = tmp_path / "db"
    size = prepare(path)
    with connect(path, page_size=512) as db:
        write(db, "MATCH (d:D {id:0}) SET d.value=$v", {"v": "new" * 1400})
        assert db._heap._overflow_reuse_cursor is None
        assert db._storage.page_count("heap.dat") == size
        assert not db.verify("all").findings
    with connect(path, page_size=512) as db:
        assert db.execute("MATCH (d:D {id:0}) RETURN d.value").rows == (("new" * 1400,),)
        assert not db.verify("all").findings


def test_index_independent_writers_and_pinned_reader(tmp_path):
    path = tmp_path / "db"
    prepare(path)
    with connect(path, page_size=512) as db, db.begin("read") as reader:
        assert reader.execute("MATCH (d:D {id:0}) RETURN d.value").rows == (("small",),)

        def update(i):
            with connect(path, page_size=512) as writer:
                write(writer, "MATCH (d:D {id:$id}) SET d.value=$v", {"id": i, "v": str(i) * 4000})

        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(update, (0, 1)))
        assert reader.execute("MATCH (d:D {id:0}) RETURN d.value").rows == (("small",),)
    with connect(path, page_size=512) as db:
        assert not db.verify("all").findings
        write(db, "MATCH (d:D) SET d.value='tiny'")
        db.maintenance.vacuum(confirm_quiescent=True)
        write(db, "MATCH (d:D {id:2}) SET d.value=$v", {"v": "x" * 5000})
        assert db._heap._overflow_reuse_cursor is None
        assert not db.verify("all").findings


@pytest.mark.parametrize("budget", [512 * 4, 512 * 128])
def test_failed_index_pop_does_not_lose_free_pages_or_rows(tmp_path, monkeypatch, budget):
    from okto_grafx.engine.heap_store import HeapStore

    path = tmp_path / "db"
    prepare(path)
    with connect(path, page_size=512, buffer_budget_bytes=budget) as db:
        def refuse(*args, **kwargs):
            raise RuntimeError("after free index pop")

        with monkeypatch.context() as scope:
            scope.setattr(HeapStore, "_append", refuse)
            with pytest.raises(RuntimeError, match="after free index pop"):
                write(db, "MATCH (d:D {id:0}) SET d.value=$v", {"v": "bad" * 1400})
    with connect(path, page_size=512) as db:
        assert db.execute("MATCH (d:D {id:0}) RETURN d.value").rows == (("small",),)
        assert not db.verify("all").findings


def test_cycle_refuses_before_root_changes(tmp_path):
    path = tmp_path / "db"
    prepare(path)
    with connect(path, page_size=512) as db:
        from okto_grafx.engine.free_page_index import pop_candidates

        with db._transactions.page_access_section(fresh_read_view=True):
            with db._pool.pinned("heap.dat", 0) as page:
                head = page.next_page
            with db._pool.pinned("heap.dat", head) as page:
                next_page = page.next_page
                page.next_page = head
            try:
                horizon = db._transactions.published_state().last_committed_lsn
                with pytest.raises(GrafxCorruptionDetected):
                    pop_candidates(db._heap, 1, horizon)
                with db._pool.pinned("heap.dat", 0) as page:
                    assert page.next_page == head
            finally:
                with db._pool.pinned("heap.dat", head) as page:
                    page.next_page = next_page


@pytest.mark.parametrize("damage", ["future", "omitted", "reserved"])
def test_fresh_device_directory_damage_is_reported(tmp_path, damage):
    path = tmp_path / "db"
    prepare(path)
    with connect(path, page_size=512) as db:
        with db._pool.pinned("heap.dat", 0) as root:
            head = root.next_page
        original = db._storage.read_page("heap.dat", head)
        page = db._pool.codec.decode_page(original)
        if damage == "future":
            page.page_lsn = db._transactions.published_state().last_committed_lsn + 1000
        elif damage == "reserved":
            page.flags = 2
        else:
            from okto_grafx.engine.free_page_index import _HEADER
            raw = page.read_slot(0)
            magic, floor, count = _HEADER.unpack_from(raw)
            page.update_slot(0, _HEADER.pack(magic, floor, count - 1) + raw[_HEADER.size:-4])
        db._storage.write_page("heap.dat", head, db._pool.codec.encode_page(page))
        try:
            assert any("free-page" in finding.detail for finding in db.verify("all").findings)
        finally:
            db._storage.write_page("heap.dat", head, original)


def test_free_directory_backup_and_old_capability_refusal(tmp_path, monkeypatch):
    import okto_grafx.domain.model.catalog as catalog_module
    from okto_grafx.errors import GrafxSchemaVersionMismatch
    from okto_grafx.backup import create_backup, restore_backup

    path = tmp_path / "db"
    prepare(path)
    with connect(path, page_size=512) as db:
        raw = db._catalog.catalog.serialize()
        with monkeypatch.context() as scoped:
            scoped.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 9))
            with pytest.raises(GrafxSchemaVersionMismatch):
                catalog_module.Catalog.deserialize(raw)
        create_backup(db, tmp_path / "backup")
    restore_backup(tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True)
    with connect(tmp_path / "restored", page_size=512) as db:
        write(db, "MATCH (d:D {id:0}) SET d.value=$v", {"v": "x" * 4000})
        assert db._heap._overflow_reuse_cursor is None
        assert not db.verify("all").findings


@pytest.mark.parametrize("cut", ["before_commit", "after_commit"])
def test_index_pop_process_death_preserves_complete_outcome(tmp_path, cut):
    path = tmp_path / "db"
    prepare(path)
    code = '''
import os, sys
from okto_grafx import connect
from okto_grafx.engine.heap_store import HeapStore
def stop(*args, **kwargs):
    os._exit(73)
with connect(sys.argv[1], page_size=512, buffer_budget_bytes=2048) as db:
    if sys.argv[2] == 'before_commit':
        HeapStore._append = stop
    with db.begin() as tx:
        tx.execute("MATCH (d:D {id:0}) SET d.value=$v", {'v': 'new' * 1400})
    os._exit(73)
'''
    result = subprocess.run([sys.executable, "-c", code, str(path), cut],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 73, result.stderr
    expected = "small" if cut == "before_commit" else "new" * 1400
    for _ in range(2):
        with connect(path, page_size=512) as db:
            assert db.execute("MATCH (d:D {id:0}) RETURN d.value").rows == ((expected,),)
            assert not db.verify("all").findings
            db.checkpoint()


def test_free_index_prefix_cost_is_bounded_by_requested_pages(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from okto_grafx.engine.buffer_pool import BufferPool
    from okto_grafx.engine.free_page_index import pop_candidates

    path = tmp_path / "db"
    prepare(path)
    original = BufferPool.pinned
    seen = []

    @contextmanager
    def observed(pool, file, page, **kwargs):
        if file == "heap.dat":
            seen.append(page)
        with original(pool, file, page, **kwargs) as image:
            yield image

    with connect(path, page_size=512) as db:
        with db._transactions.page_access_section(fresh_read_view=True):
            with db._pool.pinned("heap.dat", 0) as page:
                head = page.next_page
            horizon = db._transactions.published_state().last_committed_lsn
            try:
                with monkeypatch.context() as scope:
                    scope.setattr(BufferPool, "pinned", observed)
                    selected = pop_candidates(db._heap, 3, horizon)
                assert len(selected) == 3
                assert len(seen) <= 7 and set(seen) <= {0, head, *selected}
            finally:
                with db._pool.pinned("heap.dat", 0) as page:
                    page.next_page = head


def test_activation_without_initialization_keeps_legacy_discovery(tmp_path):
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64,value STRING,PRIMARY KEY(id))")
        db.ensure_identity_indexes()
        with db.begin() as tx:
            assert db._transactions.prepare_heap_reclaim_activation(tx._context, index_free_pages=True)
        assert not db.verify("all").findings
        db.maintenance.vacuum(confirm_quiescent=True, index_free_pages=True)
        assert not db.verify("all").findings
