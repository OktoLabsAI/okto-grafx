"""Ordered pages omit allocations, not payload validation or snapshot authority."""
from dataclasses import replace

import pytest

from okto_grafx import Timestamp, connect
from okto_grafx.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.model.schema import _is_unmaterialized_column
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.ordered_index import OrderedIndex
from okto_grafx.engine import query_engine


PAGE = "MATCH (n) WHERE n.score >= $minimum RETURN n.id,n.created_at,n.title ORDER BY n.created_at DESC,n.id DESC LIMIT 3"


@pytest.fixture(params=["strict", "generation"])
def db(tmp_path, request):
    with connect(tmp_path / "db", descriptor_revalidation=request.param) as database:
        with database.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE emb {dimension:4,metric:'cosine'}")
            for table in ("A", "B"):
                tx.execute(f"CREATE NODE TABLE {table}(id STRING,created_at TIMESTAMP,title STRING,score DOUBLE,payload STRING,embedding VECTOR(emb),PRIMARY KEY(id))")
                for i in range(3):
                    tx.execute(f"CREATE (:{table} {{id:$id,created_at:$ts,title:$title,score:1.0,payload:$payload,embedding:[1.0,0.0,0.0,0.0]}})",
                               {"id": f"{table}{i}", "ts": Timestamp(i), "title": table,
                                "payload": "x" * 12000})
        for table in ("A", "B"):
            database.create_index(f"ordered_{table}", table, ("created_at", "id"), layout="ordered")
        yield database


def test_page_matches_full_decoder_and_omits_payload_vector(db, monkeypatch):
    original = HeapStore._read_if_projected
    observed = []

    def read(heap, ref, accept, positions):
        result = original(heap, ref, accept, positions)
        if result is not None:
            assert _is_unmaterialized_column(result.values[4])
            assert _is_unmaterialized_column(result.values[5])
            assert positions == frozenset((0, 1, 2, 3))
            observed.append(ref)
        return result

    monkeypatch.setattr(HeapStore, "_read_if_projected", read)
    result = db.execute(PAGE, {"minimum": 0.5})
    assert observed and result.statistics["ordered_projected_tables"] == 2
    monkeypatch.setattr(query_engine, "_closed_node_scan_projections", lambda _root: {})
    assert result.rows == db.execute(PAGE, {"minimum": 0.5}).rows


def test_requested_large_values_are_materialized(db):
    text = PAGE.replace("n.title ORDER", "n.title,n.payload,n.embedding ORDER")
    result = db.execute(text, {"minimum": 0.5})
    assert len(result.rows) == 3
    assert all(row[3] == "x" * 12000 and len(row[4].values) == 4 for row in result.rows)


def test_omitted_malformed_vector_refuses_like_full_decoder(db, monkeypatch):
    original = HeapStore._validated_payload
    def malformed(heap, table, header, content):
        payload, labels = original(heap, table, header, content)
        return payload[:-1], labels
    monkeypatch.setattr(HeapStore, "_validated_payload", malformed)
    with pytest.raises(GrafxCorruptionDetected) as projected:
        db.execute(PAGE, {"minimum": 0.5})
    monkeypatch.setattr(query_engine, "_closed_node_scan_projections", lambda _root: {})
    with pytest.raises(GrafxCorruptionDetected) as full:
        db.execute(PAGE, {"minimum": 0.5})
    assert projected.value.details == full.value.details


def test_custom_header_and_iterator_hooks_keep_canonical_door(db, monkeypatch):
    old_read = HeapStore.read_if
    old_iter = OrderedIndex.iter_visible_desc
    calls = []
    def read(heap, ref, accept):
        calls.append("heap")
        return old_read(heap, ref, accept)
    monkeypatch.setattr(HeapStore, "read_if", read)
    assert len(db.execute(PAGE, {"minimum": 0.5}).rows) == 3
    assert calls
    calls.clear()
    def iterate(index, heap, table, snapshot, *, upper_key=None):
        calls.append("index")
        yield from old_iter(index, heap, table, snapshot, upper_key=upper_key)
    monkeypatch.setattr(OrderedIndex, "iter_visible_desc", iterate)
    assert len(db.execute(PAGE, {"minimum": 0.5}).rows) == 3
    assert "index" in calls


def test_foreign_table_does_not_publish_prefix(db, monkeypatch):
    original = HeapStore._read_if_projected
    def wrong(heap, ref, accept, positions):
        row = original(heap, ref, accept, positions)
        return replace(row, table_id=999) if row else None
    monkeypatch.setattr(HeapStore, "_read_if_projected", wrong)
    with pytest.raises(GrafxCorruptionDetected):
        db.execute(PAGE, {"minimum": 0.5})


def test_failed_post_certificate_refuses_projected_prefix(db, monkeypatch):
    original = OrderedIndex._read_certificate
    calls = {}
    def certificate(index):
        calls[index.name] = calls.get(index.name, 0) + 1
        if calls[index.name] == 2:
            raise GrafxIndexError("Injected post-certificate loss", field="index_view_changed", retryable=True)
        return original(index)
    monkeypatch.setattr(OrderedIndex, "_read_certificate", certificate)
    with pytest.raises(GrafxIndexError):
        db.execute(PAGE, {"minimum": 0.5})


@pytest.mark.parametrize("projected", [False, True])
def test_old_snapshot_and_fresh_reader_remain_distinct(db, monkeypatch, projected):
    if not projected:
        monkeypatch.setattr(query_engine, "_closed_node_scan_projections", lambda _root: {})
    with db.begin("read") as reader:
        before = reader.execute(PAGE, {"minimum": 0.5}).rows
        with connect(db.path) as writer:
            with writer.begin("write") as tx:
                tx.execute("MATCH (n:A {id:'A0'}) SET n.created_at=$ts", {"ts": Timestamp(99)})
        assert reader.execute(PAGE, {"minimum": 0.5}).rows == before
    assert db.execute(PAGE, {"minimum": 0.5}).rows[0][0] == "A0"


def test_writer_overlay_declines_ordered_projection(db):
    with db.begin("write") as tx:
        tx.execute("MATCH (n:A {id:'A0'}) SET n.created_at=$ts", {"ts": Timestamp(99)})
        result = tx.execute(PAGE, {"minimum": 0.5})
        assert result.rows[0][0] == "A0"
        assert "ordered_projected_tables" not in result.statistics
