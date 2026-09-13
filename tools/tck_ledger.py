"""Versioned, lossless accounting for the pinned reference; no inferred passes.

Classification is ownership, not a claim that a parser message is a root cause.
Unreviewed architectural differences remain required until an explicit decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy

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


def verify_ledger(ledger: dict, report: dict, *, predecessor: dict | None = None,
                  ancestor: dict | None = None) -> None:
    """Refuse missing/added cases or changed source expectations before execution."""
    if ledger["schema_version"] != 1 or ledger["upstream_revision"] != report["upstream_revision"]:
        raise ValueError("Ledger schema/revision mismatch")
    frozen_status = {"grafx-local-first-fp-v2": "frozen_model_expansion",
                     "grafx-local-first-fp-v3": "frozen_multilabel_nested_storage"}
    if (ledger.get("profile_id") in frozen_status
            and ledger.get("status") != frozen_status[ledger["profile_id"]]):
        raise ValueError("Frozen successor status changed")
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
    if ledger.get("status") == "frozen_model_expansion":
        if predecessor is None:
            raise ValueError("Expanded profile verification requires its frozen predecessor")
        expected = expand_flexible_ledger(report, predecessor, decision=ledger["review_decision"])
        if ledger != expected:
            raise ValueError("Expanded profile differs from its source-bound authorized succession")
    if ledger.get("status") == "frozen_multilabel_nested_storage":
        if predecessor is None or ancestor is None:
            raise ValueError("V3 verification requires its frozen V2 predecessor and V1 ancestor")
        expected = expand_multilabel_ledger(
            report, predecessor, ancestor=ancestor, decision=ledger["review_decision"],
            nested_storage_decision=ledger["nested_storage_decision"],
        )
        if ledger != expected:
            raise ValueError("V3 differs from its source-bound authorized succession")


def expand_flexible_ledger(report: dict, predecessor: dict, *, decision: str) -> dict:
    """Withdraw only authorized unlabeled/NaN exclusions; never infer a passing case.

    This is a monotone V1-to-V2 scope expansion, not a generic exclusion editor.
    All source contracts, ownership and historical observations are preserved.
    """
    if (not decision.strip() or predecessor.get("status") != "frozen_checkpoint_a"
            or predecessor.get("profile_id") != "grafx-local-first-fp-v1"):
        raise ValueError("Model expansion requires a frozen V1 predecessor and explicit decision")
    verify_ledger(predecessor, report)
    ledger = deepcopy(predecessor)
    changes = []
    for entry in ledger["cases"]:
        prior = entry["profile"]
        if prior["inclusion"] != "architectural_divergence":
            continue
        retained = [item for item in prior["evidence"]
                    if item["rule"] != "MODEL_UNLABELED_CREATE"
                    and not (item["rule"] == "FINITE_ARITHMETIC" and item["counterexample"] == "NaN")]
        if retained == prior["evidence"]:
            continue
        entry["profile"] = ({"inclusion": "architectural_divergence", "decision": decision,
                             "evidence": retained} if retained else
                            {"inclusion": "required", "decision": decision})
        if not retained:
            entry["root_cause"] = "authorized_model_expansion_execution_pending"
        changes.append({"id": entry["id"], "case_sha256": entry["case_sha256"],
                        "previous_profile": prior, "current_profile": deepcopy(entry["profile"])})
    ledger.update(
        profile_id="grafx-local-first-fp-v2", status="frozen_model_expansion",
        review_decision=decision,
        predecessor={"profile_id": predecessor["profile_id"], "canonical_sha256": hashlib.sha256(
            json.dumps(predecessor, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()},
        scope_changes=changes,
        profile_counts=dict(Counter(e["profile"]["inclusion"] for e in ledger["cases"])),
        policy="Authorized unlabeled storage and expression NaN expand required scope. "
               "No new exclusions, rewritten expectations or inferred passes; V1 remains historical evidence.",
    )
    return ledger


NESTED_STORAGE_CASE = "clauses/set/Set1.feature#0010"
NESTED_STORAGE_CASE_SHA256 = "101a110fe6773e7cc0e3d04f65833ad656e14105c34ed0cd677212689b4564fd"


def expand_multilabel_ledger(report: dict, predecessor: dict, *, ancestor: dict,
                            decision: str, nested_storage_decision: str) -> dict:
    """Apply the two explicit V3 decisions, not an arbitrary exclusion allowlist.

    Label exclusions become requirements. Only the exact pinned Set1 #0010 oracle
    becomes a divergence; its runtime error expectation and observations survive.
    """
    if (not decision.strip() or not nested_storage_decision.strip()
            or predecessor.get("profile_id") != "grafx-local-first-fp-v2"
            or predecessor.get("status") != "frozen_model_expansion"):
        raise ValueError("V3 requires frozen V2, V1 ancestor and both explicit decisions")
    verify_ledger(predecessor, report, predecessor=ancestor)
    nested = next((case for case in report["cases"] if case["id"] == NESTED_STORAGE_CASE), None)
    if nested is None or case_checksum(nested) != NESTED_STORAGE_CASE_SHA256:
        raise ValueError("Nested-storage decision requires the exact pinned Set1 #0010 source")
    ledger = deepcopy(predecessor)
    changes = []
    for entry in ledger["cases"]:
        prior = entry["profile"]
        if entry["id"] == NESTED_STORAGE_CASE:
            if prior["inclusion"] != "required":
                raise ValueError("Nested-storage predecessor must keep Set1 #0010 required")
            entry["profile"] = {
                "inclusion": "architectural_divergence", "decision": nested_storage_decision,
                "evidence": [{"rule": "NATIVE_NESTED_PROPERTY_STORAGE", "step": 1,
                              "counterexample": "CREATE (a)\nSET a.maplist = [{num: 1}]",
                              "expected_error": "TypeError/runtime/InvalidPropertyType"}],
            }
            entry["root_cause"] = "declared_native_nested_storage_divergence"
        elif prior["inclusion"] == "architectural_divergence":
            retained = [item for item in prior["evidence"] if item["rule"] != "MODEL_MULTILABEL"]
            if retained == prior["evidence"]:
                continue
            entry["profile"] = ({"inclusion": "architectural_divergence", "decision": decision,
                                 "evidence": retained} if retained else
                                {"inclusion": "required", "decision": decision})
            if not retained:
                entry["root_cause"] = "authorized_multilabel_execution_pending"
        else:
            continue
        changes.append({"id": entry["id"], "case_sha256": entry["case_sha256"],
                        "previous_profile": prior, "current_profile": deepcopy(entry["profile"])})
    ledger.update(
        profile_id="grafx-local-first-fp-v3", status="frozen_multilabel_nested_storage",
        review_decision=decision, nested_storage_decision=nested_storage_decision,
        predecessor={"profile_id": predecessor["profile_id"], "canonical_sha256": hashlib.sha256(
            json.dumps(predecessor, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()},
        scope_changes=changes,
        profile_counts=dict(Counter(e["profile"]["inclusion"] for e in ledger["cases"])),
        policy="Multiple labels are required native functionality. Only pinned Set1 #0010 is a "
               "new explicit divergence: Grafx retains nested property storage. Original expectations "
               "and observations are preserved; neither reclassification implies a pass. V1/V2 remain historical.",
    )
    return ledger


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


def profile_summary(report: dict, ledger: dict, *, predecessor: dict | None = None,
                    ancestor: dict | None = None) -> dict:
    """Keep observed upstream outcomes distinct from adapted profile acceptance."""
    verify_ledger(ledger, report, predecessor=predecessor, ancestor=ancestor)
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
