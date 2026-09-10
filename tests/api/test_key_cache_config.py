"""Per-index memo configuration never changes visibility or query answers."""

from dataclasses import FrozenInstanceError

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxConfigurationError


@pytest.mark.parametrize("limit", [0, 1, 64])
def test_limits_metrics_and_reopen(tmp_path, limit):
    path = tmp_path / "db"
    with connect(path, index_key_cache_pages=limit, index_key_cache_bytes=4096) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,v STRING,PRIMARY KEY(id))")
            tx.execute("CREATE (:D {id:1,v:'value'})")
        db.create_index("v", "D", ("v",), layout="sparse_hash", bucket_count=128)
        for _ in range(2):
            assert db.execute("MATCH (d:D) WHERE d.v='value' RETURN d.id").rows == ((1,),)
        usage = db.index_cache_usage("v")
        assert usage.max_pages == limit
        assert usage.pages <= limit and usage.logical_bytes <= 4096
        assert usage.misses
        if limit == 0:
            assert usage.pages == usage.hits == 0
            assert usage.admission_refusals
        else:
            assert usage.hits
        with pytest.raises(FrozenInstanceError):
            usage.hits = 0
    with connect(path, index_key_cache_bytes=0) as db:
        assert db.execute("MATCH (d:D) WHERE d.v='value' RETURN d.id").rows == ((1,),)
        assert db.index_cache_usage("v").logical_bytes == 0


@pytest.mark.parametrize("field,value", [("index_key_cache_pages", -1),
    ("index_key_cache_pages", True), ("index_key_cache_pages", 65537),
    ("index_key_cache_bytes", -1), ("index_key_cache_bytes", 2**31 + 1)])
def test_invalid_cache_limit_precedes_store_creation(tmp_path, field, value):
    path = tmp_path / "db"
    with pytest.raises(GrafxConfigurationError):
        connect(path, **{field: value})
    assert not path.exists()
