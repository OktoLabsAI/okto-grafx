"""Bounded ordered proximity agrees with a small exhaustive independent oracle."""

from itertools import product, combinations
import pytest
from okto_grafx import connect, TextIndexOptions, TextSearchLimits
from okto_grafx.engine.fulltext_positions import phrase_fields
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded, GrafxUnsupportedOperation
from tests.api.test_fts_position_results import write


def test_proximity_against_exhaustive_oracle():
    for length in range(1, 7):
        for tokens in product(('a', 'b'), repeat=length):
            postings = {term: (tuple(i for i, token in enumerate(tokens) if token == term),) for term in ('a', 'b')}
            for terms in (('a',), ('a', 'a'), ('a', 'b'), ('b', 'a', 'b')):
                for slop in range(4):
                    expected = any(tuple(tokens[i] for i in selected) == terms and selected[-1] - selected[0] - len(terms) + 1 <= slop
                                   for selected in combinations(range(length), len(terms)))
                    assert phrase_fields(postings, terms, 1, slop=slop) == (expected,)


def test_proximity_public_boundaries(tmp_path):
    with connect(tmp_path / 'db') as db:
        write(db, 'CREATE NODE TABLE N(id INT64, title STRING, body STRING, PRIMARY KEY(id))')
        for i, title, body in ((1,'graph fast database',''), (2,'database graph',''), (3,'graph','database'), (4,'graph graph','')):
            with db.begin() as tx:
                tx.execute('CREATE (:N {id:$id,title:$t,body:$b})', {'id':i,'t':title,'b':body})
        for name, enabled in (('pos', True), ('plain', False)):
            db.create_text_index(name, 'N', ('title','body'), options=TextIndexOptions(positions=enabled, field_weights=(1.,1.)), bucket_count=4)
        args = dict(index='pos', query='graph database', phrase=True)
        assert not db.search_text(**args).hits
        result = db.search_text(**args, slop=1, return_positions=True)
        assert [hit.record_id for hit in result.hits] == [1]
        assert result.regime == 'proximity_positions'
        assert result.hits[0].positions
        assert [h.record_id for h in db.search_text(index='pos',query='graph graph',phrase=True,slop=1).hits] == [4]
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.search_text(**args, slop=1, limits=TextSearchLimits(max_proximity_work=1))
        for slop in (-1, True, 65536, '1'):
            with pytest.raises(GrafxConfigurationError):
                db.search_text(**args, slop=slop)
        with pytest.raises(GrafxConfigurationError):
            db.search_text(index='pos',query='graph',slop=1)
        with pytest.raises(GrafxUnsupportedOperation):
            db.search_text(index='plain',query='graph',phrase=True,slop=1)
        assert db.verify().clean
