"""Native conversion failures have evaluated type evidence and atomic effects."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.query.scalars import scalar_value
from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import native_error


@pytest.mark.parametrize("name,valid,invalid", [
    ("toBoolean", True, []), ("toBoolean", True, {}), ("toBoolean", True, 1.0),
    ("toInteger", 1, []), ("toInteger", 1, {}),
    ("toFloat", 1.0, True), ("toFloat", 1.0, []), ("toFloat", 1.0, {}),
    ("toString", "s", []), ("toString", "s", {}),
])
def test_conversion_refuses_only_when_invalid_value_is_evaluated(name, valid, invalid):
    with connect(":memory:") as db:
        query = f"UNWIND $values AS v RETURN {name}(v)"
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(query, {"values":[valid,invalid]})
        assert failure.value.details == {
            "field":"function", "value":name.upper(),
            "reason":"conversion_argument_type", "query_phase":"execution",
        }
        observed = native_error(failure.value)
        assert (observed.type, observed.phase, observed.detail) == ("TypeError", "runtime", "InvalidArgumentValue")
        assert db.execute(f"RETURN CASE WHEN true THEN 1 ELSE {name}($v) END", {"v":invalid}).rows == ((1,),)
        assert db.execute(f"UNWIND [] AS x RETURN {name}($v)", {"v":invalid}).rows == ()


@pytest.mark.parametrize("name", ["toBoolean", "toInteger", "toFloat", "toString"])
@pytest.mark.parametrize("expression", ["n", "r", "p"])
def test_live_entity_and_path_conversion_refuses_without_python_coercion(name, expression):
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE ()-[:T]->()")
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(f"MATCH p=(n)-[r:T]->() RETURN {name}({expression})")
        assert failure.value.details["reason"] == "conversion_argument_type"
        assert failure.value.details["query_phase"] == "execution"


@pytest.mark.parametrize("budget", [None,32768])
def test_late_conversion_failure_discards_whole_instruction_not_prior_writes(tmp_path, budget):
    with connect(tmp_path / "db", query_memory_budget_bytes=budget) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Earlier)")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("UNWIND [1,2] AS i CREATE(:N {i:i}) RETURN sum(toInteger(CASE WHEN i=1 THEN '1' ELSE [] END))")
            assert failure.value.details["reason"] == "conversion_argument_type"
            tx.execute("CREATE(:Later)")
        assert db.execute("MATCH(n) RETURN labels(n) ORDER BY labels(n)").rows == ((("Earlier",),), (("Later",),))
        assert db.verify("all").findings == ()
    with connect(tmp_path / "db") as db:
        assert db.execute("MATCH(n) RETURN count(n)").rows == ((2,),)


@pytest.mark.parametrize("name", ["TOBOOLEAN", "TOINTEGER", "TOFLOAT", "TOSTRING"])
def test_host_conversion_callbacks_are_never_invoked(name):
    class HostValue:
        def __str__(self):
            raise AssertionError("host str invoked")
        def __float__(self):
            raise AssertionError("host float invoked")
        def __int__(self):
            raise AssertionError("host int invoked")
        def __bool__(self):
            raise AssertionError("host bool invoked")
    with pytest.raises(GrafxPlanError) as failure:
        scalar_value(name, HostValue())
    assert failure.value.details["reason"] == "conversion_argument_type"


@pytest.mark.parametrize("name", ["TOBOOLEAN", "TOINTEGER", "TOFLOAT"])
def test_unparseable_text_is_null_not_an_invalid_type(name):
    assert scalar_value(name, "not-convertible") is None
    assert scalar_value(name, None) is None


@pytest.mark.parametrize("override", [
    {"field":"other"}, {"value":"OTHER"}, {"reason":"other"},
    {"query_phase":"planning"}, {"query_phase":None},
])
def test_conversion_mapper_requires_exact_native_evidence(override):
    details = {"field":"function", "value":"TOINTEGER", "reason":"conversion_argument_type", "query_phase":"execution"}
    assert native_error(GrafxPlanError("refused", **(details | override))).detail != "InvalidArgumentValue"
