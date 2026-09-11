"""Schema adaptation must not invent entities, keys or answers under test."""

from copy import deepcopy

import pytest

from tests.tools.test_tck_ledger import report
from tools.tck_fixtures import infer_fixture_schema
from tools.tck_native import NativeScenarioBackend
from tools.tck_stateful import run_stateful_case


def fixture(query):
    return {"steps": [{"text": "having executed:", "argument": {"docString": {"content": query}}}]}


def test_infers_scalar_columns_without_inventing_primary_keys():
    ddl = infer_fixture_schema(fixture("CREATE (:N {name:'a', num:-2}), (:N {name:'b', ok:true})"))
    assert ddl == ("CREATE NODE TABLE N(name STRING, num INT64, ok BOOL)",)


def test_label_only_fixture_is_null_column_not_hidden_graph_property():
    backend = NativeScenarioBackend(infer_schema=True)
    try:
        case = fixture("CREATE (:N), (:N)")
        backend.admit(case)
        outcome = backend.setup(case["steps"][0]["argument"]["docString"]["content"], {})
        assert outcome.error is None
        state = backend.snapshot()
        assert len(state.nodes) == 2
        assert state.labels == ("N",)
        assert state.properties == ()
    finally:
        backend.close()


def test_inferred_fixture_runs_original_setup_write_and_control_text():
    case = report()["cases"][0]
    original = deepcopy(case)
    backend = NativeScenarioBackend(infer_schema=True)
    try:
        outcome = run_stateful_case(case, backend)
        assert outcome["conformance"] == "adapted_passed", outcome
        assert case == original
        assert "PRIMARY KEY" not in " ".join(backend.schema)
    finally:
        backend.close()


def test_endpoint_aliases_and_incoming_direction_have_typed_schema():
    ddl = infer_fixture_schema(fixture("CREATE (a:A {id:1}), (b:B {id:2}), (b)<-[:R {weight:5}]-(a)"))
    assert ddl == ("CREATE NODE TABLE A(id INT64)", "CREATE NODE TABLE B(id INT64)",
                   "CREATE REL TABLE R(FROM A TO B, weight INT64)")


@pytest.mark.parametrize("query", [
    "CREATE ()", "CREATE (:A:B)", "CREATE (:N {v:1}), (:N {v:'str'})",
    "CREATE (:N {v:null})", "CREATE (:N {v:[1]})",
    "CREATE (:A)-[:R]->(:B), (:B)-[:R]->(:A)",
    "UNWIND [1,2] AS x CREATE (:N {v:x})",
])
def test_ambiguous_fixtures_refuse_before_database_creation(query):
    backend = NativeScenarioBackend(infer_schema=True)
    try:
        with pytest.raises(ValueError):
            backend.admit(fixture(query))
        assert backend.database is None
        assert backend.temporary is None
    finally:
        backend.close()


def test_expected_result_values_never_influence_schema_inference():
    case = report()["cases"][0]
    initial = infer_fixture_schema(case)
    case["steps"][-1]["argument"]["dataTable"]["rows"][-1]["cells"][0]["value"] = "'malicious type clue'"
    assert infer_fixture_schema(case) == initial
