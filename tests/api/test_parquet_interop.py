"""Local Parquet publication, strict schema, resource limits and import atomicity."""

from contextlib import closing
import pytest

from okto_grafx import connect, QueryResult, VectorValue
from okto_grafx.arrow import ArrowVectorType
from okto_grafx.parquet import write_parquet, read_parquet_batches, import_parquet
from okto_grafx.errors import GrafxError, GrafxUnsupportedOperation, GrafxQueryBudgetExceeded

pytestmark = pytest.mark.optional_dependency("pyarrow")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


def test_parquet_roundtrip_nullable_vectors_and_atomic_no_overwrite(tmp_path):
    kinds = ("INT64", ArrowVectorType(1, 2))
    result = QueryResult(columns=("id", "v"), rows=((1, VectorValue((1., 0.), 1)), (2, None)))
    report = write_parquet(result, "data.parquet", allowed_root=tmp_path, types=kinds, batch_rows=1)
    assert report.rows == 2 and report.batches == 2 and report.bytes > 0
    original = (tmp_path / "data.parquet").read_bytes()
    batches = tuple(read_parquet_batches("data.parquet", allowed_root=tmp_path, types=kinds, max_batch_rows=1))
    assert len(batches) == 2 and batches[1].column(1).null_count == 1
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE VECTOR SPACE emb {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE N(id INT64,v VECTOR(emb),PRIMARY KEY(id))")
        with db.begin() as tx:
            assert import_parquet(tx, "CREATE (:N {id:$id,v:$v})", "data.parquet", allowed_root=tmp_path, types=kinds).statements == 2
        assert db.execute("MATCH (n:N) RETURN n.id,n.v ORDER BY n.id").rows == result.rows
    with pytest.raises(GrafxUnsupportedOperation):
        write_parquet(result, "data.parquet", allowed_root=tmp_path, types=kinds)
    assert (tmp_path / "data.parquet").read_bytes() == original
    assert not list(tmp_path.glob(".grafx-parquet-*"))


@pytest.mark.parametrize("fault", ["rows", "bytes", "batch", "producer"])
def test_failed_export_never_publishes_partial_file(tmp_path, fault):
    rows = ((1,), ("bad",)) if fault == "producer" else ((1,), (2,))
    options = {"max_rows": 1} if fault == "rows" else {"max_file_bytes": 1} if fault == "bytes" else {"max_batches": 1} if fault == "batch" else {}
    with pytest.raises(GrafxError):
        write_parquet(QueryResult(columns=("id",), rows=rows), "fail.parquet", allowed_root=tmp_path,
                      types=("INT64",), batch_rows=1, **options)
    assert not list(tmp_path.iterdir())


def test_input_bounds_bad_types_paths_and_early_close(tmp_path):
    result = QueryResult(columns=("id",), rows=((1,), (2,)))
    write_parquet(result, "data.parquet", allowed_root=tmp_path, types=("INT64",), batch_rows=1)
    for options in ({"max_file_bytes": 1}, {"max_row_group_bytes": 1}, {"max_rows": 1}, {"max_batches": 1}, {"max_batch_bytes": 1}):
        with pytest.raises(GrafxQueryBudgetExceeded):
            tuple(read_parquet_batches("data.parquet", allowed_root=tmp_path, types=("INT64",), **options))
    with pytest.raises(GrafxUnsupportedOperation):
        tuple(read_parquet_batches("data.parquet", allowed_root=tmp_path, types=("STRING",)))
    for name in ("../escape.parquet", "https://example.test/data", "sub/data.parquet", "data:stream"):
        with pytest.raises(GrafxError):
            tuple(read_parquet_batches(name, allowed_root=tmp_path, types=("INT64",)))
    with closing(read_parquet_batches("data.parquet", allowed_root=tmp_path, types=("INT64",), max_batch_rows=1)) as batches:
        assert next(batches).num_rows == 1
    (tmp_path / "data.parquet").rename(tmp_path / "closed.parquet")


def test_late_invalid_parquet_value_rolls_back_import_not_prior_staging(tmp_path):
    table = pa.table({"id": pa.array([1, 99], type=pa.int64()), "f": pa.array([1., float("nan")], from_pandas=False)})
    pq.write_table(table, tmp_path / "bad.parquet", row_group_size=1)
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,f DOUBLE,PRIMARY KEY(id))")
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:99})")
            with pytest.raises(GrafxError):
                import_parquet(tx, "CREATE (:N {id:$id,f:$f})", "bad.parquet", allowed_root=tmp_path,
                               types=("INT64", "DOUBLE"), max_batch_rows=1)
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((99,),)


def test_empty_file_shape_and_unknown_vector_encoding(tmp_path):
    kinds = ("INT64", ArrowVectorType(1, 2))
    report = write_parquet(QueryResult(columns=("id", "v")), "empty.parquet", allowed_root=tmp_path, types=kinds)
    assert report.rows == report.batches == 0
    assert tuple(read_parquet_batches("empty.parquet", allowed_root=tmp_path, types=kinds)) == ()
    metadata = {b"grafx.type": b"VECTOR_F32", b"grafx.space_ref": b"1", b"grafx.dimension": b"2", b"grafx.dtype": b"float32"}
    for i, (encoding, value) in enumerate(((b"list-v1", [1.]), (b"future", [1., 0.]), (None, [1., 0.]))):
        extra = {} if encoding is None else {b"grafx.parquet.vector": encoding}
        schema = pa.schema([pa.field("v", pa.list_(pa.float32()), metadata={**metadata, **extra})])
        pq.write_table(pa.Table.from_arrays([pa.array([value], type=pa.list_(pa.float32()))], schema=schema), tmp_path / f"bad{i}.parquet")
        with pytest.raises(GrafxUnsupportedOperation):
            tuple(read_parquet_batches(f"bad{i}.parquet", allowed_root=tmp_path, types=(kinds[1],)))


def test_publication_race_does_not_replace_competing_file(tmp_path, monkeypatch):
    import os
    real = os.link
    def competing(source, target):
        target.write_bytes(b"competitor")
        real(source, target)
    monkeypatch.setattr(os, "link", competing)
    with pytest.raises(GrafxUnsupportedOperation):
        write_parquet(QueryResult(columns=("id",), rows=((1,),)), "race.parquet", allowed_root=tmp_path, types=("INT64",))
    assert (tmp_path / "race.parquet").read_bytes() == b"competitor"
    assert not list(tmp_path.glob(".grafx-parquet-*"))


def test_network_namespace_refused_before_filesystem_lookup(tmp_path, monkeypatch):
    from pathlib import Path
    def unexpected_lookup(*args, **kwargs):
        pytest.fail("network namespace caused a filesystem lookup")
    monkeypatch.setattr(Path, "lstat", unexpected_lookup)
    for name, root in (("//server/share/data.parquet", tmp_path), ("data.parquet", "//server/share")):
        with pytest.raises(GrafxUnsupportedOperation):
            tuple(read_parquet_batches(name, allowed_root=root, types=("INT64",)))
