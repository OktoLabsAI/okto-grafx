"""RF11 exact source and opt-in compatibility contracts; no timing assertions."""

import math

from okto_grafx import TextIndexOptions, connect
from okto_grafx.domain.index.fulltext import FULLTEXT_STATISTICS_CAPABILITY


def _seed(path, *, options):
    with connect(path, codec="pure", vector_math="pure") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE D(id INT64, body STRING, PRIMARY KEY(id))")
            tx.execute("UNWIND $ids AS id CREATE (:D {id:id,body:'common'})",
                       {"ids": [1, 2, 3, 4]})
        db.create_text_index("text", "D", ("body",), options=options)
        required = db._catalog.catalog.required_capabilities()
        db.checkpoint()
    return required


def _read_sources(path):
    with connect(path, read_only=True, codec="pure", vector_math="pure") as db:
        with db.begin("read") as reader:
            cold = db.search_text(reader, index="text", query="common", k=10)
            warm = db.search_text(reader, index="text", query="common", k=10)
        assert not db.verify("all").findings
    with connect(path, read_only=True, codec="pure", vector_math="pure") as reopened:
        again = reopened.search_text(index="text", query="common", k=10)
    return cold, warm, again


def test_default_wal_and_optin_durable_keep_exact_ranking_and_capability_boundary(tmp_path):
    defaults = TextIndexOptions()
    assert (defaults.statistics_mode, defaults.statistics_history_entries) == ("wal", 0)
    wal_path, durable_path = tmp_path / "wal", tmp_path / "durable"
    assert FULLTEXT_STATISTICS_CAPABILITY not in _seed(wal_path, options=defaults)
    assert FULLTEXT_STATISTICS_CAPABILITY in _seed(
        durable_path, options=TextIndexOptions(statistics_mode="durable"),
    )

    wal_cold, wal_warm, wal_reopened = _read_sources(wal_path)
    durable_cold, durable_warm, durable_reopened = _read_sources(durable_path)
    assert (wal_cold.statistics_regime, wal_warm.statistics_regime,
            wal_reopened.statistics_regime) == (
                "full_census", "snapshot_cache", "full_census")
    assert (durable_cold.statistics_regime, durable_warm.statistics_regime,
            durable_reopened.statistics_regime) == (
                "durable_summary", "snapshot_cache", "durable_summary")

    expected_ids = (1, 2, 3, 4)
    expected_score = math.log1p(.5 / 4.5)
    for result in (wal_cold, wal_warm, wal_reopened,
                   durable_cold, durable_warm, durable_reopened):
        assert result.corpus_documents == 4
        assert tuple(hit.record_id for hit in result.hits) == expected_ids
        assert all(math.isclose(hit.score, expected_score, rel_tol=1e-12)
                   for hit in result.hits)
        assert result.postings_visited >= 4
    assert wal_cold.hits == durable_cold.hits
    assert wal_reopened.hits == durable_reopened.hits
