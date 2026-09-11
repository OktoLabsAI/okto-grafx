"""The conformance ledger cannot hide upstream failures or change their oracle."""

from copy import deepcopy
import hashlib

import pytest

from tools.check_opencypher import compile_feature
from tools.tck_ledger import build_ledger, case_checksum, step_family, verify_ledger


SOURCE = '''Feature: ledger
  Scenario: write then control
    Given an empty graph
    And having executed:
      """
      CREATE (:N {id: 1})
      """
    When executing query:
      """
      CREATE (:N {id: 2})
      """
    Then the result should be empty
    And the side effects should be:
      | +nodes | 1 |
      | +properties | 1 |
    When executing control query:
      """
      MATCH (n:N) RETURN n.id AS id ORDER BY id
      """
    Then the result should be, in order:
      | id |
      | 1 |
      | 2 |
'''


def report():
    uri = "clauses/create/Example.feature"
    cases = compile_feature(SOURCE, uri)
    return {"upstream_revision": "pinned", "case_count": len(cases), "feature_count": 1,
            "feature_sha256_lf": {uri: hashlib.sha256(SOURCE.encode()).hexdigest()}, "cases": cases}


def test_setup_is_a_first_class_query_and_effects_omit_nothing():
    current = report()
    assert len(current["cases"][0]["queries"]) == 3
    ledger = build_ledger(current)
    entry = ledger["cases"][0]
    assert entry["owner"] == "FP-4"
    assert entry["profile"]["inclusion"] == "required"
    assert entry["baseline"]["status"] == "not_run"
    effects = entry["expected"]["outcomes"][1]["counts"]
    assert effects["+nodes"] == effects["+properties"] == 1
    assert len(effects) == 8
    assert effects["-nodes"] == 0
    verify_ledger(ledger, current)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "query", "expected", "source", "count"])
def test_verifier_rejects_missing_duplicate_or_rewritten_cases(mutation):
    current = report()
    ledger = build_ledger(current)
    if mutation == "missing":
        current["cases"].clear()
    elif mutation == "duplicate":
        ledger["cases"].append(deepcopy(ledger["cases"][0]))
    elif mutation == "query":
        current["cases"][0]["steps"][2]["argument"]["docString"]["content"] = "RETURN 1"
    elif mutation == "expected":
        ledger["cases"][0]["expected"]["outcomes"][1]["counts"]["+nodes"] = 2
    elif mutation == "source":
        current["feature_sha256_lf"]["clauses/create/Example.feature"] = "wrong"
    else:
        ledger["case_count"] = 2
    with pytest.raises(ValueError):
        verify_ledger(ledger, current)


def test_checksum_ignores_generated_ast_ids_but_not_expectations():
    case = report()["cases"][0]
    copied = deepcopy(case)
    copied["steps"][0]["astNodeIds"] = ["changed-compiler-id"]
    assert case_checksum(copied) == case_checksum(case)
    copied["steps"][4]["argument"]["dataTable"]["rows"][0]["cells"][1]["value"] = "2"
    assert case_checksum(copied) != case_checksum(case)


def test_failed_observations_are_not_inferred_root_causes_or_exclusions():
    current = report()
    current["cases"][0].update(conformance="failed", reason="RETURN ends a query")
    entry = build_ledger(current)["cases"][0]
    assert entry["baseline"] == {"status": "failed", "reason": "RETURN ends a query"}
    assert entry["root_cause"] == "not_diagnosed"
    assert entry["profile"] == {"inclusion": "required", "decision": None}


def test_unknown_steps_and_capabilities_stop_classification():
    with pytest.raises(ValueError, match="Unclassified reference step"):
        step_family("some new magic")
    current = report()
    current["cases"][0]["id"] = "newCategory/newFeature.feature#0001"
    with pytest.raises(ValueError, match="Unclassified reference capability"):
        build_ledger(current)


def test_negative_error_phase_is_preserved_exactly():
    current = report()
    current["cases"][0]["steps"] = [
        {"text": "a TypeError should be raised at runtime: InvalidArgumentValue"},
        {"text": "no side effects"},
    ]
    expectation = build_ledger(current)["cases"][0]["expected"]["outcomes"][0]
    assert expectation == {"step": 0, "kind": "error", "type": "TypeError",
                           "phase": "runtime", "detail": "InvalidArgumentValue"}
