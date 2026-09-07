"""Closed relationship COUNT batches preserve canonical witnesses and refusals."""
from dataclasses import replace

import pytest
import okto_grafx

from okto_grafx.domain.index import RECORD_ID_KEY_DERIVATION
from okto_grafx.domain.index.keys import record_id_key
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexStore, INDEX_READ_RETRY_BUDGET
from okto_grafx.errors import GrafxCorruptionDetected, GrafxError, GrafxIndexError

from . import test_vector_free_traversal as vector_fixture

graph = vector_fixture.graph
SCAN_PREFIX = vector_fixture.SCAN_PREFIX


def _identity(graph, table="B"):
    return next(index for index in graph._indexes.active_indexes()
                if index.definition.table_name == table
                and index.definition.key_derivation == RECORD_ID_KEY_DERIVATION)


@pytest.mark.parametrize("expression", ["count(r)", "count(*)", "count(r) AS c"])
def test_count_matches_scalar_rows_columns_and_statistics(graph, monkeypatch, expression):
    query = SCAN_PREFIX + "RETURN " + expression
    candidate = graph.execute(query)
    monkeypatch.setattr(qe, "_batched_relationship_count", lambda *_args: None)
    oracle = graph.execute(query)
    assert candidate.rows == oracle.rows == ((6,),)
    assert candidate.columns == oracle.columns
    assert candidate.statistics == oracle.statistics


def test_frontier_never_exceeds_64_and_opens_fewer_certificates(graph, monkeypatch):
    scan = HeapStore.scan
    probes = []
    finish = HashIndex.finish_exact_read
    certificates = []
    edge_table = graph._queries.catalog.catalog.table("E").table_id

    def repeated(heap, table, snapshot):
        rows = scan(heap, table, snapshot)
        if table.table_id != edge_table:
            yield from rows
            return
        for ref, version in rows:
            for _ in range(23):
                yield ref, version

    def counted(store, certificate, lsn):
        certificates.append(store.file)
        return finish(store, certificate, lsn)

    original = qe._OwnerLandingView.counts_many

    def observed(view, identities, context, index):
        probes.append(len(identities))
        return original(view, identities, context, index)

    monkeypatch.setattr(HeapStore, "scan", repeated)
    monkeypatch.setattr(HashIndex, "finish_exact_read", counted)
    monkeypatch.setattr(qe._OwnerLandingView, "counts_many", observed)
    assert graph.execute(SCAN_PREFIX + "RETURN count(r)").rows == ((138,),)
    assert probes == [64, 64, 64, 64, 10, 10]
    assert len(certificates) == 3  # A plus the two B frontiers with new identities.
    assert graph._queries._owner_budget._used_bytes == 0


def test_many_cardinalities_match_scalar_landings_with_duplicate_and_missing_keys(graph):
    index = _identity(graph)
    with graph.begin("read") as reader:
        snapshot = reader._context.snapshot
        keys = [record_id_key(i) for i in (1, 2, 1, 999999, 3)]
        expected = tuple(len(graph._indexes.validated_identity_landings(index, key, snapshot))
                         for key in keys)
        assert graph._indexes.validated_identity_counts_many(index, keys, snapshot) == expected


@pytest.mark.parametrize("unstable", [False, True])
def test_whole_batch_retries_or_refuses_without_returning_a_prefix(graph, monkeypatch, unstable):
    index = _identity(graph)
    original = HashIndex.finish_exact_read
    calls = []

    def changed(store, certificate, lsn):
        if store.file != index.file:
            return original(store, certificate, lsn)
        calls.append(1)
        return False if unstable or len(calls) == 1 else original(store, certificate, lsn)

    with graph.begin("read") as reader:
        snapshot = reader._context.snapshot
        keys = [record_id_key(i) for i in (1, 2, 3)]
        expected = graph._indexes.validated_identity_counts_many(index, keys, snapshot)
        monkeypatch.setattr(HashIndex, "finish_exact_read", changed)
        if unstable:
            with pytest.raises(GrafxIndexError) as exc:
                graph._indexes.validated_identity_counts_many(index, keys, snapshot)
            assert exc.value.details["field"] == "index_view_changed"
            assert len(calls) == INDEX_READ_RETRY_BUDGET + 1
        else:
            assert graph._indexes.validated_identity_counts_many(index, keys, snapshot) == expected
            assert len(calls) == 2


def test_foreign_table_candidate_is_not_counted_silently(graph, monkeypatch):
    read = HeapStore.read_landing
    table_id = graph._queries.catalog.catalog.table("B").table_id

    def foreign(heap, ref):
        version = read(heap, ref)
        return replace(version, table_id=999999) if version.table_id == table_id else version

    monkeypatch.setattr(HeapStore, "read_landing", foreign)
    with pytest.raises(GrafxCorruptionDetected) as exc:
        graph.execute(SCAN_PREFIX + "RETURN count(r)")
    assert exc.value.details["field"] == "table_id"


@pytest.mark.parametrize("suffix", ["WHERE r.rank=1 RETURN count(r)",
    "RETURN b.id,count(r)", "RETURN count(DISTINCT r)", "RETURN b.v"])
def test_other_query_shapes_do_not_enter_identity_count_batch(graph, monkeypatch, suffix):
    original = qe._COUNT_CANONICAL_MANY
    calls = []

    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(type(graph._indexes), "validated_identity_counts_many", observed)
    monkeypatch.setattr(qe, "_COUNT_CANONICAL_MANY", observed)
    graph.execute(SCAN_PREFIX + suffix)
    assert not calls


def test_owner_write_keeps_pending_edges_and_original_path(graph, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("owner write must not use the durable-only batch")

    monkeypatch.setattr(type(graph._indexes), "validated_identity_counts_many", forbidden)
    monkeypatch.setattr(qe, "_COUNT_CANONICAL_MANY", forbidden)
    with graph.begin("write") as writer:
        writer.execute("MATCH (a:A),(b:B {id:'b0'}) CREATE (a)-[:E {rank:9}]->(b)")
        assert writer.execute(SCAN_PREFIX + "RETURN count(r)").rows == ((7,),)


def test_presence_cannot_escape_as_a_vector_or_cross_snapshot(graph):
    query = SCAN_PREFIX + "RETURN count(r)"
    with graph.begin("read") as reader:
        assert reader.execute(query).rows == ((6,),)
        with okto_grafx.connect(graph.path, page_size=8192) as other:
            with other.begin("write") as writer:
                writer.execute("MATCH (a:A),(b:B {id:'b0'}) CREATE (a)-[:E {rank:9}]->(b)")
        assert reader.execute(query).rows == ((6,),)
        rows = reader.execute(SCAN_PREFIX + "RETURN b.id,b.v").rows
        assert len(rows) == 6 and all(len(row[1].values) == 384 for row in rows)
    assert graph.execute(query).rows == ((7,),)
    assert graph._queries._owner_budget._used_bytes == 0


@pytest.mark.parametrize("limit", [0, 4096])
def test_presence_retention_declines_or_evicts_without_changing_answer(graph, monkeypatch, limit):
    budget = graph._queries._owner_budget
    monkeypatch.setattr(budget, "_max_bytes", limit)
    with graph.begin("read") as reader:
        for _ in range(2):
            assert reader.execute(SCAN_PREFIX + "RETURN count(r)").rows == ((6,),)
            assert 0 <= budget._used_bytes <= limit
    assert budget._used_bytes == budget._used_entries == 0


@pytest.mark.parametrize("option", ["_max_intermediate_rows", "_max_traversal_paths",
    "_max_traversal_expansions", "_query_memory_budget_bytes"])
def test_operational_limits_keep_the_canonical_admission_path(graph, monkeypatch, option):
    def forbidden(*_args, **_kwargs):
        pytest.fail("configured operational limits must keep canonical operators")

    monkeypatch.setattr(type(graph._indexes), "validated_identity_counts_many", forbidden)
    monkeypatch.setattr(qe, "_COUNT_CANONICAL_MANY", forbidden)
    monkeypatch.setattr(graph._queries, option, 65536)
    def outcome():
        try:
            return graph.execute(SCAN_PREFIX + "RETURN count(r)").rows
        except GrafxError as exc:
            return exc.to_dict()

    candidate = outcome()
    monkeypatch.setattr(qe, "_batched_relationship_count", lambda *_args: None)
    assert candidate == outcome()


def test_duplicate_visible_identity_refuses_with_scalar_details(graph, monkeypatch):
    index = _identity(graph)
    original = IndexStore._candidates_unchecked

    def duplicated(store, key):
        for entry in original(store, key):
            yield entry
            if store.file == index.file:
                yield entry

    monkeypatch.setattr(IndexStore, "_candidates_unchecked", duplicated)
    with pytest.raises(GrafxCorruptionDetected) as batch:
        graph.execute(SCAN_PREFIX + "RETURN count(r)")
    monkeypatch.setattr(qe, "_batched_relationship_count", lambda *_args: None)
    with pytest.raises(GrafxCorruptionDetected) as scalar:
        graph.execute(SCAN_PREFIX + "RETURN count(r)")
    assert batch.value.to_dict() == scalar.value.to_dict()


def test_omitted_vector_is_validated_by_count_batch(graph, monkeypatch):
    read = HeapStore._read_slot
    table_id = graph._queries.catalog.catalog.table("B").table_id

    def corrupt(heap, ref):
        found_table, payload = read(heap, ref)
        if found_table == table_id:
            payload = bytearray(payload)
            payload[-1544:-1540] = (99999).to_bytes(4, "little")
            payload = bytes(payload)
        return found_table, payload

    monkeypatch.setattr(HeapStore, "_read_slot", corrupt)
    with pytest.raises(GrafxCorruptionDetected) as batch:
        graph.execute(SCAN_PREFIX + "RETURN count(r)")
    monkeypatch.setattr(qe, "_batched_relationship_count", lambda *_args: None)
    with pytest.raises(GrafxCorruptionDetected) as scalar:
        graph.execute(SCAN_PREFIX + "RETURN count(r)")
    assert batch.value.to_dict() == scalar.value.to_dict()


def test_missing_source_does_not_probe_its_target(graph, monkeypatch):
    source = _identity(graph, "A")
    target = _identity(graph, "B")
    original = IndexStore._candidates_unchecked

    def missing(store, key):
        if store.file == source.file:
            return iter(())
        if store.file == target.file:
            pytest.fail("a missing source must short-circuit target validation")
        return original(store, key)

    monkeypatch.setattr(IndexStore, "_candidates_unchecked", missing)
    assert graph.execute(SCAN_PREFIX + "RETURN count(r)").rows == ((0,),)
    monkeypatch.setattr(qe, "_batched_relationship_count", lambda *_args: None)
    assert graph.execute(SCAN_PREFIX + "RETURN count(r)").rows == ((0,),)


def test_empty_relation_has_the_same_zero_aggregate_and_statistics(graph, monkeypatch):
    with graph.begin("write") as writer:
        writer.execute("CREATE REL TABLE Empty(FROM A TO B)")
    query = "MATCH (a:A)-[r:Empty]->(b:B) RETURN count(r)"
    candidate = graph.execute(query)
    monkeypatch.setattr(qe, "_batched_relationship_count", lambda *_args: None)
    oracle = graph.execute(query)
    assert candidate.rows == oracle.rows == ((0,),)
    assert candidate.statistics == oracle.statistics
