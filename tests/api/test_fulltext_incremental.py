"""Incremental statistics agree with an independent full snapshot census."""

from okto_grafx import connect, TextSearchLimits


def seed(path):
    db = connect(path)
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE P(id INT64, body STRING, PRIMARY KEY(id))")
        for i in range(30):
            tx.execute(
                "CREATE (:P {id:$id,body:$body})", {"id": i, "body": f"common term{i}"}
            )
    db.create_text_index("text", "P", ("body",), bucket_count=64)
    return db


def test_foreign_writer_delta_snapshot_and_census_equivalence(tmp_path):
    with seed(tmp_path / "db") as db, db.begin("read") as old:
        before = db.search_text(old, index="text", query="term1")
        with connect(tmp_path / "db") as writer, writer.begin() as tx:
            tx.execute("MATCH (p:P {id:1}) SET p.body='common term1 longer'")
            tx.execute("MATCH (p:P {id:2}) DELETE p")
            tx.execute("CREATE (:P {id:99,body:'term1 extra'})")
        delta = db.search_text(index="text", query="term1")
        assert delta.statistics_regime == "wal_delta"
        assert delta.statistics_wal_records > 0 and delta.corpus_documents == 30
        assert db.search_text(old, index="text", query="term1").hits == before.hits
        db._text_stats_cache.clear()
        census = db.search_text(index="text", query="term1")
        assert census.statistics_regime == "full_census"
        assert (
            delta.hits == census.hits
            and delta.corpus_documents == census.corpus_documents
        )
        assert delta.postings_visited < census.postings_visited


def test_budget_fallback_and_rollback(tmp_path):
    with seed(tmp_path / "db") as db:
        before = db.search_text(index="text", query="term1")
        tx = db.begin()
        tx.execute("MATCH (p:P {id:1}) SET p.body='aborted'")
        tx.rollback()
        assert db.search_text(index="text", query="term1").hits == before.hits
        with db.begin() as tx:
            tx.execute("MATCH (p:P {id:1}) SET p.body='term1 changed'")
        result = db.search_text(
            index="text",
            query="term1",
            limits=TextSearchLimits(max_statistics_wal_records=1),
        )
        assert result.statistics_regime == "full_census"


def test_operation_memo_bounds_and_commit_release(tmp_path, monkeypatch):
    import okto_grafx.domain.index.fulltext as text

    calls = []
    original = text.analyze

    def counted(value, options):
        calls.append(value)
        return original(value, options)

    with seed(tmp_path / "db") as db:
        monkeypatch.setattr(text, "analyze", counted)
        with db.begin() as tx:
            tx.execute("CREATE (:P {id:99,body:'unique payload here'})")
        assert calls.count("unique payload here") == 1
        assert tx._context._text_analysis_memo is None
        calls.clear()
        assert not db.verify("all").findings
        assert calls.count("unique payload here") <= 2
    memo = text.TextAnalysisMemo(1)
    assert memo.fields(("hello",), (0,), text.TextIndexOptions()) == (("hello",),)
    assert memo.retained_bytes == 0


def test_multi_commit_generation_and_recycled_proof_fallback(tmp_path, monkeypatch):
    from okto_grafx.engine.wal_manager import WalManager

    with seed(tmp_path / "db") as db:
        db.search_text(index="text", query="term1")
        for i in range(3):
            with db.begin() as tx:
                tx.execute(
                    "CREATE (:P {id:$id,body:$body})",
                    {"id": 100 + i, "body": "term1 extra"},
                )
        delta = db.search_text(index="text", query="term1")
        assert delta.statistics_regime == "wal_delta" and delta.corpus_documents == 33
        db._text_stats_cache.clear()
        assert db.search_text(index="text", query="term1").hits == delta.hits
        with db.begin() as tx:
            tx.execute("MATCH (p:P {id:100}) DELETE p")
        with monkeypatch.context() as scoped:
            scoped.setattr(WalManager, "read_bounded", lambda *a, **k: None)
            assert (
                db.search_text(index="text", query="term1").statistics_regime
                == "full_census"
            )
        db.rebuild_index("text")
        assert (
            db.search_text(index="text", query="term1").statistics_regime
            == "full_census"
        )


def test_multifield_null_empty_lengths_and_all_deleted(tmp_path):
    from okto_grafx import TextIndexOptions

    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute(
                "CREATE NODE TABLE P(id INT64,a STRING,b STRING,PRIMARY KEY(id))"
            )
            tx.execute("CREATE (:P {id:1,a:'wal wal',b:null})")
            tx.execute("CREATE (:P {id:2,a:'',b:'wal recovery'})")
        db.create_text_index(
            "text", "P", ("a", "b"), options=TextIndexOptions(field_weights=(3.0, 1.0))
        )
        db.search_text(index="text", query="wal")
        with db.begin() as tx:
            tx.execute("MATCH (p:P {id:1}) SET p.a=null,p.b='wal'")
            tx.execute("MATCH (p:P {id:2}) SET p.a='wal new',p.b='' ")
        delta = db.search_text(index="text", query="wal")
        assert delta.statistics_regime == "wal_delta"
        db._text_stats_cache.clear()
        assert db.search_text(index="text", query="wal").hits == delta.hits
        with db.begin() as tx:
            tx.execute("MATCH (p:P) DELETE p")
        empty = db.search_text(index="text", query="wal")
        assert (
            empty.statistics_regime == "wal_delta"
            and empty.corpus_documents == 0
            and not empty.hits
        )
        db._text_stats_cache.clear()
        assert db.search_text(index="text", query="wal").hits == empty.hits
