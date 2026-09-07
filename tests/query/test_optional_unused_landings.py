"""Unconsumed optional endpoints retain validation, not vector components."""
from collections import Counter

import pytest

import okto_grafx
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.errors import GrafxCorruptionDetected, GrafxParseError, GrafxQueryBudgetExceeded

from . import test_vector_free_traversal as vector_fixture

graph = vector_fixture.graph


DEGREES = (
    "MATCH (n:A) OPTIONAL MATCH (n)-[r_out]->() "
    "WITH n, COUNT(r_out) AS out_degree "
    "OPTIONAL MATCH (n)<-[r_in]-() "
    "RETURN n.id, out_degree, COUNT(r_in) AS in_degree"
)


@pytest.mark.parametrize("budget", [None, 32768])
def test_optional_degrees_preserve_counts_and_skip_unused_vector_materialization(graph, monkeypatch, budget):
    original = HeapStore.read_landing
    reads = Counter()

    def observed(heap, ref):
        reads["landing"] += 1
        return original(heap, ref)

    monkeypatch.setattr(HeapStore, "read_landing", observed)
    graph.checkpoint()
    with okto_grafx.connect(graph.path, read_only=True, page_size=8192,
                           query_memory_budget_bytes=budget) as reader:
        candidate = reader.execute(DEGREES)
        assert candidate.rows == (("a", 6, 0),)
        assert reads["landing"] > 0
        monkeypatch.setattr(qe, "_unused_optional_landings", lambda _: frozenset())
        oracle = reader.execute(DEGREES)
        assert candidate.rows == oracle.rows
        assert candidate.columns == oracle.columns
        assert candidate.statistics["rows_scanned"] == oracle.statistics["rows_scanned"]


@pytest.mark.parametrize("query", [
    "MATCH (n:A) OPTIONAL MATCH (n)-[r]->(target) RETURN target",
    "MATCH (n:A) OPTIONAL MATCH (n)-[r]->(target) RETURN target.v",
    "MATCH (n:A) OPTIONAL MATCH (n)-[r]->(target) WITH target RETURN target",
    "MATCH (n:A) OPTIONAL MATCH (n)-[r]->(target) WHERE target.v IS NOT NULL RETURN n.id",
])
def test_consumed_or_forwarded_target_keeps_full_path(graph, monkeypatch, query):
    def forbidden(*args):
        pytest.fail("A consumed optional endpoint must remain fully materialized")

    monkeypatch.setattr(HeapStore, "read_landing", forbidden)
    assert graph.execute(query).rows


def test_optional_degrees_preserve_owner_overlay_and_independent_snapshot(graph):
    with graph.begin("read") as reader:
        assert reader.execute(DEGREES).rows == (("a", 6, 0),)
        with okto_grafx.connect(graph.path, page_size=8192) as other:
            with other.begin("write") as writer:
                writer.execute("CREATE (:A {id:'isolated'})")
                writer.execute("MATCH (a:A {id:'a'}), (b:B {id:'b0'}) CREATE (a)-[:E {rank:9}]->(b)")
                assert writer.execute(DEGREES).rows == (("a", 7, 0), ("isolated", 0, 0))
        assert reader.execute(DEGREES).rows == (("a", 6, 0),)
    assert graph.execute(DEGREES).rows == (("a", 7, 0), ("isolated", 0, 0))


def test_optional_degrees_preserve_custom_full_read_hook(graph, monkeypatch):
    original = HeapStore.read
    calls = []

    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    def forbidden(*args):
        pytest.fail("A custom full reader must retain its validation hook")

    monkeypatch.setattr(HeapStore, "read", observed)
    monkeypatch.setattr(HeapStore, "read_landing", forbidden)
    assert graph.execute(DEGREES).rows == (("a", 6, 0),)
    assert calls


@pytest.mark.parametrize("query", [
    "MATCH (n:A) OPTIONAL MATCH (n)-[r]->(target) OPTIONAL MATCH (target)<-[r2]-() RETURN n.id",
    "MATCH (n:A) OPTIONAL MATCH (n)-[r]->(target) RETURN *",
])
def test_unsupported_shape_retains_parse_refusal(graph, monkeypatch, query):
    with pytest.raises(GrafxParseError) as candidate:
        graph.execute(query)
    monkeypatch.setattr(qe, "_unused_optional_landings", lambda _: frozenset())
    with pytest.raises(GrafxParseError) as oracle:
        graph.execute(query)
    assert candidate.value.to_dict() == oracle.value.to_dict()


def test_unused_optional_vector_still_detects_corruption(graph, monkeypatch):
    original = HeapStore._read_slot
    table_id = graph._queries.catalog.catalog.table("B").table_id

    def corrupt(heap, ref):
        found_table, content = original(heap, ref)
        if found_table == table_id:
            content = bytearray(content)
            content[-1544:-1540] = (99999).to_bytes(4, "little")
            content = bytes(content)
        return found_table, content

    monkeypatch.setattr(HeapStore, "_read_slot", corrupt)
    with pytest.raises(GrafxCorruptionDetected) as candidate:
        graph.execute(DEGREES)
    monkeypatch.setattr(qe, "_unused_optional_landings", lambda _: frozenset())
    with pytest.raises(GrafxCorruptionDetected) as oracle:
        graph.execute(DEGREES)
    assert candidate.value.to_dict() == oracle.value.to_dict()


def test_optional_degrees_keep_admission_budget_and_cleanup(graph, monkeypatch):
    with okto_grafx.connect(graph.path, page_size=8192, max_intermediate_rows=2) as bounded:
        with pytest.raises(GrafxQueryBudgetExceeded) as candidate:
            bounded.execute(DEGREES)
        monkeypatch.setattr(qe, "_unused_optional_landings", lambda _: frozenset())
        with pytest.raises(GrafxQueryBudgetExceeded) as oracle:
            bounded.execute(DEGREES)
        assert candidate.value.to_dict() == oracle.value.to_dict()
        assert bounded.execute("MATCH (n:A) RETURN n.id").rows == (("a",),)


def test_incoming_unused_vectors_and_outgoing_parallel_edges(graph, monkeypatch):
    with graph.begin("write") as writer:
        writer.execute("CREATE REL TABLE Back(FROM B TO A)")
        writer.execute("MATCH (a:A {id:'a'}), (b:B {id:'b0'}) CREATE (b)-[:Back]->(a)")
    candidate = graph.execute(DEGREES).rows
    assert candidate == (("a", 6, 1),)
    monkeypatch.setattr(qe, "_unused_optional_landings", lambda _: frozenset())
    assert graph.execute(DEGREES).rows == candidate


def test_unused_optional_landings_retain_less_and_release_every_charge(graph, monkeypatch):
    budget = graph._queries._owner_budget
    with graph.begin("read") as reader:
        expected = reader.execute(DEGREES).rows
        candidate_bytes = budget._used_bytes
    assert budget._used_bytes == 0
    monkeypatch.setattr(qe, "_unused_optional_landings", lambda _: frozenset())
    with graph.begin("read") as reader:
        assert reader.execute(DEGREES).rows == expected
        canonical_bytes = budget._used_bytes
    assert 0 < candidate_bytes < canonical_bytes
    assert budget._used_bytes == 0
