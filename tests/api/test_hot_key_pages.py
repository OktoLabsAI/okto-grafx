"""Hot-key reuse saves decoding, without caching visibility or chain membership."""

from okto_grafx import connect
from okto_grafx.domain.index.entry import IndexEntry


def test_repeated_overflow_key_skips_decode_but_keeps_foreign_versions(tmp_path, monkeypatch):
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,v STRING,PRIMARY KEY(id))")
            for i in range(60):
                tx.execute("CREATE (:D {id:$id,v:'same'})", {"id": i})
        db.create_index("v", "D", ("v",), bucket_count=1)
        store = db._indexes.active_index("v")
        key = store.definition.key_for((0, "same"))
        calls = []
        original = IndexEntry.decode_if_matches.__func__

        def decode(cls, *args):
            calls.append(1)
            return original(cls, *args)

        monkeypatch.setattr(IndexEntry, "decode_if_matches", classmethod(decode))
        first = store.candidates(key)
        assert len(first) == 60 and len(calls) >= 60
        calls.clear()
        assert store.candidates(key) == first
        assert calls == []
        with db.begin("read") as old:
            with connect(path, page_size=512) as writer, writer.begin() as tx:
                tx.execute("CREATE (:D {id:99,v:'same'})")
            assert len(old.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 60
            assert len(db.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 61
        assert store._key_page_memo.bytes <= 1024 * 1024
        assert not db.verify("all").findings


def test_memo_changed_bytes_and_ref_do_not_reuse_old_matches():
    from okto_grafx.engine.key_page_memo import KeyPageMemo
    from okto_grafx.domain.page import Page, PageType
    from okto_grafx.domain.ids import RecordRef

    memo = KeyPageMemo()
    page = Page(int(PageType.INDEX_HASH), page_size=512, page_index=1)
    a = IndexEntry(key=b"x", ref=RecordRef(2, 0), versioned=False)
    b = IndexEntry(key=b"x", ref=RecordRef(3, 0), versioned=False)
    page.insert_slot(a.encode())
    assert memo.matches(page, b"x", None)[0].ref == a.ref
    page.update_slot(0, b.encode())
    assert memo.matches(page, b"x", None)[0].ref == b.ref
    assert memo.matches(page, b"x", a.ref) == ()
