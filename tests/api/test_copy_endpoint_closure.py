"""Explicit one-hop endpoint expansion shares selection, snapshot and aggregate budgets."""

import pytest
from okto_grafx.catalog_copy import capture_copy, copy_graph, CopyLimits
from okto_grafx.errors import GrafxConfigurationError, GrafxUnsupportedOperation
from tests.api.test_catalog_copy import setup as _copy_setup


@pytest.fixture(name="setup")
def copy_setup(tmp_path):
    yield from _copy_setup.__wrapped__(tmp_path)


def test_endpoint_expansion_deduplicates_and_preserves_receipt(setup):
    source, target, whole = setup
    rel = next(item for item in whole.tables if item.schema.kind == 'rel')
    ids = {'R': tuple(rid for rid, _ in rel.rows)}
    with source.begin('read') as tx:
        selected = capture_copy(tx, tables=('R',), record_ids=ids, include_endpoints=True)
        assert selected == whole
        with pytest.raises(GrafxConfigurationError):
            capture_copy(tx, tables=('R',), record_ids=ids)
        with pytest.raises(GrafxConfigurationError):
            capture_copy(tx, tables=('R',), record_ids=ids, include_endpoints=True, limits=CopyLimits(max_tables=1))
        with pytest.raises(GrafxConfigurationError):
            capture_copy(tx, tables=('R',), record_ids=ids, include_endpoints=True, limits=CopyLimits(max_rows=2))
    receipt = copy_graph(selected, target, idempotency_key='closure')
    assert receipt.rows == 3
    assert copy_graph(selected, target, idempotency_key='closure').replayed
    assert target.execute('MATCH (a:N)-[r:R]->(b:N) RETURN a.id,b.id').rows == ((1, 2),)


def test_endpoint_expansion_respects_temporal_current_only(setup):
    source, target, whole = setup
    source.enable_system_history(('N', 'R'))
    rel = next(item for item in whole.tables if item.schema.kind == 'rel')
    ids = {'R': tuple(rid for rid, _ in rel.rows)}
    with source.begin('read') as tx:
        with pytest.raises(GrafxUnsupportedOperation):
            capture_copy(tx, tables=('R',), record_ids=ids, include_endpoints=True)
        selected = capture_copy(tx, tables=('R',), record_ids=ids, include_endpoints=True, history='current-only')
        assert len(selected.tables) == 2
        # The same explicit policy must work without endpoint expansion as well.
        assert len(capture_copy(tx, tables=('N','R'), history='current-only').tables) == 2
    assert copy_graph(selected, target, idempotency_key='temporal').rows == 3


def test_expansion_uses_owning_snapshot_and_validates_flags(setup):
    source, _, whole = setup
    ids = {'R': tuple(rid for item in whole.tables if item.schema.kind == 'rel' for rid, _ in item.rows)}
    with source.begin('read') as reader:
        with source.begin() as tx:
            tx.execute("MATCH (n:N {id:1}) SET n.body='updated'")
        assert capture_copy(reader, tables=('R',), record_ids=ids, include_endpoints=True) == whole
        for flag in (1, None, 'yes'):
            with pytest.raises(GrafxConfigurationError):
                capture_copy(reader, tables=('R',), record_ids=ids, include_endpoints=flag)
        with pytest.raises(GrafxConfigurationError):
            capture_copy(reader, tables=('R',), include_endpoints=True)
