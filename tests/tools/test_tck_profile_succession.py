"""Expanded requirements retain independent oracles and cannot manufacture passes."""

from copy import deepcopy
import hashlib
import json

import pytest

from tools.check_opencypher import compile_feature
from tools.tck_ledger import expand_flexible_ledger, freeze_ledger, profile_summary, verify_ledger
from tools.tck_profile import review_candidates


def fixture():
    queries = [
        ("unlabeled", "CREATE () RETURN 1 AS x", "1"),
        ("multilabel", "CREATE (:A:B) RETURN 1 AS x", "1"),
        ("both", "CREATE (), (:A:B) RETURN 1 AS x", "1"),
        ("nan", "CREATE () RETURN 0.0/0.0 AS x", "NaN"),
        ("already required", "RETURN 2 AS x", "2"),
    ]
    source = "Feature: model scope\n" + "".join(
        f'''  Scenario: {name}
    Given an empty graph
    When executing query:
      """
      {query}
      """
    Then the result should be, in any order:
      | x |
      | {value} |
''' for name, query, value in queries
    )
    uri = "clauses/create/Example.feature"
    cases = compile_feature(source, uri)
    cases[0].update(conformance="failed", reason="Still not implemented")
    report = {"upstream_revision": "pinned", "case_count": len(cases), "feature_count": 1,
              "feature_sha256_lf": {uri: hashlib.sha256(source.encode()).hexdigest()}, "cases": cases}
    previous = freeze_ledger(report, review_candidates(report), decision="original")
    return report, previous


def test_successor_is_monotone_without_changing_parent_source_or_observations():
    report, previous = fixture()
    before = deepcopy(previous)
    successor = expand_flexible_ledger(report, previous, decision="authorized expansion")
    assert previous == before
    assert successor["profile_counts"] == {"required": 3, "architectural_divergence": 2}
    assert len(successor["scope_changes"]) == 3
    for old, new in zip(previous["cases"], successor["cases"], strict=True):
        for field in ("case_sha256", "source_sha256_lf", "expected", "owner", "baseline"):
            assert old[field] == new[field]
    assert successor["cases"][0]["baseline"]["status"] == "failed"
    assert successor["cases"][0]["profile"]["inclusion"] == "required"
    assert successor["cases"][1]["profile"] == previous["cases"][1]["profile"]
    assert {e["rule"] for e in successor["cases"][2]["profile"]["evidence"]} == {"MODEL_MULTILABEL"}
    verify_ledger(previous, report)
    verify_ledger(successor, report, predecessor=previous)
    summary = profile_summary(report, successor, predecessor=previous)
    assert summary["required_profile"]["failed"] == 1


@pytest.mark.parametrize("mutation", ["exclusion", "pass", "expectation", "hash", "audit", "count", "owner"])
def test_successor_verification_rejects_rewriting_scope_results_or_provenance(mutation):
    report, previous = fixture()
    successor = expand_flexible_ledger(report, previous, decision="authorized expansion")
    if mutation == "exclusion":
        successor["cases"][0]["profile"] = deepcopy(previous["cases"][0]["profile"])
    elif mutation == "pass":
        successor["cases"][0]["baseline"]["status"] = "passed"
    elif mutation == "expectation":
        successor["cases"][0]["expected"] = {}
    elif mutation == "hash":
        successor["predecessor"]["canonical_sha256"] = "wrong"
    elif mutation == "audit":
        successor["scope_changes"].clear()
    elif mutation == "count":
        successor["profile_counts"]["required"] = 1
    else:
        successor["cases"][0]["owner"] = "FP-8"
    with pytest.raises(ValueError):
        verify_ledger(successor, report, predecessor=previous)


def test_missing_stale_draft_or_successor_parent_is_not_accepted():
    report, previous = fixture()
    successor = expand_flexible_ledger(report, previous, decision="authorized expansion")
    with pytest.raises(ValueError, match="predecessor"):
        verify_ledger(successor, report)
    stale = deepcopy(previous)
    stale["cases"][0]["case_sha256"] = "wrong"
    with pytest.raises(ValueError):
        verify_ledger(successor, report, predecessor=stale)
    for parent in (successor, {**previous, "status": "draft_pending_architectural_review"}):
        with pytest.raises(ValueError, match="frozen V1"):
            expand_flexible_ledger(report, parent, decision="authorized expansion")
    with pytest.raises(ValueError):
        expand_flexible_ledger(report, previous, decision=" ")


def test_inventory_cli_passes_parent_to_verification_and_summary(tmp_path, monkeypatch):
    from tools import check_opencypher

    report, previous = fixture()
    successor = expand_flexible_ledger(report, previous, decision="authorized expansion")
    parent, child, output = (tmp_path / name for name in ("v1.json", "v2.json", "report.json"))
    parent.write_text(json.dumps(previous), encoding="utf-8")
    child.write_text(json.dumps(successor), encoding="utf-8")
    monkeypatch.setattr(check_opencypher, "inventory", lambda path: deepcopy(report))
    monkeypatch.setattr("sys.argv", ["check_opencypher", "--checkout", str(tmp_path),
        "--output", str(output), "--verify-ledger", str(child), "--predecessor-ledger", str(parent)])
    assert check_opencypher.main() == 0
    observed = json.loads(output.read_text(encoding="utf-8"))
    assert observed["profile_summary"]["required_profile"]["failed"] == 1
    assert json.loads(parent.read_text(encoding="utf-8")) == previous


def test_inventory_cli_cannot_overwrite_expanded_frozen_ledger(tmp_path, monkeypatch):
    from tools import check_opencypher

    report, previous = fixture()
    successor = expand_flexible_ledger(report, previous, decision="authorized expansion")
    child = tmp_path / "v2.json"
    original = json.dumps(successor)
    child.write_text(original, encoding="utf-8")
    monkeypatch.setattr(check_opencypher, "inventory", lambda path: deepcopy(report))
    monkeypatch.setattr("sys.argv", ["check_opencypher", "--checkout", str(tmp_path),
        "--output", str(tmp_path / "report.json"), "--ledger-output", str(child)])
    with pytest.raises(SystemExit):
        check_opencypher.main()
    assert child.read_text(encoding="utf-8") == original
