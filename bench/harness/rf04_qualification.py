"""RF04 endpoint qualification over the existing RF03 connected graph fixture.

The four source arms are qualified through separate, same-query A/B campaigns.
An IN+WITH query isolates RF04 on a source without RF03; the original connected
query measures RF03, RF04 added to RF03, and their combined effect. These are
distinct effects and must never be summed as independent speedups.
"""
from __future__ import annotations

from dataclasses import replace


CASES = ("target", "fan_limit", "hub", "fallback")
PROFILES = ("original", "with")
PHASES = {"rf03": "original", "rf04": "with", "incremental": "original",
          "combined": "original"}
PHYSICAL_COUNTERS = ("physical_node_versions_examined", "physical_edge_versions_examined")


def matrix(scenario_type):
    """Fixed semantic cuts: selective 16, fan-limit 65, skew, and real scan fallback."""
    scenarios = []
    for profile in PROFILES:
        family = "connected_in" if profile == "original" else "connected_with"
        for case in CASES:
            dimensions = {"nodes": 16, "keys": tuple(range(1024)), "parallel_edges": 127} if case == "fallback" else {
                "nodes": 1024, "keys": tuple(range(65 if case == "fan_limit" else 16)),
                "parallel_edges": 127}
            scenarios.append(scenario_type(
                f"rf04-{profile}-{case}", family, rf04_case=case,
                destination_nodes=128, distribution="hub" if case == "hub" else "uniform",
                role="target" if case == "target" else "control",
                work_counter="physical_edge_versions_examined" if case == "target" else None,
                **dimensions,
            ))
    return scenarios


def validate(scenario):
    if scenario.rf04_case is None:
        return
    from bench.harness.campaign_fixtures import Scenario

    profile = "original" if scenario.family == "connected_in" else "with" if scenario.family == "connected_with" else None
    case = scenario.rf04_case
    if profile is None or case not in CASES or scenario.name != f"rf04-{profile}-{case}":
        raise ValueError("RF04 requires one named fixture from the fixed matrix")
    dimensions = {"nodes": 16, "keys": tuple(range(1024)), "parallel_edges": 127} if case == "fallback" else {
        "nodes": 1024, "keys": tuple(range(65 if case == "fan_limit" else 16)),
        "parallel_edges": 127}
    reference = Scenario(
        scenario.name, scenario.family, destination_nodes=128,
        distribution="hub" if case == "hub" else "uniform",
        role="target" if case == "target" else "control",
        work_counter="physical_edge_versions_examined" if case == "target" else None,
        **dimensions,
    )
    # The runner may apply the same positive logical budget and select the RF03
    # target's node counter. Neither operation may change the fixture or query.
    normalized = replace(scenario, rf04_case=None, query_memory_budget_bytes=None,
                         work_counter=reference.work_counter)
    if normalized != reference or (scenario.query_memory_budget_bytes is not None
                                   and scenario.query_memory_budget_bytes < 1) or (
        scenario.work_counter not in (reference.work_counter,
                                      "physical_node_versions_examined" if case == "target" else None)
    ):
        raise ValueError("RF04 fixture or role differs from the fixed matrix")


def contract(scenario):
    return {
        "rf04_case": scenario.rf04_case,
        "query_profile": "original_connected_IN" if scenario.family == "connected_in" else "IN_WITH_then_connected",
        "semantic_oracle": "input-derived ordered rows including every parallel edge; duplicate IN probes do not multiply starts",
        "plan_scope": "EXPLAIN includes unexecuted NodeMultiKeySeek alternatives; counters identify actual access",
        "source_arms": "base, RF03-only, RF04-only, combined; each comparison keeps an identical query",
        "memory_scope": "positive common query budget and same observer in both arms; RSS is process-lifetime peak",
        "cpu_scope": "process_time_ns plus host CPU before/after and calibrated executor",
    }


def access(sample):
    """Name only work actually observed; a planned fallback is not a scan."""
    stats = sample["statistics"]
    if any(type(stats.get(key)) is not int or stats[key] < 0 for key in PHYSICAL_COUNTERS):
        raise ValueError("RF04 physical edge/node counters unavailable")
    lookups, scans = stats.get("edge_lookups", 0), stats.get("edge_scans", 0)
    if type(lookups) is not int or type(scans) is not int or lookups < 0 or scans < 0:
        raise ValueError("RF04 endpoint/grouped counters invalid")
    if lookups and scans:
        regime = "endpoint_then_grouped"
    elif lookups:
        regime = "endpoint"
    elif scans:
        regime = "grouped"
    else:
        regime = "no_edge_access"
    return {
        "conditional_seek_in_plan": "NodeMultiKeySeek" in sample["selected_plan"],
        "edge_regime": regime,
        "edge_lookups": lookups,
        "edge_scans": scans,
        "physical_node_versions_examined": stats["physical_node_versions_examined"],
        "physical_edge_versions_examined": stats["physical_edge_versions_examined"],
        "actual_fallbacks": {key: value for key, value in stats.items()
                             if key.startswith("fallback_reason_") and value},
        "scope": "runtime statistics; selected_plan may contain unexecuted alternatives",
    }


def validate_sample(sample, scenario):
    if sample.get("rf04_access") != access(sample):
        raise ValueError("RF04 access diagnostics differ from observed counters/plan")
    if scenario.rf04_case == "target" and sample["rows"] < 1:
        raise ValueError("RF04 target returned no edge rows")
    if scenario.rf04_case == "fallback" and sample["rf04_access"]["conditional_seek_in_plan"] and sample["statistics"].get(
        "fallback_reason_nonselective_probe_set", 0
    ) != 1:
        raise ValueError("RF04 broad control did not execute the node-scan fallback")
    if scenario.rf04_case == "fan_limit" and sample["rf04_access"]["edge_lookups"] > 64:
        raise ValueError("RF04 fan-limit exceeded")


def phase_scenarios(phase, scenario_type, *, budget):
    if phase not in PHASES or type(budget) is not int or budget < 1:
        raise ValueError("RF04 phase requires known role and positive common memory budget")
    profile = PHASES[phase]
    result = []
    for item in matrix(scenario_type):
        if item.name.startswith(f"rf04-{profile}-"):
            counter = ("physical_node_versions_examined" if phase == "rf03"
                       and item.rf04_case == "target" else item.work_counter)
            result.append(replace(item, query_memory_budget_bytes=budget,
                                  work_counter=counter))
    return result


def interaction_report(campaigns):
    """Validate four complete pairwise campaigns and their factorial source lineage.

    Source-arm labels remain declared Git revisions; Pulse Code Traceability must
    separately attest the exact patches and implementation associations.
    """
    from bench.harness.campaign_fixtures import Scenario
    from bench.harness.campaign_schema import CampaignError, canonical_hash, compare_campaign

    if set(campaigns) != set(PHASES):
        raise CampaignError("RF04 requires four campaigns: rf03, rf04, incremental and combined")
    reports, revisions, budgets, environments, cpu_policies = {}, {}, set(), set(), set()
    for phase in PHASES:
        data = campaigns[phase]
        if data.get("intent") != "qualification" or data.get("mode") != "optimization":
            raise CampaignError("RF04 interaction requires qualified optimization campaigns")
        scenarios = [Scenario.from_dict(item) for item in data["scenarios"]]
        if not scenarios or any(item.query_memory_budget_bytes is None for item in scenarios):
            raise CampaignError("RF04 requires a positive common logical budget")
        budget = scenarios[0].query_memory_budget_bytes
        budgets.add(budget)
        expected = phase_scenarios(phase, Scenario, budget=budget)
        if {item.name: item for item in scenarios} != {item.name: item for item in expected}:
            raise CampaignError(f"RF04 {phase} scenarios differ from fixed target and controls")
        report = compare_campaign(data)
        reports[phase] = report
        target_name = f"rf04-{PHASES[phase]}-target"
        target_samples = [sample for run in data["runs"] if run["arm"] == "B"
                          for sample in run["samples"] if sample["scenario_id"] == target_name]
        if len(target_samples) != 75 or (phase != "rf03" and any(
            not sample["rf04_access"]["conditional_seek_in_plan"] for sample in target_samples
        )):
            raise CampaignError(f"RF04 {phase} candidate did not plan the conditional seek in all target samples")
        if phase != "rf03" and any(
            sample["rf04_access"]["edge_regime"] != "endpoint"
            or sample["rf04_access"]["edge_lookups"] != 16
            or sample["rf04_access"]["physical_edge_versions_examined"] >= 1151
            for sample in target_samples
        ):
            raise CampaignError(f"RF04 {phase} candidate did not use sixteen endpoint lookups without a global scan")
        if phase != "rf03":
            fan_name = f"rf04-{PHASES[phase]}-fan_limit"
            fan_samples = [sample for run in data["runs"] if run["arm"] == "B"
                           for sample in run["samples"] if sample["scenario_id"] == fan_name]
            if len(fan_samples) != 75 or any(
                sample["rf04_access"]["edge_lookups"] != 64
                or sample["rf04_access"]["edge_scans"] != 1
                for sample in fan_samples
            ):
                raise CampaignError(f"RF04 {phase} candidate did not switch once at the 64-start fan-limit")
        # The disk-free observation changes while campaigns write raw output.
        # Compare the same stable executor identity used by campaign validation.
        environment = data["runs"][0]["environment"]
        environments.add(canonical_hash({key: environment[key] for key in (
            "python", "dependencies", "os", "cpu", "cpu_count", "ram_bytes", "gc_enabled")}))
        cpu_policies.add(data["protocol"]["max_cpu_percent"])
        arms = {arm: {run["revision"] for run in data["runs"] if run["arm"] == arm}
                for arm in ("A", "B")}
        if any(len(values) != 1 for values in arms.values()):
            raise CampaignError("RF04 arm revision changed within a campaign")
        revisions[phase] = {arm: next(iter(values)) for arm, values in arms.items()}
    if len(budgets) != 1 or len(environments) != 1 or len(cpu_policies) != 1:
        raise CampaignError("RF04 campaigns changed budget, executor environment or CPU policy")
    base = revisions["rf03"]["A"]
    rf03 = revisions["rf03"]["B"]
    rf04 = revisions["rf04"]["B"]
    both = revisions["combined"]["B"]
    if len({base, rf03, rf04, both}) != 4 or revisions["rf04"]["A"] != base or (
        revisions["incremental"]["A"], revisions["incremental"]["B"]
    ) != (rf03, both) or revisions["combined"]["A"] != base:
        raise CampaignError("RF04 source-arm lineage is not a four-revision factorial comparison")
    verdicts = {phase: report["decision"] for phase, report in reports.items()}
    decision = ("qualified" if all(value == "passed" for value in verdicts.values()) else
                "inconclusive" if any(value == "inconclusive" for value in verdicts.values()) else
                "failed")
    return {"schema_version": "grafx-rf04-interaction/v1", "decision": decision,
            "source_revisions": {"base": base, "rf03_only": rf03, "rf04_only": rf04, "both": both},
            "source_isolation": "revision labels and campaign lineage checked; exact patch isolation requires separate code receipts",
            "query_budget_bytes": next(iter(budgets)),
            "campaign_hashes": {phase: canonical_hash(campaigns[phase]) for phase in PHASES},
            "phase_decisions": verdicts,
            "phase_reports": {phase: {"decision": reports[phase]["decision"],
                                      "comparisons": reports[phase]["comparisons"],
                                      "calibration_valid": reports[phase]["calibration_valid"],
                                      "noisy": reports[phase]["noisy"]} for phase in PHASES},
            "comparison_scope": "same query within each A/B; IN+WITH isolates RF04, original connected IN measures RF03 and combination",
            "gain_scope": "RF03 and RF04 effects overlap; never sum phase speedups",
            "raw_samples": "retained in four input campaigns; no synthetic timing samples"}
