"""Native DECIMAL procedure signatures, ownership, queries, writes and recovery."""

from dataclasses import replace
from decimal import Decimal

import pytest

from okto_grafx import connect, DecimalValue
from okto_grafx.errors import GrafxError, GrafxPlanError, GrafxQueryBudgetExceeded, GrafxConfigurationError, GrafxTransactionStateError
from okto_grafx.extensions import ExtensionRegistry, ScalarFunction, TabularProcedure
from tests.api.test_procedure_native_values import echo, registered


VALUE = DecimalValue(-(10**38 - 1), 38, 19)


@pytest.mark.parametrize("signature", ("DECIMAL", "NUMBER"))
def test_decimal_signature_preserves_type_ownership_null_and_native_composition(signature):
    seen = []
    proc = echo(signature, seen)
    native = tuple(proc.invoke((VALUE,)))[0][0]
    assert native == VALUE and native is not VALUE and seen[-1] is not VALUE
    with connect(":memory:", extensions=registered(proc)) as db:
        queries = ("CALL app.echo($input)", "CALL app.echo",
                   "CALL app.echo(decimal('-9999999999999999999.9999999999999999999',38,19))")
        for query in queries:
            assert db.execute(query, {"input": VALUE}).rows == ((VALUE,),)
        assert db.execute("CALL app.echo(null)").rows == ((None,),)
        assert db.execute("CALL app.echo($input) YIELD value RETURN value UNION RETURN $input AS value",
                          {"input": VALUE}).rows == ((VALUE,),)
        with db.query("CALL app.echo($input)", {"input": VALUE}).cursor(batch_size=1) as cursor:
            assert tuple(cursor) == ((VALUE,),)
        assert not db._catalog.catalog.requires_capability("decimal_values_v1")


@pytest.mark.parametrize("signature,value", (("ANY", VALUE), ("LIST", [VALUE, {"v": VALUE}]),
                                            ("MAP", {"v": [VALUE]})))
def test_nested_decimal_values_are_defensively_owned(signature, value):
    seen = []
    proc = echo(signature, seen)
    result = tuple(proc.invoke((value,)))[0][0]
    leaf = result if signature == "ANY" else result[0] if signature == "LIST" else result["v"][0]
    observed = seen[-1] if signature == "ANY" else seen[-1][0] if signature == "LIST" else seen[-1]["v"][0]
    assert leaf == VALUE and leaf is not VALUE and leaf is not observed
    object.__setattr__(observed, "coefficient", 0)
    assert leaf == VALUE
    if signature == "LIST":
        assert result[1]["v"] is not leaf


@pytest.mark.parametrize("bad", (1, 1.25, True, "1.25", Decimal("1.25")))
def test_decimal_signature_does_not_implicitly_cast_host_values(bad):
    seen = []
    with pytest.raises(GrafxPlanError):
        tuple(echo("DECIMAL", seen).invoke((bad,)))
    assert not seen


@pytest.mark.parametrize("signature", ("INT64", "DOUBLE"))
def test_decimal_does_not_widen_into_existing_numeric_contracts(signature):
    seen = []
    proc = echo(signature, seen)
    with pytest.raises(GrafxPlanError):
        tuple(proc.invoke((VALUE,)))
    with connect(":memory:", extensions=registered(proc)) as db:
        for query, params in (("CALL app.echo($input)", {"input": VALUE}),
                              ("CALL app.echo(decimal('1.25',3,2))", {})):
            with pytest.raises(GrafxPlanError):
                db.execute(query, params)
    assert not seen


@pytest.mark.parametrize("signature", ("DECIMAL", "NUMBER", "ANY"))
def test_decimal_frame_charges_full_nineteen_bytes(signature):
    with pytest.raises(GrafxQueryBudgetExceeded):
        tuple(echo(signature, max_value_bytes=18).invoke((VALUE,)))
    with pytest.raises(GrafxQueryBudgetExceeded):
        tuple(echo(signature, max_result_bytes=18).invoke((VALUE,)))
    assert tuple(echo(signature, max_value_bytes=19, max_result_bytes=19).invoke((VALUE,))) == ((VALUE,),)


def test_number_union_composes_decimal_int_and_double_without_converting():
    proc = TabularProcedure("app.rows", (), (("v", "NUMBER"),),
                            lambda: ((DecimalValue(125, 3, 2),), (1.25,), (2,)))
    with connect(":memory:", extensions=registered(proc)) as db:
        assert db.execute("CALL app.rows() YIELD v RETURN DISTINCT v ORDER BY v").rows == ((DecimalValue(125, 3, 2),), (2,))
        with pytest.raises(GrafxPlanError):
            db.execute("CALL app.rows() YIELD v RETURN sum(v)")  # No implicit DOUBLE/DECIMAL aggregate coercion.
    proc = replace(proc, implementation=lambda: ((DecimalValue(125, 3, 2),), (2,)))
    with connect(":memory:", extensions=registered(proc)) as db:
        assert db.execute("CALL app.rows() YIELD v RETURN sum(v)").rows == ((DecimalValue(325, 38, 2),),)


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_decimal_reader_writer_query_and_snapshot_reopen(tmp_path, codec):
    def write(writer, value):
        return writer.query("CREATE(n:N {id:1,v:$v}) RETURN n.v", {"v": value}).rows

    def read(reader):
        return reader.query("MATCH(n:N) RETURN n.v").rows

    writer = TabularProcedure("app.store", ("DECIMAL",), (("v", "DECIMAL"),), write,
                              mode="write", required_permissions=frozenset({"graph"}))
    reader = TabularProcedure("app.read", (), (("v", "DECIMAL"),), read,
                              graph_read=True, required_permissions=frozenset({"graph"}))
    registry = ExtensionRegistry(trusted=True, procedures=(writer, reader), procedure_permissions=frozenset({"graph"}))
    with connect(tmp_path / "db", codec=codec, extensions=registry) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v DECIMAL(38,19),PRIMARY KEY(id))")
        with db.begin("read") as old:
            with db.begin() as tx:
                assert tx.execute("CALL app.store($v)", {"v": VALUE}).rows == ((VALUE,),)
                assert tx.execute("CALL app.read()").rows == ((VALUE,),)
            assert old.execute("CALL app.read()").rows == ()
        assert db.execute("CALL app.read()").rows == ((VALUE,),)
        assert not db.verify("all").findings
    with connect(tmp_path / "db", codec=codec) as db:
        assert db.execute("MATCH(n:N) RETURN n.v").rows == ((VALUE,),)


@pytest.mark.parametrize("signature", ("DECIMAL", "NUMBER", "ANY"))
def test_late_invalid_decimal_output_rolls_back_unselected_cells(signature, tmp_path):
    bad = DecimalValue(1, 1, 0)
    object.__setattr__(bad, "precision", 0)
    proc = TabularProcedure("app.rows", (), (("id", "INT64"), ("v", signature)), lambda: ((1, VALUE), (2, bad)))
    with connect(tmp_path / "db", extensions=registered(proc)) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v ANY,PRIMARY KEY(id))")
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99,v:'prior'})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.rows() YIELD id CREATE(:N {id:id})")
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert not db._catalog.catalog.requires_capability("decimal_values_v1")
        assert not db.verify("all").findings


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_decimal_procedure_write_recovers_after_durable_apply_failure(tmp_path, codec, monkeypatch):
    from okto_grafx.engine.txn_manager import TransactionManager
    with connect(tmp_path / "db", codec=codec, extensions=registered(echo("MAP"))) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v ANY,PRIMARY KEY(id))")
        tx = db.begin()
        tx.execute("CALL app.echo($input) YIELD value CREATE(:N {id:1,v:value})", {"input": {"nested": [VALUE]}})

        def fail(manager, images):
            raise RuntimeError("injected failure after durable COMMIT")

        with monkeypatch.context() as patch:
            patch.setattr(TransactionManager, "_apply_images", fail)
            with pytest.raises(GrafxTransactionStateError) as error:
                tx.commit()
            assert error.value.details["committed"] and error.value.details["durable"]
    with connect(tmp_path / "db", codec=codec) as db:
        assert db.execute("MATCH(n:N) RETURN n.v").rows == (({"nested": (VALUE,)},),)
        assert db._catalog.catalog.requires_capability("decimal_values_v1")
        assert not db.verify("all").findings


def test_no_new_scalar_udf_signature_or_parameterized_procedure_signature():
    with pytest.raises(GrafxConfigurationError):
        ScalarFunction("app.scalar", ("DECIMAL",), "DECIMAL", lambda v: v)
    with pytest.raises(GrafxError):
        echo("DECIMAL(12,4)")
