"""Canonical tagged temporal imports through local CSV/JSONL/SQLite boundaries."""

import csv
import json
import sqlite3

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxQueryBudgetExceeded
from okto_grafx.domain.model.temporal_interchange import temporal_json_value
from okto_grafx.text_import import import_csv, import_jsonl, read_csv_batches, read_jsonl_batches, TextImportLimits
from okto_grafx.sqlite_import import import_sqlite, read_sqlite_rows, SQLiteImportLimits
from tests.api.test_temporal_transfer import VALUES, TYPES

NAMES = ("id", *(f"v{i}" for i in range(6)))
KINDS = ("INT64", *TYPES)
WRITE = "CREATE(:N {" + ",".join(f"{name}:${name}" for name in NAMES) + "})"
READ = "MATCH(n:N) RETURN " + ",".join(f"n.{name}" for name in NAMES) + " ORDER BY n.id"


def source(tmp_path, kind, rows, names=NAMES):
    path = tmp_path / f"input.{kind}"
    if kind == "jsonl":
        path.write_text("\n".join(json.dumps(dict(zip(names, row))) for row in rows), encoding="utf-8")
    else:
        encoded = [tuple(json.dumps(value) if type(value) is dict else value for value in row) for row in rows]
        if kind == "csv":
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(names)
                writer.writerows(tuple("\\N" if value is None else value for value in row) for row in encoded)
        else:
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE items(" + ",".join(names) + ")")
                db.executemany("INSERT INTO items VALUES(" + ",".join("?" for _ in names) + ")", encoded)
                db.commit()
            finally:
                db.close()
    return path


def options(tmp_path, kind, names=NAMES, types=KINDS, **bounds):
    return dict(allowed_root=tmp_path, columns=names, types=types,
                **({"query": "SELECT " + ",".join(names) + " FROM items ORDER BY id",
                    "limits": SQLiteImportLimits(**bounds)} if kind == "sqlite" else
                   {"limits": TextImportLimits(batch_rows=1, **bounds)}))


def schema(db):
    db.ensure_identity_indexes()
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE N(" + ",".join(f"{name} {kind}" for name, kind in zip(NAMES, KINDS)) + ",PRIMARY KEY(id))")


@pytest.mark.parametrize("kind", ["csv", "jsonl", "sqlite"])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_exact_temporal_reader_and_native_import_roundtrip(tmp_path, kind, codec):
    expected = ((1, *VALUES), (2, *([None] * 6)))
    path = source(tmp_path, kind, [(1, *(temporal_json_value(value) for value in VALUES)), (2, *([None] * 6))])
    kwargs = options(tmp_path, kind)
    if kind == "sqlite":
        rows = read_sqlite_rows(path, **kwargs)
    else:
        reader = read_csv_batches if kind == "csv" else read_jsonl_batches
        rows = tuple(row for batch in reader(path, **kwargs) for row in batch)
    assert tuple(tuple(row[name] for name in NAMES) for row in rows) == expected
    importer = {"csv": import_csv, "jsonl": import_jsonl, "sqlite": import_sqlite}[kind]
    with connect(tmp_path / "db", codec=codec) as db:
        schema(db)
        with db.begin() as tx:
            assert importer(tx, WRITE, path, **kwargs).statements == 2
        assert db.execute(READ).rows == expected
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "db", codec=codec, read_only=True) as db:
        assert db.execute(READ).rows == expected


@pytest.mark.parametrize("kind", ["csv", "jsonl", "sqlite"])
@pytest.mark.parametrize("fault", ["tag", "extra", "numeric_wide", "noncanonical", "missing",
                                  "nonfinite", "duplicate", "null_child", "iso_inference", "nested"])
def test_late_invalid_temporal_field_preserves_prior_staging(tmp_path, kind, fault):
    good = temporal_json_value(VALUES[0])
    bad = dict(good)
    if fault == "tag":
        bad["type"] = "datetime"
    elif fault == "extra":
        bad["extra"] = True
    elif fault == "numeric_wide":
        bad["epoch_day"] = VALUES[0].epoch_day
    elif fault == "noncanonical":
        bad["epoch_day"] = "-0"
    elif fault == "missing":
        del bad["epoch_day"]
    elif fault == "null_child":
        bad["epoch_day"] = None
    elif fault == "nonfinite":
        bad = '{"type":"date","epoch_day":NaN}'
    elif fault == "duplicate":
        bad = '{"type":"date","epoch_day":"0","epoch_day":"1"}'
    elif fault == "iso_inference":
        bad = "2024-02-29"
    else:
        bad["epoch_day"] = {"unexpected": [1]}
    names, types = ("id", "v0"), ("INT64", "DATE")
    path = source(tmp_path, kind, [(1, good), (2, bad)], names)
    importer = {"csv": import_csv, "jsonl": import_jsonl, "sqlite": import_sqlite}[kind]
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                importer(tx, "CREATE(:N {id:$id,v0:$v0})", path, **options(tmp_path, kind, names, types))
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert not db.verify("all").findings
    if kind == "sqlite":
        db = sqlite3.connect(path, timeout=0)
        try:
            db.execute("BEGIN EXCLUSIVE")  # Failed read released its source snapshot.
        finally:
            db.close()


@pytest.mark.parametrize("kind", ["csv", "jsonl", "sqlite"])
def test_temporal_field_byte_bound_applies_to_object_and_text(tmp_path, kind):
    names, types = ("id", "v0"), ("INT64", "DATE")
    path = source(tmp_path, kind, [(1, temporal_json_value(VALUES[0]))], names)
    importer = {"csv": import_csv, "jsonl": import_jsonl, "sqlite": import_sqlite}[kind]
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            with pytest.raises(GrafxQueryBudgetExceeded):
                importer(tx, "CREATE(:N {id:$id,v0:$v0})", path,
                         **options(tmp_path, kind, names, types, max_field_bytes=20))
            assert tx.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)
