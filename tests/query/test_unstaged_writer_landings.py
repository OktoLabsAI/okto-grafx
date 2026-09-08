"""A read-only preflight may batch before, never after, owner staging."""
import pytest
from types import SimpleNamespace

from okto_grafx import connect
from okto_grafx.engine import query_engine as qe
from okto_grafx.errors import GrafxError, GrafxWriteConflict
from . import test_batched_landing_traversal as base

graph = base.graph
QUERY = base.QUERY


def test_clean_writer_batches_same_rows_and_read_partitions(graph, monkeypatch):
    calls = base._observe(monkeypatch)
    with graph.begin('write') as tx:
        result = tx.execute(QUERY)
        candidate_reads = set(tx._context.read_partitions)
        assert not tx._context.wrote
    assert calls and max(calls) <= 64
    monkeypatch.setattr(qe, '_admits_batched_landings', lambda *_args: False)
    with graph.begin('write') as tx:
        oracle = tx.execute(QUERY)
        assert set(tx._context.read_partitions) == candidate_reads
        assert not tx._context.wrote
    assert result.rows == oracle.rows
    assert result.columns == oracle.columns
    assert result.plan == oracle.plan
    assert graph._queries._owner_budget._used_bytes == 0


def test_first_staging_declines_batches_and_invalidates_partial_values(graph, monkeypatch):
    calls = base._observe(monkeypatch)
    with graph.begin('write') as tx:
        assert len(tx.execute(QUERY).rows) == 3
        assert calls
        calls.clear()
        tx.execute("MATCH (b:B {id:'b1'}) SET b.title='changed'")
        assert ('b1', 'changed', 2) in tx.execute(QUERY).rows
        assert calls == []
        full = tx.execute(base.PREFIX + 'RETURN b.v').rows
        assert all(len(row[0].values) == 384 for row in full)
    assert graph._queries._owner_budget._used_bytes == 0


def test_schema_staging_also_declines_batches(graph, monkeypatch):
    calls = base._observe(monkeypatch)
    with graph.begin('write') as tx:
        tx.execute('CREATE NODE TABLE Extra(id INT64, PRIMARY KEY(id))')
        assert tx._context.wrote
        assert len(tx.execute(QUERY).rows) == 3
    assert calls == []


@pytest.mark.parametrize('suffix', ['RETURN b.id LIMIT 1', 'RETURN b.v', 'RETURN b'])
def test_streaming_and_full_payload_remain_scalar_in_clean_writer(graph, monkeypatch, suffix):
    calls = base._observe(monkeypatch)
    with graph.begin('write') as tx:
        tx.execute(base.PREFIX + suffix)
    assert calls == []


@pytest.mark.parametrize('quota', ['_max_intermediate_rows', '_max_traversal_paths',
    '_max_traversal_expansions', '_query_memory_budget_bytes'])
def test_configured_quota_keeps_scalar_boundary(graph, monkeypatch, quota):
    calls = base._observe(monkeypatch)
    monkeypatch.setattr(graph._queries, quota, 65536)

    def outcome():
        with graph.begin('write') as tx:
            try:
                return tx.execute(QUERY).rows
            except GrafxError as error:
                return error.to_dict()

    candidate = outcome()
    assert not calls
    monkeypatch.setattr(qe, '_admits_batched_landings', lambda *_args: False)
    assert outcome() == candidate


def test_clean_writer_keeps_snapshot_after_independent_publication(graph, monkeypatch):
    calls = base._observe(monkeypatch)
    writer = graph.begin('write')
    try:
        before = writer.execute(QUERY).rows
        assert calls
        with connect(graph.path, page_size=8192) as other:
            with other.begin('write') as changed:
                changed.execute("MATCH (b:B {id:'b1'}) SET b.title='other generation'")
        assert writer.execute(QUERY).rows == before
        assert not writer._context.wrote
    finally:
        writer.rollback()
    assert ('b1', 'other generation', 2) in graph.execute(QUERY).rows
    assert graph._queries._owner_budget._used_bytes == 0


@pytest.mark.parametrize('fault', ['duplicate', 'corruption'])
def test_clean_writer_preserves_corruption_refusal(graph, monkeypatch, fault):
    # Reuse the existing hostile scalar/batch oracle but bind every execute to
    # a fresh, unstaged writer. Rollback retains the exact original exception.
    def in_writer(statement, *args, **kwargs):
        tx = graph.begin('write')
        try:
            return tx.execute(statement, *args, **kwargs)
        finally:
            tx.rollback()

    facade = SimpleNamespace(execute=in_writer, _indexes=graph._indexes, _queries=graph._queries)
    if fault == 'duplicate':
        base.test_duplicate_identity_refuses_with_scalar_error(facade, monkeypatch)
    else:
        base.test_omitted_vector_corruption_still_refuses_before_grouped_answer(facade, monkeypatch)


@pytest.mark.parametrize('batched', [False, True])
def test_preflight_then_conflicting_update_still_refuses_publication(graph, monkeypatch, batched):
    if not batched:
        monkeypatch.setattr(qe, '_admits_batched_landings', lambda *_args: False)
    writer = graph.begin('write')
    try:
        writer.execute(QUERY)
        with connect(graph.path, page_size=8192) as other:
            with other.begin('write') as changed:
                changed.execute("MATCH (b:B {id:'b1'}) SET b.title='winner'")
        writer.execute("MATCH (b:B {id:'b1'}) SET b.title='must not publish'")
        with pytest.raises(GrafxWriteConflict):
            writer.commit()
    finally:
        if writer.active:
            writer.rollback()
    assert graph.execute("MATCH (b:B {id:'b1'}) RETURN b.title").rows == (('winner',),)
