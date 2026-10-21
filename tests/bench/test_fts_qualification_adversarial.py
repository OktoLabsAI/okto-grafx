"""RF11 independent score/corpus oracles under foreign commits and corruption."""
from __future__ import annotations

import math

import pytest

from bench.harness.campaign_fixtures import Scenario
from bench.harness.fts_qualification import build_fixture
from okto_grafx import connect
from okto_grafx.errors import GrafxCorruptionDetected


def input_corpus():
    # Record identities are allocated from one by this fresh fixture. Expected
    # documents come from the fixture specification, never a database census.
    return {i + 1: ("common rare" if i == 0 else "common usual").split()
            for i in range(256)}


def ranked(corpus, term):
    documents = len(corpus)
    frequency = sum(term in tokens for tokens in corpus.values())
    average = sum(map(len, corpus.values())) / documents
    inverse_frequency = math.log1p((documents - frequency + .5) / (frequency + .5))
    scores = []
    for identity, tokens in corpus.items():
        count = tokens.count(term)
        if count:
            score = (inverse_frequency * count * (1.2 + 1)
                     / (count + 1.2 * (1 - .75 + .75 * len(tokens) / average)))
            scores.append((identity, score))
    return tuple(sorted(scores, key=lambda item: (-item[1], item[0])))


def assert_corpus(result, corpus, term):
    assert result.corpus_documents == len(corpus)
    actual = tuple((hit.record_id, hit.score) for hit in result.hits)
    expected = ranked(corpus, term)
    assert tuple(row[0] for row in actual) == tuple(row[0] for row in expected)
    assert tuple(row[1] for row in actual) == pytest.approx(
        tuple(row[1] for row in expected), rel=1e-14, abs=1e-15)


@pytest.mark.parametrize("history,regime", [(0, "full_census"), (1, "full_census"),
                                           (8, "durable_summary"), (32, "durable_summary")])
def test_external_writer_keeps_retained_scores_and_corpus(tmp_path, history, regime):
    path = tmp_path / "db"
    scenario = Scenario("foreign-writer", "fts_matrix", nodes=256, edges_per_node=0,
                        fts_statistics_mode="durable", fts_history=history)
    before = input_corpus()
    after = {identity: list(tokens) for identity, tokens in before.items()}
    after[2] = "common common usual extra".split()
    del after[3]
    after[257] = "common extra".split()
    del after[4]
    with connect(path) as db:
        build_fixture(db, scenario)
        with db.begin("read") as reader:
            # Pin a read snapshot before any external commit, without warming
            # the FTS statistics cache that could conceal history selection.
            assert reader.execute("MATCH (n:N) RETURN count(n)").rows == ((256,),)
            with connect(path) as writer:
                for query in (
                    "MATCH (n:N {id:1}) SET n.body='common common usual extra'",
                    "MATCH (n:N {id:2}) DELETE n",
                    "CREATE (:N {id:256,body:'common extra'})",
                    "MATCH (n:N {id:3}) DELETE n",
                ):
                    with writer.begin("write") as tx:
                        tx.execute(query)
            for term in ("common", "rare"):
                db._text_stats_cache.clear()
                retained = db.search_text(reader, index="campaign_text", query=term, k=256)
                assert retained.statistics_regime == regime
                assert_corpus(retained, before, term)
                db._text_stats_cache.clear()
                current = db.search_text(index="campaign_text", query=term, k=256)
                assert_corpus(current, after, term)
                assert current.corpus_documents == 255
                assert tuple(hit.score for hit in current.hits) != tuple(hit.score for hit in retained.hits)
        assert not db.verify("all").findings
    with connect(path) as reopened:
        assert_corpus(reopened.search_text(index="campaign_text", query="common", k=256),
                      after, "common")


@pytest.mark.parametrize("history", [0, 8])
def test_invalid_persisted_summary_refuses_instead_of_census(tmp_path, history):
    import okto_grafx.engine.fulltext_durable as durable

    scenario = Scenario("damaged-summary", "fts_matrix", nodes=256, edges_per_node=0,
                        fts_statistics_mode="durable", fts_history=history)
    with connect(tmp_path / "db") as db:
        build_fixture(db, scenario)
        assert_corpus(db.search_text(index="campaign_text", query="common", k=256),
                      input_corpus(), "common")
        store = db._indexes.index("campaign_text")
        with store._pool.pinned(store.file, 0) as page:
            original = bytes(page.read_slot(2))
            damaged = bytearray(original)
            offset = 16 if history else 0
            fields = list(durable._FORMAT.unpack(damaged[offset:offset + 64]))
            fields[2] = 2**63  # Impossible committed coverage, valid page encoding.
            damaged[offset:offset + 64] = durable._FORMAT.pack(*fields)
            page.update_slot(2, damaged)
        store._pool.flush(store.file)
        try:
            db._text_stats_cache.clear()
            with pytest.raises(GrafxCorruptionDetected):
                db.search_text(index="campaign_text", query="common", k=256)
            assert any("text statistics" in finding.detail for finding in db.verify("all").findings)
        finally:
            with store._pool.pinned(store.file, 0) as page:
                page.update_slot(2, original)
            store._pool.flush(store.file)
            db._text_stats_cache.clear()
        assert_corpus(db.search_text(index="campaign_text", query="common", k=256),
                      input_corpus(), "common")
        assert not db.verify("all").findings
