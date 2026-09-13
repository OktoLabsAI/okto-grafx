"""Native positional postings, field boundaries, snapshot updates and canonical chunks."""

import pytest

from okto_grafx import TextIndexOptions, TextSearchLimits, connect
from okto_grafx.domain.index.fulltext import decode_options
from okto_grafx.domain.index.text_positions import decode_position, position_keys
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded


@pytest.mark.parametrize("statistics,history,prefix", [("wal", 0, 0), ("durable", 0, 4), ("durable", 2, 0)])
def test_options_roundtrip_and_old_derivations_unchanged(statistics, history, prefix):
    options = TextIndexOptions(positions=True, statistics_mode=statistics,
                               statistics_history_entries=history, prefix_max_characters=prefix)
    assert options.derivation().startswith("fulltext_v5_")
    assert decode_options(options.derivation()) == options
    assert not decode_options(TextIndexOptions().derivation()).positions
    with pytest.raises(GrafxConfigurationError):
        TextIndexOptions(positions=1)


def test_position_chunks_repeat_order_and_fields():
    keys = tuple(position_keys((("a",) * 65, ("b", "a"))))
    parsed = [decode_position(key) for key in keys]
    assert [len(p[-1]) for p in parsed if p[0] == "a" and p[1] == 0] == [32, 32, 1]
    assert parsed[-1] == ("b", 1, 0, 2, (0,))


@pytest.mark.parametrize("damage", ["missing", "duplicate", "wrong_position"])
def test_missing_positional_chunk_refuses_instead_of_false_negative(tmp_path, damage):
    from okto_grafx.errors import GrafxError
    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, body STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1,body:'graph database'})")
        db.create_text_index("text", "N", ("body",), bucket_count=1, options=TextIndexOptions(positions=True))
        store = db._indexes.active_index("text")
        with db._transactions.page_access_section():
            entries = [entry for number in store._bucket_pages(0) for entry in store._entries_on(number)]
            damaged = next(entry for entry in entries if entry.key[:1] == b"\x03")
            if damage == "missing":
                store._erase(damaged.page, damaged.slot, db._transactions.published_state().last_committed_lsn)
            else:
                from dataclasses import replace
                with db._pool.pinned(store.file, damaged.page) as page:
                    if damage == "duplicate":
                        page.insert_slot(damaged.encode())
                    else:
                        raw = damaged.key[:-2] + b"\x01\x00"
                        # Select graph at position zero, so one becomes a forged position.
                        if decode_position(damaged.key)[0] == "database":
                            raw = damaged.key[:-2] + b"\x00\x00"
                        page.update_slot(damaged.slot, replace(damaged, key=raw).encode())
            db._pool.flush(store.file)
        with pytest.raises(GrafxError):
            db.search_text(index="text", query="graph database", phrase=True)
        assert not db.verify().clean


def test_positional_phrase_native_update_reopen_rebuild(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, title STRING, body STRING, PRIMARY KEY(id))")
            tx.executemany("CREATE (:N {id:$id,title:$t,body:$b})", [
                {"id": 1, "t": "graph database", "b": "graph " * 70 + "database"},
                {"id": 2, "t": "database graph", "b": "graph fast database"},
                {"id": 3, "t": "graph", "b": "database"}])
        db.create_text_index("positions", "N", ("title", "body"), bucket_count=8,
            options=TextIndexOptions(positions=True, statistics_mode="durable", field_weights=(2., 1.)))
        db.create_text_index("plain", "N", ("title", "body"), bucket_count=8,
            options=TextIndexOptions(statistics_mode="durable", field_weights=(2., 1.)))
        assert "fulltext_positions_v1" in db._catalog.catalog.required_capabilities()
        for query in ("graph database", "database graph", "graph graph", "graph", "missing"):
            pos = db.search_text(index="positions", query=query, phrase=True)
            plain = db.search_text(index="plain", query=query, phrase=True)
            assert pos.hits == plain.hits
            assert pos.regime == "phrase_positions"
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.search_text(index="positions", query="graph", phrase=True, limits=TextSearchLimits(max_postings=1))
        with db.begin("read") as reader:
            before = db.search_text(reader, index="positions", query="graph database", phrase=True)
            with db.begin() as tx:
                tx.execute("MATCH (n:N {id:1}) SET n.title='gone', n.body='gone'")
            assert db.search_text(reader, index="positions", query="graph database", phrase=True).hits == before.hits
        assert not db.search_text(index="positions", query="graph database", phrase=True).hits
        assert db.verify().clean
        db.rebuild_index("positions")
        db.checkpoint()
    with connect(root, page_size=512, read_only=True) as db:
        assert not db.search_text(index="positions", query="graph database", phrase=True).hits
        assert db.search_text(index="positions", query="database graph", phrase=True).hits[0].record_id == 2
        assert db.verify().clean
