"""Temporal, container and native-ANY procedure signatures retain ownership and durability."""

from dataclasses import replace
from datetime import date

import pytest

from okto_grafx import connect, DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue
from okto_grafx.errors import GrafxConfigurationError, GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.extensions import ExtensionRegistry, ScalarFunction, TabularProcedure
from okto_grafx.domain.model.value import Timestamp, Uuid, VectorValue, MAX_VALUE_DEPTH


TEMPORALS = (
    ("DATE", DateValue(2024, 2, 29), "date('2024-02-29')"),
    ("LOCALTIME", LocalTimeValue(1_000_000_001), "localtime('00:00:01.000000001')"),
    ("TIME", TimeValue(LocalTimeValue(1_000_000_001), 3600), "time('00:00:01.000000001+01:00')"),
    ("LOCALDATETIME", LocalDateTimeValue(DateValue(2024, 2, 29), LocalTimeValue(1)),
     "localdatetime('2024-02-29T00:00:00.000000001')"),
    ("DATETIME", DateTimeValue.from_epoch_parts(1, nanosecond=7, offset_seconds=3600),
     "datetime('1970-01-01T01:00:01.000000007+01:00')"),
    ("DURATION", DurationValue(1, 2, 3, 4), "duration({months:1,days:2,seconds:3,nanoseconds:4})"),
)


def echo(kind, seen=None, **options):
    """Expose one explicitly declared input/output family through direct and native CALL."""
    def callback(value):
        if seen is not None:
            seen.append(value)
        return ((value,),)
    return TabularProcedure("app.echo", (kind,), (("value", kind),), callback,
                             argument_names=("input",), **options)


def registered(procedure):
    """Create only the immutable per-connection allowlist needed by this test."""
    return ExtensionRegistry(trusted=True, procedures=(procedure,))


def public_value(value):
    """Spell the established public immutable-list shape independently of callback copying."""
    if type(value) in (tuple, list):
        return tuple(public_value(item) for item in value)
    if type(value) is dict:
        return {key: public_value(item) for key, item in value.items()}
    return value


@pytest.mark.parametrize("kind,value,expression", TEMPORALS)
def test_temporal_arguments_outputs_null_and_native_expressions(kind, value, expression):
    seen = []
    procedure = echo(kind, seen)
    direct = tuple(procedure.invoke((value,)))[0][0]
    assert type(direct) is type(value) and direct == value and direct is not value
    assert seen[-1] == value and seen[-1] is not value
    with connect(":memory:", extensions=registered(procedure)) as db:
        for query, params in (("CALL app.echo($input)", {"input": value}),
                              ("CALL app.echo", {"input": value}),
                              (f"CALL app.echo({expression})", None)):
            result = db.execute(query, params)
            assert result.rows == ((value,),)
            assert type(result.rows[0][0]) is type(value)
        assert db.execute("CALL app.echo(null)").rows == ((None,),)
        assert seen[-1] is None
        assert db.execute("CALL app.echo($input) YIELD value RETURN DISTINCT value UNION RETURN $input AS value",
                          {"input": value}).rows == ((value,),)
        with db.query("CALL app.echo($input)", {"input": value}).cursor(batch_size=1) as cursor:
            assert tuple(cursor) == ((value,),)


@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("kind,value,expression", TEMPORALS)
def test_temporal_procedure_output_is_native_durable_column(tmp_path, codec, kind, value, expression):
    path = tmp_path / "temporal"
    with connect(path, codec=codec, extensions=registered(echo(kind))) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute(f"CREATE NODE TABLE T (id INT64, value {kind}, PRIMARY KEY(id))")
            tx.execute("CALL app.echo($input) YIELD value CREATE (:T {id:1, value:value})", {"input": value})
        assert db.execute("MATCH (n:T) RETURN n.value").rows == ((value,),)
        db.verify()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:T) RETURN n.value").rows == ((value,),)
        db.verify()


@pytest.mark.parametrize("kind,value", (
    ("LIST", [1, {"date": DateValue(2024), "v": [True, None, 1.25]}]),
    ("LIST", (1, {"x": 2})),
    ("MAP", {"items": [1, 2], "when": DateValue(2024)}),
    ("ANY", {"nested": [DateValue(2024), None]}),
    ("ANY", 2**63-1), ("ANY", 2.5), ("ANY", True), ("ANY", "text"),
    ("ANY", b"payload"), ("ANY", None), ("ANY", Timestamp(123)),
    ("ANY", Uuid(bytes(range(16)))),
))
def test_native_container_and_any_arguments_results_and_implicit_invocation(kind, value):
    procedure = echo(kind)
    direct = tuple(procedure.invoke((value,)))[0][0]
    expected = list(value) if type(value) is tuple else value
    assert direct == expected
    expected = public_value(expected)
    with connect(":memory:", extensions=registered(procedure)) as db:
        assert db.execute("CALL app.echo", {"input": value}).rows == ((expected,),)
        assert db.execute("CALL app.echo($input) YIELD value WITH value AS x RETURN x", {"input": value}).rows == ((expected,),)


def test_map_and_list_outputs_support_postfix_unwind_and_aggregation():
    with connect(":memory:", extensions=registered(echo("MAP"))) as db:
        assert db.execute("CALL app.echo({items:[1,2,3]}) YIELD value "
                          "UNWIND value.items AS x RETURN sum(x)").rows == ((6,),)
    with connect(":memory:", extensions=registered(echo("LIST"))) as db:
        assert db.execute("CALL app.echo([1,2,3]) YIELD value "
                          "RETURN value[0], value[1..], size(value)").rows == ((1, (2,3), 3),)


def test_callbacks_cannot_mutate_input_bindings_or_prior_output_rows():
    original = {"items": [1]}
    seen = []

    def callback(value):
        seen.append(value)
        value["items"].append(2)
        yield (value,)
        value["items"].append(3)
        yield (value,)
        value["items"].append(4)

    procedure = TabularProcedure("app.echo", ("MAP",), (("value", "MAP"),), callback)
    with connect(":memory:", extensions=registered(procedure)) as db:
        result = db.execute("WITH $input AS original CALL app.echo(original) YIELD value RETURN original, value",
                            {"input": original})
        assert result.rows == (({"items": (1,)}, {"items": (1,2)}),
                               ({"items": (1,)}, {"items": (1,2,3)}))
        assert original == {"items": [1]}
        seen[0]["items"].clear()
        assert result.rows[0][1] == {"items": (1,2)}


@pytest.mark.parametrize("kind,bad", (("DATE", "2024-01-01"), ("DATE", date(2024,1,1)),
                                     ("LOCALTIME", DateValue(2024)), ("LIST", {}), ("MAP", []),
                                     ("ANY", object()), ("MAP", {1: "bad"}),
                                     ("LIST", [2**63]), ("ANY", {"x": [float("nan")]}),
                                     ("ANY", [float("inf")]), ("ANY", bytearray(b"mutable"))))
def test_invalid_extended_values_are_rejected_before_callback(kind, bad):
    seen = []
    with pytest.raises(GrafxPlanError) as failure:
        tuple(echo(kind, seen).invoke((bad,)))
    assert failure.value.details["field"] == "procedure_value"
    assert seen == []


@pytest.mark.parametrize("kind,expression", (("DATE", "1"), ("LIST", "{}"), ("MAP", "[]")))
def test_static_mismatches_are_planning_errors_even_on_empty_inputs(kind, expression):
    seen = []
    with connect(":memory:", extensions=registered(echo(kind, seen))) as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(f"UNWIND [] AS x CALL app.echo({expression}) YIELD value RETURN value")
        assert failure.value.details["field"] == "procedure_type"
        assert seen == []


@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("bad", ({"x": [float("nan")]}, [DateValue(2024), object()]))
def test_bad_unselected_container_rolls_back_writing_procedure(tmp_path, codec, bad):
    def callback(writer, value):
        writer.execute("CREATE (:T {id:$id})", {"id": value})
        return ((value, bad),)

    procedure = TabularProcedure("app.write", ("INT64",), (("id", "INT64"), ("hidden", "ANY")),
                                  callback, mode="write", required_permissions=frozenset({"write"}))
    registry = ExtensionRegistry(trusted=True, procedures=(procedure,), procedure_permissions=frozenset({"write"}))
    path = tmp_path / "rollback"
    with connect(path, codec=codec, extensions=registry) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T (id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:1})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.write(2) YIELD id RETURN id")
            tx.execute("CREATE (:T {id:3})")
        db.verify()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
        db.verify()


def test_cycles_depth_cardinality_and_value_budgets():
    cycle = []
    cycle.append(cycle)
    deep = 1
    for _ in range(MAX_VALUE_DEPTH + 1):
        deep = [deep]
    for value, reason in ((cycle, "cycle"), (deep, "depth"), ([0]*1025, "list_elements"),
                           ({str(i):i for i in range(257)}, "map_entries")):
        with pytest.raises(GrafxPlanError) as failure:
            tuple(echo("ANY").invoke((value,)))
        assert failure.value.details["reason"] == reason
    with pytest.raises(GrafxQueryBudgetExceeded) as failure:
        tuple(echo("MAP", max_value_bytes=20).invoke(({"x": "12345"},)))
    assert failure.value.details["resource"] == "procedure_value"


def test_native_output_bytes_remain_bounded_after_detachment():
    procedure = replace(echo("LIST"), max_result_bytes=5)
    with pytest.raises(GrafxQueryBudgetExceeded) as failure:
        tuple(procedure.invoke(([1],)))
    assert failure.value.details["resource"] == "procedure_bytes"


@pytest.mark.parametrize("kind,dtype", (("VECTOR_F32", "float32"), ("VECTOR_F64", "float64")))
def test_native_vector_signature_keeps_space_dtype_and_owned_values(kind, dtype):
    value = VectorValue((1, 2), 7, dtype)
    result = tuple(echo(kind).invoke((value,)))[0][0]
    assert result == value and result is not value
    with connect(":memory:", extensions=registered(echo(kind))) as db:
        assert db.execute("CALL app.echo($input)", {"input": value}).rows == ((value,),)
    wrong = VectorValue((1, 2), 7, "float64" if dtype == "float32" else "float32")
    with pytest.raises(GrafxPlanError):
        tuple(echo(kind).invoke((wrong,)))


@pytest.mark.parametrize("kind", ("DATE", "LIST", "MAP", "ANY", "VECTOR_F32"))
def test_extended_procedure_types_do_not_silently_change_scalar_udfs(kind):
    with pytest.raises(GrafxConfigurationError):
        ScalarFunction("app.scalar", (kind,), kind, lambda value: value)


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_nested_temporal_write_rollback_does_not_publish_capability(tmp_path, codec):
    from okto_grafx.domain.model.temporal_codec import TEMPORAL_VALUES_CAPABILITY

    def callback(writer, value):
        writer.execute("CREATE (:T {id:$id, value:$value})", {"id":value["id"], "value":value["data"]})
        return 1 if value["reject"] else None

    procedure = TabularProcedure("app.store", ("MAP",), (), callback, mode="write",
                                  required_permissions=frozenset({"write"}))
    registry = ExtensionRegistry(trusted=True, procedures=(procedure,), procedure_permissions=frozenset({"write"}))
    path = tmp_path / "native"
    value = {"dates": [item[1] for item in TEMPORALS], "nested": {"v": [1, None, "s"]}}
    with connect(path, codec=codec, extensions=registry) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T (id INT64, value ANY, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:1, value:'prior'})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CALL app.store($input)", {"input":{"id":2, "data":value, "reject":True}})
        assert not db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:T) RETURN n.id, n.value").rows == ((1,"prior"),)
        with db.begin("write") as tx:
            tx.execute("CALL app.store($input)", {"input":{"id":3, "data":value, "reject":False}})
        assert db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        db.verify()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:T) RETURN n.id, n.value ORDER BY n.id").rows == ((1,"prior"), (3,public_value(value)))
        db.verify()


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_procedure_temporal_values_recover_after_durable_apply_failure(tmp_path, codec, monkeypatch):
    from okto_grafx.errors import GrafxTransactionStateError
    from okto_grafx.engine.txn_manager import TransactionManager
    from okto_grafx.domain.model.temporal_codec import TEMPORAL_VALUES_CAPABILITY

    value = {"all": [item[1] for item in TEMPORALS]}
    path = tmp_path / "durable"
    with connect(path, codec=codec, extensions=registered(echo("MAP"))) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T (id INT64, value ANY, PRIMARY KEY(id))")
        tx = db.begin("write")
        tx.execute("CALL app.echo($input) YIELD value CREATE (:T {id:1, value:value})", {"input":value})

        def fail(manager, images):
            raise RuntimeError("injected post-durable apply fault")

        with monkeypatch.context() as patch:
            patch.setattr(TransactionManager, "_apply_images", fail)
            with pytest.raises(GrafxTransactionStateError) as failure:
                tx.commit()
            assert failure.value.details["committed"] is True
            assert failure.value.details["durable"] is True
    with connect(path, codec=codec) as db:
        assert db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:T) RETURN n.value").rows == ((public_value(value),),)
        db.verify()


def test_alias_copy_is_per_occurrence_and_host_subclasses_are_not_executed():
    leaf = {"items": [1]}
    copied = tuple(echo("LIST").invoke(([leaf, leaf],)))[0][0]
    copied[0]["items"].append(2)
    assert copied[1] == leaf == {"items": [1]}

    class HostList(list):
        def __iter__(self):
            raise AssertionError("Host list conversion must not run")

    class HostInt(int):
        def __int__(self):
            raise AssertionError("Host numeric conversion must not run")

    for value in (HostList([1]), {"x": HostInt(1)}):
        with pytest.raises(GrafxPlanError):
            tuple(echo("ANY").invoke((value,)))


def test_forged_native_components_and_recorded_zone_values():
    malformed = object.__new__(DateValue)
    object.__setattr__(malformed, "year", 2024)
    object.__setattr__(malformed, "month", 2)
    object.__setattr__(malformed, "day", 31)
    with pytest.raises(GrafxPlanError):
        tuple(echo("DATE").invoke((malformed,)))
    zoned = DateTimeValue.from_epoch_parts(-1, nanosecond=999999999, offset_seconds=1234, zone="Future/Recorded")
    returned = tuple(echo("DATETIME").invoke((zoned,)))[0][0]
    assert returned == zoned
    assert returned.local is not zoned.local and returned.local.date is not zoned.local.date


def test_any_signatures_keep_null_and_heterogeneous_composition():
    outputs = ((1,), ("a",), (None,), ({"k":[1]},), (DateValue(2024),))
    procedure = TabularProcedure("app.all", (), (("value", "ANY"),), lambda: iter(outputs))
    with connect(":memory:", extensions=registered(procedure)) as db:
        result = db.execute("CALL app.all() YIELD value RETURN value")
        assert result.rows == tuple((public_value(row[0]),) for row in outputs)
        assert db.execute("CALL app.all() YIELD value RETURN count(value)").rows == ((4,),)


def test_value_budget_refuses_before_encodings_for_large_text_and_vectors(monkeypatch):
    from okto_grafx.domain.query import procedure_values

    def unexpected_encode(value):
        raise AssertionError("Encoding happened before budget admission")

    with monkeypatch.context() as patch:
        patch.setattr(procedure_values, "encode_value", unexpected_encode)
        for signature, value in (("LIST", ["x"*100]), ("VECTOR_F64", VectorValue((1,)*100, 1, "float64"))):
            with pytest.raises(GrafxQueryBudgetExceeded):
                tuple(echo(signature, max_value_bytes=10).invoke((value,)))


def test_unknown_host_metaclass_is_not_compared_or_hashed():
    class HostMeta(type):
        def __eq__(self, other):
            raise AssertionError("Host type comparison must not run")

        def __hash__(self):
            raise AssertionError("Host type hashing must not run")

    class HostObject(metaclass=HostMeta):
        pass

    with pytest.raises(GrafxPlanError) as failure:
        tuple(echo("ANY").invoke((HostObject(),)))
    assert failure.value.details["reason"] == "unsupported_value"
