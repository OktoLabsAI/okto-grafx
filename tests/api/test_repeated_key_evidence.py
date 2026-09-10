"""Operation counts for the conditional repeated-key layout milestone, not a speed gate."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.index.entry import IndexEntry


@pytest.mark.parametrize("rows", [60, 240])
def test_cold_warm_repeated_key_work_preserves_complete_answers(
    tmp_path, monkeypatch, rows
):
    with connect(tmp_path / "skew", page_size=512) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64, value STRING, PRIMARY KEY(id))")
            tx.executemany(
                "CREATE (:D {id:$id,value:'same'})", ({"id": i} for i in range(rows))
            )
        db.create_index("value", "D", ("value",), bucket_count=1)
        store = db._indexes.active_index("value")
        key = store.definition.key_for((0, "same"))
        calls = []
        native = IndexEntry.decode_if_matches.__func__

        def decode(cls, *args):
            calls.append(1)
            return native(cls, *args)

        monkeypatch.setattr(IndexEntry, "decode_if_matches", classmethod(decode))
        cold = store.candidates(key)
        cold_decodes = len(calls)
        calls.clear()
        warm = store.candidates(key)
        warm_decodes = len(calls)
        assert warm == cold and len(warm) == rows
        assert cold_decodes >= rows and not calls
        distribution = db.index_distribution("value")
        assert distribution.dominant_key_fraction == 1.0
        assert distribution.recommendation == "inspect_key_skew"
        assert db.rehash_index_if_needed("value", check_skew=True) is None
        assert db.execute("MATCH (d:D) WHERE d.value='same' RETURN count(*)").rows == (
            (rows,),
        )
        print(
            {
                "rows": rows,
                "cold_decodes": cold_decodes,
                "warm_decodes": warm_decodes,
                "bucket_pages": distribution.pages,
                "candidates": len(warm),
            }
        )
