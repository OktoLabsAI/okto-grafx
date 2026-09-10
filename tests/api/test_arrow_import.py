"""Optional scalar import is one native staging savepoint across input batches."""

import pytest

from okto_grafx import connect, QueryResult
from okto_grafx.arrow import import_arrow_batches, to_arrow_batches
from okto_grafx.domain.model.value import Timestamp, Uuid
from okto_grafx.errors import GrafxUnsupportedOperation, GrafxQueryBudgetExceeded, GrafxConfigurationError

pytestmark = pytest.mark.optional_dependency("pyarrow")
pa = pytest.importorskip("pyarrow")


def test_scalar_roundtrip_and_caller_owned_commit(tmp_path):
    names = ("id", "s", "flag", "f", "raw", "time", "uuid")
    kinds = ("INT64", "STRING", "BOOL", "DOUBLE", "BYTES", "TIMESTAMP", "UUID")
    rows = ((1, "α", True, 1.5, b"abc", Timestamp(-10), Uuid(b"x" * 16)),
            (2, None, None, None, None, None, None))
    batches = tuple(to_arrow_batches(QueryResult(columns=names, rows=rows), types=kinds, batch_rows=1))
    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,s STRING,flag BOOL,f DOUBLE,raw BLOB,time TIMESTAMP,uuid UUID,PRIMARY KEY(id))")
        with db.begin() as tx:
            import_arrow_batches(tx, "CREATE (:D {id:$id,s:$s,flag:$flag,f:$f,raw:$raw,time:$time,uuid:$uuid})", batches, types=kinds)
            assert tx.execute("MATCH (d:D) RETURN count(d)").rows == ((2,),)
            assert db.execute("MATCH (d:D) RETURN count(d)").rows == ((0,),)
        assert db.execute("MATCH (d:D) RETURN d.id,d.s,d.flag,d.f,d.raw,d.time,d.uuid ORDER BY d.id").rows == rows
        assert not db.verify().findings


@pytest.mark.parametrize("fault", ["type", "budget", "names", "iterator"])
def test_late_failure_discards_whole_call_preserves_prior_staging(tmp_path, fault):
    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,PRIMARY KEY(id))")
        good = pa.record_batch([pa.array([1], type=pa.int64())], names=["id"])
        bad = (pa.record_batch([pa.array(["x"])], names=["id"]) if fault == "type" else
               pa.record_batch([pa.array([2], type=pa.int64())], names=["other"]) if fault == "names" else good)
        def source():
            yield good
            if fault == "iterator":
                raise GrafxConfigurationError("input failure", field="source")
            yield bad
        with db.begin() as tx:
            tx.execute("CREATE (:D {id:99})")
            with pytest.raises((GrafxUnsupportedOperation, GrafxQueryBudgetExceeded, GrafxConfigurationError)):
                import_arrow_batches(tx, "CREATE (:D {id:$id})", source(), types=("INT64",),
                                     max_rows=1 if fault == "budget" else 10)
            assert tx.execute("MATCH (d:D) RETURN d.id").rows == ((99,),)
        assert db.execute("MATCH (d:D) RETURN d.id").rows == ((99,),)


@pytest.mark.parametrize("fault", ["bytes", "batches", "rows", "statement", "metadata", "empty"])
def test_batch_boundaries_and_empty_input(tmp_path, fault):
    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,PRIMARY KEY(id))")
        good = pa.record_batch([pa.array([1], type=pa.int64())], names=["id"])
        empty = pa.record_batch([pa.array([], type=pa.int64())], names=["id"])
        wrong = good.replace_schema_metadata(None)
        wrong = pa.RecordBatch.from_arrays(wrong.columns, schema=pa.schema([
            pa.field("id", pa.int64(), metadata={b"grafx.type": b"STRING"})]))
        with db.begin() as tx:
            tx.execute("CREATE (:D {id:99})")
            if fault == "empty":
                assert import_arrow_batches(tx, "CREATE (:D {id:$id})", (empty,), types=("INT64",)).statements == 0
            else:
                batches = (good, wrong) if fault == "metadata" else (good, good)
                options = ({"max_batch_bytes": 1} if fault == "bytes" else
                           {"max_batches": 1} if fault == "batches" else
                           {"max_rows": 1} if fault == "rows" else {})
                from okto_grafx.errors import GrafxError
                with pytest.raises(GrafxError):
                    import_arrow_batches(tx, "CREATE (:D {id:$id})", batches, types=("INT64",), **options)
            assert tx.execute("MATCH (d:D) RETURN d.id").rows == ((99,),)
