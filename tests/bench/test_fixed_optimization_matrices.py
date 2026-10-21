"""Qualified RF02/RF03/RF05 claims require every declared target and control."""
from argparse import Namespace
from copy import deepcopy
from dataclasses import replace

import pytest

from bench.harness.campaign_fixtures import Scenario, oracle
from bench.harness.campaign import run_campaign
from bench.harness.campaign_schema import (CampaignError, validate_campaign,
                                           validate_fixed_optimization_matrix, compare_campaign)
from bench.harness.campaign_worker import sample_record
from bench.harness.connected_qualification import matrix as rf03_matrix
from bench.harness.pagination_qualification import matrix as rf05_matrix
from bench.harness.rf02_qualification import matrix as rf02_matrix


def fixed_matrix(family):
    if family == "RF02":
        scenarios = rf02_matrix()
    elif family == "RF03":
        scenarios = rf03_matrix()
    else:
        scenarios = rf05_matrix(Scenario)
        scenarios[0] = replace(scenarios[0], role="target",
                               work_counter="physical_node_versions_examined")
    return [replace(scenario, query_memory_budget_bytes=4096).to_dict()
            for scenario in scenarios]


@pytest.mark.parametrize("family", ["RF02", "RF03", "RF05"])
def test_fixed_matrix_accepts_only_full_ordered_selection(family):
    selected = fixed_matrix(family)
    validate_fixed_optimization_matrix(selected, mode="optimization", intent="qualification")
    for changed in (selected[:-1], selected[::-1],
                    [*selected[:1], *selected[2:]],
                    [{**selected[0], "role": "control"}, *selected[1:]],
                    [{**selected[0], "query_memory_budget_bytes": None}, *selected[1:]]):
        with pytest.raises(CampaignError, match="matrix|budget"):
            validate_fixed_optimization_matrix(changed, mode="optimization",
                                               intent="qualification")


def test_rf05_first_page_is_the_only_qualified_target():
    selected = fixed_matrix("RF05")
    assert selected[0]["work_counter"] == "physical_node_versions_examined"
    assert all(item["role"] == "control" for item in selected[1:])
    changed = [dict(item) for item in selected]
    changed[1] = {**changed[1], "role": "target",
                  "work_counter": "physical_node_versions_examined"}
    with pytest.raises(CampaignError, match="RF05.*matrix"):
        validate_fixed_optimization_matrix(changed, mode="optimization",
                                           intent="qualification")


def test_schema_rejects_subset_before_workers_or_ratios():
    selected = fixed_matrix("RF02")[:-1]
    data = {"schema_version": "grafx-campaign/v1", "mode": "optimization",
            "intent": "qualification", "calibrated_executor": False,
            "command": ["python", "-m", "bench.harness.campaign"],
            "protocol": {"pairs": 3, "warmups": 5, "retained": 20,
                         "orders": ["AB", "BA", "AB"], "attempt": 1,
                         "previous_campaign_sha256": None, "max_cpu_percent": 20},
            "scenarios": selected, "runs": []}
    with pytest.raises(CampaignError, match="RF02.*matrix"):
        validate_campaign(data)


def test_runner_rejects_subset_before_source_lookup_or_worker_launch(tmp_path):
    chosen = rf02_matrix()
    args = Namespace(output=tmp_path / "out", scenario=[chosen[0].name, chosen[-1].name],
                     target=[], memory_budget=4096, max_cpu_percent=20,
                     worker_timeout=3600, baseline=tmp_path / "absent-A",
                     candidate=tmp_path / "absent-B", mode="optimization",
                     intent="qualification")
    with pytest.raises(CampaignError, match="RF02.*matrix"):
        run_campaign(args)
    assert not list(args.output.glob("request-*.json"))


def test_exploratory_slice_and_rf04_isolation_keep_their_own_scope():
    validate_fixed_optimization_matrix(fixed_matrix("RF02")[:2],
                                       mode="optimization", intent="exploratory")
    from bench.harness.rf04_qualification import phase_scenarios
    selected = [item.to_dict() for item in phase_scenarios("rf03", Scenario, budget=4096)]
    validate_fixed_optimization_matrix(selected, mode="optimization", intent="qualification")


def rf05_campaign():
    from tests.bench.test_campaign_schema import campaign

    data = campaign()
    data.update(mode="optimization", scenarios=fixed_matrix("RF05"))
    for run in data["runs"]:
        arm = run["arm"]
        run["samples"] = []
        for spec in data["scenarios"]:
            scenario = Scenario.from_dict(spec)
            case = scenario.pagination_case
            ordered = arm == "B" and case in ("first", "membership", "dynamic_membership", "keyset")
            stats = {"physical_node_versions_examined": 16 if ordered else scenario.nodes}
            if ordered:
                stats.update(ordered_candidates_examined=32 if case in ("membership", "dynamic_membership") else 16,
                             ordered_merge_rows=16)
            template = sample_record(
                spec, 0, oracle(scenario), stats,
                elapsed_ns=80 if ordered and case == "first" else 100,
                cpu_ns=10, prepare_ns=2,
                memory={"logical_memory_peak_bytes": 100, "baseline_retained_bytes": 0,
                        "coverage": "instrumented budgets", "unavailable_reason": None},
                rss_peak_bytes=100,
                selected_plan=["OrderedNodeMerge"] if ordered else ["Scan"],
            )
            for index in range(25):
                sample = deepcopy(template)
                sample["index"] = index
                sample["kind"] = "warmup" if index < 5 else "retained"
                run["samples"].append(sample)
    return data


def test_rf05_report_exposes_actual_ordered_work_and_rejects_missing_counter():
    data = rf05_campaign()
    report = compare_campaign(data)
    assert report["decision"] == "passed"
    target = report["comparisons"][0]
    assert target["rf05_access"]["by_arm"]["B"]["ordered_candidates_examined"]["median_count"] == 16
    assert target["rf05_access"]["by_arm"]["A"]["ordered_merge_rows"]["median_count"] == 0
    candidate = next(run for run in data["runs"] if run["arm"] == "B")
    candidate["samples"][0]["statistics"].pop("ordered_candidates_examined")
    with pytest.raises(CampaignError, match="RF05 candidate lacks"):
        compare_campaign(data)
