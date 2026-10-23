"""Reproducible, non-temporal four-arm RF04 evidence.

Run ``report`` with four clean worktrees. Each arm executes identical queries
against input-derived fixtures in its own Python process. This report qualifies
semantic equivalence and physical access only; it makes no latency claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


REVISIONS = {
    "base": "da8d821b233a792553a833d69050aa19be2778aa",
    "rf03_only": "237775048ab4ee162a563328215e4ce2c6029f2f",
    "rf04_only": "3736ae08a5ca30310b4f383e001258bf98f4fb52",
    "both": "1b8e5291a5dd77e8725297f86af9333237ec34e2",
}
PLANNER = "src/okto_grafx/domain/query/planner.py"
ENGINE = "src/okto_grafx/engine/query_engine.py"
CASES = ("target", "fan_limit", "hub", "fallback")
PROFILES = ("original", "with")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).encode("utf-8")).hexdigest()


def _require_clean_source(root: Path) -> None:
    if (_git(root, "status", "--porcelain", "--untracked-files=no")
            or _git(root, "status", "--porcelain", "--untracked-files=all", "--",
                    "src", "bench", "tests/query/test_conditional_endpoint_evidence.py")):
        raise ValueError("RF04 source worktree has tracked or shadowing untracked changes")


def source_isolation(roots: dict[str, Path]) -> dict:
    """Reject changed source trees and a factorial with unrelated code drift."""
    if set(roots) != set(REVISIONS):
        raise ValueError("all four RF04 source arms are required")
    resolved = {arm: Path(root).resolve(strict=True) for arm, root in roots.items()}
    for arm, root in resolved.items():
        if _git(root, "rev-parse", "HEAD") != REVISIONS[arm]:
            raise ValueError(f"{arm} worktree does not match its immutable revision")
        _require_clean_source(root)
    expected = {
        ("base", "rf03_only"): {PLANNER},
        ("base", "rf04_only"): {ENGINE},
        ("rf03_only", "both"): {ENGINE},
        ("rf04_only", "both"): {PLANNER},
    }
    changes = {}
    for (left, right), names in expected.items():
        observed = set(_git(resolved[left], "diff", "--name-only", REVISIONS[left],
                            REVISIONS[right]).splitlines())
        if observed != names:
            raise ValueError(f"{left}/{right} changes {sorted(observed)}, expected {sorted(names)}")
        changes[f"{left}_to_{right}"] = sorted(observed)
    # The same RF03 and RF04 patches must apply regardless of the other factor.
    for left, right, other_left, other_right, path in (
        ("base", "rf03_only", "rf04_only", "both", PLANNER),
        ("base", "rf04_only", "rf03_only", "both", ENGINE),
    ):
        before = _git(resolved[left], "show", f"{REVISIONS[left]}:{path}")
        after = _git(resolved[right], "show", f"{REVISIONS[right]}:{path}")
        other_before = _git(resolved[other_left], "show", f"{REVISIONS[other_left]}:{path}")
        other_after = _git(resolved[other_right], "show", f"{REVISIONS[other_right]}:{path}")
        if before != other_before or after != other_after:
            raise ValueError(f"{path} differs across the two factorial contrasts")
    return {"revisions": REVISIONS, "changed_files": changes,
            "scope": "all four commits have identical trees except the two isolated feature switches"}


def observe_arm(arm: str, root: Path) -> dict:
    if arm not in REVISIONS:
        raise ValueError("unknown RF04 arm")
    root = root.resolve(strict=True)
    if _git(root, "rev-parse", "HEAD") != REVISIONS[arm]:
        raise ValueError("arm revision differs from source root")
    _require_clean_source(root)
    sys.path[:0] = [str(root / "src"), str(root)]
    import okto_grafx  # noqa: PLC0415
    from bench.harness.campaign_fixtures import Scenario, build_fixture, oracle, query_for  # noqa: PLC0415
    from bench.harness.rf04_qualification import matrix  # noqa: PLC0415

    if not Path(okto_grafx.__file__).resolve().is_relative_to(root / "src"):
        raise ValueError("Grafx import escaped the selected source worktree")
    scenarios = {item.name: item for item in matrix(Scenario)}
    output = {}
    with TemporaryDirectory(prefix=f"grafx-rf04-{arm}-") as temporary:
        # Target/fan-limit share one immutable uniform graph. Both query profiles
        # use the same database snapshot, so planner differences cannot be
        # explained by fixture construction or setup churn.
        for case_group, reference in (("uniform", "target"), ("hub", "hub"),
                                      ("fallback", "fallback")):
            fixture = scenarios[f"rf04-with-{reference}"]
            with okto_grafx.connect(Path(temporary) / case_group, codec="pure",
                                    vector_math="pure", query_memory_budget_bytes=1 << 20) as db:
                build_fixture(db, fixture)
                for profile in PROFILES:
                    for case in (("target", "fan_limit") if case_group == "uniform" else (reference,)):
                        scenario = scenarios[f"rf04-{profile}-{case}"]
                        query, params = query_for(scenario)
                        expected = oracle(scenario)
                        plan = [type(node).__name__ for node in db.explain(query).walk()]
                        if db._queries._query_memory_budget_bytes != 1 << 20:
                            raise AssertionError("RF04 logical query budget was not active")
                        with db._queries._observe_logical_memory() as observer:
                            result = db.execute(query, params)
                        memory = observer.result()
                        peak = memory.get("logical_memory_peak_bytes")
                        if (type(peak) is not int or not 0 <= peak <= 1 << 20
                                or type(memory.get("baseline_retained_bytes")) is not int
                                or not 0 <= memory["baseline_retained_bytes"] <= 1 << 20
                                or not memory.get("coverage")
                                or memory.get("unavailable_reason") is not None):
                            raise AssertionError(f"{arm}/{scenario.name}: logical memory is unproved")
                        if result.rows != expected:
                            raise AssertionError(f"{arm}/{scenario.name}: source-derived row oracle differs")
                        stats = dict(result.statistics)
                        for key in ("physical_node_versions_examined", "physical_edge_versions_examined",
                                    "edge_lookups", "edge_scans", "rows_seeked", "rows_scanned"):
                            stats.setdefault(key, 0)
                        output[scenario.name] = {
                            "rows": len(result.rows), "rows_sha256": _hash(result.rows),
                            "query_sha256": _hash([query, params]),
                            "fixture_sha256": _hash([scenario.nodes, scenario.destination_nodes,
                                                      scenario.parallel_edges, scenario.distribution,
                                                      scenario.seed]),
                            "plan": plan, "statistics": stats, "memory": memory,
                        }
    return {"schema_version": "grafx-rf04-structural-arm/v1", "arm": arm,
            "revision": REVISIONS[arm], "query_memory_budget_bytes": 1 << 20,
            "source_module": str(Path(okto_grafx.__file__).resolve().relative_to(root)),
            "cases": output}


def evaluate(arms: dict[str, dict], lineage: dict) -> dict:
    if set(arms) != set(REVISIONS):
        raise ValueError("four RF04 observations required")
    names = {f"rf04-{profile}-{case}" for profile in PROFILES for case in CASES}
    for arm, observation in arms.items():
        if (observation.get("arm"), observation.get("revision"),
                observation.get("query_memory_budget_bytes")) != (arm, REVISIONS[arm], 1 << 20):
            raise ValueError(f"{arm}: invalid source or query budget")
        if set(observation["cases"]) != names:
            raise ValueError(f"{arm}: incomplete RF04 matrix")
        for name, sample in observation["cases"].items():
            peak = sample["memory"].get("logical_memory_peak_bytes")
            if (type(peak) is not int or not 0 <= peak <= 1 << 20
                    or type(sample["memory"].get("baseline_retained_bytes")) is not int
                    or not 0 <= sample["memory"]["baseline_retained_bytes"] <= 1 << 20
                    or not sample["memory"].get("coverage")
                    or sample["memory"].get("unavailable_reason") is not None):
                raise ValueError(f"{arm}/{name}: logical memory missing or over quota")
    contrasts = {}
    for name in sorted(names):
        samples = {arm: arms[arm]["cases"][name] for arm in REVISIONS}
        for field in ("rows", "rows_sha256", "query_sha256", "fixture_sha256"):
            if len({sample[field] for sample in samples.values()}) != 1:
                raise ValueError(f"{name}: four-arm semantic/query/fixture mismatch in {field}")
        contrasts[name] = {arm: sample["statistics"] for arm, sample in samples.items()}
    for profile, inactive, active in (("with", "base", "rf04_only"),
                                      ("original", "rf03_only", "both")):
        target = f"rf04-{profile}-target"
        left = contrasts[target][inactive]
        right = contrasts[target][active]
        if not (left["edge_lookups"] == 0 and left["edge_scans"] >= 1
                and right["edge_lookups"] == 16 and right["edge_scans"] == 0
                and 0 < right["physical_edge_versions_examined"]
                < left["physical_edge_versions_examined"]):
            raise ValueError(f"{profile}: endpoint contrast is absent")
        fan = contrasts[f"rf04-{profile}-fan_limit"][active]
        if (fan["edge_lookups"], fan["edge_scans"]) != (64, 1):
            raise ValueError(f"{profile}: fan-limit guard is absent")
        hub = contrasts[f"rf04-{profile}-hub"][active]
        if hub["edge_lookups"] != 16 or hub["edge_scans"] != 0:
            raise ValueError(f"{profile}: skewed hub lost bounded endpoint access")
    for arm in REVISIONS:
        fallback = arms[arm]["cases"]["rf04-with-fallback"]
        if ("NodeMultiKeySeek" not in fallback["plan"]
                or fallback["statistics"].get("fallback_reason_nonselective_probe_set") != 1
                or fallback["statistics"]["physical_node_versions_examined"] < 16):
            raise ValueError(f"{arm}: broad conditional node-scan fallback not observed")
    return {"schema_version": "grafx-rf04-structural-report/v1",
            "decision": "structural_pass", "latency_certified": False,
            "source_isolation": lineage, "arm_hashes": {arm: _hash(data) for arm, data in arms.items()},
            "contrasts": contrasts,
            "scope": "input-derived ordered rows, equal fixtures/queries, physical counters, fan limit, hub and actual broad fallback; no temporal gain asserted"}


def functional_guards(root: Path) -> dict:
    """Execute the candidate's real-storage overlay/direction/self-loop suite."""
    path = root / "tests/query/test_conditional_endpoint_evidence.py"
    methods = (
        "test_sixteen_starts_without_global_edge_census",
        "test_fan_limit_and_actual_conditional_node_fallback",
        "test_directions_parallel_self_loop_and_owned_overlay",
    )
    result = subprocess.run([sys.executable, str(path), "-v"], cwd=root,
                            check=True, capture_output=True, text=True)
    if "Ran 3 tests" not in result.stderr or "OK" not in result.stderr or any(
            method not in result.stderr for method in methods):
        raise ValueError("candidate RF04 semantic guard suite was not fully exercised")
    return {"revision": REVISIONS["both"], "module": str(path.relative_to(root)),
            "module_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "passed_methods": list(methods)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    single = sub.add_parser("observe")
    single.add_argument("--arm", choices=REVISIONS, required=True)
    single.add_argument("--source-root", type=Path, required=True)
    report = sub.add_parser("report")
    for arm in REVISIONS:
        report.add_argument("--" + arm.replace("_", "-"), type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "observe":
        print(json.dumps(observe_arm(args.arm, args.source_root), sort_keys=True))
        return
    roots = {arm: getattr(args, arm).resolve(strict=True) for arm in REVISIONS}
    lineage = source_isolation(roots)
    arms = {}
    for arm, root in roots.items():
        command = [sys.executable, str(Path(__file__).resolve()), "observe", "--arm", arm,
                   "--source-root", str(root)]
        arms[arm] = json.loads(subprocess.run(command, cwd=root, check=True, capture_output=True,
                                              text=True).stdout)
    guards = functional_guards(roots["both"])
    result = {"report": evaluate(arms, lineage), "observations": arms,
              "candidate_functional_guards": guards}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"decision": result["report"]["decision"],
                      "output": str(args.output), "arm_hashes": result["report"]["arm_hashes"]},
                     sort_keys=True))


if __name__ == "__main__":
    main()
