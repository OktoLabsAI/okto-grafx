"""Explicit tagged DECIMAL imports preserve metadata and statement atomicity."""

import sqlite3

import pytest

from okto_grafx import connect, DecimalValue
from okto_grafx.domain.model.decimal_interchange import decimal_json_value
from okto_grafx.errors import GrafxError, GrafxQueryBudgetExceeded
from okto_grafx.text_import import import_csv, import_jsonl, read_csv_batches, read_jsonl_batches
from okto_grafx.sqlite_import import import_sqlite, read_sqlite_rows
from tests.api.test_temporal_text_import import source, options


NAMES, TYPES = ("id", "v"), ("INT64", "DECIMAL")
VALUES = (DecimalValue(10**38-1, 38, 19), DecimalValue(-12300, 12, 4), DecimalValue(0, 38, 38))
IMPORTERS = {"csv": import_csv, "jsonl": import_jsonl, "sqlite": import_sqlite}


@pytest.mark.parametrize("kind", ("csv", "jsonl", "sqlite"))
@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("typed", (False, True))
def test_tagged_readers_and_import_preserve_native_values_and_exact_assignment(tmp_path, kind, codec, typed):
    rows = [(i, decimal_json_value(v)) for i, v in enumerate(VALUES, 1)] + [(4, None)]
    path = source(tmp_path, kind, rows, NAMES)
    kwargs = options(tmp_path, kind, NAMES, TYPES)
    if kind == "sqlite":
        decoded = read_sqlite_rows(path, **kwargs)
    else:
        reader = read_csv_batches if kind == "csv" else read_jsonl_batches
        decoded = tuple(row for batch in reader(path, **kwargs) for row in batch)
    assert tuple(row["v"] for row in decoded) == (*VALUES, None)
    with connect(tmp_path / "db", codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute(f"CREATE NODE TABLE N(id INT64,v {'DECIMAL(38,19)' if typed else 'ANY'},PRIMARY KEY(id))")
        with db.begin() as tx:
            assert IMPORTERS[kind](tx, "CREATE(:N {id:$id,v:$v})", path, **kwargs).statements == 4
        expected = tuple((v.rescale(38, 19) if typed else v,) for v in VALUES) + ((None,),)
        assert db.execute("MATCH(n:N) RETURN n.v ORDER BY n.id").rows == expected
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "db", codec=codec, read_only=True) as db:
        assert db.execute("MATCH(n:N) RETURN n.v ORDER BY n.id").rows == expected


@pytest.mark.parametrize("kind", ("csv", "jsonl", "sqlite"))
@pytest.mark.parametrize("fault", ("tag", "numeric_coefficient", "noncanonical", "precision", "scale", "overflow",
                                  "missing", "extra", "null_child", "nonfinite", "duplicate", "untyped"))
def test_late_bad_decimal_leaves_no_import_rows_or_capability(tmp_path, kind, fault):
    good = decimal_json_value(DecimalValue(12300, 12, 4))
    bad = dict(good)
    if fault == "tag":
        bad["type"] = "number"
    elif fault == "numeric_coefficient":
        bad["coefficient"] = 12300
    elif fault == "noncanonical":
        bad["coefficient"] = "-0"
    elif fault == "precision":
        bad["precision"] = True
    elif fault == "scale":
        bad["scale"] = 13
    elif fault == "overflow":
        bad["coefficient"] = "9" * 13
    elif fault == "missing":
        del bad["scale"]
    elif fault == "extra":
        bad["extra"] = "field"
    elif fault == "null_child":
        bad["coefficient"] = None
    elif fault == "nonfinite":
        bad = '{"type":"decimal","coefficient":NaN,"precision":12,"scale":4}'
    elif fault == "duplicate":
        bad = '{"type":"decimal","coefficient":"1","coefficient":"2","precision":12,"scale":4}'
    else:
        bad = 1.23
    path = source(tmp_path, kind, [(1, good), (2, good), (3, bad)], NAMES)
    kwargs = options(tmp_path, kind, NAMES, TYPES)
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
        assert db.execute("MATCH(n:N) RETURN n.id,n.v").rows == ((99, "prior"),)
        assert not db.verify("all").findings
    if kind == "sqlite":
        with sqlite3.connect(path, timeout=0) as connection:
            connection.execute("BEGIN EXCLUSIVE")


@pytest.mark.parametrize("kind", ("csv", "jsonl", "sqlite"))
def test_decimal_field_budget_and_inexact_target_scale_refuse_atomically(tmp_path, kind):
    path = source(tmp_path, kind, [(1, decimal_json_value(DecimalValue(123, 3, 2))),
                                  (2, decimal_json_value(DecimalValue(1234, 4, 3)))], NAMES)
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v DECIMAL(12,2),PRIMARY KEY(id))")
        with db.begin() as tx:
            with pytest.raises(GrafxQueryBudgetExceeded):
                IMPORTERS[kind](tx, "CREATE(:N {id:$id,v:$v})", path,
                                **options(tmp_path, kind, NAMES, TYPES, max_field_bytes=20))
            assert tx.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)
            with pytest.raises(GrafxError):
                IMPORTERS[kind](tx, "CREATE(:N {id:$id,v:$v})", path, **options(tmp_path, kind, NAMES, TYPES))
            assert tx.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)
