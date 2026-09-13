"""Raise-site evidence distinguishes known static types from runtime parameter types."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import native_error


@pytest.mark.parametrize("literal", ["123", "42.45", "true", "false", "'string'", "[123,true]"])
def test_static_property_type_is_refused_by_explain_and_execute(literal):
    with connect(":memory:") as db:
        query = f"WITH {literal} AS nonMap RETURN nonMap.num"
        for operation in (db.explain, db.execute):
            with pytest.raises(GrafxPlanError) as caught:
                operation(query)
            assert caught.value.details["query_phase"] == "planning"
            assert caught.value.details["reason"] == "property_subject_type"
            assert native_error(caught.value).phase == "compile time"


@pytest.mark.parametrize("subject,index,reason,detail", [
    ({"name": "Apa"}, 0, "map_key_type", "MapElementAccessByNonString"),
    ({"name": "Apa"}, 12.3, "map_key_type", "MapElementAccessByNonString"),
    (100, 0, "subscript_subject_type", "InvalidArgumentType"),
    ([1], 1.5, "list_index_type", "ListElementAccessByNonInteger"),
])
def test_bound_parameter_errors_are_runtime_and_keep_earlier_writes(subject, index, reason, detail):
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})")
            with pytest.raises(GrafxPlanError) as caught:
                tx.execute("CREATE (:N {id:2}) WITH $expr AS expr, $idx AS idx RETURN expr[idx]",
                           {"expr": subject, "idx": index})
            assert caught.value.details["query_phase"] == "execution"
            assert caught.value.details["reason"] == reason
            observed = native_error(caught.value)
            assert (observed.type, observed.phase, observed.detail) == ("TypeError", "runtime", detail)
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)


@pytest.mark.parametrize("details", [
    {}, {"field": "subscript", "reason": "map_key_type"},
    {"field": "property", "reason": "map_key_type", "query_phase": "execution"},
    {"field": "subscript", "reason": "map_key_type", "query_phase": "invented"},
])
def test_incomplete_or_inconsistent_evidence_is_not_reference_conformance(details):
    observed = native_error(GrafxPlanError("A map subscript needs a string key.", **details))
    assert observed.phase == "unknown"
    assert observed.detail == "plan_error"


def test_dynamic_row_error_is_not_mislabeled_as_static():
    with connect(":memory:") as db:
        query = "UNWIND [{a:1}, 2] AS v RETURN v.a"
        db.explain(query)
        with pytest.raises(GrafxPlanError) as caught:
            db.execute(query)
        assert caught.value.details["query_phase"] == "execution"
