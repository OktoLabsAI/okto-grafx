"""Architectural review cannot turn a language failure into a model exclusion."""

from copy import deepcopy

import pytest

from tools.tck_profile import architectural_candidates, review_candidates
from tools.tck_ledger import freeze_ledger
from tests.tools.test_tck_ledger import report


def with_query(query, *, setup=False, error=False):
    current = report()
    current["cases"][0]["steps"] = [{"text": "having executed:" if setup else "executing query:",
                                     "argument": {"docString": {"content": query}}}]
    if error:
        current["cases"][0]["steps"].append({"text": "a SyntaxError should be raised at compile time: UnexpectedSyntax"})
    return current


@pytest.mark.parametrize("query,expected", [
    ("CREATE ()", "MODEL_UNLABELED_CREATE"),
    ("CREATE (:A:B)", "MODEL_MULTILABEL"),
    ("CREATE (:A {text:'(:A:B)'})", None),
    ("MATCH (a:A) CREATE (a)-[:R]->(:A)", None),
    ("CREATE (a:A) CREATE (a)-[:R]->(a)", None),
    ("RETURN missing_temporal_feature()", None),
    ("RETURN 0x12", None),
])
def test_only_proven_creation_model_changes_are_candidates(query, expected):
    candidate = architectural_candidates(with_query(query)["cases"][0])
    assert {e["rule"] for e in candidate["evidence"]} == ({expected} if expected else set())


def test_negative_syntax_case_is_not_excluded_for_its_invalid_create():
    assert not review_candidates(with_query("CREATE ()", error=True))["cases"]
    assert review_candidates(with_query("CREATE ()", setup=True, error=True))["cases"]


def test_freeze_preserves_nondivergent_failures_as_required():
    current = with_query("RETURN still_missing()")
    current["cases"][0].update(conformance="failed", reason="missing function")
    frozen = freeze_ledger(current, review_candidates(current), decision="approved-scope-v1")
    assert frozen["status"] == "frozen_checkpoint_a"
    assert frozen["cases"][0]["profile"]["inclusion"] == "required"
    assert frozen["cases"][0]["baseline"]["status"] == "failed"


def test_freeze_keeps_divergence_counterexamples_separate_from_passes():
    current = with_query("CREATE ()")
    frozen = freeze_ledger(current, review_candidates(current), decision="approved-scope-v1")
    assert frozen["profile_counts"] == {"architectural_divergence": 1}
    assert frozen["cases"][0]["baseline"]["status"] == "not_run"
    assert frozen["cases"][0]["profile"]["evidence"][0]["counterexample"] == "()"


def test_forged_counterexample_cannot_exclude_a_bound_valid_case():
    current = with_query("CREATE (:A {id:1})")
    source = with_query("CREATE ()")
    reviewed = review_candidates(source)
    from tools.tck_ledger import case_checksum
    reviewed["cases"][0]["case_sha256"] = case_checksum(current["cases"][0])
    with pytest.raises(ValueError, match="counterexample is not present"):
        freeze_ledger(current, reviewed, decision="invalid-review")


def test_review_stale_checksum_and_duplicate_case_refuse():
    current = with_query("CREATE ()")
    reviewed = review_candidates(current)
    duplicate = deepcopy(reviewed)
    duplicate["cases"].append(deepcopy(duplicate["cases"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        freeze_ledger(current, duplicate, decision="review")
    reviewed["cases"][0]["case_sha256"] = "stale"
    with pytest.raises(ValueError, match="stale source"):
        freeze_ledger(current, reviewed, decision="review")


def test_summary_does_not_promote_adapted_execution_to_upstream_pass():
    from tools.tck_ledger import profile_summary
    current = with_query("RETURN 1 AS n")
    frozen = freeze_ledger(current, review_candidates(current), decision="review")
    current["cases"][0]["conformance"] = "adapted_passed"
    # Ledger verification remains independent of current execution observations.
    summary = profile_summary(current, frozen)
    assert summary["upstream"] == {"adapted_not_upstream_certified": 1}
    assert summary["required_profile"] == {"passed_with_adaptation": 1}
