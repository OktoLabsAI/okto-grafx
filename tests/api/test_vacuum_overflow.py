"""Quiescent overflow retirement: ownership, bounds and persistent FREE images."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxTransactionStateError, GrafxCorruptionDetected
from okto_grafx.domain.page import PageType
from okto_grafx.engine.heap_store import HeapStore


def seed(db):
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE P(id INT64, value STRING, PRIMARY KEY(id))")
    db.ensure_identity_indexes()
    for i, value in enumerate(("a" * 2200, "b" * 1500, "current")):
        with db.begin("write") as tx:
            tx.execute("CREATE (:P {id:1,value:$v})" if i == 0 else
                       "MATCH (p:P {id:1}) SET p.value=$v", {"v": value})


@pytest.mark.parametrize("page_size", [512, 8192])
def test_overflow_vacuum_reopen_and_limit(tmp_path, page_size):
    root = tmp_path / "db"
    with connect(root, page_size=page_size) as db:
        seed(db)
        before = db._storage.page_count("heap.dat")
        first = db.maintenance.vacuum(confirm_quiescent=True, max_versions=1)
        assert first.reclaimed_versions == 1
        assert not first.complete
        assert first.reclaimed_overflow_pages > 0 if page_size == 512 else first.reclaimed_overflow_pages == 0
        second = db.maintenance.vacuum(confirm_quiescent=True)
        assert second.reclaimed_versions == 1
        assert second.complete
        assert db._storage.page_count("heap.dat") == before  # No truncation claim.
        assert db.verify("all").findings == ()
        assert db.execute("MATCH (p:P) RETURN p.value").rows == (("current",),)
        if page_size == 512:
            assert sum(db._pool.read_fresh_page("heap.dat", i).page_type == int(PageType.FREE)
                       for i in range(1, before)) == first.reclaimed_overflow_pages + second.reclaimed_overflow_pages
    with connect(root, page_size=page_size) as db:
        assert db.verify("all").findings == ()
        assert db.execute("MATCH (p:P) RETURN p.value").rows == (("current",),)
        assert db.maintenance.vacuum(confirm_quiescent=True).reclaimed_versions == 0


def test_overflow_vacuum_refuses_own_active_reader(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as writer:
        seed(writer)
        with writer.begin("read"):
            with pytest.raises(GrafxTransactionStateError):
                writer.maintenance.vacuum(confirm_quiescent=True)


def test_overflow_alias_in_unselected_table_refuses_before_wal(tmp_path, monkeypatch):
    with connect(tmp_path / "db", page_size=512) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Q(id INT64, value STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:Q {id:1,value:$v})", {"v": "q" * 2200})
        # Activate independently so refusal is measured after that durable boundary.
        db.maintenance.vacuum("Q", confirm_quiescent=True)
        table = db._catalog.catalog.table("P")
        alias = next(content for _ref, header, content in db._heap._walk(table)
                     if header.has_overflow)
        original = HeapStore._walk

        def walk(self, table, **kwargs):
            for ref, header, content in original(self, table, **kwargs):
                if table.name == "Q" and kwargs.get("copy_content", True):
                    from okto_grafx.domain.model.record import RECORD_HEADER_SIZE
                    content = header.encode() + alias[RECORD_HEADER_SIZE:]
                yield ref, header, content

        before = db.wal.last_lsn
        monkeypatch.setattr(HeapStore, "_walk", walk)
        with pytest.raises(GrafxCorruptionDetected) as refused:
            db.maintenance.vacuum("P", confirm_quiescent=True)
        assert refused.value.details["field"] == "overflow_ownership"
        assert db.wal.last_lsn == before
