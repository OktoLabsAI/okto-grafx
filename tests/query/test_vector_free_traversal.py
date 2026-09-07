"""Closed scalar traversals validate landing vectors without retaining components."""
from __future__ import annotations

from collections import Counter

import pytest

import okto_grafx
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.errors import GrafxCorruptionDetected, GrafxPlanError
from okto_grafx.domain.model.errors import SchemaMismatchError


@pytest.fixture
def graph(tmp_path):
    with okto_grafx.connect(tmp_path / "db", page_size=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE emb {dimension: 384, metric:'cosine'}")
            tx.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id STRING, title STRING, v VECTOR(emb), PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM A TO B, rank INT64)")
            tx.execute("CREATE (:A {id:'a'})")
            for i in range(3):
                tx.execute("CREATE (:B {id:$id, title:$title, v:$v})", {
                    "id": f"b{i}", "title": f"Title {i}", "v": [float(i + 1)] * 384,
                })
                for rank in range(2):
                    tx.execute("MATCH (a:A {id:'a'}), (b:B {id:$id}) CREATE (a)-[:E {rank:$rank}]->(b)",
                               {"id": f"b{i}", "rank": rank})
        db.ensure_identity_indexes()
        yield db


PREFIX = "MATCH (a:A)-[r:E]->(b:B) WHERE a.id='a' "


@pytest.mark.parametrize("suffix,enabled", [
    ("RETURN b.id, b.title, r.rank", True),
    ("RETURN b.id, label(b) ORDER BY b.id DESC LIMIT 4", True),
    ("RETURN b.id, count(r) ORDER BY b.id", True),
    ("RETURN count(r)", True),
    ("RETURN b.id, b.v", False),
    ("RETURN b", False),
    ("WITH b RETURN b.id", False),
    ("RETURN b.id ORDER BY b.v IS NULL", False),
])
def test_native_results_equal_full_decode_and_proof_is_exact(graph, monkeypatch, suffix, enabled):
    counts = Counter()
    original = HeapStore.read_landing

    def counted(heap, ref):
        counts["landing"] += 1
        return original(heap, ref)

    monkeypatch.setattr(HeapStore, "read_landing", counted)
    query = PREFIX + suffix
    candidate = graph.execute(query)
    assert bool(counts["landing"]) is enabled
    monkeypatch.setattr(qe, "_closed_vector_free_landings", lambda _root: frozenset())
    oracle = graph.execute(query)
    assert candidate.rows == oracle.rows
    assert candidate.columns == oracle.columns


def test_partial_then_full_then_owner_update_cannot_reuse_partial_values(graph):
    with graph.begin("write") as tx:
        assert len(tx.execute(PREFIX + "RETURN b.id").rows) == 6
        full = tx.execute(PREFIX + "RETURN b.id,b.v").rows
        assert all(len(row[1].values) == 384 for row in full)
        tx.execute("MATCH (b:B {id:'b1'}) SET b.title='changed', b.v=$v",
                   {"v": okto_grafx.VectorValue((9.0,) * 384, 1, "float32")})
        rows = tx.execute(PREFIX + "RETURN b.id,b.title").rows
        assert [row[1] for row in rows if row[0] == "b1"] == ["changed", "changed"]
        full = tx.execute(PREFIX + "RETURN b.id,b.v").rows
        assert all(row[1].values == (9.0,) * 384 for row in full if row[0] == "b1")
    budget = graph._queries._owner_budget
    assert budget._used_bytes == budget._used_entries == 0


@pytest.mark.parametrize("optimized", [False, True])
def test_pending_vector_parameter_tuple_is_not_a_quota_attribute_error(graph, monkeypatch, optimized):
    if not optimized:
        monkeypatch.setattr(qe, "_closed_vector_free_landings", lambda _root: frozenset())
    with pytest.raises(SchemaMismatchError):
        with graph.begin("write") as tx:
            tx.execute("MATCH (b:B {id:'b1'}) SET b.v=$v", {"v": [7.0] * 384})
            assert len(tx.execute(PREFIX + "RETURN b.id,b.title").rows) == 6
    rows = graph.execute("MATCH (b:B {id:'b1'}) RETURN b.v").rows
    assert rows[0][0].values == (2.0,) * 384


def test_capability_absence_keeps_full_validation_and_same_rows(graph, monkeypatch):
    expected = graph.execute(PREFIX + "RETURN b.id,b.title").rows
    monkeypatch.delattr(IndexManager, "validated_identity_landings")
    assert graph.execute(PREFIX + "RETURN b.id,b.title").rows == expected


@pytest.mark.parametrize("owner,method", [(HeapStore, "read"), (HeapStore, "_decode_version"),
                                         (IndexManager, "validated_versions")])
def test_specialized_full_read_hook_is_not_bypassed(graph, monkeypatch, owner, method):
    original = getattr(owner, method)
    calls = []

    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    def forbidden(*_args, **_kwargs):
        pytest.fail("specialized full validation may not be replaced by landing validation")

    monkeypatch.setattr(owner, method, observed)
    monkeypatch.setattr(HeapStore, "read_landing", forbidden)
    assert len(graph.execute(PREFIX + "RETURN b.id").rows) == 6
    assert calls


def test_wrong_projection_proof_refuses_instead_of_exposing_a_marker(graph, monkeypatch):
    monkeypatch.setattr(qe, "_closed_vector_free_landings", lambda root: frozenset(
        id(node) for node in root.walk() if type(node) is qe.TraverseRelationship
    ))
    with pytest.raises(GrafxPlanError) as caught:
        graph.execute(PREFIX + "RETURN b.v")
    assert caught.value.details["field"] == "projection"


def test_independent_writer_does_not_change_reader_snapshot_or_leak_partial_cache(graph):
    with okto_grafx.connect(graph.path, page_size=8192) as writer:
        with graph.begin("read") as reader:
            before = reader.execute(PREFIX + "RETURN b.id,b.title").rows
            with writer.begin("write") as tx:
                tx.execute("MATCH (b:B {id:'b1'}) SET b.title='new generation'")
            assert reader.execute(PREFIX + "RETURN b.id,b.title").rows == before
            vectors = reader.execute(PREFIX + "RETURN b.id,b.v").rows
            assert all(len(row[1].values) == 384 for row in vectors)
        assert [row for row in graph.execute(PREFIX + "RETURN b.id,b.title").rows
                if row[0] == "b1"] == [("b1", "new generation")] * 2


def test_cursor_keeps_scalar_projection_and_releases_its_reader(graph):
    query = PREFIX + "RETURN b.id,b.title ORDER BY b.id"
    expected = graph.execute(query).rows
    with graph.query(query).cursor(batch_size=2) as cursor:
        assert tuple(cursor) == expected
    assert graph._queries._owner_budget._used_bytes == 0


def test_omitted_vector_corruption_still_refuses_with_oracle_error(graph, monkeypatch):
    original = HeapStore._read_slot
    table_id = graph._queries.catalog.catalog.table("B").table_id

    def corrupt(heap, ref):
        found_table, content = original(heap, ref)
        if found_table == table_id:
            content = bytearray(content)
            # Last column: vector tag, dimension, space, 384 float32 components.
            content[-1544:-1540] = (99999).to_bytes(4, "little")
            content = bytes(content)
        return found_table, content

    monkeypatch.setattr(HeapStore, "_read_slot", corrupt)
    with pytest.raises(GrafxCorruptionDetected) as candidate:
        graph.execute(PREFIX + "RETURN b.id")
    monkeypatch.setattr(qe, "_closed_vector_free_landings", lambda _root: frozenset())
    with pytest.raises(GrafxCorruptionDetected) as oracle:
        graph.execute(PREFIX + "RETURN b.id")
    assert candidate.value.to_dict() == oracle.value.to_dict()
