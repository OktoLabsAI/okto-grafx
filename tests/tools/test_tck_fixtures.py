"""Schema adaptation must not invent entities, keys or answers under test."""

from copy import deepcopy

import pytest

from tests.tools.test_tck_ledger import report
from tools.tck_fixtures import infer_fixture_schema
from tools.tck_native import NativeScenarioBackend
from tools.tck_stateful import run_stateful_case


def fixture(query):
    return {"steps": [{"text": "having executed:", "argument": {"docString": {"content": query}}}]}


def fixtures(*queries):
    return {"steps": [step for query in queries for step in fixture(query)["steps"]]}


def test_unlabeled_heterogeneous_fixture_uses_original_native_storage():
    case = fixture("CREATE(a {v:'start'})-[:R {v:1}]->(b {v:2})")
    original = deepcopy(case)
    assert infer_fixture_schema(case) == ()
    backend = NativeScenarioBackend(infer_schema=True)
    try:
        backend.admit(case)
        assert backend.setup(case["steps"][0]["argument"]["docString"]["content"], {}).error is None
        state = backend.snapshot()
        assert len(state.nodes) == 2 and state.labels == ()
        assert len(state.relationships) == 1
        assert case == original
    finally:
        backend.close()


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
    "CREATE (:A:B)", "CREATE (:N {v:1}), (:N {v:'str'})",
    "CREATE (:N {v:null})", "CREATE (:N {v:[1]})",
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


def test_initial_typed_create_action_declares_schema_but_does_not_execute_or_rewrite_action():
    query = "CREATE(n:Person)-[:OWNS]->(:Dog) RETURN labels(n)"
    case = {"steps":[{"text":"an empty graph"},
                     {"text":"executing query:","argument":{"docString":{"content":query}}}]}
    original = deepcopy(case)
    assert infer_fixture_schema(case) == ()
    backend = NativeScenarioBackend(infer_schema=True)
    try:
        backend.admit(case)
        before = backend.snapshot()
        assert before.nodes == () and before.relationships == () and before.labels == ()
        assert all("initial CREATE" in entry for entry in backend.adaptations)
        observation = backend.execute(query, {}, control=False)
        assert observation.error is None
        assert observation.rows == ((('Person',),),)
        state = backend.snapshot()
        assert len(state.nodes) == 2 and len(state.relationships) == 1
        assert state.labels == ('Dog','Person') and state.properties == ()
        backend.reopen()
        assert backend.snapshot() == state
        assert case == original
    finally:
        backend.close()


@pytest.mark.parametrize("query", ["CREATE (", "RETURN 1", "MATCH(n:N) CREATE(:Other)",
                                  "UNWIND [1] AS x CREATE(:N {v:x})"])
def test_initial_action_inference_does_not_mask_grammar_or_guess_other_pipeline_schema(query):
    case = {"steps":[{"text":"executing query:","argument":{"docString":{"content":query}}}]}
    assert infer_fixture_schema(case, allow_initial_create=True) == ()


def test_action_inference_never_changes_existing_fixture_schema_or_uses_return_values():
    case = fixture("CREATE(:N {v:1})")
    case["steps"].append({"text":"executing query:","argument":{"docString":{
        "content":"CREATE(:Other {v:'new'}) RETURN 'not a schema clue'"}}})
    assert infer_fixture_schema(case, allow_initial_create=True) == ("CREATE NODE TABLE N(v INT64)",)
    action = {"steps":[case["steps"][-1]]}
    expected = ("CREATE NODE TABLE Other(v STRING)",)
    assert infer_fixture_schema(action, allow_initial_create=True) == expected
    action["steps"].append({"text":"the result should be, in any order:",
                            "argument":{"dataTable":{"rows":[{"cells":[{"value":"123"}]}]}}})
    assert infer_fixture_schema(action, allow_initial_create=True) == expected


@pytest.mark.parametrize("query,parameters", [("CREATE(:N {v:$x})", {"x":1})])
def test_parameterized_action_uses_native_label_without_inferred_schema(query, parameters):
    backend = NativeScenarioBackend(infer_schema=True)
    try:
        backend.admit({"steps":[{"text":"executing query:","argument":{"docString":{"content":query}}}]})
        assert backend.schema == () and backend.adaptations == ()
        assert backend.execute(query, parameters, control=False).error is None
        assert len(backend.snapshot().nodes) == 1
    finally:
        backend.close()


def test_unlabeled_action_uses_native_graph_without_invented_fixture_schema():
    backend = NativeScenarioBackend(infer_schema=True)
    query = "CREATE(a {v:'text'}),(b {v:2}),(empty) RETURN labels(a)"
    try:
        backend.admit({"steps":[{"text":"executing query:","argument":{"docString":{"content":query}}}]})
        assert backend.schema == () and backend.adaptations == ()
        observed = backend.execute(query, {}, control=False)
        assert observed.error is None and observed.rows == (((),),)
        state = backend.snapshot()
        assert len(state.nodes) == 3 and state.labels == () and len(state.properties) == 2
        assert {entry[1] for entry in state.properties} == {"v"}
        backend.reopen()
        assert backend.snapshot() == state
    finally:
        backend.close()


def test_multiple_endpoint_pairs_use_native_group_without_rewriting_fixture():
    query = "CREATE (a:A)-[:R {weight:1}]->(b:B)-[:R {weight:2}]->(c:C)"
    case = fixture(query)
    before = deepcopy(case)
    assert infer_fixture_schema(case)[-1] == (
        "CREATE REL TABLE GROUP R(FROM A TO B, FROM B TO C, weight INT64)"
    )
    backend = NativeScenarioBackend(infer_schema=True)
    try:
        backend.admit(case)
        assert backend.setup(query, {}).error is None
        result = backend.database.execute("MATCH p=(:A)-[:R*2..2]->() RETURN relationships(p)")
        assert [edge.label for edge in result.rows[0][0]] == ["R", "R"]
        backend.reopen()
        assert backend.database.execute("MATCH ()-[r:R]->() RETURN count(r)").rows == ((2,),)
        assert any("catalog-v2" in item for item in backend.adaptations)
        assert case == before
    finally:
        backend.close()


def test_match_create_property_type_proof_executes_original_setup_and_reopens():
    case = fixtures("CREATE(:D {name:'root'}),(:D {name:'other'})",
                    "MATCH(d:D) CREATE(e:E {name:d.name+'0'}) CREATE(d)-[:R]->(e)")
    original = deepcopy(case)
    assert infer_fixture_schema(case) == (
        "CREATE NODE TABLE D(name STRING)", "CREATE NODE TABLE E(name STRING)",
        "CREATE REL TABLE R(FROM D TO E)",
    )
    backend = NativeScenarioBackend(infer_schema=True)
    try:
        backend.admit(case)
        for step in case["steps"]:
            result = backend.setup(step["argument"]["docString"]["content"], {})
            assert result.error is None, result
        assert backend.database.execute("MATCH(d:D)-[:R]->(e:E) RETURN e.name ORDER BY e.name").rows == (
            ("other0",), ("root0",),
        )
        before = backend.snapshot()
        backend.reopen()
        assert backend.snapshot() == before
        assert len(before.nodes) == 4 and len(before.relationships) == 2
        assert case == original
    finally:
        backend.close()


def test_endpoint_reversal_retains_pair_correlation_not_cartesian_labels():
    case = fixtures("CREATE(a:A)-[:R]->(b:B)-[:R]->(c:C)",
                    "MATCH(a)-[r]->(b) DELETE r CREATE(b)-[:R]->(a)")
    ddl = infer_fixture_schema(case)
    assert ddl[-1] == "CREATE REL TABLE GROUP R(FROM A TO B, FROM B TO A, FROM B TO C, FROM C TO B)"
    assert "FROM C TO A" not in ddl[-1] and "FROM B TO B" not in ddl[-1]


def test_anchored_match_and_incoming_edges_narrow_endpoint_domains():
    base = "CREATE(a:A)-[:R]->(b:B)-[:R]->(c:C)"
    assert infer_fixture_schema(fixtures(base, "MATCH(a:A)-[r]->(b) DELETE r CREATE(b)-[:R]->(a)"))[-1] == (
        "CREATE REL TABLE GROUP R(FROM A TO B, FROM B TO A, FROM B TO C)"
    )
    assert infer_fixture_schema(fixtures(base, "MATCH(c:C)<-[r]-(b) DELETE r CREATE(c)-[:R]->(b)"))[-1] == (
        "CREATE REL TABLE GROUP R(FROM A TO B, FROM B TO C, FROM C TO B)"
    )


def test_multihop_fixed_schema_constraints_propagate_to_both_endpoints():
    case = fixtures("CREATE(a:A)-[:R]->(b:B)-[:R]->(c:C),(:X)-[:R]->(:Y)",
                    "MATCH(a)-[:R]->(b)-[:R]->(c:C) CREATE(c)-[:BACK]->(a)")
    assert "CREATE REL TABLE BACK(FROM C TO A)" in infer_fixture_schema(case)


@pytest.mark.parametrize("setup", [
    "MATCH(d:D) CREATE(:E {name:d.missing})",
    "MATCH(d:D) CREATE(:E {name:$external})",
    "MATCH(d:D) CREATE(:E {name:rand()})",
    "MATCH(d:D) CREATE(:E {name:d.name+1})",
    "MATCH(x:Unknown) CREATE(:E {name:x.name})",
    "OPTIONAL MATCH(d:D) CREATE(:E {name:d.name})",
    "MATCH(d:D)-[:R*2]->(e) CREATE(e)-[:BACK]->(d)",
])
def test_unproved_setup_types_or_bindings_refuse_without_creating_database(setup):
    backend = NativeScenarioBackend(infer_schema=True)
    try:
        with pytest.raises(ValueError):
            backend.admit(fixtures("CREATE(:D {name:'root'})", setup))
        assert backend.database is None and backend.temporary is None
    finally:
        backend.close()


def test_scalar_addition_inference_uses_input_types_not_expected_values():
    case = fixtures("CREATE(:D {i:1,f:0.5})", "MATCH(d:D) CREATE(:E {i:d.i+2,f:d.i+d.f})")
    before = deepcopy(case)
    assert infer_fixture_schema(case)[1] == "CREATE NODE TABLE E(f DOUBLE, i INT64)"
    case["steps"].append({"text": "the result should be, in any order:",
                          "argument": {"dataTable": {"rows": [{"cells": [{"value": "invented"}]}]}}})
    assert infer_fixture_schema(case) == infer_fixture_schema(before)


def test_heterogeneous_source_property_does_not_invent_any_storage():
    case = fixtures("CREATE(:A {v:1}),(:B {v:'text'})", "MATCH(n) CREATE(:E {v:n.v})")
    with pytest.raises(ValueError, match="unambiguous stored type"):
        infer_fixture_schema(case)
