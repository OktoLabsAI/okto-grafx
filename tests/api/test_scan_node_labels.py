"""Raw, projected and paginated reads preserve the native label-set metadata."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.errors import GrafxError


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_raw_scan_label_metadata_survives_projection_snapshot_and_reopen(tmp_path, codec):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1}), (:N:B {id:2}), (n:N {id:3}) REMOVE n:N")
        reader = db.begin("read")
        try:
            first = reader.scan_rows_v1("N", limit=1, columns=())
            assert first.rows[0].node_labels is None
            assert first.rows[0].values == ()
            assert first.next_cursor is not None
            with db.begin("write") as writer:
                writer.execute("MATCH (n:N {id:2}) REMOVE n:B SET n:C")
            second = reader.scan_rows_v1("N", limit=1, columns=(), cursor=first.next_cursor)
            assert second.rows[0].node_labels == ("B", "N")
            assert second.rows[0].values == ()
            assert second.next_cursor is not None
            third = reader.scan_rows_v1("N", limit=1, columns=(), cursor=second.next_cursor)
            assert third.rows[0].node_labels == ()
            assert third.next_cursor is None
        finally:
            reader.rollback()
    with connect(path) as db:
        with db.begin("read") as tx:
            rows = tx.scan_rows_v1("N", limit=10, columns=("id",)).rows
            assert {row.values[0]: row.node_labels for row in rows} == {
                1: None, 2: ("C", "N"), 3: (),
            }
        assert not db.verify("all").findings


@pytest.mark.parametrize("metadata", [["N"], ("N", "B"), ("Missing",), ("",)])
def test_raw_scan_rejects_noncanonical_or_unadmitted_metadata(tmp_path, monkeypatch, metadata):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:N:B {id:1})")
        original = HeapStore.scan_page

        def forged(self, *args, **kwargs):
            rows, continuation = original(self, *args, **kwargs)
            return tuple((ref, replace(version, node_labels=metadata)) for ref, version in rows), continuation

        monkeypatch.setattr(HeapStore, "scan_page", forged)
        with db.begin("read") as tx:
            with pytest.raises(GrafxError):
                tx.scan_rows_v1("N", limit=10)


def test_relationship_scan_never_claims_node_label_metadata(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (a:N:B)-[:R]->(b:N:C)")
        physical = db.catalog.catalog.relationship_tables("R")[0].name
        with db.begin("read") as tx:
            assert tx.scan_rows_v1(physical, limit=10).rows[0].node_labels is None
