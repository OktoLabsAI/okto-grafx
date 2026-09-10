"""Strict Arrow-backed DataFrame contracts and atomic native staging."""

import pytest
import math

from okto_grafx import connect, QueryResult, VectorValue, Timestamp
from okto_grafx.arrow import ArrowVectorType
from okto_grafx.tabular import to_pandas, import_pandas
from okto_grafx.errors import GrafxError, GrafxUnsupportedOperation, GrafxQueryBudgetExceeded

pytestmark = [pytest.mark.optional_dependency("pyarrow"), pytest.mark.optional_dependency("pandas")]
pa = pytest.importorskip("pyarrow")
pd = pytest.importorskip("pandas")


def test_nullable_scalar_vector_frame_roundtrip():
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE VECTOR SPACE emb {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE N(id INT64,v VECTOR(emb),f DOUBLE,t TIMESTAMP,PRIMARY KEY(id))")
        kinds = ("INT64", ArrowVectorType(db.catalog.catalog.space("emb").space_id, 2), "DOUBLE", "TIMESTAMP")
        result = QueryResult(columns=("id", "v", "f", "t"), rows=((1, VectorValue((1., 0.), 1), 1.5, Timestamp(-1)), (2, None, None, None)))
        frame = to_pandas(result, types=kinds, batch_rows=1)
        assert all(type(dtype) is pd.ArrowDtype for dtype in frame.dtypes)
        with db.begin() as tx:
            report = import_pandas(tx, "CREATE (:N {id:$id,v:$v,f:$f,t:$t})", frame, types=kinds, max_batch_rows=1)
            assert report.statements == 2
        assert db.execute("MATCH (n:N) RETURN n.id,n.v,n.f,n.t ORDER BY n.id").rows == result.rows
        empty = to_pandas(QueryResult(columns=("id", "v", "f", "t")), types=kinds)
        assert len(empty) == 0 and empty.attrs["grafx.arrow_schema"] == frame.attrs["grafx.arrow_schema"]


def test_frame_refusals_nan_does_not_become_null_and_late_failure_atomicity():
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,f DOUBLE,PRIMARY KEY(id))")
        frame = pd.DataFrame({"id": pd.Series([1, 2], dtype=pd.ArrowDtype(pa.int64())),
                              "f": pd.Series(pd.arrays.ArrowExtensionArray(pa.array([1., float('nan')], from_pandas=False)))})
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:99})")
            failing = frame.copy()
            failing["id"] = pd.Series([1, 99], dtype=pd.ArrowDtype(pa.int64()))
            with pytest.raises(GrafxError):
                import_pandas(tx, "CREATE (:N {id:$id,f:$f})", failing, types=("INT64", "DOUBLE"), max_batch_rows=1)
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((99,),)
            with pytest.raises(GrafxUnsupportedOperation):
                import_pandas(tx, "CREATE (:N {id:$id})", pd.DataFrame({"id": [1]}), types=("INT64",))
        with db.begin() as tx:
            import_pandas(tx, "CREATE (:N {id:$id,f:$f})", frame, types=("INT64", "DOUBLE"), max_batch_rows=1)
        assert math.isnan(db.execute("MATCH (n:N) WHERE n.id=2 RETURN n.f").rows[0][0])
        source = QueryResult(columns=("id",), rows=((1,), (2,)))
        with pytest.raises(GrafxQueryBudgetExceeded):
            to_pandas(source, types=("INT64",), max_rows=1)
        with pytest.raises(GrafxQueryBudgetExceeded):
            to_pandas(source, types=("INT64",), max_bytes=1)


def test_vector_frame_metadata_cannot_be_silently_remapped():
    frame = to_pandas(QueryResult(columns=("v",), rows=((VectorValue((1., 0.), 1),),)), types=(ArrowVectorType(1, 2),))
    with connect(":memory:") as db, db.begin() as tx:
        with pytest.raises(GrafxUnsupportedOperation):
            import_pandas(tx, "CREATE (:N {v:$v})", frame, types=(ArrowVectorType(2, 2),))
        frame.attrs.clear()
        with pytest.raises(GrafxUnsupportedOperation):
            import_pandas(tx, "CREATE (:N {v:$v})", frame, types=(ArrowVectorType(1, 2),))
