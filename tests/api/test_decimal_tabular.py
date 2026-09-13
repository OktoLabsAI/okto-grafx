"""Exact decimal128 transport, bounded native admission and all four consumers."""

from decimal import Decimal, localcontext, Inexact, Rounded, FloatOperation

import pytest

from okto_grafx import DecimalValue, QueryResult, connect
from okto_grafx.arrow import ArrowDecimalType, import_arrow_batches, to_arrow_batches
from okto_grafx.errors import GrafxError, GrafxConfigurationError, GrafxQueryBudgetExceeded, GrafxTransactionStateError

pytestmark = pytest.mark.optional_dependency("pyarrow")
pa = pytest.importorskip("pyarrow")
ROUTES = ("arrow", "pandas", "polars", "parquet")
VALUES = (DecimalValue(10**38 - 1, 38, 19), DecimalValue(-1234500, 12, 4),
          DecimalValue(0, 38, 38), DecimalValue(-9, 1, 0))
KINDS = ("INT64", *(ArrowDecimalType(v.precision, v.scale) for v in VALUES))
NAMES = ("id", "v0", "v1", "v2", "v3")
STATEMENT = "CREATE (:N {" + ",".join(f"{name}:${name}" for name in NAMES) + "})"
READ = "MATCH(n:N) RETURN " + ",".join(f"n.{name}" for name in NAMES) + " ORDER BY n.id"


def result():
    return QueryResult(columns=NAMES, rows=((1, *VALUES), (2, None, None, None, None)))


def schema(db, *, typed=False, declaration=None):
    db.ensure_identity_indexes()
    declarations = [declaration or (f"DECIMAL({v.precision},{v.scale})" if typed else "ANY") for v in VALUES]
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE N(id INT64," + ",".join(
            f"{name} {kind}" for name, kind in zip(NAMES[1:], declarations)) + ",PRIMARY KEY(id))")


def artifact(route, root, source=None, *, kinds=KINDS, **options):
    source = result() if source is None else source
    if route == "arrow":
        return list(to_arrow_batches(source, types=kinds, batch_rows=1, **options))
    if route == "pandas":
        pytest.importorskip("pandas")
        from okto_grafx.tabular import to_pandas
        return to_pandas(source, types=kinds, batch_rows=1, **options)
    if route == "polars":
        pytest.importorskip("polars")
        from okto_grafx.polars import to_polars
        return to_polars(source, types=kinds, batch_rows=1, **options)
    from okto_grafx.parquet import write_parquet
    path = root / "decimals.parquet"
    write_parquet(source, path, allowed_root=root, types=kinds, batch_rows=1, **options)
    return path


def consume(route, tx, value, root, *, kinds=KINDS, statement=STATEMENT, **options):
    if route == "arrow":
        return import_arrow_batches(tx, statement, value, types=kinds, max_batch_rows=1, **options)
    if route == "pandas":
        from okto_grafx.tabular import import_pandas
        return import_pandas(tx, statement, value, types=kinds, max_batch_rows=1, **options)
    if route == "polars":
        from okto_grafx.polars import import_polars
        return import_polars(tx, statement, value, types=kinds, max_batch_rows=1, **options)
    from okto_grafx.parquet import import_parquet
    return import_parquet(tx, statement, value, allowed_root=root, types=kinds, max_batch_rows=1, **options)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("typed", [False, True])
def test_decimal_all_tabular_roundtrip_native_commit_reopen(tmp_path, route, codec, typed):
    with localcontext() as context:
        context.prec = 1
        context.Emax, context.Emin = 1, -1
        context.traps[Inexact] = context.traps[Rounded] = context.traps[FloatOperation] = True
        value = artifact(route, tmp_path)
        with connect(tmp_path / "db", codec=codec) as db:
            schema(db, typed=typed)
            with db.begin() as tx:
                report = consume(route, tx, value, tmp_path)
                assert report.statements == 2
                assert tx.execute(READ).rows == result().rows
            assert db.execute(READ).rows == result().rows
            assert not db.verify("all").findings
            db.checkpoint()
        with connect(tmp_path / "db", read_only=True, codec=codec) as db:
            assert db.execute(READ).rows == result().rows
        assert not any(context.flags.values())


@pytest.mark.parametrize("precision,scale", [(0, 0), (39, 0), (True, 0), (1.0, 0), ("12", 0),
                                            (1, -1), (1, 2), (12, True), (12, 1.0), (12, None)])
def test_decimal_descriptor_exact_bounded_coordinates(precision, scale):
    with pytest.raises(GrafxConfigurationError):
        ArrowDecimalType(precision, scale)


@pytest.mark.parametrize("scale", range(39))
def test_all_scales_preserve_arrow_values_and_sliced_offsets(tmp_path, scale):
    kinds = ("INT64", ArrowDecimalType(38, scale))
    values = (DecimalValue(0, 38, scale), DecimalValue(10**38 - 1, 38, scale),
              None, DecimalValue(-(10**38 - 1), 38, scale))
    source = QueryResult(columns=("id", "v0"), rows=tuple(enumerate(values)))
    batch = next(to_arrow_batches(source, types=kinds))
    assert batch.schema.field(1).type == pa.decimal128(38, scale)
    assert batch.schema.field(1).metadata == {b"grafx.type": b"DECIMAL", b"grafx.decimal": b"decimal128-v1"}
    for index, native in enumerate(values):
        observed = batch.column(1)[index].as_py()
        assert observed is None if native is None else observed.as_tuple().exponent == -scale
        if native is not None:
            assert observed == Decimal(native.to_string())
    with connect(":memory:") as db:
        schema(db)
        with db.begin() as tx:
            import_arrow_batches(tx, "CREATE(:N {id:$id,v0:$v0})", [batch.slice(1, 3)], types=kinds)
        assert db.execute("MATCH(n:N) RETURN n.id,n.v0 ORDER BY n.id").rows == source.rows[1:]


@pytest.mark.parametrize("bad", [1, 1.25, True, "1.25", Decimal("1.25"),
                               DecimalValue(125, 4, 2), DecimalValue(1250, 12, 3)])
def test_export_never_infers_host_values_or_rescales_native_metadata(bad):
    source = QueryResult(columns=("v",), rows=((bad,),))
    with pytest.raises(GrafxError):
        list(to_arrow_batches(source, types=(ArrowDecimalType(12, 2),)))


def test_export_revalidates_forged_native_values_and_descriptor():
    value = DecimalValue(1, 12, 2)
    object.__setattr__(value, "coefficient", 10**12)
    with pytest.raises(GrafxError):
        list(to_arrow_batches(QueryResult(columns=("v",), rows=((value,),)), types=(ArrowDecimalType(12, 2),)))
    kind = ArrowDecimalType(12, 2)
    object.__setattr__(kind, "scale", -1)
    with pytest.raises(GrafxConfigurationError):
        list(to_arrow_batches(QueryResult(columns=("v",), rows=((None,),)), types=(kind,)))


@pytest.mark.parametrize("fault", ["missing_metadata", "wrong_tag", "unknown_encoding", "precision", "scale",
                                  "decimal256", "float", "dictionary", "out_of_precision"])
def test_late_bad_decimal_arrow_batch_rolls_back_call_not_prior_staging(tmp_path, fault):
    good = next(to_arrow_batches(result(), types=KINDS, batch_rows=1))
    fields, arrays = list(good.schema), list(good.columns)
    field = fields[1]
    if fault in ("missing_metadata", "wrong_tag", "unknown_encoding"):
        metadata = {} if fault == "missing_metadata" else {**field.metadata,
            (b"grafx.type" if fault == "wrong_tag" else b"grafx.decimal"): b"unknown"}
        fields[1] = field.with_metadata(metadata)
    elif fault == "out_of_precision":
        arrays[1] = pa.Array.from_buffers(field.type, 1, [None, pa.py_buffer((2**127-1).to_bytes(16, "little"))])
    else:
        dtype = (pa.decimal128(37, 19) if fault == "precision" else pa.decimal128(38, 18) if fault == "scale"
                 else pa.decimal256(38, 19) if fault == "decimal256" else pa.float64() if fault == "float"
                 else pa.dictionary(pa.int32(), field.type))
        arrays[1] = (pa.DictionaryArray.from_arrays(pa.array([None], type=pa.int32()),
                                                   pa.array([Decimal(0)], type=field.type))
                     if fault == "dictionary" else pa.array([None], type=dtype))
        fields[1] = pa.field(field.name, dtype, metadata=field.metadata)
    arrays[0] = pa.array([2], type=pa.int64())
    bad = pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                import_arrow_batches(tx, STATEMENT, [good, bad], types=KINDS)
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert not db._catalog.catalog.requires_capability("decimal_values_v1")
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert not db.verify("all").findings


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("fault", ["inexact", "overflow"])
def test_tabular_late_destination_assignment_preserves_prior_staging(tmp_path, route, fault):
    source = QueryResult(columns=("id", "v0"), rows=((1, DecimalValue(10000, 12, 4)),
        (2, DecimalValue(12345 if fault == "inexact" else 10**10, 12, 4))))
    kinds = ("INT64", ArrowDecimalType(12, 4))
    value = artifact(route, tmp_path, source, kinds=kinds)
    with connect(tmp_path / "db") as db:
        schema(db, declaration="DECIMAL(4,1)")
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                consume(route, tx, value, tmp_path, kinds=kinds, statement="CREATE(:N {id:$id,v0:$v0})")
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert not db.verify("all").findings
    if route == "parquet":
        # Closed file even on late native-staging failure (Windows rename proof).
        value.rename(tmp_path / "closed.parquet")


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("limit", ["max_rows", "max_batches", "max_batch_bytes"])
def test_tabular_decimal_bounds_rollback_whole_import(tmp_path, route, limit):
    value = artifact(route, tmp_path)
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxQueryBudgetExceeded):
                consume(route, tx, value, tmp_path, **{limit: 1})
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert not db._catalog.catalog.requires_capability("decimal_values_v1")


@pytest.mark.parametrize("route", ROUTES)
def test_decimal_empty_and_all_null_transport_does_not_activate_storage(tmp_path, route):
    for empty in (True, False):
        root = tmp_path / str(empty)
        root.mkdir()
        source = QueryResult(columns=NAMES, rows=() if empty else ((1, None, None, None, None),))
        value = artifact(route, root, source)
        with connect(root / "db") as db:
            schema(db)
            with db.begin() as tx:
                report = consume(route, tx, value, root)
                assert report.statements == (0 if empty else 1)
            assert db.execute(READ).rows == source.rows
            assert not db._catalog.catalog.requires_capability("decimal_values_v1")


@pytest.mark.parametrize("route", ROUTES)
def test_decimal_export_budget_and_parquet_no_partial_publication(tmp_path, route):
    with pytest.raises(GrafxQueryBudgetExceeded):
        artifact(route, tmp_path, max_batch_bytes=256)
    assert not (tmp_path / "decimals.parquet").exists()
    assert not list(tmp_path.glob(".grafx-parquet-*"))


def test_decimal_cursor_snapshot_and_owned_batches_survive_independent_commit(tmp_path):
    with connect(tmp_path / "db") as db:
        schema(db, typed=True)
        with db.begin() as tx:
            tx.executemany(STATEMENT, [dict(zip(NAMES, row)) for row in result().rows])
        with db.query(READ).cursor(batch_size=1) as cursor:
            batches = to_arrow_batches(cursor, types=KINDS, batch_rows=1)
            first = next(batches)
            with connect(tmp_path / "db") as writer, writer.begin() as tx:
                tx.execute("MATCH(n:N {id:2}) SET n.v0=$v", {"v": VALUES[0]})
            assert next(batches).column(1)[0].as_py() is None
            assert list(batches) == []
        assert db.execute("MATCH(n:N {id:2}) RETURN n.v0").rows == ((VALUES[0],),)
    assert first.column(1)[0].as_py() == Decimal(VALUES[0].to_string())


@pytest.mark.parametrize("fault", ["metadata_missing", "metadata_encoding", "dtype_object", "dtype_scale"])
def test_pandas_decimal_schema_and_arrow_dtype_must_both_match(tmp_path, fault):
    pd = pytest.importorskip("pandas")
    frame = artifact("pandas", tmp_path)
    if fault == "metadata_missing":
        frame.attrs.clear()
    elif fault == "metadata_encoding":
        fields = list(frame.attrs["grafx.arrow_schema"])
        fields[1] = fields[1].with_metadata({b"grafx.type": b"DECIMAL", b"grafx.decimal": b"unknown"})
        frame.attrs["grafx.arrow_schema"] = pa.schema(fields)
    else:
        frame["v0"] = pd.Series([None, None], dtype=object if fault == "dtype_object" else pd.ArrowDtype(pa.decimal128(38, 18)))
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            with pytest.raises(GrafxError):
                consume("pandas", tx, frame, tmp_path)
            assert tx.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)


@pytest.mark.parametrize("fault", ["precision", "scale", "float", "metadata"])
def test_polars_decimal_actual_and_wrapper_schema_must_both_match(tmp_path, fault):
    pl = pytest.importorskip("polars")
    from okto_grafx.polars import PolarsFrame
    value = artifact("polars", tmp_path)
    if fault == "metadata":
        fields = list(value.arrow_schema)
        fields[1] = fields[1].with_metadata({})
        value = PolarsFrame(value.frame, pa.schema(fields))
    else:
        dtype = pl.Decimal(37, 19) if fault == "precision" else pl.Decimal(38, 18) if fault == "scale" else pl.Float64
        value = PolarsFrame(value.frame.with_columns(pl.Series("v0", [None, None], dtype=dtype)), value.arrow_schema)
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                consume("polars", tx, value, tmp_path)
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)


@pytest.mark.parametrize("route", ROUTES)
def test_decimal_export_late_mismatch_never_publishes_partial_file(tmp_path, route):
    source = QueryResult(columns=("v",), rows=((DecimalValue(12500, 12, 4),), (DecimalValue(125, 12, 2),)))
    with pytest.raises(GrafxError):
        artifact(route, tmp_path, source, kinds=(ArrowDecimalType(12, 4),))
    assert not (tmp_path / "decimals.parquet").exists()
    assert not list(tmp_path.glob(".grafx-parquet-*"))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_decimal_tabular_committed_apply_failure_recovers_once(tmp_path, route, codec, monkeypatch):
    from okto_grafx.engine.txn_manager import TransactionManager
    value = artifact(route, tmp_path)
    with connect(tmp_path / "db", codec=codec) as db:
        schema(db)
        tx = db.begin()
        consume(route, tx, value, tmp_path)

        def fail(manager, images):
            raise RuntimeError("injected failure after durable COMMIT")

        with monkeypatch.context() as patch:
            patch.setattr(TransactionManager, "_apply_images", fail)
            with pytest.raises(GrafxTransactionStateError) as error:
                tx.commit()
            assert error.value.details["committed"] and error.value.details["durable"]
    for _ in range(2):
        with connect(tmp_path / "db", codec=codec) as db:
            assert db.execute(READ).rows == result().rows
            assert db._catalog.catalog.requires_capability("decimal_values_v1")
            assert not db.verify("all").findings


def test_decimal_logical_tariff_is_charged_before_conversion(tmp_path):
    source = QueryResult(columns=("v0",), rows=((DecimalValue(125, 12, 2),),))
    kinds = (ArrowDecimalType(12, 2),)
    with pytest.raises(GrafxQueryBudgetExceeded):
        list(to_arrow_batches(source, types=kinds, max_batch_bytes=1599))
    batches = list(to_arrow_batches(source, types=kinds, max_batch_bytes=1600))
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            with pytest.raises(GrafxQueryBudgetExceeded):
                import_arrow_batches(tx, "CREATE(:N {id:1,v0:$v0})", batches, types=kinds, max_batch_bytes=1871)
            assert tx.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)
            import_arrow_batches(tx, "CREATE(:N {id:1,v0:$v0})", batches, types=kinds, max_batch_bytes=1872)
        assert db.execute("MATCH(n:N) RETURN n.v0").rows == source.rows


@pytest.mark.parametrize("fault", ["metadata_missing", "encoding", "physical_scale"])
def test_external_decimal_parquet_requires_exact_type_and_metadata(tmp_path, fault):
    import pyarrow.parquet as pq
    good = next(to_arrow_batches(result(), types=KINDS, batch_rows=1))
    fields, arrays = list(good.schema), list(good.columns)
    if fault == "physical_scale":
        fields[1] = pa.field("v0", pa.decimal128(38, 18), metadata=fields[1].metadata)
        arrays[1] = pa.array([None], type=fields[1].type)
    else:
        fields[1] = fields[1].with_metadata({} if fault == "metadata_missing" else
            {b"grafx.type": b"DECIMAL", b"grafx.decimal": b"unknown"})
    bad = pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))
    path = tmp_path / "external.parquet"
    pq.write_table(pa.Table.from_batches([bad]), path)
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                consume("parquet", tx, path, tmp_path)
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
    path.rename(tmp_path / "closed.parquet")


@pytest.mark.parametrize("route", ROUTES)
def test_decimal_transport_keeps_source_coordinates_then_exact_target_assignment(tmp_path, route):
    kinds = ("INT64", ArrowDecimalType(3, 2))
    source = QueryResult(columns=("id", "v0"), rows=((1, DecimalValue(125, 3, 2)),))
    value = artifact(route, tmp_path, source, kinds=kinds)
    with connect(tmp_path / "db") as db:
        schema(db, declaration="DECIMAL(12,4)")
        with db.begin() as tx:
            consume(route, tx, value, tmp_path, kinds=kinds, statement="CREATE(:N {id:$id,v0:$v0})")
        assert db.execute("MATCH(n:N) RETURN n.v0").rows == ((DecimalValue(12500, 12, 4),),)
