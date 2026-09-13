"""Numeric procedure unions and binary64 widening retain native validation and rollback."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxConfigurationError, GrafxParseError, GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.extensions import ExtensionRegistry, ScalarFunction, TabularProcedure


def echo(kind, seen, **budgets):
    """Expose callback input types as well as returned native numeric values."""
    def callback(value):
        seen.append(value)
        return ((value,),)
    return TabularProcedure("app.echo", (kind,), (("value", kind),), callback,
                             argument_names=("input",), **budgets)


@pytest.mark.parametrize("kind", ("NUMBER", "DOUBLE"))
@pytest.mark.parametrize("value", (None, 0, -3, 2.5, 2**53 + 1, -(2**63), 2**63 - 1))
def test_numeric_inputs_use_declared_union_or_widening_semantics(kind, value):
    seen = []
    proc = echo(kind, seen)
    expected = float(value) if kind == "DOUBLE" and type(value) is int else value
    assert tuple(proc.invoke((value,))) == ((expected,),)
    assert type(seen[-1]) is type(expected)
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        for query in ("CALL app.echo($input)", "CALL app.echo"):
            result = db.execute(query, {"input": value})
            assert result.rows == ((expected,),)
            assert type(result.rows[0][0]) is type(expected)
            assert type(seen[-1]) is type(expected)


@pytest.mark.parametrize("kind", ("NUMBER", "DOUBLE"))
@pytest.mark.parametrize("value", (True, "1", 2**63, -(2**63)-1, float("nan"), float("inf"), float("-inf")))
def test_numeric_inputs_refuse_booleans_out_of_range_and_nonfinite_values(kind, value):
    seen = []
    proc = echo(kind, seen)
    with pytest.raises(GrafxPlanError) as direct_failure:
        tuple(proc.invoke((value,)))
    assert direct_failure.value.details["field"] == "udf_type"
    assert seen == []
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        error_type = GrafxConfigurationError if type(value) is int else GrafxPlanError
        with pytest.raises(error_type) as failure:
            db.execute("CALL app.echo($input)", {"input": value})
        assert type(failure.value) is error_type
        expected_field = ("parameters.input" if type(value) is int else
                          "udf_type" if type(value) is float else "procedure_type")
        assert failure.value.details["field"] == expected_field
        assert seen == []


@pytest.mark.parametrize("kind", ("NUMBER", "DOUBLE"))
def test_output_widening_and_number_kind_preservation(kind):
    proc = TabularProcedure("app.rows", (), (("value", kind),), lambda: ((1,), (2.5,), (None,)))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        result = db.execute("CALL app.rows()")
        assert result.rows == ((1,), (2.5,), (None,))
        assert type(result.rows[0][0]) is (int if kind == "NUMBER" else float)
        assert type(result.rows[1][0]) is float


@pytest.mark.parametrize("query,expected", (
    ("CALL app.rows() YIELD value RETURN sum(value), avg(value)", ((4.5, 1.5),)),
    ("CALL app.rows() YIELD value RETURN DISTINCT value ORDER BY value", ((1,), (2.5,), (None,))),
    ("CALL app.rows() YIELD value WITH value AS n RETURN n + 1 ORDER BY n", ((2,), (2.0,), (3.5,), (None,))),
    ("CALL { CALL app.rows() YIELD value RETURN value } RETURN count(value)", ((3,),)),
    ("CALL app.rows() YIELD value RETURN value UNION RETURN 1.0 AS value", ((1,), (2.5,), (None,))),
))
def test_number_outputs_flow_through_runtime_typing_and_composition(query, expected):
    proc = TabularProcedure("app.rows", (), (("value", "NUMBER"),), lambda: ((1,), (1.0,), (2.5,), (None,)))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        assert db.execute(query).rows == expected
        with db.query(query).cursor(batch_size=1) as cursor:
            assert tuple(cursor) == expected


@pytest.mark.parametrize("bad", (True, "1", float("nan"), float("inf"), 2**63))
@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_unselected_bad_numeric_output_rolls_back_the_statement(tmp_path, bad, codec):
    def rows():
        yield "first", 1
        yield "bad", bad
    proc = TabularProcedure("app.rows", (), (("name", "STRING"), ("value", "NUMBER")), rows)
    path = tmp_path / "numeric"
    with connect(path, codec=codec, extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 1})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CREATE (:N {id: 2}) WITH 1 AS keep CALL app.rows() YIELD name RETURN name")
            tx.execute("CREATE (:N {id: 3})")
        assert db.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
        db.verify()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
        db.verify()


@pytest.mark.parametrize("kind", ("NUMBER", "DOUBLE"))
@pytest.mark.parametrize("option", ({"max_value_bytes": 1}, {"max_result_bytes": 1}))
def test_numeric_signature_budgets_are_not_bypassed(kind, option):
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(echo(kind, [], **option),))) as db:
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("CALL app.echo(1)")


def test_number_is_not_a_scalar_udf_or_storage_type_and_strict_scalar_double_is_unchanged():
    with pytest.raises(GrafxConfigurationError):
        ScalarFunction("app.scalar", ("NUMBER",), "NUMBER", lambda value: value)
    scalar = ScalarFunction("app.scalar", ("DOUBLE",), "DOUBLE", lambda value: value)
    with pytest.raises(GrafxPlanError):
        scalar.invoke((1,))
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxParseError) as failure:
                tx.execute("CREATE NODE TABLE T(value NUMBER)")
            assert getattr(failure.value, "code", None) is not None


def test_number_preserves_integers_for_typed_storage(tmp_path):
    proc = TabularProcedure("app.rows", (), (("value", "NUMBER"),), lambda: ((2**53 + 1,),))
    with connect(tmp_path / "typed", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T(value INT64)")
            tx.execute("CALL app.rows() YIELD value CREATE (:T {value: value})")
        assert db.execute("MATCH (n:T) RETURN n.value").rows == ((2**53 + 1,),)
