"""Versioned, lossless accounting for the pinned reference; no inferred passes.

Classification is ownership, not a claim that a parser message is a root cause.
Unreviewed architectural differences remain required until an explicit decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

ERROR_STEP = re.compile(
    r"a (\w+) should be raised at (compile time|runtime|any time): (\w+|\*)\Z"
)
EFFECT_KEYS = frozenset({"+nodes", "-nodes", "+relationships", "-relationships",
                         "+labels", "-labels", "+properties", "-properties"})
STEP_NAMES = {
    "any graph": "graph",
    "an empty graph": "graph",
    "the binary-tree-1 graph": "named_fixture",
    "the binary-tree-2 graph": "named_fixture",
    "having executed:": "setup_query",
    "executing query:": "query",
    "executing control query:": "control_query",
    "parameters are:": "parameters",
    "the result should be, in any order:": "bag_result",
    "the result should be, in order:": "ordered_result",
    "the result should be (ignoring element order for lists):": "list_bag_result",
    "the result should be, in order (ignoring element order for lists):": "ordered_list_bag_result",
    "the result should be empty": "empty_result",
    "no side effects": "zero_effects",
    "the side effects should be:": "effects",
}


def semantic_steps(case: dict) -> list[dict]:
    """Retain original steps/arguments, excluding compiler-generated AST IDs."""
    return [{key: step[key] for key in ("text", "argument") if key in step}
            for step in case["steps"]]


def case_checksum(case: dict) -> str:
    """Bind the expanded query, fixture, parameters and expectations together."""
    payload = {"id": case["id"], "name": case["name"], "steps": semantic_steps(case)}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def step_family(name: str) -> str:
    """Inventory every recognized step; unfamiliar grammar refuses ledger creation."""
    if name in STEP_NAMES:
        return STEP_NAMES[name]
    if ERROR_STEP.fullmatch(name):
        return "expected_error"
    if name.startswith("there exists a procedure ") and name.endswith(":"):
        return "procedure_fixture"
    raise ValueError(f"Unclassified reference step: {name}")


def expected_contract(case: dict) -> dict:
    """Extract every outcome in sequence, including separate control-query results."""
    outcomes = []
    for index, step in enumerate(case["steps"]):
        name = step["text"]
        family = step_family(name)
        error = ERROR_STEP.fullmatch(name)
        if error:
            outcomes.append({"step": index, "kind": "error", "type": error[1],
                             "phase": error[2], "detail": error[3]})
        elif family in {"effects", "zero_effects"}:
            effects = {key: 0 for key in sorted(EFFECT_KEYS)}
            seen = set()
            if family == "effects":
                for row in step["argument"]["dataTable"]["rows"]:
                    key, count = [cell["value"] for cell in row["cells"]]
                    if key not in EFFECT_KEYS or key in seen or not count.isdigit():
                        raise ValueError(f"Invalid side-effect expectation: {key}={count}")
                    seen.add(key)
                    effects[key] = int(count)
            outcomes.append({"step": index, "kind": "effects", "counts": effects})
        elif "result" in family:
            outcomes.append({"step": index, "kind": family})
    return {"outcomes": outcomes}


def capability_owner(case: dict) -> tuple[str, str]:
    """Assign primary family ownership; cross-cutting dependencies remain visible."""
    family = "/".join(case["id"].split("/")[:2])
    if family == "expressions/temporal":
        return "FP-5", family
    if family == "clauses/call":
        return "FP-7", family
    if family == "expressions/existentialSubqueries":
        return "FP-4", family
    if family in {"expressions/graph", "expressions/path", "expressions/pattern",
                  "clauses/match", "clauses/match-where", "clauses/union"}:
        return "FP-3", family
    if family.startswith("expressions/") or family in {
        "clauses/return", "clauses/return-orderby", "clauses/return-skip-limit",
        "clauses/with", "clauses/with-orderBy", "clauses/with-skip-limit",
        "clauses/with-where", "clauses/unwind",
    }:
        return "FP-2", family
    if family in {"clauses/create", "clauses/delete", "clauses/merge",
                  "clauses/remove", "clauses/set"}:
        return "FP-4", family
    if family.startswith("useCases/"):
        return "FP-8", family
    raise ValueError(f"Unclassified reference capability: {family}")


def build_ledger(report: dict) -> dict:
    """Build all-case accounting without converting unexecuted cases into success."""
    entries = []
    seen = set()
    families = Counter()
    for case in report["cases"]:
        if case["id"] in seen:
            raise ValueError(f"Duplicate case: {case['id']}")
        seen.add(case["id"])
        steps = [step_family(step["text"]) for step in case["steps"]]
        families.update(steps)
        owner, capability = capability_owner(case)
        uri = case["id"].split("#")[0]
        status = case.get("conformance", "not_run")
        if status not in {"passed", "failed", "not_run", "adapted_passed"}:
            raise ValueError(f"Invalid observed conformance: {status}")
        entries.append({
            "id": case["id"], "case_sha256": case_checksum(case),
            "source_sha256_lf": report["feature_sha256_lf"][uri],
            "owner": owner, "capability": capability,
            "step_families": sorted(set(steps)),
            "expected": expected_contract(case),
            "profile": {"inclusion": "required", "decision": None},
            "baseline": {"status": status, "reason": case.get("reason")},
            "root_cause": "not_diagnosed" if status != "passed" else None,
        })
    if len(entries) != report["case_count"]:
        raise ValueError("Inventory case count does not match its cases")
    return {
        "schema_version": 1, "profile_id": "grafx-local-first-fp-v1",
        "status": "draft_pending_architectural_review",
        "upstream_revision": report["upstream_revision"],
        "graph_fixture_sha256_lf": {name: fixture["sha256_lf"]
                                     for name, fixture in report.get("graph_fixtures", {}).items()},
        "case_count": len(entries), "feature_count": report["feature_count"],
        "step_family_counts": dict(sorted(families.items())),
        "owner_counts": dict(sorted(Counter(e["owner"] for e in entries).items())),
        "policy": "No automatic exclusions; ownership is not root-cause diagnosis. "
                  "Baseline observations are not current profile acceptance.",
        "cases": entries,
    }


def verify_ledger(ledger: dict, report: dict) -> None:
    """Refuse missing/added cases or changed source expectations before execution."""
    if ledger["schema_version"] != 1 or ledger["upstream_revision"] != report["upstream_revision"]:
        raise ValueError("Ledger schema/revision mismatch")
    graph_hashes = {name: fixture["sha256_lf"] for name, fixture in report.get("graph_fixtures", {}).items()}
    if ledger.get("graph_fixture_sha256_lf", {}) != graph_hashes:
        raise ValueError("Ledger named graph fixture coverage/source mismatch")
    for fixture in report.get("graph_fixtures", {}).values():
        if hashlib.sha256(fixture["query"].encode("utf-8")).hexdigest() != fixture["sha256_lf"]:
            raise ValueError("Named graph fixture query checksum mismatch")
    current = {case["id"]: case for case in report["cases"]}
    saved = {entry["id"]: entry for entry in ledger["cases"]}
    if (len(saved) != len(ledger["cases"]) or len(current) != len(report["cases"])
            or saved.keys() != current.keys()
            or ledger["case_count"] != len(saved) or report["case_count"] != len(current)):
        raise ValueError("Ledger case coverage mismatch")
    for identity, case in current.items():
        entry = saved[identity]
        uri = identity.split("#")[0]
        if (entry["case_sha256"] != case_checksum(case)
                or entry["source_sha256_lf"] != report["feature_sha256_lf"][uri]
                or entry["expected"] != expected_contract(case)):
            raise ValueError(f"Ledger source/expectation changed: {identity}")
    if ledger.get("status") == "frozen_checkpoint_a":
        reviewed = {"upstream_revision": report["upstream_revision"], "cases": [
            {"id": e["id"], "case_sha256": e["case_sha256"], "evidence": e["profile"].get("evidence", [])}
            for e in ledger["cases"] if e["profile"]["inclusion"] == "architectural_divergence"]}
        checked = freeze_ledger(report, reviewed, decision=ledger["review_decision"])
        if ([e["profile"] for e in ledger["cases"]] != [e["profile"] for e in checked["cases"]]
                or ledger["profile_counts"] != checked["profile_counts"]):
            raise ValueError("Frozen profile decisions/counts changed")


def freeze_ledger(report: dict, reviewed: dict, *, decision: str) -> dict:
    """Apply a source-bound explicit review; never infer exclusions from failures."""
    if not decision.strip() or reviewed["upstream_revision"] != report["upstream_revision"]:
        raise ValueError("A matching source revision and explicit review decision are required")
    ledger = build_ledger(report)
    if __package__:
        from tools.tck_profile import architectural_candidates
    else:
        from tck_profile import architectural_candidates
    original = {case["id"]: case for case in report["cases"]}
    entries = {entry["id"]: entry for entry in ledger["cases"]}
    seen = set()
    for candidate in reviewed["cases"]:
        identity = candidate["id"]
        if identity in seen or identity not in entries:
            raise ValueError("Duplicate or unknown reviewed case")
        seen.add(identity)
        entry = entries[identity]
        if candidate["case_sha256"] != entry["case_sha256"] or not candidate["evidence"]:
            raise ValueError("Review has stale source or no architectural counterexample")
        proven = architectural_candidates(original[identity])["evidence"]
        if any(evidence not in proven for evidence in candidate["evidence"]):
            raise ValueError("Reviewed counterexample is not present in the bound source")
        if any(e["rule"] not in {"MODEL_UNLABELED_CREATE", "MODEL_MULTILABEL", "FINITE_ARITHMETIC"}
               or not e["counterexample"] for e in candidate["evidence"]):
            raise ValueError("Review uses a rule outside the approved architectural boundaries")
        entry["profile"] = {"inclusion": "architectural_divergence", "decision": decision,
                             "evidence": candidate["evidence"]}
        entry["root_cause"] = "declared_model_divergence"
    for entry in entries.values():
        if entry["root_cause"] == "not_diagnosed":
            entry["root_cause"] = "initial_execution_pending"
    ledger["status"] = "frozen_checkpoint_a"
    ledger["review_decision"] = decision
    ledger["profile_counts"] = dict(Counter(e["profile"]["inclusion"] for e in entries.values()))
    ledger["policy"] = "All source cases stay in upstream accounting. Reviewed model divergences are not passes. " \
                       "Every other case remains required; new exclusions require an explicit recorded decision."
    return ledger


def profile_summary(report: dict, ledger: dict) -> dict:
    """Keep observed upstream outcomes distinct from adapted profile acceptance."""
    verify_ledger(ledger, report)
    scope = {entry["id"]: entry["profile"]["inclusion"] for entry in ledger["cases"]}
    upstream, required, divergent = Counter(), Counter(), Counter()
    for case in report["cases"]:
        observed = case.get("conformance", "not_run")
        if observed not in {"passed", "failed", "not_run", "adapted_passed"}:
            raise ValueError(f"Unknown execution outcome: {observed}")
        upstream["adapted_not_upstream_certified" if observed == "adapted_passed" else observed] += 1
        if scope[case["id"]] == "required":
            required["passed_with_adaptation" if observed == "adapted_passed" else observed] += 1
        else:
            divergent[observed] += 1
    return {"upstream": dict(upstream), "required_profile": dict(required),
            "architectural_divergences": dict(divergent)}
