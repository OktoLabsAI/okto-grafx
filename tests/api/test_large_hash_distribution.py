"""Explicit large-directory capability and privacy-safe bounded skew diagnostics."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxQueryBudgetExceeded, GrafxSchemaVersionMismatch


def test_large_directory_rehash_old_snapshot_reopen_and_version_refusal(tmp_path, monkeypatch):
    import okto_grafx.domain.model.catalog as catalog_module

    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:D {id:1,v:'needle'})")
        db.create_index("v", "D", ("v",), bucket_count=64)
        with db.begin("read") as old:
            original = old.execute("MATCH (d:D) WHERE d.v='needle' RETURN d.id").rows
            db.rehash_index("v", bucket_count=8192)
            assert old.execute("MATCH (d:D) WHERE d.v='needle' RETURN d.id").rows == original
        assert "large_hash_directories_v1" in db._catalog.catalog.required_capabilities()
        raw = db._catalog.catalog.serialize()
        with monkeypatch.context() as scoped:
            scoped.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 7))
            with pytest.raises(GrafxSchemaVersionMismatch):
                catalog_module.Catalog.deserialize(raw)
        assert not db.verify("all").findings
    with connect(tmp_path / "db", page_size=512) as db:
        assert db.execute("MATCH (d:D) WHERE d.v='needle' RETURN d.id").rows == original
        distribution = db.index_distribution("v", max_pages=9000)
        assert distribution.bucket_count == 8192 and distribution.entries == 1


def test_skew_report_budget_and_assisted_growth_does_not_repeat_hot_key(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
            for i in range(40):
                tx.execute("CREATE (:D {id:$i,v:'same'})", {"i": i})
        db.create_index("v", "D", ("v",), bucket_count=1)
        before = db.index_distribution("v")
        assert before.overflow_pages > 0
        assert before.dominant_key_fraction == 1.0
        assert before.recommendation == "inspect_key_skew"
        assert "same" not in repr(before)
        assert db.rehash_index_if_needed("v", check_skew=True) is None
        assert db.index_distribution("v") == before
        for limits in ({"max_pages": 1}, {"max_entries": 1}, {"max_memory_bytes": 1}):
            with pytest.raises(GrafxQueryBudgetExceeded):
                db.index_distribution("v", **limits)
        assert not db.verify("all").findings
