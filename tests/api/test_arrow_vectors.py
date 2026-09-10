"""Explicit Arrow vectors preserve native identity and atomic write admission."""

import pytest

from okto_grafx import connect, QueryResult
from okto_grafx.arrow import ArrowVectorType, import_arrow_batches, to_arrow_batches
from okto_grafx.domain.model.value import VectorValue
from okto_grafx.errors import GrafxError, GrafxQueryBudgetExceeded

pytestmark = pytest.mark.optional_dependency("pyarrow")
pa = pytest.importorskip("pyarrow")


def schema(db, dtype="float32"):
    with db.begin() as tx:
        tx.execute(f"CREATE VECTOR SPACE emb {{dimension:2,metric:'cosine',storage_dtype:'{dtype}'}}")
        tx.execute("CREATE NODE TABLE N(id INT64,v VECTOR(emb),PRIMARY KEY(id))")
    return ArrowVectorType(db.catalog.catalog.space("emb").space_id, 2, dtype)


@pytest.mark.parametrize("dtype", ["float32", "float64"])
def test_vector_roundtrip_null_precision_and_reopen(tmp_path, dtype):
    path = tmp_path / "db"
    with connect(path) as db:
        kind = schema(db, dtype)
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:1,v:[0.1,0.2]})")
            tx.execute("CREATE (:N {id:2})")
        result = db.execute("MATCH (n:N) RETURN n.id AS id,n.v AS v ORDER BY n.id")
        batches = tuple(to_arrow_batches(result, types=("INT64", kind), batch_rows=1))
        assert batches[0].schema.field("v").type == pa.list_(pa.float32() if dtype == "float32" else pa.float64(), 2)
        with db.begin() as tx:
            report = import_arrow_batches(tx, "CREATE (:N {id:$id+10,v:$v})", batches, types=("INT64", kind))
            assert report.statements == 2
        stored = db.execute("MATCH (n:N) WHERE n.id>10 RETURN n.v ORDER BY n.id").rows
        assert stored == tuple((row[1],) for row in result.rows)
        assert not db.verify().findings
    with connect(path) as db:
        assert db.execute("MATCH (n:N) RETURN count(n)").rows == ((4,),)
        assert db.execute("MATCH (n:N) WHERE n.id>10 RETURN n.v ORDER BY n.id").rows == stored


@pytest.mark.parametrize("fault", ["metadata", "nan", "null_component", "shape", "space", "dimension", "precision", "budget"])
def test_late_vector_failure_is_atomic(tmp_path, fault):
    with connect(tmp_path / "db") as db:
        kind = schema(db)
        good = tuple(to_arrow_batches(QueryResult(columns=("id", "v"), rows=((1, VectorValue((1., 0.), kind.space_ref)),)), types=("INT64", kind)))[0]
        field = good.schema.field("v")
        values = [float("nan"), 0.] if fault == "nan" else [None, 0.] if fault == "null_component" else [0., 1.]
        array_type = pa.list_(pa.float32()) if fault == "shape" else field.type
        metadata = None if fault == "metadata" else field.metadata
        bad = pa.RecordBatch.from_arrays([pa.array([2], type=pa.int64()), pa.array([values], type=array_type)],
            schema=pa.schema([good.schema.field("id"), pa.field("v", array_type, metadata=metadata)]))
        target = "N"
        if fault in ("space", "dimension", "precision"):
            dim = 3 if fault == "dimension" else 2
            dtype = "float64" if fault == "precision" else "float32"
            with db.begin() as tx:
                tx.execute(f"CREATE VECTOR SPACE other {{dimension:{dim},metric:'cosine',storage_dtype:'{dtype}'}}")
                tx.execute("CREATE NODE TABLE Other(id INT64,v VECTOR(other),PRIMARY KEY(id))")
            target = "Other"
        with db.begin() as tx:
            tx.execute(f"CREATE (:{target} {{id:99}})")
            with pytest.raises(GrafxError):
                import_arrow_batches(tx, f"CREATE (:{target} {{id:$id,v:$v}})", (good, bad), types=("INT64", kind),
                                     max_rows=1 if fault == "budget" else 10)
            assert tx.execute(f"MATCH (n:{target}) RETURN n.id").rows == ((99,),)
        assert not db.verify().findings


def test_export_refuses_mismatched_descriptor_and_budget():
    result = QueryResult(columns=("v",), rows=((VectorValue((1., 0.), 1),),))
    for kind in (ArrowVectorType(2, 2), ArrowVectorType(1, 3), ArrowVectorType(1, 2, "float64")):
        with pytest.raises(GrafxError):
            tuple(to_arrow_batches(result, types=(kind,)))
    with pytest.raises(GrafxQueryBudgetExceeded):
        tuple(to_arrow_batches(result, types=(ArrowVectorType(1, 2),), max_batch_bytes=1))


@pytest.mark.parametrize("value", [VectorValue((1.,), 1), VectorValue((1., 0.), 2), VectorValue((1., 0.), 1, "float64")])
def test_native_vector_parameter_obeys_target_space(tmp_path, value):
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:99})")
            with pytest.raises(GrafxError):
                tx.executemany("CREATE (:N {id:$id,v:$v})", [{"id": 1, "v": VectorValue((1., 0.), 1)}, {"id": 2, "v": value}])
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((99,),)
