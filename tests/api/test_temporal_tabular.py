"""Lossless temporal interchange, explicit metadata and atomic late refusals."""

import pytest

from okto_grafx import connect, QueryResult
from okto_grafx.arrow import to_arrow_batches, import_arrow_batches
from okto_grafx.errors import GrafxError, GrafxQueryBudgetExceeded
from okto_grafx.domain.model.temporal_interchange import temporal_components
from tests.api.test_temporal_transfer import VALUES, TYPES

pytestmark = pytest.mark.optional_dependency("pyarrow")
pa = pytest.importorskip("pyarrow")
NAMES = ("id", *(f"v{i}" for i in range(6)))
KINDS = ("INT64", *TYPES)
STATEMENT = "CREATE (:N {" + ",".join(f"{name}:${name}" for name in NAMES) + "})"
READ = "MATCH(n:N) RETURN " + ",".join(f"n.{name}" for name in NAMES) + " ORDER BY n.id"


def result():
    return QueryResult(columns=NAMES, rows=((1, *VALUES), (2, *([None] * 6))))


def schema(db):
    db.ensure_identity_indexes()
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE N(" + ",".join(f"{name} {kind}" for name, kind in zip(NAMES, KINDS)) + ", PRIMARY KEY(id))")


@pytest.mark.parametrize("transport", ["arrow", "pandas", "polars", "parquet"])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_native_temporal_roundtrip_all_tabular_routes(tmp_path, transport, codec):
    source = result()
    if transport == "arrow":
        artifact = list(to_arrow_batches(source, types=KINDS, batch_rows=1))
        importer = import_arrow_batches
    elif transport == "pandas":
        pytest.importorskip("pandas")
        from okto_grafx.tabular import to_pandas, import_pandas
        artifact = to_pandas(source, types=KINDS, batch_rows=1)
        importer = import_pandas
    elif transport == "polars":
        pytest.importorskip("polars")
        from okto_grafx.polars import to_polars, import_polars
        artifact = to_polars(source, types=KINDS, batch_rows=1)
        importer = import_polars
    else:
        from okto_grafx.parquet import write_parquet, import_parquet
        artifact = tmp_path / "values.parquet"
        write_parquet(source, artifact, allowed_root=tmp_path, types=KINDS, batch_rows=1)
        importer = import_parquet
    with connect(tmp_path / "db", codec=codec) as db:
        schema(db)
        with db.begin() as tx:
            report = importer(tx, STATEMENT, artifact, types=KINDS, max_batch_rows=1,
                              **({"allowed_root": tmp_path} if transport == "parquet" else {}))
            assert report.statements == 2
        assert db.execute(READ).rows == source.rows
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "db", read_only=True, codec=codec) as db:
        assert db.execute(READ).rows == source.rows


def test_arrow_coordinates_and_metadata_are_explicit_and_wide():
    first, nulls = list(to_arrow_batches(result(), types=KINDS, batch_rows=1))
    for i, (kind, value) in enumerate(zip(TYPES, VALUES), 1):
        field = first.schema.field(i)
        assert pa.types.is_struct(field.type)
        assert field.metadata == {b"grafx.type": kind.encode(), b"grafx.temporal": b"components-v1"}
        assert first.column(i)[0].as_py() == temporal_components(value)
        assert not nulls.column(i)[0].is_valid


@pytest.mark.parametrize("fault", ["missing_metadata", "wrong_type", "unknown_encoding", "narrow_arrow_type",
                                  "null_child", "invalid_date", "normalized_duration", "oversize_zone"])
def test_late_temporal_batch_refusal_rolls_back_whole_call(tmp_path, fault):
    good = next(to_arrow_batches(result(), types=KINDS, batch_rows=1))
    fields = list(good.schema)
    arrays = list(good.columns)
    position = 6 if fault == "normalized_duration" else 5 if fault == "oversize_zone" else 1
    field = fields[position]
    if fault in ("missing_metadata", "wrong_type", "unknown_encoding"):
        meta = {} if fault == "missing_metadata" else {**field.metadata,
            (b"grafx.type" if fault == "wrong_type" else b"grafx.temporal"):
                (b"LOCALTIME" if fault == "wrong_type" else b"components-v99")}
        fields[position] = field.with_metadata(meta)
    elif fault == "narrow_arrow_type":
        fields[position] = pa.field(field.name, pa.date32(), metadata=field.metadata)
        arrays[position] = pa.array([0], type=pa.date32())
    else:
        components = good.column(position)[0].as_py()
        key = "nanoseconds" if fault == "normalized_duration" else "zone" if fault == "oversize_zone" else "epoch_day"
        components[key] = None if fault == "null_child" else 10**9 if fault == "normalized_duration" else "z" * 256 if fault == "oversize_zone" else 2**63 - 1
        arrays[position] = pa.array([components], type=field.type)
    arrays[0] = pa.array([2], type=pa.int64())
    bad = pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                import_arrow_batches(tx, STATEMENT, [good, bad], types=KINDS)
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert not db.verify("all").findings


def test_temporal_export_and_import_account_for_structured_memory(tmp_path):
    with pytest.raises(GrafxQueryBudgetExceeded):
        list(to_arrow_batches(result(), types=KINDS, max_batch_bytes=4000))
    batches = list(to_arrow_batches(result(), types=KINDS, batch_rows=1))
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            with pytest.raises(GrafxQueryBudgetExceeded):
                import_arrow_batches(tx, STATEMENT, batches, types=KINDS, max_batch_bytes=2500)
            assert tx.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)


def test_temporal_cursor_batches_keep_snapshot_across_independent_writer(tmp_path):
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            tx.executemany(STATEMENT, [dict(zip(NAMES, row)) for row in result().rows])
        with db.query(READ).cursor(batch_size=1) as cursor:
            batches = to_arrow_batches(cursor, types=KINDS, batch_rows=1)
            first = next(batches)
            with connect(tmp_path / "db") as writer, writer.begin() as tx:
                tx.execute("MATCH(n:N {id:2}) SET n.v0=$v", {"v": VALUES[0]})
            second = next(batches)
            assert second.column(1)[0].as_py() is None
            assert list(batches) == []
        assert first.column(1)[0].as_py() == temporal_components(VALUES[0])
        assert db.execute("MATCH(n:N {id:2}) RETURN n.v0").rows == ((VALUES[0],),)


@pytest.mark.parametrize("fault", ["schema_missing", "encoding_unknown"])
def test_pandas_temporal_metadata_is_mandatory(tmp_path, fault):
    pytest.importorskip("pandas")
    from okto_grafx.tabular import to_pandas, import_pandas
    frame = to_pandas(result(), types=KINDS)
    if fault == "schema_missing":
        frame.attrs.clear()
    else:
        fields = list(frame.attrs["grafx.arrow_schema"])
        fields[1] = fields[1].with_metadata({b"grafx.type": b"DATE", b"grafx.temporal": b"unknown"})
        frame.attrs["grafx.arrow_schema"] = pa.schema(fields)
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            with pytest.raises(GrafxError):
                import_pandas(tx, STATEMENT, frame, types=KINDS)
            assert tx.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)


@pytest.mark.parametrize("fault", ["null_child", "wide_integer", "extra_field"])
def test_polars_temporal_normalization_does_not_admit_wrong_coordinates(tmp_path, fault):
    pl = pytest.importorskip("polars")
    from okto_grafx.polars import PolarsFrame, to_polars, import_polars
    artifact = to_polars(result(), types=KINDS)
    first = temporal_components(VALUES[0])
    value = {"epoch_day": None if fault == "null_child" else 2**63-1}
    if fault == "extra_field":
        first["extra"] = 0  # Polars infers struct fields from the first record.
        value = {"epoch_day": VALUES[0].epoch_day, "extra": 1}
    altered = artifact.frame.with_columns(pl.Series("v0", [first, value]))
    assert altered["v0"][1] == value
    frame = PolarsFrame(altered, artifact.arrow_schema)
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                import_polars(tx, STATEMENT, frame, types=KINDS, max_batch_rows=1)
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
