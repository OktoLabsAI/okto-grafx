"""Projected pages preserve identity, validation and one-shot snapshot cursors."""

import pytest

from okto_grafx import connect, CancellationToken
from okto_grafx.errors import GrafxError, GrafxQueryBudgetExceeded, GrafxQueryCancelled, GrafxTransactionStateError
from okto_grafx.projections import project_graph, ProjectionLimits


def seed(db, count=20):
    with db.begin() as tx:
        tx.execute("CREATE VECTOR SPACE emb {dimension:2,metric:'cosine'}")
        tx.execute("CREATE NODE TABLE N(id INT64,body STRING,v VECTOR(emb),PRIMARY KEY(id))")
        for i in range(count):
            tx.execute("CREATE (:N {id:$i,body:$body,v:[1.0,0.0]})", {"i": i, "body": "x" * 600})


def test_projection_order_cursor_binding_and_old_snapshot(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        seed(db)
        with db.begin("read") as reader:
            first = reader.scan_rows_v1("N", limit=3, columns=("body", "id"), max_batch_bytes=200000)
            assert first.rows[0].values == ("x" * 600, 0)
            with pytest.raises(GrafxTransactionStateError):
                reader.scan_rows_v1("N", limit=3, cursor=first.next_cursor, columns=("id",))
            with connect(path) as writer, writer.begin() as tx:
                tx.execute("CREATE (:N {id:99,body:'later',v:[0.0,1.0]})")
            second = reader.scan_rows_v1("N", limit=100, cursor=first.next_cursor, columns=("body", "id"))
            assert len(first.rows) + len(second.rows) == 20
            with pytest.raises(GrafxTransactionStateError):
                reader.scan_rows_v1("N", limit=100, cursor=first.next_cursor, columns=("body", "id"))
            assert len(reader.scan_rows_v1("N", limit=100, columns=()).rows) == 20


def test_batch_calls_and_no_vector_materialization(tmp_path, monkeypatch):
    from okto_grafx.domain.model.value import VectorValue
    with connect(tmp_path / "db") as db:
        seed(db)
        def forbidden(*args, **kwargs):
            raise AssertionError("omitted vector materialized")
        monkeypatch.setattr(VectorValue, "_from_decoded", forbidden)
        graph = project_graph(db, node_tables=("N",), limits=ProjectionLimits(batch_rows=8))
        assert len(graph.nodes) == 20
        assert graph.diagnostics.scan_calls == 3
        assert graph.diagnostics.max_batch_rows == 8


def test_byte_bounds_control_and_omitted_corruption(tmp_path, monkeypatch):
    from okto_grafx.engine.heap_store import HeapStore
    with connect(tmp_path / "db") as db:
        seed(db, 2)
        with db.begin("read") as reader:
            with pytest.raises(GrafxQueryBudgetExceeded):
                reader.scan_rows_v1("N", limit=10, columns=(), max_batch_bytes=1)
            page = reader.scan_rows_v1("N", limit=10, columns=(), max_batch_bytes=60000)
            assert len(page.rows) == 1 and page.next_cursor is not None
            token = CancellationToken()
            token.cancel()
            with pytest.raises(GrafxQueryCancelled):
                reader.scan_rows_v1("N", limit=1, columns=(), cancellation=token)
            original = HeapStore._validated_payload
            def truncated(self, *args, **kwargs):
                return original(self, *args, **kwargs)[:-1]
            with monkeypatch.context() as scoped:
                scoped.setattr(HeapStore, "_validated_payload", truncated)
                with pytest.raises(GrafxError):
                    reader.scan_rows_v1("N", limit=1, columns=())
            assert reader.execute("MATCH (n:N) RETURN count(n)").rows == ((2,),)


@pytest.mark.parametrize("capture", [False, True])
def test_deadline_during_payload_decode_refuses_without_closing_reader(tmp_path, monkeypatch, capture):
    from okto_grafx.engine.heap_store import HeapStore
    from okto_grafx.errors import GrafxQueryDeadlineExceeded
    with connect(tmp_path / "db") as db:
        seed(db, 2)
        with db.begin("read") as reader:
            now = [100.0]
            original = HeapStore._decode_version_with_header
            def advance(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                now[0] += 2.0
                return result
            with monkeypatch.context() as scoped:
                scoped.setattr(type(db._clock), "monotonic", lambda self: now[0])
                scoped.setattr(HeapStore, "_decode_version_with_header", advance)
                with pytest.raises(GrafxQueryDeadlineExceeded):
                    if capture:
                        project_graph(db, reader, node_tables=("N",), timeout_seconds=1.0)
                    else:
                        reader.scan_rows_v1("N", limit=2, columns=(), timeout_seconds=1.0)
            assert reader.execute("MATCH (n:N) RETURN count(n)").rows == ((2,),)
