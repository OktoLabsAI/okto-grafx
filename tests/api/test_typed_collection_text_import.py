"""Exact collection CSV/JSONL/SQLite ingestion, ownership, budgets and atomicity."""

from dataclasses import replace
import json
import sqlite3

import pytest

from okto_grafx import connect, StoredType, DecimalValue, DateValue
from okto_grafx.collection_json import collection_json_value
from okto_grafx.domain.model.stored_types import normalize_typed_value
from okto_grafx.errors import GrafxError, GrafxQueryBudgetExceeded
from okto_grafx.text_import import import_csv, import_jsonl, read_csv_batches, read_jsonl_batches, TextImportLimits
from okto_grafx.sqlite_import import import_sqlite, read_sqlite_rows
from tests.api.test_temporal_text_import import source, options

NAMES = ("id", "v")
KINDS = ("csv", "jsonl", "sqlite")
IMPORTERS = {"csv": import_csv, "jsonl": import_jsonl, "sqlite": import_sqlite}
RECORD = StoredType("STRUCT", fields=(
    ("xs", StoredType("LIST", element=StoredType("INT64", nullable=False))),
    ("price", StoredType("DECIMAL", precision=12, scale=4)),
    ("pair", StoredType("ARRAY", element=StoredType("STRING"), length=2)),
    ("meta", StoredType("MAP", element=StoredType("ANY"))),
))
RECORD_VALUE = {"xs": (1, 2), "price": DecimalValue(12500, 12, 4), "pair": ("a", None),
                "meta": {"literal": {"type": "decimal", "value": 42}, "d": DateValue.from_epoch_day(20000)}}
ROOTS = (
    (StoredType("LIST", element=StoredType("DECIMAL", precision=12, scale=4)), (DecimalValue(12500, 12, 4), None)),
    (StoredType("MAP", element=StoredType("ARRAY", element=StoredType("DATE"), length=2)),
     {"dates": (DateValue.from_epoch_day(20000), None)}),
    (StoredType("ARRAY", element=RECORD, length=2), (RECORD_VALUE, None)),
    (RECORD, RECORD_VALUE),
)


def input_file(root, kind, values):
    rows = [(index, json.dumps(value) if type(value) in (list, dict) and kind != "jsonl" else value)
            for index, value in enumerate(values, 1)]
    return source(root, kind, rows, NAMES)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("descriptor,native", ROOTS)
def test_all_collection_roots_read_import_snapshot_and_reopen(tmp_path, kind, codec, descriptor, native):
    wire = collection_json_value(descriptor, native)
    path = input_file(tmp_path, kind, [wire, None])
    kwargs = options(tmp_path, kind, NAMES, ("INT64", descriptor))
    if kind == "sqlite":
        values = read_sqlite_rows(path, **kwargs)
    else:
        reader = read_csv_batches if kind == "csv" else read_jsonl_batches
        values = tuple(row for batch in reader(path, **kwargs) for row in batch)
    assert tuple(v["v"] for v in values) == (normalize_typed_value(descriptor, native), None)
    with connect(tmp_path / "db", codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute(f"CREATE NODE TABLE N(id INT64,v {descriptor.describe()},PRIMARY KEY(id))")
        with db.begin("read") as reader:
            with db.begin() as tx:
                assert IMPORTERS[kind](tx, "CREATE(:N {id:$id,v:$v})", path, **kwargs).statements == 2
            assert reader.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)
        assert db.execute("MATCH(n:N) RETURN n.v ORDER BY n.id").rows == ((native,), (None,))
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "db", codec=codec, read_only=True) as db:
        assert db.execute("MATCH(n:N) RETURN n.v ORDER BY n.id").rows == ((native,), (None,))
    if kind == "sqlite":
        with sqlite3.connect(path, timeout=0) as connection:
            connection.execute("BEGIN EXCLUSIVE")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fault", ("root", "missing", "extra", "length", "nonnull", "integer", "decimal", "dynamic",
                                  "duplicate", "nonfinite", "unicode", "overflow"))
def test_late_malformed_collection_rolls_back_call_and_capabilities(tmp_path, kind, fault):
    good = collection_json_value(RECORD, RECORD_VALUE)
    bad = json.loads(json.dumps(good))
    if fault == "root":
        bad = 7
    elif fault == "missing":
        del bad["meta"]
    elif fault == "extra":
        bad["extra"] = None
    elif fault == "length":
        bad["pair"] = ["x"]
    elif fault == "nonnull":
        bad["xs"] = [None]
    elif fault == "integer":
        bad["xs"] = [1]
    elif fault == "decimal":
        bad["price"]["scale"] = 3
    elif fault == "dynamic":
        bad["meta"]["literal"] = {"literal": "no tag"}
    elif fault == "duplicate":
        bad = json.dumps(good).replace('"xs":', '"xs": null,"xs":', 1)
    elif fault == "nonfinite":
        bad = json.dumps(good).replace('"xs": ["1", "2"]', '"xs": [NaN]')
    elif fault == "unicode":
        bad["pair"] = ["\ud800", None]
    else:
        bad["xs"] = ["9223372036854775808"]
    path = input_file(tmp_path, kind, [good, good, bad])
    kwargs = options(tmp_path, kind, NAMES, ("INT64", RECORD))
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v ANY,PRIMARY KEY(id))")
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99,v:'prior'})")
            with pytest.raises(GrafxError):
                IMPORTERS[kind](tx, "CREATE(:N {id:$id,v:$v})", path, **kwargs)
            assert tx.execute("MATCH(n:N) RETURN n.id,n.v").rows == ((99, "prior"),)
        assert not db._catalog.catalog.requires_capability("decimal_values_v1")
        assert not db._catalog.catalog.requires_capability("temporal_values_v1")
        assert not db._catalog.catalog.requires_capability("typed_collections_v1")
        assert not db.verify("all").findings


@pytest.mark.parametrize("kind", KINDS)
def test_root_nonnull_field_and_work_bounds_apply_before_success(tmp_path, kind):
    path = input_file(tmp_path, kind, [collection_json_value(RECORD, RECORD_VALUE), None])
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v ANY,PRIMARY KEY(id))")
        with db.begin() as tx:
            with pytest.raises(GrafxError):
                IMPORTERS[kind](tx, "CREATE(:N {id:$id,v:$v})", path,
                                **options(tmp_path, kind, NAMES, ("INT64", replace(RECORD, nullable=False))))
            with pytest.raises(GrafxQueryBudgetExceeded):
                IMPORTERS[kind](tx, "CREATE(:N {id:$id,v:$v})", path,
                                **options(tmp_path, kind, NAMES, ("INT64", RECORD), max_field_bytes=30))
            with pytest.raises(GrafxQueryBudgetExceeded):
                IMPORTERS[kind](tx, "CREATE(:N {id:$id,v:$v})", path,
                                **options(tmp_path, kind, NAMES, ("INT64", RECORD), max_work=20))
            assert tx.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)


@pytest.mark.parametrize("kind", KINDS)
def test_transport_precision_is_exact_but_target_assignment_can_rescale(tmp_path, kind):
    descriptor = StoredType("LIST", element=StoredType("DECIMAL", precision=12, scale=4))
    path = input_file(tmp_path, kind, [collection_json_value(descriptor, (DecimalValue(12500, 12, 4),))])
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v LIST<DECIMAL(14,5)>,PRIMARY KEY(id))")
            IMPORTERS[kind](tx, "CREATE(:N {id:$id,v:$v})", path,
                            **options(tmp_path, kind, NAMES, ("INT64", descriptor)))
        assert db.execute("MATCH(n:N) RETURN n.v").rows == (((DecimalValue(125000, 14, 5),),),)


def test_jsonl_reader_owns_descriptor_across_yields(tmp_path):
    descriptor = StoredType("LIST", element=StoredType("INT64"))
    path = input_file(tmp_path, "jsonl", [["1"], ["2"]])
    reader = read_jsonl_batches(path, allowed_root=tmp_path, columns=NAMES, types=("INT64", descriptor),
                                limits=TextImportLimits(batch_rows=1))
    assert next(reader)[0]["v"] == (1,)
    object.__setattr__(descriptor.element, "kind", "STRING")
    assert next(reader)[0]["v"] == (2,)
    with pytest.raises(StopIteration):
        next(reader)


@pytest.mark.parametrize("value", [b'["1"]', 1, 1.0])
def test_sqlite_does_not_infer_collection_json_from_non_text_storage(tmp_path, value):
    path = input_file(tmp_path, "sqlite", [value])
    with pytest.raises(GrafxError):
        read_sqlite_rows(path, **options(tmp_path, "sqlite", NAMES, ("INT64", StoredType("LIST", element=StoredType("INT64")))))
