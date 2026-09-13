"""Pure decode memo: unchanged bytes reuse work; same-LSN mutations never reuse truth."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxCorruptionDetected
from tests.api.test_posting_hash import write


def test_decode_memo_reuses_only_complete_equal_content(tmp_path, monkeypatch):
    from okto_grafx.engine.posting_hash import PostingHashIndex
    with connect(tmp_path / "db", page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="posting_hash", bucket_count=1)
        write(db, "CREATE (:D {id:1,v:'same'})")
        store = db._indexes.active_index("v")
        with db._pool.pinned(store.file, 1) as page:
            clone = type(page).from_bytes(page.to_bytes())
        store._decode_memo.clear()
        store._decode_memo_bytes = 0
        calls = []
        original = PostingHashIndex._decode_page_uncached
        def count(self, page):
            calls.append(1)
            return original(self, page)
        monkeypatch.setattr(PostingHashIndex, "_decode_page_uncached", count)
        first = store._decode_page(clone)
        assert store._decode_page(clone) == first
        assert len(calls) == 1
        same = type(clone).from_bytes(clone.to_bytes())
        assert store._decode_page(same) == first and len(calls) == 1
        with pytest.raises(TypeError):
            first[0][0] = b"forged"
        prior_lsn = clone.page_lsn
        clone.free_slot(0)
        assert clone.page_lsn == prior_lsn
        for _ in range(2):
            with pytest.raises(GrafxCorruptionDetected):
                store._decode_page(clone)
        assert len(calls) == 3  # failures never cached


def test_decode_memo_bounds_and_mutation_reuse(tmp_path, monkeypatch):
    import okto_grafx.engine.posting_hash as module
    monkeypatch.setattr(module, "_DECODE_MEMO_MAX_PAGES", 2)
    monkeypatch.setattr(module, "_DECODE_MEMO_MAX_BYTES", 4096)
    with connect(tmp_path / "db", page_size=512) as db:
        write(db, "CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
        db.create_index("v", "D", ("v",), layout="posting_hash", bucket_count=1)
        for number in range(5):
            write(db, "CREATE (:D {id:$id,v:'same'})", {"id": number})
        store = db._indexes.active_index("v")
        assert len(store._decode_memo) <= 2 and store._decode_memo_bytes <= 4096
        assert len(db.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 5
        write(db, "MATCH (d:D {id:1}) SET d.v='changed'")
        assert len(db.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 4
        db.maintenance.vacuum(confirm_quiescent=True)
        db.rebuild_index("v")
        assert len(db.execute("MATCH (d:D) WHERE d.v='same' RETURN d.id").rows) == 4
        assert db.verify().clean
