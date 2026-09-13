"""Projection batches and optional metadata-bearing Polars round trips."""

from dataclasses import replace
import math

import pytest

from okto_grafx import connect, QueryResult, VectorValue
from okto_grafx.arrow import ArrowVectorType
from okto_grafx.graph_interop import projection_arrow_batches
from okto_grafx.polars import to_polars, import_polars, PolarsFrame
from okto_grafx.errors import (
    GrafxError,
    GrafxConfigurationError,
    GrafxQueryBudgetExceeded,
    GrafxUnsupportedOperation,
)
from tests.api.test_projection_algorithms import picture

pytestmark = pytest.mark.optional_dependency("pyarrow")
pa = pytest.importorskip("pyarrow")


def test_projection_export_identity_results_limits():
    g = replace(picture(3, [(0, 1), (0, 1), (2, 2)]), weights=(1.0, 2.0, 0.0))
    batches = list(projection_arrow_batches(g, kind="edges", batch_rows=1))
    assert len(batches) == 3
    assert (
        batches[0].schema.metadata[b"grafx.snapshot_lsn"]
        == str(g.snapshot_lsn).encode()
    )
    assert batches[0].column("source_record_id")[0].as_py() == str(g.nodes[0].record_id)
    assert batches[1].column("weight")[0].as_py() == 2.0
    nodes = list(projection_arrow_batches(g, results=g.k_core(), result_type="INT64"))
    assert nodes[0].column("result").to_pylist() == list(g.k_core())
    with pytest.raises(GrafxConfigurationError):
        list(projection_arrow_batches(g, results=(1,)))
    with pytest.raises(GrafxUnsupportedOperation):
        list(projection_arrow_batches(g, results=(True, 2.0, 3.0)))
    with pytest.raises(GrafxQueryBudgetExceeded):
        list(projection_arrow_batches(g, max_batch_bytes=1))


@pytest.mark.optional_dependency("polars")
def test_polars_scalar_vector_null_nan_and_atomic_import():
    pl = pytest.importorskip("polars")
    kinds = ("INT64", ArrowVectorType(1, 2), "DOUBLE", "STRING", "UUID")
    from okto_grafx.domain.model.value import Uuid

    source = QueryResult(
        ("id", "v", "f", "s", "u"),
        (
            (1, VectorValue((1.0, 0.0), 1), float("nan"), "hi", Uuid(b"a" * 16)),
            (2, None, None, None, None),
        ),
    )
    frame = to_polars(source, types=kinds, batch_rows=1)
    assert type(frame.frame) is pl.DataFrame
    assert math.isnan(frame.frame[0, "f"]) and frame.frame[1, "f"] is None
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE VECTOR SPACE emb {dimension:2,metric:'cosine'}")
            tx.execute(
                "CREATE NODE TABLE N(id INT64,v VECTOR(emb),f DOUBLE,s STRING,u UUID,PRIMARY KEY(id))"
            )
        with db.begin() as tx:
            with pytest.raises(GrafxError):
                import_polars(tx, "CREATE (:N {id:$id,v:$v,f:$f,s:$s,u:$u})",
                              frame, types=kinds, max_batch_rows=1)
            assert tx.execute("MATCH (n:N) RETURN count(n)").rows == ((0,),)
        finite_source = QueryResult(source.columns,
            ((1, source.rows[0][1], 1.5, "hi", source.rows[0][4]), source.rows[1]))
        finite_frame = to_polars(finite_source, types=kinds, batch_rows=1)
        with db.begin() as tx:
            assert (
                import_polars(
                    tx,
                    "CREATE (:N {id:$id,v:$v,f:$f,s:$s,u:$u})",
                    finite_frame,
                    types=kinds,
                    max_batch_rows=1,
                ).statements
                == 2
            )
        actual = db.execute(
            "MATCH (n:N) RETURN n.id,n.v,n.f,n.s,n.u ORDER BY n.id"
        ).rows
        assert actual[0] == finite_source.rows[0]
        assert actual[1] == source.rows[1]
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:99})")
            duplicate = to_polars(QueryResult(("id",), ((3,), (99,))), types=("INT64",))
            with pytest.raises(GrafxError):
                import_polars(
                    tx,
                    "CREATE (:N {id:$id})",
                    duplicate,
                    types=("INT64",),
                    max_batch_rows=1,
                )
            assert tx.execute("MATCH (n:N) WHERE n.id=3 RETURN n.id").rows == ()
            with pytest.raises(GrafxConfigurationError):
                import_polars(tx, "CREATE (:N {id:$id})", frame.frame, types=kinds)
            bad = PolarsFrame(pl.DataFrame({"id": [True]}), duplicate.arrow_schema)
            with pytest.raises(GrafxUnsupportedOperation):
                import_polars(tx, "CREATE (:N {id:$id})", bad, types=("INT64",))
    assert to_polars(QueryResult(("id",)), types=("INT64",)).frame.height == 0
    with pytest.raises(GrafxQueryBudgetExceeded):
        to_polars(source, types=kinds, max_rows=1)
    with pytest.raises(GrafxQueryBudgetExceeded):
        to_polars(source, types=kinds, max_bytes=1)


@pytest.mark.optional_dependency("polars")
def test_polars_empty_dtype_and_vector_metadata_refuse():
    pl = pytest.importorskip("polars")
    vector = to_polars(QueryResult(("v",), ((VectorValue((1., 0.), 1),),)), types=(ArrowVectorType(1, 2),))
    empty = to_polars(QueryResult(("id",)), types=("INT64",))
    wrong = PolarsFrame(pl.DataFrame({"id": pl.Series([], dtype=pl.Boolean)}), empty.arrow_schema)
    with connect(":memory:") as db, db.begin() as tx:
        with pytest.raises(GrafxUnsupportedOperation):
            import_polars(tx, "CREATE (:N {v:$v})", vector, types=(ArrowVectorType(2, 2),))
        # Reuse a valid schema statement so native statement validation cannot mask dtype admission.
        tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
        with pytest.raises(GrafxUnsupportedOperation):
            import_polars(tx, "CREATE (:N {id:$id})", wrong, types=("INT64",))
        assert import_polars(tx, "CREATE (:N {id:$id})", empty, types=("INT64",)).statements == 0
