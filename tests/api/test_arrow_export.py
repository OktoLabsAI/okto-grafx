"""Optional Arrow export preserves types, snapshots, budgets and explicit ownership."""

import importlib.util

import pytest

from okto_grafx import connect, QueryResult, Timestamp
from okto_grafx.domain.model.value import Uuid
from okto_grafx.arrow import to_arrow_batches
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded, GrafxUnsupportedOperation

pytestmark = [pytest.mark.optional_dependency("pyarrow"),
              pytest.mark.skipif(importlib.util.find_spec("pyarrow") is None, reason="optional pyarrow absent")]


def test_exact_scalar_types_nulls_and_detached_ownership():
    result = QueryResult(columns=("i", "s", "b", "t", "u", "d", "bool"),
                         rows=((2**63 - 1, "á", b"\x00", Timestamp(-1), Uuid(bytes(16)), 1.5, True),
                               (None,) * 7))
    batches = list(to_arrow_batches(result, types=("INT64", "STRING", "BYTES", "TIMESTAMP", "UUID", "DOUBLE", "BOOL"), batch_rows=1))
    assert len(batches) == 2 and batches[0].column(0).to_pylist() == [2**63 - 1]
    assert batches[0].column(3).cast("int64").to_pylist() == [-1]
    assert batches[0].column(4).to_pylist() == [bytes(16)]
    assert batches[1].column(1).null_count == 1
    assert batches[0].schema.field(4).metadata[b"grafx.type"] == b"UUID"


def test_cursor_snapshot_foreign_writer_and_early_close(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,PRIMARY KEY(id))")
            for i in range(5):
                tx.execute("CREATE (:D {id:$id})", {"id": i})
        with db.query("MATCH (d:D) RETURN d.id ORDER BY d.id").cursor(batch_size=2) as cursor:
            batches = to_arrow_batches(cursor, types=("INT64",), batch_rows=2)
            first = next(batches)
            with connect(path) as writer, writer.begin() as tx:
                tx.execute("CREATE (:D {id:9})")
            assert [value for batch in batches for value in batch.column(0).to_pylist()] == [2, 3, 4]
        assert first.column(0).to_pylist() == [0, 1]
        with db.query("MATCH (d:D) RETURN d.id").cursor() as cursor:
            with pytest.raises(GrafxQueryBudgetExceeded):
                next(to_arrow_batches(cursor, types=("INT64",), max_batch_bytes=1))
        assert db.execute("RETURN 1").rows == ((1,),)


def test_refusals_no_coercion_and_empty_result():
    result = QueryResult(columns=("v",), rows=((True,),))
    with pytest.raises(GrafxUnsupportedOperation):
        list(to_arrow_batches(result, types=("INT64",)))
    for options in ({"types": ("MAP",)}, {"types": ("BOOL",), "batch_rows": True}):
        with pytest.raises(GrafxConfigurationError):
            list(to_arrow_batches(result, **options))
    assert list(to_arrow_batches(QueryResult(columns=("v",)), types=("STRING",))) == []
