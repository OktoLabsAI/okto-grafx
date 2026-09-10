"""Sparse maintenance walks bounded directory pages, not one pin per empty bucket."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxQueryBudgetExceeded, GrafxCorruptionDetected


def test_empty_wide_distribution_charges_directory_pages(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,v STRING,PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="sparse_hash", bucket_count=8192)
        store = db._indexes.active_index("v")
        expected = store._minimum_pages() - 1
        visits = []
        assert tuple(store._populated_buckets(lambda: visits.append(1))) == ()
        assert len(visits) == expected == 72
        assert db.index_distribution("v", max_pages=expected).entries == 0
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.index_distribution("v", max_pages=expected - 1)


def test_sparse_walk_observes_new_heads_and_refuses_changed_directory(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,v STRING,PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="sparse_hash", bucket_count=128)
        assert db.index_distribution("v").entries == 0
        with connect(tmp_path / "db", page_size=512) as writer:
            with writer.begin() as tx:
                tx.execute("CREATE (:D {id:1,v:'new'})")
        assert db.index_distribution("v").entries == 1
        with db._transactions.page_access_section():
            store = db._indexes.active_index("v")
            with db._pool.pinned(store.file, 1) as page:
                page.flags = 1
            try:
                with pytest.raises(GrafxCorruptionDetected):
                    tuple(store._populated_buckets())
            finally:
                with db._pool.pinned(store.file, 1) as page:
                    page.flags = 0
