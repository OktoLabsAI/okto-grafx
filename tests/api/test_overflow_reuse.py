"""Persisted FREE-page reuse keeps ordinary visibility and commit boundaries."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from okto_grafx import connect


def write(db, query, parameters=None):
    """One public explicit write transaction."""
    with db.transaction() as tx:
        tx.execute(query, parameters)


def prepare(root):
    """Retire enough committed overflow capacity for two subsequent writes."""
    with connect(root, page_size=512) as db:
        write(db, "CREATE NODE TABLE P(id INT64, value STRING, PRIMARY KEY(id))")
        db.ensure_identity_indexes()
        for i in (1, 2):
            write(db, "CREATE (:P {id:$id,value:$v})", {"id": i, "v": "old" * 1800})
            write(db, "MATCH (p:P {id:$id}) SET p.value='small'", {"id": i})
        report = db.maintenance.vacuum(confirm_quiescent=True)
        assert report.reclaimed_overflow_pages >= 20
        return db._storage.page_count("heap.dat")


def test_reuses_durable_pages_after_reopen_without_growing_heap(tmp_path):
    root = tmp_path / "db"
    pages = prepare(root)
    with connect(root, page_size=512) as db:
        write(db, "MATCH (p:P {id:1}) SET p.value=$v", {"v": "new" * 1400})
        assert db._storage.page_count("heap.dat") == pages
        assert db.verify("all").findings == ()
    with connect(root, page_size=512) as db:
        assert db.execute("MATCH (p:P {id:1}) RETURN p.value").rows == (
            ("new" * 1400,),
        )
        assert db.verify("all").findings == ()


def test_reuse_preserves_live_reader_and_foreign_writers(tmp_path):
    root = tmp_path / "db"
    prepare(root)
    with connect(root, page_size=512) as reader:
        with reader.begin("read") as snapshot:
            assert snapshot.execute("MATCH (p:P {id:1}) RETURN p.value").rows == (
                ("small",),
            )

            def update(i):
                """Each participant must revalidate the same physical FREE inventory."""
                with connect(root, page_size=512) as writer:
                    write(
                        writer,
                        "MATCH (p:P {id:$id}) SET p.value=$v",
                        {"id": i, "v": str(i) * 4000},
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(update, (1, 2)))
            assert snapshot.execute("MATCH (p:P {id:1}) RETURN p.value").rows == (
                ("small",),
            )
    with connect(root, page_size=512) as db:
        for i in (1, 2):
            assert db.execute(
                "MATCH (p:P {id:$id}) RETURN p.value", {"id": i}
            ).rows == ((str(i) * 4000,),)
        assert db.verify("all").findings == ()


def test_reuse_scan_is_amortized_and_new_vacuum_restarts_it(tmp_path):
    root = tmp_path / "db"
    prepare(root)
    with connect(root, page_size=512) as db:
        write(db, "MATCH (p:P {id:1}) SET p.value=$v", {"v": "a" * 4000})
        first = db._heap._overflow_reuse_cursor
        write(db, "MATCH (p:P {id:2}) SET p.value=$v", {"v": "b" * 4000})
        second = db._heap._overflow_reuse_cursor
        assert second[0] == first[0]
        assert second[1] > first[1]
        write(db, "MATCH (p:P) SET p.value='tiny'")
        db.maintenance.vacuum(confirm_quiescent=True)
        write(db, "MATCH (p:P {id:1}) SET p.value=$v", {"v": "c" * 4000})
        assert db._heap._overflow_reuse_cursor[0] > second[0]
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("budget", [512 * 4, 512 * 128])
def test_failed_reuse_never_changes_committed_rows(tmp_path, monkeypatch, budget):
    root = tmp_path / "db"
    prepare(root)
    with connect(root, page_size=512, buffer_budget_bytes=budget) as db:
        from okto_grafx.engine.heap_store import HeapStore

        original = HeapStore._append

        def refuse(*args, **kwargs):
            """Refuse after overflow materialization, including eviction pressure."""
            raise RuntimeError("after overflow")

        monkeypatch.setattr(HeapStore, "_append", refuse)
        with pytest.raises(RuntimeError, match="after overflow"):
            write(db, "MATCH (p:P {id:1}) SET p.value=$v", {"v": "fail" * 1000})
        monkeypatch.setattr(HeapStore, "_append", original)
    with connect(root, page_size=512) as db:
        assert db.execute("MATCH (p:P {id:1}) RETURN p.value").rows == (("small",),)
        assert db.verify("all").findings == ()
        # A later writer must not allocate from an earlier participant's stale cursor.
        write(db, "MATCH (p:P {id:2}) SET p.value=$v", {"v": "ok" * 1000})
        assert db.execute("MATCH (p:P {id:2}) RETURN p.value").rows == (("ok" * 1000,),)


def test_transaction_quota_refusal_precedes_reuse_or_file_growth(tmp_path):
    from okto_grafx.domain.errors import GrafxError

    root = tmp_path / "db"
    pages = prepare(root)
    with connect(root, page_size=512, max_transaction_bytes=1024) as db:
        lsn = db.wal.last_lsn
        with pytest.raises(GrafxError) as refused:
            write(db, "MATCH (p:P {id:1}) SET p.value=$v", {"v": "large" * 2000})
        assert (
            "budget" in str(refused.value).lower()
            or "quota" in str(refused.value).lower()
        )
        assert db._heap._overflow_reuse_cursor is None
        assert db._storage.page_count("heap.dat") == pages
        assert db.wal.last_lsn == lsn
        assert db.execute("MATCH (p:P {id:1}) RETURN p.value").rows == (("small",),)


def test_foreign_publication_revokes_cached_free_page_before_reuse(tmp_path):
    from okto_grafx.domain.page import PageType

    root = tmp_path / "db"
    prepare(root)
    with connect(root, page_size=512) as first, connect(root, page_size=512) as second:
        # Deliberately cache every free page in the losing participant before the winner uses it.
        with second._transactions.page_access_section(fresh_read_view=True):
            for i in range(1, second._storage.page_count("heap.dat")):
                with second._pool.pinned("heap.dat", i):
                    pass
        write(first, "MATCH (p:P {id:1}) SET p.value=$v", {"v": "first" * 800})
        used = {
            i
            for i in range(1, first._storage.page_count("heap.dat"))
            if first._pool.read_fresh_page("heap.dat", i).page_type
            == int(PageType.OVERFLOW)
        }
        write(second, "MATCH (p:P {id:2}) SET p.value=$v", {"v": "second" * 650})
        assert second.execute("MATCH (p:P {id:1}) RETURN p.value").rows == (
            ("first" * 800,),
        )
        assert second.execute("MATCH (p:P {id:2}) RETURN p.value").rows == (
            ("second" * 650,),
        )
        assert used
        assert second.verify("all").findings == ()


@pytest.mark.parametrize("damage", ["future", "nonempty", "flags"])
def test_reuse_refuses_malformed_or_future_free_pages(tmp_path, monkeypatch, damage):
    from contextlib import contextmanager
    from okto_grafx.domain.errors import GrafxCorruptionDetected
    from okto_grafx.domain.page import PageType
    from okto_grafx.engine.buffer_pool import BufferPool

    root = tmp_path / "db"
    prepare(root)
    with connect(root, page_size=512) as db:
        original = BufferPool.pinned

        @contextmanager
        def pinned(pool, file, index):
            with original(pool, file, index) as page:
                if file == "heap.dat" and page.page_type == int(PageType.FREE):
                    detached = pool.codec.decode_page(
                        pool.codec.encode_page(page), page_index=index
                    )
                    if damage == "future":
                        detached.page_lsn = (1 << 63) - 2
                    elif damage == "flags":
                        detached.flags = 1
                    else:
                        detached.insert_slot(b"unexpected")
                    yield detached
                else:
                    yield page

        with db._transactions.page_access_section(fresh_read_view=True):
            monkeypatch.setattr(BufferPool, "pinned", pinned)
            with pytest.raises(GrafxCorruptionDetected):
                db._heap._retired_overflow_candidates(1)
        monkeypatch.setattr(BufferPool, "pinned", original)


def test_retired_record_reference_never_resolves_to_reallocated_payload(tmp_path):
    from okto_grafx.domain.errors import GrafxError

    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        write(db, "CREATE NODE TABLE P(id INT64, value STRING, PRIMARY KEY(id))")
        db.ensure_identity_indexes()
        write(db, "CREATE (:P {id:1,value:$v})", {"v": "old" * 1500})
        table = db._catalog.catalog.table("P")
        old_ref = next(db._heap.scan_all(table))[0]
        write(db, "MATCH (p:P {id:1}) SET p.value='small'")
        db.maintenance.vacuum(confirm_quiescent=True)
        write(db, "MATCH (p:P {id:1}) SET p.value=$v", {"v": "new" * 1400})
        with pytest.raises(GrafxError):
            db._heap.read(old_ref)
        assert db.execute("MATCH (p:P) RETURN p.value").rows == (("new" * 1400,),)


def test_no_reclaim_capability_does_not_pay_reuse_header_io(tmp_path, monkeypatch):
    from okto_grafx.engine.heap_store import HeapStore

    with connect(tmp_path / "db", page_size=512) as db:

        def refuse(_heap):
            raise AssertionError("unnecessary reclaim header IO")

        monkeypatch.setattr(HeapStore, "reclaim_floor", refuse)
        assert db._heap._retired_overflow_candidates(10) == ()
