"""Reconcile every frozen supplemental ID without treating test paths as passes.

This records mapped test-case outcomes and the exact source/file receipts. The
native result is eligible for final reconciliation only with a terminal successful
full regression and an unchanged-input proof bound to the same JUnit bytes.
External contracts additionally require their actual installed/runtime evidence.
"""

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "docs/conformance/EXTENSION_SCENARIOS_V1.json"
COVERAGE = ROOT / "docs/conformance/EXTENSION_COVERAGE.md"
REQUIRED_HASH = "b87801ba4952bbe9d4921edbf7aeea84f1ef8bea54fb40f9b4d50d1be026f3a0"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def junit_index(path):
    """Retain original parameter IDs and failure/skip reasons, not just totals."""
    tree = ET.parse(path)
    result = []
    for case in tree.findall(".//testcase"):
        status = next((state for state in ("error", "failure", "skipped") if case.find(state) is not None), "passed")
        reason = case.find(status)
        result.append({"class": case.attrib.get("classname", ""), "name": case.attrib["name"],
                       "status": status, "seconds": float(case.attrib.get("time", 0)),
                       **({"reason": reason.attrib.get("message", reason.text or "")}
                          if reason is not None else {})})
    return result


def maps():
    result = {}
    for line in COVERAGE.read_text(encoding="utf-8").splitlines():
        found = re.match(r"\| (\d+) / ([A-Z0-9-]+) \|", line)
        if found:
            number, identity = int(found[1]), found[2]
            paths = list(dict.fromkeys(re.findall(r"\]\(../../(tests/[^)#]+\.py)\)", line)))
            result[identity] = {"number": number, "paths": paths, "coverage_text": line}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--proof", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert digest(REQUIREMENTS) == REQUIRED_HASH
    frozen = load(REQUIREMENTS)["scenarios"]
    covered = maps()
    assert len(frozen) == len(covered) == 22
    assert {case["id"] for case in frozen} == set(covered)
    entries = junit_index(args.junit)
    proof = load(args.proof) if args.proof else None
    qualified_full = bool(proof and proof.get("status") == "terminal" and proof.get("exit_code") == 0
                          and proof.get("inputs_unchanged") is True and not proof.get("changed_inputs")
                          and proof.get("junit_sha256") == digest(args.junit))
    assert not qualified_full or all(entry["status"] in ("passed", "skipped") for entry in entries)
    result = {"requirements_sha256": REQUIRED_HASH, "coverage_sha256": digest(COVERAGE),
              "junit": str(args.junit), "junit_sha256": digest(args.junit),
              "full_result": dict(Counter(entry["status"] for entry in entries)),
              "qualified_current_full_regression": qualified_full,
              "scope": "Coverage/outcome reconciliation, not a substitute for reviewing assertions, "
                       "installed old-reader tests, actual Pulse runtime or pinned competitor execution",
              "contracts": []}
    for requirement in frozen:
        identity = requirement["id"]
        mapping = covered[identity]
        record = {"id": identity, "requirement": requirement, "number": mapping["number"], "modules": []}
        for path in mapping["paths"]:
            module = path[:-3].replace("/", ".")
            current = ROOT / path
            assert current.is_file(), "Coverage points to a missing file: " + path
            tests = [entry for entry in entries if entry["class"] == module or entry["class"].startswith(module + ".")]
            assert tests, "Mapped test module not executed: " + path
            definitions = [node.name for node in ast.walk(ast.parse(current.read_text(encoding="utf-8")))
                           if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")]
            executed_base = {entry["name"].split("[", 1)[0] for entry in tests}
            assert set(definitions).issubset(executed_base), "Native test function absent from JUnit: " + path
            record["modules"].append({"path": path, "current_source_sha256": digest(current),
                                       "outcomes": dict(Counter(entry["status"] for entry in tests)),
                                       "cases": tests})
        if mapping["number"] <= 20:
            assert record["modules"], "Native contract without executable mapping: " + identity
            all_passed = all(entry["status"] == "passed" for mod in record["modules"] for entry in mod["cases"])
            current_hashes_match = bool(proof and all(proof["before"].get(mod["path"]) == mod["current_source_sha256"]
                                                     for mod in record["modules"]))
            record["status"] = ("mapped_native_tests_pass_on_qualified_full" if qualified_full and all_passed
                                and current_hashes_match else "pending_final_current_candidate_reconciliation")
            record["mapped_junit_all_passed"] = all_passed
        else:
            record["status"] = "requires_separate_external_receipts"
        result["contracts"].append(record)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    print("MAPPED_CONTRACTS", len(result["contracts"]), "QUALIFIED_FULL", qualified_full)
    for contract in result["contracts"]:
        outcomes = Counter()
        for mod in contract["modules"]:
            outcomes.update(mod["outcomes"])
        print(contract["number"], contract["id"], len(contract["modules"]), dict(outcomes), contract["status"])


if __name__ == "__main__":
    main()
