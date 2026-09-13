"""Consumer-owned bounded positional evidence, never offsets inferred from normalized text."""

from dataclasses import FrozenInstanceError
import pytest
from okto_grafx import connect, TextIndexOptions, TextSearchLimits, TextMatchPositions
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded, GrafxUnsupportedOperation


def write(db, query):
    with db.begin('write') as tx:
        tx.execute(query)


def test_positions_are_explicit_bounded_snapshot_values(tmp_path):
    with connect(tmp_path / 'db') as db:
        write(db, 'CREATE NODE TABLE N(id INT64, body STRING, PRIMARY KEY(id))')
        write(db, "CREATE (:N {id:1, body:'GRAPH other graph database'})")
        db.create_text_index('text', 'N', ('body',), options=TextIndexOptions(positions=True), bucket_count=4)
        db.create_text_index('plain', 'N', ('body',), bucket_count=4)
        assert not db.search_text(index='text', query='graph').hits[0].positions
        with db.begin('read') as reader:
            hit = db.search_text(reader, index='text', query='graph', return_positions=True).hits[0]
            assert hit.positions == (TextMatchPositions('body', 'graph', (0, 2)),)
            with pytest.raises(FrozenInstanceError):
                hit.positions[0].term = 'changed'
            write(db, "MATCH (n:N {id:1}) SET n.body='different'")
            assert db.search_text(reader, index='text', query='graph', return_positions=True).hits[0] == hit
        assert not db.search_text(index='text', query='graph', return_positions=True).hits
        with pytest.raises(GrafxUnsupportedOperation):
            db.search_text(index='plain', query='graph', return_positions=True)
        with pytest.raises(GrafxConfigurationError):
            db.search_text(index='text', query='graph', return_positions=1)
        write(db, "MATCH (n:N {id:1}) SET n.body='graph graph'")
        for limits in (TextSearchLimits(max_position_results=1), TextSearchLimits(max_explanation_bytes=1)):
            with pytest.raises(GrafxQueryBudgetExceeded):
                db.search_text(index='text', query='graph', return_positions=True, limits=limits)
        db.checkpoint()
    with connect(tmp_path / 'db', read_only=True) as db:
        assert db.search_text(index='text', query='graph', return_positions=True).hits[0].positions[0].positions == (0, 1)
