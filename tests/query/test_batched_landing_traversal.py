"""Blocking scalar projections use bounded, snapshot-safe destination batches."""
from dataclasses import replace

import pytest
import okto_grafx

from okto_grafx.domain.index.definition import RECORD_ID_KEY_DERIVATION
from okto_grafx.domain.index.keys import record_id_key
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager, IndexStore, INDEX_READ_RETRY_BUDGET
from okto_grafx.errors import GrafxCorruptionDetected, GrafxError, GrafxIndexError
from . import test_vector_free_traversal as fixture

graph = fixture.graph
PREFIX = fixture.PREFIX
QUERY = PREFIX + "RETURN b.id, b.title, count(r) ORDER BY b.id"


def _observe(monkeypatch):
    original = IndexManager.validated_identity_landings_many
    calls = []

    def observed(manager, index, keys, snapshot):
        calls.append(len(keys))
        return original(manager, index, keys, snapshot)

    monkeypatch.setattr(IndexManager, "validated_identity_landings_many", observed)
    monkeypatch.setattr(qe, "_BATCH_LANDING_CANONICAL_MANY", observed)
    return calls


@pytest.mark.parametrize("suffix", [
    "RETURN b.id, b.title, count(r) ORDER BY b.id",
    "RETURN b.id, r.rank ORDER BY b.id DESC LIMIT 3",
    "RETURN count(r)",
    "RETURN b.id, count(r) LIMIT 1",
])
def test_blocking_results_columns_order_and_statistics_match_scalar(graph, monkeypatch, suffix):
    calls = _observe(monkeypatch)
    candidate = graph.execute(PREFIX + suffix)
    assert calls == [3]  # Repeated destinations are validated once per batch.
    monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_: False)
    scalar = graph.execute(PREFIX + suffix)
    assert candidate.rows == scalar.rows
    assert candidate.columns == scalar.columns
    assert candidate.statistics == scalar.statistics


def test_batch_never_exceeds_64_steps_and_preserves_parallel_edge_multiplicity(graph, monkeypatch):
    original = qe._edge_steps

    def repeated(*args, **kwargs):
        steps = original(*args, **kwargs)

        def expand(identity):
            for edge in steps(identity):
                for _ in range(23):
                    yield edge
        return expand

    monkeypatch.setattr(qe, "_edge_steps", repeated)
    calls = _observe(monkeypatch)
    frontier_sizes = []
    original_many = qe._OwnerLandingView.landings_many

    def observed_frontier(view, identities, context, index):
        frontier_sizes.append(len(identities))
        return original_many(view, identities, context, index)

    monkeypatch.setattr(qe._OwnerLandingView, 'landings_many', observed_frontier)
    candidate = graph.execute(QUERY)
    assert frontier_sizes == [64, 64, 10]
    assert calls == [2, 1]  # Later frontiers reuse already-paid, bounded payloads.
    assert candidate.rows == tuple((f"b{i}", f"Title {i}", 46) for i in range(3))
    assert graph._queries._owner_budget._used_bytes == 0


@pytest.mark.parametrize("suffix", ["RETURN b.id LIMIT 1", "RETURN b.id",
    "RETURN b.v ORDER BY b.id", "WITH b RETURN count(b)"])
def test_streaming_or_full_vector_consumers_do_not_prefetch(graph, monkeypatch, suffix):
    calls = _observe(monkeypatch)
    graph.execute(PREFIX + suffix)
    assert calls == []


def test_owner_write_overlay_remains_scalar(graph, monkeypatch):
    calls = _observe(monkeypatch)
    with graph.begin("write") as tx:
        tx.execute("MATCH (a:A),(b:B {id:'b0'}) CREATE (a)-[:E {rank:9}]->(b)")
        assert tx.execute(PREFIX + "RETURN count(r)").rows == ((7,),)
    assert calls == []


def test_reader_snapshot_survives_independent_writer_and_no_proof_leaks(graph):
    with graph.begin("read") as reader:
        before = reader.execute(QUERY).rows
        with okto_grafx.connect(graph.path, page_size=8192) as other:
            with other.begin('write') as writer:
                writer.execute("MATCH (b:B {id:'b1'}) SET b.title='new generation'")
        assert reader.execute(QUERY).rows == before
        vectors = reader.execute(PREFIX + "RETURN b.v").rows
        assert all(len(row[0].values) == 384 for row in vectors)
    assert ("b1", "new generation", 2) in graph.execute(QUERY).rows
    assert graph._queries._owner_budget._used_bytes == 0


def test_empty_edge_stream_does_not_touch_destination_authority(graph, monkeypatch):
    original = qe._endpoint_identity_index

    def no_target(engine, context, table):
        if table.name == "B":
            pytest.fail("an empty source must not consult destination authority")
        return original(engine, context, table)

    monkeypatch.setattr(qe, "_endpoint_identity_index", no_target)
    assert graph.execute(QUERY.replace("a.id='a'", "a.id='absent'")).rows == ()


def test_missing_identity_index_retains_canonical_resolution(graph, monkeypatch):
    expected = graph.execute(QUERY).rows
    original = qe._endpoint_identity_index
    monkeypatch.setattr(qe, "_endpoint_identity_index",
                        lambda engine, context, table: None if table.name == 'B'
                        else original(engine, context, table))
    calls = _observe(monkeypatch)
    assert graph.execute(QUERY).rows == expected
    assert not calls


@pytest.mark.parametrize("option", ["_max_intermediate_rows", "_max_traversal_paths",
    "_max_traversal_expansions", "_query_memory_budget_bytes"])
def test_operational_quotas_preserve_scalar_admission_and_refusal(graph, monkeypatch, option):
    calls = _observe(monkeypatch)
    monkeypatch.setattr(graph._queries, option, 65536)

    def outcome():
        try:
            return graph.execute(QUERY).rows
        except GrafxError as error:
            return error.to_dict()

    candidate = outcome()
    assert calls == []
    monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_: False)
    assert candidate == outcome()


def _target_index(graph):
    return next(index for index in graph._indexes.active_indexes()
                if index.definition.table_name == 'B'
                and index.definition.key_derivation == RECORD_ID_KEY_DERIVATION)


@pytest.mark.parametrize("unstable", [False, True])
def test_batch_certificate_transition_retries_all_or_refuses(graph, monkeypatch, unstable):
    index = _target_index(graph)
    finish = HashIndex.finish_exact_read
    calls = []
    with graph.begin('read') as reader:
        keys = tuple(record_id_key(i) for i in (1, 2, 1, 999999))
        expected = graph._indexes.validated_identity_landings_many(index, keys, reader._context.snapshot)

        def changed(store, before, lsn):
            answer = finish(store, before, lsn)
            calls.append(answer)
            return False if unstable or len(calls) == 1 else answer

        monkeypatch.setattr(HashIndex, 'finish_exact_read', changed)
        if unstable:
            with pytest.raises(GrafxIndexError) as error:
                graph._indexes.validated_identity_landings_many(index, keys, reader._context.snapshot)
            assert error.value.details['field'] == 'index_view_changed'
            assert len(calls) == INDEX_READ_RETRY_BUDGET + 1
        else:
            assert graph._indexes.validated_identity_landings_many(
                index, keys, reader._context.snapshot) == expected
            assert len(calls) == 2


def test_duplicate_identity_refuses_with_scalar_error(graph, monkeypatch):
    index = _target_index(graph)
    original = IndexStore._candidates_unchecked

    def duplicates(store, key):
        for entry in original(store, key):
            yield entry
            if store.file == index.file:
                yield entry

    monkeypatch.setattr(IndexStore, '_candidates_unchecked', duplicates)
    with pytest.raises(GrafxCorruptionDetected) as candidate:
        graph.execute(QUERY)
    monkeypatch.setattr(qe, '_admits_batched_landings', lambda *_: False)
    with pytest.raises(GrafxCorruptionDetected) as scalar:
        graph.execute(QUERY)
    assert candidate.value.to_dict() == scalar.value.to_dict()


def test_invisible_candidates_remain_validated_but_do_not_become_rows(graph, monkeypatch):
    original = HeapStore.read_landing
    calls = []

    def invisible(heap, ref):
        version = original(heap, ref)
        calls.append(version.record_id)
        return replace(version, xmin=10**12)

    monkeypatch.setattr(HeapStore, 'read_landing', invisible)
    assert graph.execute(QUERY).rows == ()
    assert calls


@pytest.mark.parametrize('method', ['validated_identity_landings', '_validated_items'])
def test_specialized_scalar_witness_is_not_bypassed(graph, monkeypatch, method):
    original = getattr(IndexManager, method)
    calls = []

    def specialized(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(IndexManager, method, specialized)
    batches = _observe(monkeypatch)
    assert len(graph.execute(QUERY).rows) == 3
    assert calls and not batches


@pytest.mark.parametrize('limit', [0, 4096])
def test_payload_budget_declines_or_evicts_without_losing_rows(graph, monkeypatch, limit):
    expected = graph.execute(QUERY).rows
    monkeypatch.setattr(graph._queries._owner_budget, '_max_bytes', limit)
    with graph.begin('read') as reader:
        for _ in range(2):
            assert reader.execute(QUERY).rows == expected
            assert 0 <= graph._queries._owner_budget._used_bytes <= limit
    assert graph._queries._owner_budget._used_bytes == 0


def test_omitted_vector_corruption_still_refuses_before_grouped_answer(graph, monkeypatch):
    original = HeapStore._read_slot
    target_id = graph._queries.catalog.catalog.table('B').table_id

    def corrupt(heap, ref):
        table, content = original(heap, ref)
        if table == target_id:
            content = bytearray(content)
            content[-1544:-1540] = (99999).to_bytes(4, 'little')
            content = bytes(content)
        return table, content

    monkeypatch.setattr(HeapStore, '_read_slot', corrupt)
    with pytest.raises(GrafxCorruptionDetected) as candidate:
        graph.execute(QUERY)
    monkeypatch.setattr(qe, '_admits_batched_landings', lambda *_: False)
    with pytest.raises(GrafxCorruptionDetected) as scalar:
        graph.execute(QUERY)
    assert candidate.value.to_dict() == scalar.value.to_dict()
    assert graph._queries._owner_budget._used_bytes == 0


def test_column_derived_index_refuses_landing_batch(graph):
    column_index = next(index for index in graph._indexes.active_indexes()
                        if index.definition.table_name == 'B'
                        and index.definition.key_derivation != RECORD_ID_KEY_DERIVATION)
    with graph.begin('read') as reader:
        with pytest.raises(GrafxIndexError) as error:
            graph._indexes.validated_identity_landings_many(
                column_index, (b'key',), reader._context.snapshot)
        assert error.value.details['field'] == 'key_derivation'


def test_batch_capability_absence_keeps_scalar_path(graph, monkeypatch):
    expected = graph.execute(QUERY).rows
    monkeypatch.delattr(IndexManager, 'validated_identity_landings_many')
    assert graph.execute(QUERY).rows == expected


def test_specialized_payload_view_get_is_not_bypassed(graph, monkeypatch):
    original = qe._OwnerLandingView.get
    calls = []

    def specialized(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(qe._OwnerLandingView, 'get', specialized)
    batches = _observe(monkeypatch)
    assert len(graph.execute(QUERY).rows) == 3
    assert calls and not batches
