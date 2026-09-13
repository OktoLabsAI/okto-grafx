"""Reference procedure and named-graph fixtures are admitted before execution."""

import hashlib

import pytest

from tools.check_opencypher import compile_feature
from tools.tck_native import NativeScenarioBackend
from tools.tck_procedures import procedure_registry
from tools.tck_stateful import run_stateful_case
from tools.tck_ledger import build_ledger, verify_ledger
from tests.tools.test_tck_ledger import report


PROCEDURE = '''Feature: procedures
  Scenario: native fixture rows
    Given an empty graph
    And there exists a procedure test.proc(in :: INTEGER?) :: (out :: STRING?):
      | in | out |
      | 1 | 'a' |
      | 2 | 'b' |
      | 2 | 'b' |
    When executing query:
      """
      CALL test.proc(2) YIELD out RETURN out
      """
    Then the result should be, in order:
      | out |
      | 'b' |
      | 'b' |
    And no side effects
'''


def test_native_table_procedure_keeps_input_filtering_and_duplicates():
    case = compile_feature(PROCEDURE, "clauses/call/Example.feature")[0]
    backend = NativeScenarioBackend()
    try:
        outcome = run_stateful_case(case, backend)
        assert outcome["conformance"] == "passed", outcome
        assert not backend.snapshot().nodes
    finally:
        backend.close()


@pytest.mark.parametrize("before,after", [
    ("INTEGER?", "DECIMAL?"),
    ("| in | out |", "| wrong | out |"),
    ("| 1 | 'a' |", "| true | 'a' |"),
])
def test_invalid_procedure_fixtures_are_not_silently_coerced(before, after):
    case = compile_feature(PROCEDURE.replace(before, after), "clauses/call/Example.feature")[0]
    backend = NativeScenarioBackend()
    try:
        outcome = run_stateful_case(case, backend)
        assert outcome["conformance"] == "not_run", outcome
        assert backend.database is None
    finally:
        backend.close()


def test_unit_procedure_requires_its_fixture_table():
    with pytest.raises(ValueError, match="explicit fixture table"):
        procedure_registry({"steps": [{"text": "there exists a procedure test.proc() :: ():"}]})


def test_native_unit_fixture_does_not_fabricate_a_column_or_result_row():
    source = '''Feature: unit procedure
  Scenario: no output
    Given an empty graph
    And there exists a procedure test.unit() :: ():
      |
    When executing query:
      """
      CALL test.unit()
      """
    Then the result should be empty
    And no side effects
'''
    case = compile_feature(source, "clauses/call/Unit.feature")[0]
    registry = procedure_registry(case)
    assert registry.procedures[0].columns == ()
    assert tuple(registry.procedures[0].invoke(())) == ()
    backend = NativeScenarioBackend()
    try:
        outcome = run_stateful_case(case, backend)
        assert outcome["conformance"] == "passed", outcome
        assert not backend.snapshot().nodes
    finally:
        backend.close()


def test_procedure_registry_refuses_forged_compiled_row_width():
    # Gherkin rejects non-rectangular source tables itself. Exercise the registry
    # independently by corrupting an already compiled fixture.
    case = compile_feature(PROCEDURE, "clauses/call/Example.feature")[0]
    case["steps"][1]["argument"]["dataTable"]["rows"][1]["cells"].append({"value": "'extra'"})
    with pytest.raises(ValueError, match="width"):
        procedure_registry(case)


def named_graph_case():
    source = '''Feature: named
  Scenario: scalar graph count
    Given the binary-tree-1 graph
    When executing query:
      """
      MATCH (n:N) RETURN count(*) AS count
      """
    Then the result should be, in any order:
      | count |
      | 2 |
    And no side effects
'''
    return compile_feature(source, "useCases/named/Example.feature")[0]


def test_named_fixture_runs_unchanged_script_and_records_schema_adaptation():
    query = "CREATE (:N {id:1}), (:N {id:2})"
    fixtures = {"binary-tree-1": {"query": query, "sha256_lf": hashlib.sha256(query.encode()).hexdigest()}}
    backend = NativeScenarioBackend(infer_schema=True, graph_fixtures=fixtures)
    try:
        result = run_stateful_case(named_graph_case(), backend)
        assert result["conformance"] == "adapted_passed", result
        assert backend.named_queries["the binary-tree-1 graph"] == query
    finally:
        backend.close()


@pytest.mark.parametrize("fixtures", [{}, {"binary-tree-1": {"query": "CREATE ()", "sha256_lf": "wrong"}}])
def test_missing_or_changed_named_graph_is_refused_before_database_open(fixtures):
    backend = NativeScenarioBackend(infer_schema=True, graph_fixtures=fixtures)
    try:
        result = run_stateful_case(named_graph_case(), backend)
        assert result["conformance"] == "not_run"
        assert backend.database is None
    finally:
        backend.close()


def test_ledger_binds_named_graph_contents_not_only_feature_source():
    current = report()
    query = "CREATE (:N {id:1})"
    current["graph_fixtures"] = {"tree": {"query": query, "sha256_lf": hashlib.sha256(query.encode()).hexdigest()}}
    ledger = build_ledger(current)
    verify_ledger(ledger, current)
    current["graph_fixtures"]["tree"]["query"] = "CREATE (:N {id:2})"
    with pytest.raises(ValueError, match="query checksum"):
        verify_ledger(ledger, current)
