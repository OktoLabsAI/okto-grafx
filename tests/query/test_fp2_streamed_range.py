"""UNWIND range consumes a bounded prefix without constructing the entire list."""

from types import SimpleNamespace

import pytest

from okto_grafx import connect
from okto_grafx.domain.query.ast import Literal
from okto_grafx.domain.query.plan import LimitRows, SingleRow
from okto_grafx.domain.query.scalars import range_values, scalar_value
from okto_grafx.engine import query_engine
from okto_grafx.errors import GrafxPlanError, GrafxQueryBudgetExceeded


def test_original_large_range_sum_without_scalar_materialization(monkeypatch):
    original = query_engine.scalar_value

    def refuse_range_materialization(name, *args):
        assert name != "RANGE", "UNWIND materialized its full range carrier"
        return original(name, *args)

    monkeypatch.setattr(query_engine, "scalar_value", refuse_range_materialization)
    with connect(":memory:") as db:
        assert db.execute("UNWIND range(1000000, 2000000) AS i WITH i LIMIT 3000 RETURN sum(i)").rows == (
            (3004498500,),)


@pytest.mark.parametrize("args,wanted", [
    ((1, 5, 2), (1, 3, 5)), ((5, 1, -2), (5, 3, 1)),
    ((1, 5, -1), ()), ((5, 1), ()), ((5, 5, -2), (5,)),
    ((None, 5), None), ((1, None), None), ((1, 2, None), None),
])
def test_materialized_and_streamed_range_share_inclusive_null_and_step_rules(args, wanted):
    sequence = range_values(*args)
    assert (tuple(sequence) if sequence is not None else None) == wanted
    assert scalar_value("RANGE", *args) == wanted


@pytest.mark.parametrize("args", [(1, 2, 0), (True, 2), (1.0, 2), (1, 1 << 63), (-(1 << 63) - 1, 2)])
def test_range_rejects_bad_types_zero_step_and_int64_overflow(args):
    with pytest.raises(GrafxPlanError):
        range_values(*args)


def test_extreme_span_is_lazy_and_projection_still_has_its_list_cap():
    with connect(":memory:") as db:
        assert db.execute("UNWIND range(-9223372036854775808, 9223372036854775807) AS n "
                          "RETURN n LIMIT 2").rows == ((-(1 << 63),), (-(1 << 63) + 1,))
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("RETURN range(0, 1000000)")


def test_consumed_range_cap_and_intermediate_limit_are_not_bypassed(monkeypatch):
    monkeypatch.setattr(query_engine, "MAX_GENERATED_LIST_ELEMENTS", 3)
    with connect(":memory:", max_intermediate_rows=3) as db:
        assert db.execute("UNWIND range(1,1000000) AS n WITH n LIMIT 3 RETURN n").rows == ((1,), (2,), (3,))
    with connect(":memory:") as db:
        with pytest.raises(GrafxQueryBudgetExceeded) as caught:
            db.execute("UNWIND range(1,1000000) AS n RETURN count(*)")
        assert caught.value.details["resource"] == "generated_list"
    with connect(":memory:", max_intermediate_rows=2) as db:
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("UNWIND range(1,1000000) AS n WITH n LIMIT 3 RETURN n")


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_limit_never_pulls_a_discarded_read_row_and_always_closes(limit):
    class Source:
        consumed = 0
        closed = False

        def __next__(self):
            assert self.consumed < limit, "LIMIT pulled one extra row"
            self.consumed += 1
            return query_engine._Row(bindings={"n": self.consumed})

        def close(self):
            self.closed = True

    source = Source()
    engine = SimpleNamespace(_rows=lambda *_: source)
    rows = tuple(query_engine._limit_rows(engine, LimitRows(SingleRow(), Literal(limit)), object()))
    assert len(rows) == source.consumed == limit
    assert source.closed


def test_limit_preserves_primary_failure_when_source_close_also_fails():
    primary = ValueError("read failed")

    class Source:
        def __next__(self):
            raise primary

        def close(self):
            raise RuntimeError("close failed")

    engine = SimpleNamespace(_rows=lambda *_: Source())
    with pytest.raises(ValueError) as caught:
        tuple(query_engine._limit_rows(engine, LimitRows(SingleRow(), Literal(1)), object()))
    assert caught.value is primary
    assert any("close failed" in note for note in primary.__notes__)


def test_large_range_cursor_early_close_and_budget_failure_release_transactions(monkeypatch):
    monkeypatch.setattr(query_engine, "MAX_GENERATED_LIST_ELEMENTS", 3)
    with connect(":memory:") as db:
        cursor = db.query("UNWIND range(1,1000000) AS n RETURN n").cursor(batch_size=1)
        assert cursor.fetchone() == (1,)
        cursor.close()
        assert db.transactions.open_transactions == 0
        cursor = db.query("UNWIND range(1,1000000) AS n RETURN n").cursor(batch_size=1)
        assert cursor.fetchmany(3) == ((1,), (2,), (3,))
        with pytest.raises(GrafxQueryBudgetExceeded):
            cursor.fetchone()
        assert cursor.closed
        assert db.transactions.open_transactions == 0


@pytest.mark.parametrize("limit", [0, 1])
def test_output_limit_keeps_all_preceding_writes_and_reopen(tmp_path, limit):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            result = tx.execute(f"UNWIND range(1,3) AS n CREATE (:N {{id:n}}) RETURN n LIMIT {limit}")
            assert len(result.rows) == limit
    with connect(path) as db:
        assert db.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((1,), (2,), (3,))


def test_range_cap_failure_rolls_back_every_statement_write(tmp_path, monkeypatch):
    monkeypatch.setattr(query_engine, "MAX_GENERATED_LIST_ELEMENTS", 3)
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:10})")
            with pytest.raises(GrafxQueryBudgetExceeded):
                tx.execute("UNWIND range(1,1000000) AS n CREATE (:N {id:n}) RETURN n LIMIT 1")
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((10,),)
            tx.execute("UNWIND range(1,1000000) AS n WITH n LIMIT 0 CREATE (:N {id:n})")
    with connect(path) as db:
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((10,),)
