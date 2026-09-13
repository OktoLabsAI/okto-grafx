"""External sort preserves WITH scope and native authority without promoting DTOs."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxCorruptionDetected, GrafxError
from okto_grafx.engine.query_engine import _decode_sort_row, _spill_encode


@pytest.mark.parametrize("modifier", ["", "DISTINCT "])
@pytest.mark.parametrize("operation", ["SET n.v=9", "SET n += {v:9}", "DETACH DELETE n"])
@pytest.mark.parametrize("committed", [False, True])
def test_direct_bound_writes_after_external_sort(tmp_path, modifier, operation, committed):
    with connect(tmp_path / "db", query_memory_budget_bytes=8192) as db:
        if committed:
            with db.begin("write") as tx:
                tx.execute("UNWIND [1,2,3] AS i CREATE(:N {id:i,v:i})")
        with db.begin("write") as tx:
            if not committed:
                tx.execute("UNWIND [1,2,3] AS i CREATE(:N {id:i,v:i})")
            result = tx.execute(f"MATCH(n:N) WITH {modifier}n ORDER BY n.id DESC "
                                f"SKIP 1 LIMIT 1 {operation} RETURN n.id" if operation.startswith("SET") else
                                f"MATCH(n:N) WITH {modifier}n ORDER BY n.id DESC SKIP 1 LIMIT 1 {operation}")
            if operation.startswith("SET"):
                assert result.rows == ((2,),)
        expected = ((1, 1), (2, 9), (3, 3)) if operation.startswith("SET") else ((1, 1), (3, 3))
        assert db.execute("MATCH(n:N) RETURN n.id,n.v ORDER BY n.id").rows == expected
        assert db.verify("all").findings == ()


def test_sort_does_not_promote_scalar_map_to_write_authority(tmp_path):
    with connect(tmp_path / "db", query_memory_budget_bytes=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:N {id:1})")
            with pytest.raises(GrafxError):
                tx.execute("MATCH(n:N) WITH {id:n.id} AS n ORDER BY n.id SET n.id=9")
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((1,),)


@pytest.mark.parametrize("names", [("missing",), ("n", "n"), (1,), None])
def test_corrupt_binding_inventory_is_refused(names):
    class ScalarCodec:
        def _restore(self, value):
            return value
    payload = _spill_encode(("sorted-row", (("n", 1),), names), key=False)
    with pytest.raises(GrafxCorruptionDetected):
        _decode_sort_row(payload, ScalarCodec())
