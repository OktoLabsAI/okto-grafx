"""RF04 four-arm recipe: fixed controls and real endpoint access evidence."""
from dataclasses import replace

import okto_grafx
import pytest

from bench.harness.campaign_fixtures import Scenario, build_fixture, matrix, oracle, query_for
from bench.harness.rf04_qualification import access, interaction_report, matrix as rf04_matrix, phase_scenarios
from bench.harness.campaign_schema import CampaignError
from bench.harness.campaign_worker import measure_statement


def test_four_phase_matrix_uses_fixed_equal_query_profiles_and_oracles():
    cases = {scenario.name: scenario for scenario in rf04_matrix(Scenario)}
    assert len(cases) == 8
    assert set(cases) <= {scenario.name for scenario in matrix()}
    for profile in ("original", "with"):
        target = cases[f"rf04-{profile}-target"]
        assert (target.nodes, target.destination_nodes, target.parallel_edges, len(target.keys)) == (
            1024, 128, 127, 16)
        assert len(oracle(target)) == len(oracle(replace(target, rf04_case=None,
                                                        name="control", keys=target.keys + target.keys)))
        assert Scenario.from_dict(target.to_dict()) == target
        assert len(cases[f"rf04-{profile}-fan_limit"].keys) == 65
        broad = cases[f"rf04-{profile}-fallback"]
        assert len(set(broad.keys)) >= 2 * broad.nodes
        assert len(oracle(cases[f"rf04-{profile}-hub"])) > len(oracle(target))
    assert query_for(cases["rf04-original-target"])[0] != query_for(cases["rf04-with-target"])[0]
    assert phase_scenarios("rf03", Scenario, budget=1 << 20)[0].work_counter == (
        "physical_node_versions_examined")
    assert phase_scenarios("incremental", Scenario, budget=1 << 20)[0].work_counter == (
        "physical_edge_versions_examined")
    with pytest.raises(ValueError, match="fixed matrix"):
        replace(cases["rf04-with-target"], keys=(0, 1))
    with pytest.raises(ValueError, match="fixed matrix"):
        replace(cases["rf04-with-target"], work_counter="index_probe_keys")


def test_real_rf04_target_preserves_rows_and_uses_endpoint_work(tmp_path):
    target = next(s for s in rf04_matrix(Scenario) if s.name == "rf04-with-target")
    with okto_grafx.connect(tmp_path / "db", codec="pure", vector_math="pure") as db:
        build_fixture(db, target)
        sample = measure_statement(db, target, 5)
        assert sample["rf04_access"] == access(sample)
        assert sample["cpu_ns"] > 0 and sample["rss_peak_bytes"] > 0
        assert "logical_memory_peak_bytes" in sample["memory"]
        for profile in ("with", "original"):
            scenario = next(s for s in rf04_matrix(Scenario) if s.name == f"rf04-{profile}-target")
            query, params = query_for(scenario)
            result = db.execute(query, params)
            assert result.rows == oracle(scenario)
            observed = access({"statistics": result.statistics,
                               "selected_plan": [type(node).__name__ for node in db.explain(query).walk()]})
            assert observed["conditional_seek_in_plan"] is True
            assert observed["edge_regime"] == "endpoint"
            assert observed["edge_lookups"] == 16
            assert observed["edge_scans"] == 0
            assert observed["physical_edge_versions_examined"] < 1151


def test_real_broad_control_reports_executed_fallback(tmp_path):
    scenario = next(s for s in rf04_matrix(Scenario) if s.name == "rf04-with-fallback")
    with okto_grafx.connect(tmp_path / "db", codec="pure", vector_math="pure") as db:
        build_fixture(db, scenario)
        query, params = query_for(scenario)
        result = db.execute(query, params)
        assert result.rows == oracle(scenario)
        observed = access({"statistics": result.statistics,
                           "selected_plan": [type(node).__name__ for node in db.explain(query).walk()]})
        assert observed["conditional_seek_in_plan"] is True
        assert observed["actual_fallbacks"]["fallback_reason_nonselective_probe_set"] == 1
        assert result.statistics.get("rows_scanned", 0) >= scenario.nodes


def test_interaction_refuses_missing_fourth_campaign():
    with pytest.raises(CampaignError, match="four"):
        interaction_report({"rf03": {}, "rf04": {}, "incremental": {}})


def test_interaction_binds_four_distinct_revisions_and_checks_actual_fan_limit(monkeypatch):
    from bench.harness import campaign_schema

    monkeypatch.setattr(campaign_schema, "compare_campaign", lambda data: {
        "decision": "passed", "comparisons": [], "calibration_valid": True, "noisy": False})
    revisions = {"base": "a" * 40, "rf03": "b" * 40,
                 "rf04": "c" * 40, "both": "d" * 40}
    arms = {"rf03": ("base", "rf03"), "rf04": ("base", "rf04"),
            "incremental": ("rf03", "both"), "combined": ("base", "both")}
    campaigns = {}
    for phase, (left, right) in arms.items():
        scenarios = phase_scenarios(phase, Scenario, budget=1 << 20)
        target = scenarios[0].name
        fan = scenarios[1].name
        runs = []
        for pair, order in enumerate(("AB", "BA", "AB")):
            for arm in order:
                observations = []
                for _ in range(25):
                    observations.append({"scenario_id": target, "rf04_access": {
                        "conditional_seek_in_plan": arm == "B" and phase != "rf03",
                        "edge_regime": "endpoint", "edge_lookups": 16,
                        "physical_edge_versions_examined": 16}})
                    observations.append({"scenario_id": fan, "rf04_access": {
                        "edge_lookups": 64, "edge_scans": 1}})
                runs.append({"arm": arm, "revision": revisions[left if arm == "A" else right],
                             "environment": {"python": "same", "dependencies": {}, "os": "same",
                                             "cpu": "same", "cpu_count": 1, "ram_bytes": 1024,
                                             "gc_enabled": True, "disk": {"free_bytes": len(runs)}},
                             "samples": observations})
        campaigns[phase] = {"intent": "qualification", "mode": "optimization",
                            "scenarios": [scenario.to_dict() for scenario in scenarios],
                            "protocol": {"max_cpu_percent": 20}, "runs": runs}
    assert interaction_report(campaigns)["source_revisions"]["both"] == revisions["both"]
    campaigns["incremental"]["runs"][1]["samples"][1]["rf04_access"]["edge_scans"] = 2
    with pytest.raises(CampaignError, match="fan-limit"):
        interaction_report(campaigns)
    campaigns["incremental"]["runs"][1]["samples"][1]["rf04_access"]["edge_scans"] = 1
    campaigns["combined"]["runs"][0]["revision"] = revisions["rf04"]
    with pytest.raises(CampaignError, match="revision changed"):
        interaction_report(campaigns)
