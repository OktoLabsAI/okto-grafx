"""Exact analyzed phrase positions: repeats/order/fields, snapshots and bounded verification."""

import pytest

from okto_grafx import connect, TextIndexOptions, TextSearchLimits, CancellationToken
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxUnsupportedOperation,
    GrafxQueryBudgetExceeded,
    GrafxQueryCancelled,
)
from okto_grafx.engine.fulltext import _phrase_failure, _contains_phrase


@pytest.fixture
def corpus(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute(
                "CREATE NODE TABLE N(id INT64, title STRING, body STRING, PRIMARY KEY(id))"
            )
            tx.executemany(
                "CREATE (:N {id:$id,title:$title,body:$body})",
                [
                    {
                        "id": 1,
                        "title": "graph database",
                        "body": "graph graph database",
                    },
                    {"id": 2, "title": "database graph", "body": "graph fast database"},
                    {"id": 3, "title": "graph", "body": "database"},
                    {"id": 4, "title": "GRAPH, database!", "body": None},
                ],
            )
        db.create_text_index(
            "text",
            "N",
            ("title", "body"),
            options=TextIndexOptions(field_weights=(2.0, 1.0)),
        )
        yield db


def test_order_fields_repetition_and_normalization(corpus):
    result = corpus.search_text(index="text", query="graph database", phrase=True)
    assert result.regime == "phrase_verified"
    assert {h.record_id for h in result.hits} == {1, 4}
    assert next(h for h in result.hits if h.record_id == 1).matched_fields == (
        "body",
        "title",
    )
    assert [
        h.record_id
        for h in corpus.search_text(index="text", query="graph graph", phrase=True).hits
    ] == [1]
    assert [
        h.record_id
        for h in corpus.search_text(
            index="text", query="database graph", phrase=True
        ).hits
    ] == [2]
    assert not corpus.search_text(index="text", query=" ", phrase=True).hits
    assert len(corpus.search_text(index="text", query="graph database").hits) == 4


def test_snapshot_updates_rebuild_reopen(corpus):
    with corpus.begin("read") as reader:
        before = corpus.search_text(
            reader, index="text", query="graph database", phrase=True
        )
        with corpus.begin() as tx:
            tx.execute("MATCH (n:N) WHERE n.id=1 SET n.title='gone', n.body='gone'")
        assert (
            corpus.search_text(
                reader, index="text", query="graph database", phrase=True
            ).hits
            == before.hits
        )
    assert [
        h.record_id
        for h in corpus.search_text(
            index="text", query="graph database", phrase=True
        ).hits
    ] == [4]
    corpus.rebuild_index("text")
    corpus.checkpoint()
    with connect(corpus.path, read_only=True) as readonly:
        assert [
            h.record_id
            for h in readonly.search_text(
                index="text", query="graph database", phrase=True
            ).hits
        ] == [4]
        assert not readonly.verify("all").findings


def test_filters_controls_and_strict_options(corpus):
    assert not corpus.search_text(
        index="text",
        query="graph database",
        phrase=True,
        filter=RecordIdFilter.of([2, 3]),
    ).hits
    with pytest.raises(GrafxConfigurationError):
        corpus.search_text(index="text", query="graph", phrase=1)
    with pytest.raises(GrafxUnsupportedOperation):
        corpus.search_text(index="text", query="graph", phrase=True, prefix=True)
    with pytest.raises(GrafxQueryBudgetExceeded):
        corpus.search_text(
            index="text",
            query="graph database",
            phrase=True,
            limits=TextSearchLimits(max_candidates=1),
        )
    with pytest.raises(GrafxQueryBudgetExceeded):
        corpus.search_text(
            index="text",
            query="graph graph",
            phrase=True,
            limits=TextSearchLimits(max_query_tokens=1),
        )
    token = CancellationToken()
    token.cancel()
    with pytest.raises(GrafxQueryCancelled):
        corpus.search_text(index="text", query="graph", phrase=True, cancellation=token)


def test_kmp_matches_bounded_reference():
    from itertools import product

    for size in range(6):
        for tokens in product(("a", "b"), repeat=size):
            for length in range(1, 4):
                for pattern in product(("a", "b"), repeat=length):
                    expected = any(
                        tokens[i : i + length] == pattern for i in range(len(tokens))
                    )
                    assert (
                        _contains_phrase(tokens, pattern, _phrase_failure(pattern))
                        == expected
                    )


def test_relationship_phrase(corpus):
    with corpus.begin() as tx:
        tx.execute("CREATE REL TABLE R(FROM N TO N, body STRING)")
        tx.execute(
            "MATCH (a:N),(b:N) WHERE a.id=1 AND b.id=2 CREATE (a)-[:R {body:'graph database'}]->(b)"
        )
    corpus.create_text_index("edges", "R", ("body",))
    hits = corpus.search_text(index="edges", query="graph database", phrase=True).hits
    assert len(hits) == 1 and hits[0].matched_fields == ("body",)


@pytest.mark.parametrize(
    "analyzer", ["standard", "keyword", "whitespace", "code_identifier"]
)
def test_all_declared_analyzers(corpus, analyzer):
    corpus.create_text_index(
        "variant", "N", ("body",), options=TextIndexOptions(analyzer=analyzer)
    )
    result = corpus.search_text(
        index="variant", query="graph graph database", phrase=True
    )
    assert [hit.record_id for hit in result.hits] == [1]
    assert result.hits[0].matched_fields == ("body",)


def test_phrase_memory_admission(corpus):
    with pytest.raises(GrafxQueryBudgetExceeded):
        corpus.search_text(
            index="text",
            query="graph database",
            phrase=True,
            limits=TextSearchLimits(max_memory_bytes=1),
        )
