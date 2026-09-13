"""Exact nested columnar transport, actual optional consumers and atomic refusals."""

from dataclasses import replace
import struct

import pytest

from okto_grafx import StoredType, DecimalValue, QueryResult, connect
from okto_grafx.arrow import to_arrow_batches, import_arrow_batches
from okto_grafx.domain.model.stored_types import encode_stored_type
from okto_grafx.domain.model.value import encode_value
from okto_grafx.errors import GrafxError, GrafxQueryBudgetExceeded
from tests.api.test_collection_json import SCALARS
from tests.api.test_decimal_tabular import artifact, consume, ROUTES

pytestmark = pytest.mark.optional_dependency("pyarrow")
pa = pytest.importorskip("pyarrow")
LEAVES = tuple((kind, value) for kind, value, _ in SCALARS)
RECORD = StoredType("STRUCT", fields=tuple((f"v{i}", kind) for i, (kind, _) in enumerate(LEAVES)))
RECORD_VALUE = {f"v{i}": value for i, (_, value) in enumerate(LEAVES)}
ANY_VALUE = {"literal": {"type": "decimal", "coefficient": "ordinary user map"},
             "keys": {(1, 2): b"abc", None: "null key", 7: True},
             "leaves": tuple(value for _, value in LEAVES)}
CASES = (
    (StoredType("LIST", element=RECORD), (RECORD_VALUE, None)),
    (StoredType("MAP", element=RECORD), {"x": RECORD_VALUE, "null": None}),
    (StoredType("ARRAY", element=RECORD, length=2), (RECORD_VALUE, None)),
    (RECORD, RECORD_VALUE),
    (StoredType("MAP", element=StoredType("ANY")), ANY_VALUE),
    (StoredType("STRUCT"), {}),
    (StoredType("ARRAY", element=StoredType("INT64"), length=0), ()),
    (StoredType("LIST", element=StoredType("MAP", element=StoredType("ARRAY", element=StoredType("STRING"), length=2))),
     ({"a": ("x", None), "b": None}, None, {})),
)
WRITE = "CREATE(:N {id:$id,v:$v})"
READ = "MATCH(n:N) RETURN n.id,n.v ORDER BY n.id"


def setup(db, kind, *, typed=True):
    db.ensure_identity_indexes()
    with db.begin() as tx:
        tx.execute(f"CREATE NODE TABLE N(id INT64,v {kind.describe() if typed else 'ANY'},PRIMARY KEY(id))")


def source(value):
    return QueryResult(columns=("id", "v"), rows=((1, value), (2, None)))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("kind,value", CASES)
def test_nested_all_leaf_families_roundtrip_commit_verify_reopen(tmp_path, route, codec, kind, value):
    kinds = ("INT64", kind)
    transport = artifact(route, tmp_path, source(value), kinds=kinds)
    with connect(tmp_path / "db", codec=codec) as db:
        setup(db, kind)
        with db.begin() as tx:
            assert consume(route, tx, transport, tmp_path, kinds=kinds, statement=WRITE).statements == 2
            assert tx.execute(READ).rows == source(value).rows
        assert db.execute(READ).rows == source(value).rows
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "db", codec=codec, read_only=True) as db:
        assert db.execute(READ).rows == source(value).rows


def test_independent_physical_oracles_metadata_and_exact_timestamp_extrema():
    kind = StoredType("LIST", element=RECORD)
    batch = next(to_arrow_batches(source((RECORD_VALUE,)), types=("INT64", kind)))
    field = batch.schema.field(1)
    assert field.metadata == {b"grafx.type": b"LIST", b"grafx.collection": b"nested-v1",
                              b"grafx.any": b"native-value-v1",
                              b"grafx.stored_type": encode_stored_type(kind).hex().encode("ascii")}
    record = batch.column(1)[0].values[0]
    assert record["v1"].as_py() == -(2**63)
    assert record["v2"].as_py() == 2**63 - 1
    assert record["v6"].cast(pa.int64()).as_py() == 2**63 - 1
    assert record["v8"].type == pa.decimal128(12, 4)
    assert record["v8"].as_py().as_tuple().exponent == -4
    assert record["v12"].as_py() == {"epoch_day": 20000, "nanoseconds": 123}
    for kind, value in CASES[-3:-1]:
        encoded = next(to_arrow_batches(source(value), types=("INT64", kind)))
        assert encoded.column(1)[0].as_py() == ({"$empty": True} if kind.kind == "STRUCT" else [])
        assert not encoded.column(1)[1].is_valid
    dynamic = next(to_arrow_batches(source((1, "1", None)), types=("INT64", StoredType("LIST", element=StoredType("ANY")))))
    assert dynamic.column(1)[0].as_py() == [b"\x02\x01\x00\x00\x00\x00\x00\x00\x00", b"\x04\x01\x00\x00\x001", None]


@pytest.mark.parametrize("fault", ("missing", "version", "descriptor", "any_version", "root_null", "child_null", "wrong_width"))
def test_late_arrow_schema_or_nullability_refusal_preserves_prior_staging(tmp_path, fault):
    kind = StoredType("LIST", nullable=False, element=StoredType("INT64", nullable=False))
    kinds = ("INT64", kind)
    good = next(to_arrow_batches(QueryResult(columns=("id", "v"), rows=((1, (1, 2)),)), types=kinds))
    field = good.schema.field(1)
    values = [1, 2]
    if fault in ("missing", "version", "descriptor", "any_version"):
        metadata = {} if fault == "missing" else dict(field.metadata)
        if fault == "version":
            metadata[b"grafx.collection"] = b"nested-v99"
        if fault == "descriptor":
            metadata[b"grafx.stored_type"] = encode_stored_type(replace(kind, nullable=True)).hex().encode("ascii")
        if fault == "any_version":
            metadata[b"grafx.any"] = b"native-value-v99"
        field = field.with_metadata(metadata)
    elif fault == "root_null":
        values = None
    elif fault == "child_null":
        values = [1, None]
    else:
        field = pa.field("v", pa.list_(pa.int32()), metadata=field.metadata)
    bad = pa.RecordBatch.from_arrays([pa.array([2]), pa.array([values], type=field.type)], schema=pa.schema([good.schema.field(0), field]))
    with connect(tmp_path / "db") as db:
        setup(db, kind, typed=False)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                import_arrow_batches(tx, WRITE, [good, bad], types=kinds)
            assert tx.execute(READ).rows == ((99, None),)
        assert not db.verify("all").findings


@pytest.mark.parametrize("fault", ("duplicate_map", "array_length", "sentinel", "any_unknown", "any_trailing", "any_null", "any_nan", "any_duplicate_map"))
@pytest.mark.parametrize("route", ROUTES)
def test_late_structurally_valid_but_semantically_invalid_columnar_payload(tmp_path, fault, route):
    if fault == "duplicate_map":
        kind, good_value, bad_value = StoredType("MAP", element=StoredType("INT64")), {"a": 1}, [("a", 1), ("a", 2)]
    elif fault == "array_length":
        kind, good_value, bad_value = StoredType("ARRAY", element=StoredType("INT64"), length=2), (1, 2), [1]
    elif fault == "sentinel":
        kind, good_value, bad_value = StoredType("STRUCT"), {}, {"$empty": False}
    else:
        kind, good_value = StoredType("LIST", element=StoredType("ANY")), (1,)
        raw = (b"\xff" if fault == "any_unknown" else encode_value(1) + b"\x00" if fault == "any_trailing"
               else encode_value(None) if fault == "any_null" else b"\x03" + struct.pack("<d", float("nan")) if fault == "any_nan"
               else b"\x07" + struct.pack("<I", 2) + (encode_value("a") + encode_value(1)) * 2)
        bad_value = [raw]
    kinds = ("INT64", kind)
    good = next(to_arrow_batches(QueryResult(columns=("id", "v"), rows=((1, good_value),)), types=kinds))
    bad = pa.RecordBatch.from_arrays([pa.array([2]), pa.array([bad_value], type=good.schema.field(1).type)], schema=good.schema)
    transport = raw_artifact(route, tmp_path, [good, bad])
    with connect(tmp_path / "db") as db:
        setup(db, kind)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                consume(route, tx, transport, tmp_path, kinds=kinds, statement=WRITE)
            assert tx.execute(READ).rows == ((99, None),)


def raw_artifact(route, root, batches):
    """Build hostile external input directly; do not pass through native export validation."""
    if route == "arrow":
        return batches
    table = pa.Table.from_batches(batches)
    if route == "pandas":
        pd = pytest.importorskip("pandas")
        frame = table.to_pandas(types_mapper=pd.ArrowDtype)
        frame.attrs["grafx.arrow_schema"] = table.schema
        return frame
    if route == "polars":
        pl = pytest.importorskip("polars")
        from okto_grafx.polars import PolarsFrame
        return PolarsFrame(pl.from_arrow(table), table.schema)
    import pyarrow.parquet as pq
    path = root / "external.parquet"
    pq.write_table(table, path, row_group_size=1)
    return path


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("bound", ("max_rows", "max_batches", "max_batch_bytes"))
def test_nested_import_bounds_rollback_all_batches(tmp_path, route, bound):
    kind, value = CASES[0]
    kinds = ("INT64", kind)
    transport = artifact(route, tmp_path, source(value), kinds=kinds)
    with connect(tmp_path / "db") as db:
        setup(db, kind)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxQueryBudgetExceeded):
                consume(route, tx, transport, tmp_path, kinds=kinds, statement=WRITE, **{bound: 1})
            assert tx.execute(READ).rows == ((99, None),)


@pytest.mark.parametrize("route", ROUTES)
def test_export_nested_work_bound_no_partial_parquet_publication(tmp_path, route):
    kind = StoredType("LIST", element=StoredType("INT64"))
    with pytest.raises(GrafxQueryBudgetExceeded):
        artifact(route, tmp_path, source(tuple(range(1000))), kinds=("INT64", kind), max_batch_bytes=8192)
    assert not list(tmp_path.glob("*.parquet"))
    assert not list(tmp_path.glob(".grafx-parquet-*"))


@pytest.mark.parametrize("kind,bad", ((StoredType("ARRAY",element=StoredType("INT64"),length=2), (1,)),
    (StoredType("STRUCT",fields=(("v",StoredType("INT64")),)), {}),
    (StoredType("LIST",element=StoredType("DECIMAL",precision=12,scale=4)), (DecimalValue(125,3,2),)),
    (StoredType("LIST",element=StoredType("DOUBLE")), (float("inf"),)),
    (StoredType("LIST",element=StoredType("ANY")), (float("nan"),))))
def test_export_never_repairs_or_coerces_native_collections(kind, bad):
    with pytest.raises(GrafxError):
        list(to_arrow_batches(source(bad), types=("INT64", kind)))


def test_descriptor_owned_after_iterator_admission_and_import_generator_boundary(tmp_path):
    kind = StoredType("LIST", element=StoredType("INT64"))
    kinds = ("INT64", kind)
    batches = to_arrow_batches(QueryResult(columns=("id", "v"), rows=((1, (1,)), (2, (2,)))), types=kinds, batch_rows=1)
    first = next(batches)
    object.__setattr__(kind.element, "kind", "STRING")
    second = next(batches)
    assert first.schema == second.schema
    assert second.column(1).to_pylist() == [[2]]
    declared = StoredType("LIST", element=StoredType("INT64"))
    def inputs():
        yield first
        object.__setattr__(declared.element, "kind", "STRING")
        yield second
    with connect(tmp_path / "db") as db:
        setup(db, declared)
        with db.begin() as tx:
            import_arrow_batches(tx, WRITE, inputs(), types=("INT64", declared))
        assert db.execute(READ).rows == ((1, (1,)), (2, (2,)))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("empty", (True, False))
def test_empty_and_all_null_collections_preserve_schema_without_activation(tmp_path, route, empty):
    kind = StoredType("STRUCT", fields=(("nested", StoredType("MAP", element=StoredType("DECIMAL", precision=12, scale=4))),))
    kinds = ("INT64", kind)
    rows = () if empty else ((1, None),)
    transport = artifact(route, tmp_path, QueryResult(columns=("id", "v"), rows=rows), kinds=kinds)
    with connect(tmp_path / "db") as db:
        setup(db, kind, typed=False)
        with db.begin() as tx:
            assert consume(route, tx, transport, tmp_path, kinds=kinds, statement=WRITE).statements == len(rows)
        assert db.execute(READ).rows == rows
        assert not db._catalog.catalog.requires_capability("typed_collections_v1")
        assert not db._catalog.catalog.requires_capability("decimal_values_v1")


@pytest.mark.parametrize("route", ROUTES)
def test_collection_metadata_cannot_be_dropped_in_wrappers(tmp_path, route):
    kind = StoredType("LIST", element=StoredType("INT64"))
    kinds = ("INT64", kind)
    batch = next(to_arrow_batches(source((1,)), types=kinds))
    fields = [batch.schema.field(0), batch.schema.field(1).remove_metadata()]
    untagged = pa.RecordBatch.from_arrays(batch.columns, schema=pa.schema(fields))
    transport = raw_artifact(route, tmp_path, [untagged])
    with connect(tmp_path / "db") as db:
        setup(db, kind)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                consume(route, tx, transport, tmp_path, kinds=kinds, statement=WRITE)
            assert tx.execute(READ).rows == ((99, None),)


def test_any_expansion_budget_refuses_before_native_decode(tmp_path, monkeypatch):
    from okto_grafx import _arrow_values
    kind = StoredType("LIST", element=StoredType("ANY"))
    batch = next(to_arrow_batches(source(((None,) * 1000,)), types=("INT64", kind)))
    calls = []
    original = _arrow_values.decode_value
    def observed(raw):
        calls.append(len(raw))
        return original(raw)
    monkeypatch.setattr(_arrow_values, "decode_value", observed)
    with connect(tmp_path / "db") as db:
        setup(db, kind)
        with db.begin() as tx:
            with pytest.raises(GrafxQueryBudgetExceeded):
                import_arrow_batches(tx, WRITE, [batch], types=("INT64", kind), max_batch_bytes=64000)
            assert tx.execute(READ).rows == ()
    assert calls == []


def test_nested_cursor_snapshot_survives_independent_commit_and_output_mutation(tmp_path):
    kind = StoredType("MAP", element=StoredType("INT64"))
    kinds = ("INT64", kind)
    with connect(tmp_path / "db") as db:
        setup(db, kind)
        with db.begin() as tx:
            tx.executemany(WRITE, [{"id": 1, "v": {"a": 1}}, {"id": 2, "v": {"b": 2}}])
        with db.query("MATCH(n:N) RETURN n.id AS id,n.v AS v ORDER BY n.id").cursor(batch_size=1) as cursor:
            batches = to_arrow_batches(cursor, types=kinds, batch_rows=1)
            first = next(batches)
            with connect(tmp_path / "db") as writer, writer.begin() as tx:
                tx.execute("MATCH(n:N {id:2}) SET n.v={b:20}")
            second = next(batches)
            assert second.column(1).to_pylist() == [[("b", 2)]]
        detached = first.column(1).to_pylist()
        detached[0].append(("changed", 99))
        assert first.column(1).to_pylist() == [[("a", 1)]]
        assert db.execute(READ).rows == ((1, {"a": 1}), (2, {"b": 20}))


@pytest.mark.parametrize("fault", ("numeric_width", "decimal_scale", "null_entry", "null_key", "array_length"))
def test_polars_only_normalizes_documented_layout_differences(tmp_path, fault):
    pl = pytest.importorskip("polars")
    from okto_grafx.polars import PolarsFrame, import_polars
    kind = StoredType("MAP", element=StoredType("ARRAY", element=StoredType("DECIMAL", precision=12, scale=4), length=2))
    kinds = ("INT64", kind)
    good = next(to_arrow_batches(source({"x": (DecimalValue(10000,12,4), None)}), types=kinds))
    leaf = pa.int64() if fault == "numeric_width" else pa.decimal128(12,3) if fault == "decimal_scale" else pa.decimal128(12,4)
    dtype = pa.list_(pa.struct([("key",pa.string()), ("value",pa.list_(leaf))]))
    row = [None] if fault == "null_entry" else [{"key": None if fault == "null_key" else "x", "value": [None] if fault == "array_length" else [None,None]}]
    table = pa.table({"id": pa.array([1]), "v": pa.array([row],type=dtype)})
    frame = PolarsFrame(pl.from_arrow(table), good.schema)
    with connect(tmp_path / "db") as db:
        setup(db, kind)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                import_polars(tx, WRITE, frame, types=kinds)
            assert tx.execute(READ).rows == ((99,None),)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("depth", (16, 63, 64))
def test_deep_nested_roundtrip_uses_existing_native_depth_contract(tmp_path, route, depth):
    kind, value = StoredType("INT64"), 7
    for _ in range(depth):
        kind, value = StoredType("LIST", element=kind), (value,)
    kinds = ("INT64", kind)
    transport = artifact(route, tmp_path, source(value), kinds=kinds)
    with connect(tmp_path / "db") as db:
        setup(db, kind)
        with db.begin() as tx:
            consume(route, tx, transport, tmp_path, kinds=kinds, statement=WRITE)
        assert db.execute(READ).rows == source(value).rows


def test_large_declared_array_all_null_does_not_allocate_fixed_width_slots():
    kind = StoredType("ARRAY", element=StoredType("INT64"), length=2**32-1)
    batch = next(to_arrow_batches(source(None), types=("INT64", kind)))
    assert batch.column(1).values == pa.array([],type=pa.int64())
    assert batch.column(1).null_count == 2


@pytest.mark.parametrize("types", ((StoredType("INT64"),), ("INT64",) * 257, ["INT64"]))
def test_invalid_declarations_refuse_before_source_iteration(tmp_path, types):
    visited = []
    def batches():
        visited.append(True)
        yield None
    with connect(tmp_path / "db") as db, db.begin() as tx:
        with pytest.raises(GrafxError):
            import_arrow_batches(tx, "RETURN $v", batches(), types=types)
    assert not visited
