"""Explicit header reads are independent of cheap-view cache residency."""
from dataclasses import FrozenInstanceError

import pytest
from okto_grafx import connect
from okto_grafx.domain.errors import GrafxIndexError


def test_cold_header_status_is_immutable_and_does_not_publish(tmp_path):
    root = tmp_path / 'graph'
    with connect(root) as db:
        with db.begin('write') as tx:
            tx.execute('CREATE NODE TABLE Item (id STRING, name STRING, PRIMARY KEY(id))')
        db.create_index('by_name', 'Item', ('name',))
        with db.begin('write') as tx:
            tx.execute("CREATE (:Item {id:'1', name:'one'})")
        db.checkpoint()
    with connect(root) as db:
        filename = db.indexes.index('by_name').file
        db._pool.discard_clean_file(filename)
        assert db.indexes.index('by_name').built_through_lsn is None
        before = db.transactions.published_lsn()
        status = db.read_index_status('BY_NAME')
        assert type(status.built_through_lsn) is int
        assert 0 <= status.built_through_lsn <= before
        assert status.name == 'by_name' and not status.stale
        assert db.transactions.published_lsn() == before
        with pytest.raises(FrozenInstanceError):
            status.stale = True
        with pytest.raises(GrafxIndexError):
            db.read_index_status('missing')
