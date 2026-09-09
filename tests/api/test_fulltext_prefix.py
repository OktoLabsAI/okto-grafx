"""Prefix postings retain exact-term BM25, snapshots and fail-closed bounds."""

import pytest

from okto_grafx import connect, TextIndexOptions, TextSearchLimits
from okto_grafx.errors import GrafxQueryBudgetExceeded, GrafxUnsupportedOperation, GrafxSchemaVersionMismatch


def seed(db, mode="durable"):
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE D(id INT64,body STRING,PRIMARY KEY(id))")
        for i, body in enumerate(("graph graphs", "graphite", "database")):
            tx.execute("CREATE (:D {id:$i,body:$body})", {"i": i, "body": body})
    db.create_text_index("fts", "D", ("body",), options=TextIndexOptions(
        prefix_max_characters=8, statistics_mode=mode, statistics_history_entries=2 if mode == "durable" else 0))


@pytest.mark.parametrize("mode", ["wal", "durable"])
def test_prefix_scores_equal_expanded_exact_oracle_and_reopen(tmp_path, mode):
    path = tmp_path / "db"
    with connect(path) as db:
        seed(db, mode)
        result = db.search_text(index="fts", query="GRAP", prefix=True)
        oracle = db.search_text(index="fts", query="graph graphs graphite")
        assert result.hits == oracle.hits and len(result.hits) == 2
        assert result.regime == "prefix_index"
        assert not db.search_text(index="fts", query="missing", prefix=True).hits
        assert not db.verify().findings
    with connect(path) as db:
        assert db.search_text(index="fts", query="grap", prefix=True).hits == result.hits


def test_prefix_old_reader_update_rollback_rebuild_and_bounds(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        seed(db)
        with db.begin("read") as old:
            before = db.search_text(old, index="fts", query="grap", prefix=True).hits
            with connect(path) as writer, writer.begin() as tx:
                tx.execute("MATCH (d:D {id:1}) SET d.body='other'")
            assert db.search_text(old, index="fts", query="grap", prefix=True).hits == before
            assert len(db.search_text(index="fts", query="grap", prefix=True).hits) == 1
        tx = db.begin()
        tx.execute("CREATE (:D {id:99,body:'grapefruit'})")
        tx.rollback()
        db.rebuild_index("fts")
        assert len(db.search_text(index="fts", query="grap", prefix=True).hits) == 1
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.search_text(index="fts", query="grap", prefix=True, limits=TextSearchLimits(max_expanded_terms=1))
        with pytest.raises(GrafxUnsupportedOperation):
            db.search_text(index="fts", query="toolongprefix", prefix=True)
        assert not db.verify().findings


def test_prefix_capability_old_reader_refusal(tmp_path, monkeypatch):
    import okto_grafx.domain.model.catalog as catalog
    with connect(tmp_path / "db") as db:
        seed(db)
        raw = db._catalog.catalog.serialize()
        assert "fulltext_prefixes_v1" in db._catalog.catalog.required_capabilities()
        monkeypatch.setattr(catalog, "_KNOWN_CAPABILITY_BITS", catalog._KNOWN_CAPABILITY_BITS & ~(1 << 11))
        with pytest.raises(GrafxSchemaVersionMismatch):
            catalog.Catalog.deserialize(raw)


@pytest.mark.parametrize("value", [True, -1, 33, 1.5])
def test_invalid_prefix_bound(value):
    from okto_grafx.errors import GrafxConfigurationError
    with pytest.raises(GrafxConfigurationError):
        TextIndexOptions(prefix_max_characters=value)


def test_prefix_document_hard_bound_and_canonical_metadata():
    from okto_grafx.domain.index.fulltext import keys_from_fields, decode_options
    from okto_grafx.errors import GrafxIndexError
    fields = (tuple(f"{i:04d}" + "x" * 28 for i in range(2500)),)
    with pytest.raises(GrafxQueryBudgetExceeded) as failure:
        keys_from_fields(fields, prefix_max_characters=32)
    assert failure.value.details["resource"] == "text_prefix_postings"
    options = TextIndexOptions(prefix_max_characters=8, statistics_mode="durable", statistics_history_entries=2)
    encoded = options.derivation()
    assert decode_options(encoded) == options
    for malformed in (encoded[:-2], encoded + "00", encoded[:-2] + "00", encoded[:-6] + "ff0208"):
        with pytest.raises(GrafxIndexError):
            decode_options(malformed)
