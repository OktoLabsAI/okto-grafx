"""Host-trusted scalar allowlists are isolated, typed and explicit."""

import pytest

from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, ScalarFunction
from okto_grafx.errors import GrafxConfigurationError, GrafxPlanError, GrafxQueryBudgetExceeded


def registry(implementation=str.upper, **options):
    return ExtensionRegistry((ScalarFunction("app.upper", ("STRING",), "STRING", implementation, **options),), trusted=True)


def test_scalar_query_projection_filter_union_and_write(tmp_path):
    with connect(tmp_path / "db", extensions=registry()) as db:
        assert db.execute("RETURN udf('app.upper','hello') AS v").rows == (("HELLO",),)
        assert db.execute("RETURN udf('app.upper',NULL) AS v").rows == ((None,),)
        assert db.execute("RETURN udf('app.upper','a') AS v UNION RETURN udf('app.upper','b') AS v").rows == (("A",), ("B",))
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,v STRING,PRIMARY KEY(id))")
            tx.execute("CREATE (:D {id:1,v:udf('app.upper','hello')})")
        assert db.execute("MATCH (d:D) WHERE udf('app.upper',d.v)='HELLO' RETURN d.id").rows == ((1,),)
        assert not db.verify("all").findings
    with connect(tmp_path / "db") as db:
        assert db.execute("MATCH (d:D) RETURN d.v").rows == (("HELLO",),)
        with pytest.raises(GrafxPlanError):
            db.execute("RETURN udf('app.upper','hello') AS v")


def test_registry_failures_and_handle_isolation(tmp_path):
    with pytest.raises(GrafxConfigurationError):
        ExtensionRegistry()
    with pytest.raises(GrafxConfigurationError):
        connect(tmp_path / "invalid", extensions={})
    assert not (tmp_path / "invalid").exists()
    with connect(":memory:", extensions=registry()) as a, connect(":memory:", extensions=registry(str.lower)) as b:
        assert a.execute("RETURN udf('app.upper','Hello') AS v").rows == (("HELLO",),)
        assert b.execute("RETURN udf('app.upper','Hello') AS v").rows == (("hello",),)
        for query in ("RETURN udf('app.unknown',1)", "RETURN udf('app.upper',1)", "RETURN udf('app.upper')"):
            with pytest.raises(GrafxPlanError):
                a.execute(query)
    for callback in (lambda value: 1, lambda value: 1 / 0):
        with connect(":memory:", extensions=registry(callback)) as db, pytest.raises(GrafxPlanError):
            db.execute("RETURN udf('app.upper','hello')")
    with connect(":memory:", extensions=registry(max_value_bytes=4)) as db, pytest.raises(GrafxQueryBudgetExceeded):
        db.execute("RETURN udf('app.upper','hello')")


@pytest.mark.parametrize("kind,value", [("BOOL", True), ("INT64", 2**63-1), ("DOUBLE", 1.25), ("BYTES", b"abc")])
def test_direct_and_query_scalar_types(kind, value):
    fn = ScalarFunction("app.identity", (kind,), kind, lambda value: value)
    extension = ExtensionRegistry((fn,), trusted=True)
    assert extension.call_scalar("app.identity", (value,)) == value
    with connect(":memory:", extensions=extension) as db:
        assert db.execute("RETURN udf('app.identity',$v) AS v", {"v": value}).rows == ((value,),)


def test_planning_null_and_empty_matches_never_invoke_callback():
    calls = []

    def upper(value):
        calls.append(value)
        return value.upper()

    with connect(":memory:", extensions=registry(upper)) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,v STRING,PRIMARY KEY(id))")
        assert db.execute("MATCH (d:D) RETURN udf('app.upper',d.v)").rows == ()
        assert db.execute("RETURN udf('app.upper',NULL)").rows == ((None,),)
        assert calls == []
        with pytest.raises(GrafxPlanError):
            db.execute("MATCH (d:D) RETURN udf('app.missing',d.v)")
        with pytest.raises(GrafxPlanError):
            db.execute("RETURN udf($name,'a')", {"name": "app.upper"})
        assert db.execute("RETURN udf('app.upper','a') AS v UNION RETURN udf('app.upper','b') AS v").rows == (("A",), ("B",))
        assert sorted(calls) == ["a", "b"]


def test_registry_is_frozen_and_rejects_duplicate_names():
    from dataclasses import FrozenInstanceError

    existing = registry()
    with pytest.raises(FrozenInstanceError):
        existing.trusted = False
    with pytest.raises(GrafxConfigurationError):
        ExtensionRegistry(existing.scalars * 2, trusted=True)
    with pytest.raises(GrafxConfigurationError):
        ScalarFunction("upper", ("STRING",), "STRING", str.upper)
