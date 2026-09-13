"""NaN is an expression DOUBLE, never a newly stored graph property."""

import math

import pytest

import okto_grafx
from okto_grafx.errors import GrafxError
from okto_grafx.domain.model.schema import ColumnDef, TableDef, encode_tuple
from okto_grafx.domain.model.value import ValueType, encode_value, decode_value


@pytest.mark.parametrize("rhs", ["1", "1.0", "0.0/0.0", "'a'"])
@pytest.mark.parametrize("family", ["equality", "ordering"])
def test_eight_required_nan_comparison_expectations(rhs, family):
    operators = ("=", "<>") if family == "equality" else (">", ">=", "<", "<=")
    query = "RETURN " + ", ".join(f"0.0/0.0 {operator} {rhs} AS v{index}" for index, operator in enumerate(operators))
    expected = (False, True) if family == "equality" else ((None,) * 4 if rhs == "'a'" else (False,) * 4)
    with okto_grafx.connect(":memory:") as db:
        assert db.execute(query).rows == (expected,)


@pytest.mark.parametrize("expression", ["0.0/0.0", "0/0.0", "-0.0/0", "-(0.0/0.0)",
    "(0.0/0.0)+1", "(0.0/0.0)*2", "(0.0/0.0)/0.0", "abs(0.0/0.0)",
    "sqrt(0.0/0.0)", "floor(0.0/0.0)", "round(0.0/0.0)"])
def test_nan_generation_and_expression_propagation(expression):
    with okto_grafx.connect(":memory:") as db:
        assert math.isnan(db.execute(f"RETURN {expression} AS n").rows[0][0])


def test_parameters_nested_results_null_and_integer_zero_contract():
    with okto_grafx.connect(":memory:") as db:
        row = db.execute("RETURN [$n,{x:0.0/0.0}] AS v, $n IS NULL AS n", {"n": math.nan}).rows[0]
        assert math.isnan(row[0][0]) and math.isnan(row[0][1]["x"]) and row[1] is False
        assert db.execute("RETURN null / 0 AS n").rows == ((None,),)
        for expression in ("0/0", "1/0", "1.0/0.0"):
            with pytest.raises(GrafxError):
                db.execute("RETURN " + expression)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("kind,wrap", [(ValueType.DOUBLE, lambda x:x),
    (ValueType.LIST, lambda x:(1,x)), (ValueType.MAP, lambda x:{"nested": (x,)})])
def test_storage_tuple_admission_is_distinct_from_transient_value_codec(value, kind, wrap):
    wrapped = wrap(value)
    # The generic codec is also used by temporary expression spill.
    raw = encode_value(wrapped)
    assert decode_value(raw)[1] == len(raw)
    table = TableDef(1, "N", "node", (ColumnDef("v", kind),))
    with pytest.raises(GrafxError):
        encode_tuple(table, (wrapped,))


@pytest.mark.parametrize("statement", [
    "CREATE (:N {id:2,f:0.0/0.0})", "CREATE (:N {id:2,f:$n})",
    "MATCH(n:N {id:1}) SET n.f=0.0/0.0", "MERGE(:N {id:2,f:0.0/0.0})",
])
def test_bad_persisted_value_rolls_back_statement_not_prior_work(tmp_path, statement):
    path = tmp_path / "db"
    with okto_grafx.connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,f DOUBLE,PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1,f:1.0})")
            with pytest.raises(GrafxError):
                tx.execute(statement, {"n": math.nan})
            assert tx.execute("MATCH(n:N) RETURN n.id,n.f").rows == ((1,1.0),)
        assert db.verify("all").findings == ()
    with okto_grafx.connect(path) as db:
        assert db.execute("MATCH(n:N) RETURN n.id,n.f").rows == ((1,1.0),)
        assert db.verify("all").findings == ()


def test_late_nan_batch_refusal_preserves_prior_work_and_reopen(tmp_path):
    path = tmp_path / "batch"
    with okto_grafx.connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,f DOUBLE,PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:99,f:9.0})")
            with pytest.raises(GrafxError):
                tx.executemany("CREATE (:N {id:$id,f:$f})",
                               [{"id": 1, "f": 1.0}, {"id": 2, "f": math.nan}])
            assert tx.execute("MATCH(n:N) RETURN n.id,n.f").rows == ((99, 9.0),)
    with okto_grafx.connect(path) as db:
        assert db.execute("MATCH(n:N) RETURN n.id,n.f").rows == ((99, 9.0),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("statement", [
    "MATCH(a:N {id:1}),(b:N {id:2}) CREATE (a)-[:R {f:0.0/0.0}]->(b)",
    "MATCH(:N)-[r:R]->(:N) SET r.f=0.0/0.0",
])
def test_relationship_nonfinite_property_refuses_without_effects(statement):
    with okto_grafx.connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N,f DOUBLE)")
            tx.execute("CREATE (:N {id:1}),(:N {id:2})")
            tx.execute("MATCH(a:N {id:1}),(b:N {id:2}) CREATE (a)-[:R {f:1.0}]->(b)")
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute(statement)
            assert tx.execute("MATCH(:N)-[r:R]->(:N) RETURN r.f").rows == ((1.0,),)
