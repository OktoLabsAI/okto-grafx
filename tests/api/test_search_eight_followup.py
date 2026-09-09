"""Finite eight-item continuation: discriminating tests and semantic oracles."""

import pytest

from okto_grafx import TextIndexOptions, connect
from okto_grafx.domain.index.fulltext import query_term_frequencies


def test_frequencies_single_pass_and_no_unrequested_retention():
    class Tokens(tuple):
        visits = 0

        def __iter__(self):
            for token in super().__iter__():
                self.visits += 1
                yield token

        def count(self, value):
            raise AssertionError("Repeated document scan")

    tokens = Tokens(("wal", "graph", "wal", "other") * 1000)
    assert query_term_frequencies((tokens, ()), ("wal", "graph", "missing")) == (
        {"wal": 2000, "graph": 1000, "missing": 0},
        {"wal": 0, "graph": 0, "missing": 0},
    )
    assert tokens.visits == len(tokens)


@pytest.mark.parametrize("query", ["graph wal", "GRAPH graph wal missing", "wal", "absent"])
def test_bm25_bit_exact_against_repeated_count_oracle(tmp_path, monkeypatch, query):
    import okto_grafx.engine.fulltext as engine

    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64, a STRING, b STRING, PRIMARY KEY(id))")
            for i, (a, b) in enumerate([
                ("graph wal graph " * 200, "wal graph"),
                ("wal", None), ("", "graph graph"), ("other", "other"),
            ]):
                tx.execute("CREATE (:D {id:$i,a:$a,b:$b})", {"i": i, "a": a, "b": b})
        db.create_text_index("text", "D", ("a", "b"),
                             options=TextIndexOptions(field_weights=(2.0, 0.5)))
        with db.begin("read") as old:
            before = db.search_text(old, index="text", query=query)
            with db.begin() as tx:
                tx.execute("MATCH (d:D {id:1}) SET d.a='graph wal wal'")
            current = db.search_text(index="text", query=query)
            monkeypatch.setattr(engine, "query_term_frequencies", lambda fields, terms:
                                tuple({term: tokens.count(term) for term in terms}
                                      for tokens in fields))
            assert db.search_text(old, index="text", query=query).hits == before.hits
            assert db.search_text(index="text", query=query).hits == current.hits
        assert not db.verify("all").findings
