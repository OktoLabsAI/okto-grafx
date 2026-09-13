"""V3 is a source-bound decision record, never a mechanism for manufacturing passes."""

from copy import deepcopy
import hashlib
import json

import pytest

from tools.check_opencypher import compile_feature
from tools.tck_ledger import (
    NESTED_STORAGE_CASE, NESTED_STORAGE_CASE_SHA256, case_checksum,
    expand_flexible_ledger, expand_multilabel_ledger, freeze_ledger,
    profile_summary, verify_ledger,
)
from tools.tck_profile import review_candidates


def fixture():
    uri = "clauses/create/Example.feature"
    source = '''Feature: scope
  Scenario: labels
    Given an empty graph
    When executing query:
      """
      CREATE (:A:B) RETURN 1 AS x
      """
    Then the result should be, in any order:
      | x |
      | 1 |
  Scenario: required
    Given an empty graph
    When executing query:
      """
      RETURN 2 AS x
      """
    Then the result should be, in any order:
      | x |
      | 2 |
'''
    cases = compile_feature(source, uri)
    nested = {
        "id": NESTED_STORAGE_CASE,
        "name": "[10] Failing when setting a list of maps as a property",
        "steps": [
            {"text": "any graph"},
            {"text": "executing query:", "argument": {
                "docString": {"content": "CREATE (a)\nSET a.maplist = [{num: 1}]"}}},
            {"text": "a TypeError should be raised at runtime: InvalidPropertyType"},
        ],
        "conformance": "failed", "reason": "Native nested property succeeds",
    }
    assert case_checksum(nested) == NESTED_STORAGE_CASE_SHA256
    cases.append(nested)
    cases[0].update(conformance="failed", reason="Labels not implemented")
    report = {"upstream_revision": "pinned", "case_count": 3, "feature_count": 2,
              "feature_sha256_lf": {uri: hashlib.sha256(source.encode()).hexdigest(),
                                    "clauses/set/Set1.feature": "test-source"}, "cases": cases}
    v1 = freeze_ledger(report, review_candidates(report), decision="original")
    v2 = expand_flexible_ledger(report, v1, decision="flexible authorization")
    return report, v1, v2


def successor(report, v1, v2):
    return expand_multilabel_ledger(report, v2, ancestor=v1, decision="multiple labels authorized",
                                   nested_storage_decision="retain nested storage authorized")


def test_exact_transition_preserves_oracles_baselines_and_predecessors():
    report, v1, v2 = fixture()
    before = deepcopy((report, v1, v2))
    v3 = successor(report, v1, v2)
    assert (report, v1, v2) == before
    assert v3["profile_counts"] == {"required": 2, "architectural_divergence": 1}
    assert len(v3["scope_changes"]) == 2
    for old, new in zip(v2["cases"], v3["cases"], strict=True):
        for field in ("id", "case_sha256", "source_sha256_lf", "expected", "owner",
                      "baseline", "step_families", "capability"):
            assert old[field] == new[field]
    assert v3["cases"][0]["profile"]["inclusion"] == "required"
    assert v3["cases"][0]["baseline"]["status"] == "failed"
    assert v3["cases"][-1]["expected"]["outcomes"][0]["detail"] == "InvalidPropertyType"
    verify_ledger(v3, report, predecessor=v2, ancestor=v1)
    observed = profile_summary(report, v3, predecessor=v2, ancestor=v1)
    assert observed == {"upstream": {"failed": 2, "not_run": 1},
                        "required_profile": {"failed": 1, "not_run": 1},
                        "architectural_divergences": {"failed": 1}}


@pytest.mark.parametrize("mutation", ["exclusion", "pass", "expectation", "hash", "audit", "count",
                                      "owner", "evidence", "status", "decision", "parent", "ancestor"])
def test_rejects_tampered_v3_or_ancestor_chain(mutation):
    report, v1, v2 = fixture()
    v3 = successor(report, v1, v2)
    if mutation == "exclusion":
        v3["cases"][0]["profile"] = deepcopy(v2["cases"][0]["profile"])
    elif mutation == "pass":
        v3["cases"][-1]["baseline"]["status"] = "passed"
    elif mutation == "expectation":
        v3["cases"][-1]["expected"]["outcomes"].clear()
    elif mutation == "hash":
        v3["predecessor"]["canonical_sha256"] = "wrong"
    elif mutation == "audit":
        v3["scope_changes"].clear()
    elif mutation == "count":
        v3["profile_counts"]["required"] = 3
    elif mutation == "owner":
        v3["cases"][-1]["owner"] = "FP-8"
    elif mutation == "evidence":
        v3["cases"][-1]["profile"]["evidence"][0]["counterexample"] = "SET n.x = []"
    elif mutation == "status":
        v3["status"] = "draft_pending_architectural_review"
    elif mutation == "decision":
        v3["nested_storage_decision"] = "another decision"
    elif mutation == "parent":
        v2["cases"][0]["baseline"]["status"] = "passed"
    else:
        v1["cases"][0]["baseline"]["status"] = "passed"
    with pytest.raises(ValueError):
        verify_ledger(v3, report, predecessor=v2, ancestor=v1)


@pytest.mark.parametrize("missing", ["ancestor", "predecessor", "both", "label_decision", "storage_decision"])
def test_both_decisions_and_complete_frozen_chain_are_required(missing):
    report, v1, v2 = fixture()
    if missing in {"ancestor", "predecessor", "both"}:
        v3 = successor(report, v1, v2)
        with pytest.raises(ValueError, match="predecessor.*ancestor"):
            verify_ledger(v3, report, predecessor=None if missing != "ancestor" else v2,
                          ancestor=None if missing != "predecessor" else v1)
    else:
        with pytest.raises(ValueError, match="both explicit decisions"):
            expand_multilabel_ledger(report, v2, ancestor=v1,
                                    decision=" " if missing == "label_decision" else "approved",
                                    nested_storage_decision=" " if missing == "storage_decision" else "approved")


@pytest.mark.parametrize("mutation", ["id", "query", "name", "error", "phase", "missing"])
def test_nested_exception_cannot_expand_to_other_cases_even_with_refrozen_parents(mutation):
    report, _, _ = fixture()
    case = report["cases"][-1]
    if mutation == "id":
        case["id"] = "clauses/set/Set1.feature#9999"
    elif mutation == "query":
        case["steps"][1]["argument"]["docString"]["content"] = "CREATE (a) SET a.x = [1]"
    elif mutation == "name":
        case["name"] += " altered"
    elif mutation in {"error", "phase"}:
        case["steps"][2]["text"] = ("a TypeError should be raised at runtime: Other" if mutation == "error"
                                    else "a TypeError should be raised at compile time: InvalidPropertyType")
    else:
        report["cases"].pop()
        report["case_count"] -= 1
    v1 = freeze_ledger(report, review_candidates(report), decision="original")
    v2 = expand_flexible_ledger(report, v1, decision="flexible authorization")
    with pytest.raises(ValueError, match="exact pinned Set1"):
        successor(report, v1, v2)


def test_both_clis_generate_verify_and_preserve_history(tmp_path, monkeypatch):
    from tools import check_opencypher, review_tck_profile

    report, v1, v2 = fixture()
    ancestor, parent, child, output = (tmp_path / name for name in ("v1.json", "v2.json", "v3.json", "report.json"))
    ancestor.write_text(json.dumps(v1), encoding="utf-8")
    parent.write_text(json.dumps(v2), encoding="utf-8")
    before = (ancestor.read_bytes(), parent.read_bytes())
    monkeypatch.setattr(review_tck_profile, "inventory", lambda path: deepcopy(report))
    args = ["review_tck_profile", "--checkout", str(tmp_path), "--output", str(child),
            "--expand-multilabel-ledger", str(parent), "--predecessor-ledger", str(ancestor),
            "--decision", "multiple labels authorized", "--nested-storage-decision", "retain nested storage authorized"]
    monkeypatch.setattr("sys.argv", args)
    review_tck_profile.main()
    assert json.loads(child.read_text()) == successor(report, v1, v2)
    with pytest.raises(SystemExit):
        review_tck_profile.main()
    monkeypatch.setattr(check_opencypher, "inventory", lambda path: deepcopy(report))
    monkeypatch.setattr("sys.argv", ["check_opencypher", "--checkout", str(tmp_path),
        "--output", str(output), "--verify-ledger", str(child), "--predecessor-ledger", str(parent),
        "--ancestor-ledger", str(ancestor)])
    assert check_opencypher.main() == 0
    observed = json.loads(output.read_text())
    assert observed["profile_summary"]["architectural_divergences"] == {"failed": 1}
    assert before == (ancestor.read_bytes(), parent.read_bytes())
    monkeypatch.setattr("sys.argv", ["check_opencypher", "--checkout", str(tmp_path),
        "--output", str(output), "--ledger-output", str(child)])
    with pytest.raises(SystemExit):
        check_opencypher.main()
    assert json.loads(child.read_text()) == successor(report, v1, v2)


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("flag", ["--output", "--ledger-output"])
def test_neither_report_nor_draft_output_can_destroy_any_frozen_profile(tmp_path, monkeypatch, version, flag):
    from tools import check_opencypher

    report, v1, v2 = fixture()
    ledger = (v1, v2, successor(report, v1, v2))[version - 1]
    frozen = tmp_path / "frozen.json"
    frozen.write_text(json.dumps(ledger), encoding="utf-8")
    before = frozen.read_bytes()
    args = ["check_opencypher", "--checkout", str(tmp_path)]
    if flag == "--ledger-output":
        args += ["--output", str(tmp_path / "report.json")]
    args += [flag, str(frozen)]
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.setattr(check_opencypher, "inventory", lambda path: pytest.fail("Must refuse before inventory"))
    with pytest.raises(SystemExit):
        check_opencypher.main()
    assert frozen.read_bytes() == before
