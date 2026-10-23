"""RF12 recipes retain semantic oracles and never authorize maintenance."""
from copy import deepcopy
from dataclasses import replace

import pytest

from bench.harness import capacity_qualification as capacity
from bench.harness.campaign_fixtures import Scenario, matrix, oracle
from bench.harness.campaign_worker import measure_statement
from okto_grafx import connect


def test_capacity_catalog_has_relevant_slices_without_cartesian_explosion():
    cases = [s for s in matrix() if s.family == "capacity"]
    assert {s.buffer_budget_bytes for s in cases} == {16 << 20, 64 << 20, 256 << 20}
    assert {s.checkpoint_interval_records for s in cases} == {128, 512, 2048}
    assert {s.identity_lease_size for s in cases} >= {1, 32, 128, 512}
    assert {s.write_rows for s in cases} >= {1, 32, 128, 512}
    assert {s.capacity_case for s in cases} == {"retained", "released", "external"}
    assert len(cases) < 30
    assert all(Scenario.from_dict(s.to_dict()) == s for s in cases)


def test_closed_qualification_partitions_repeat_controls_without_reusing_campaigns():
    partitions = capacity.qualification_partitions(Scenario)
    assert {key: len(value) for key, value in partitions.items()} == {
        "Q1": 3, "Q2": 3, "Q3": 3, "Q4": 5, "Q5": 4}
    assert sum(map(len, partitions.values())) == 18
    assert len({s.name for members in partitions.values() for s in members}) == 15
    assert len(capacity.matrix(Scenario)) == 21
    for name, members in partitions.items():
        assert capacity.validate_qualification_partition(members) == name
    with pytest.raises(ValueError, match="exactly one"):
        capacity.validate_qualification_partition(partitions["Q1"][:2])
    with pytest.raises(ValueError, match="exactly one"):
        capacity.validate_qualification_partition(partitions["Q4"] + partitions["Q5"])
    with pytest.raises(ValueError, match="exactly one"):
        capacity.validate_qualification_partition([
            replace(partitions["Q3"][0], checkpoint_interval_records=129),
            *partitions["Q3"][1:]])


def test_incomplete_or_unregistered_capacity_raw_cannot_be_qualification():
    from bench.harness.campaign_schema import CampaignError, validate_campaign
    from tests.bench.test_campaign_schema import campaign

    data = campaign()
    data.update(mode="recipe", scenarios=[s.to_dict() for s in
                 capacity.qualification_partitions(Scenario)["Q2"][:2]])
    with pytest.raises(CampaignError, match="exactly one"):
        validate_campaign(data)
    with pytest.raises(ValueError, match="all five"):
        capacity.qualification_report({"Q1": data}, objective="local read latency")


def test_aggregate_requires_fresh_invocations_and_keeps_functional_proof_separate(monkeypatch):
    from bench.harness import campaign_schema

    def fake_comparison(raw):
        return {"decision": "tradeoff_reported", "calibration_valid": True,
                "noisy": False, "campaign_hash": raw["partition"],
                "comparisons": [{"capacity_tradeoff": {"comparison_scope": "local"}}]}

    monkeypatch.setattr(campaign_schema, "compare_campaign", fake_comparison)
    partitions = capacity.qualification_partitions(Scenario)
    raw = {part: {"partition": part, "intent": "qualification",
                  "calibrated_executor": True, "protocol": {"max_cpu_percent": 20},
                  "scenarios": [s.to_dict() for s in members],
                  "runs": [{"invocation_id": f"{part}-{i}", "arm": "A" if i % 2 == 0 else "B",
                            "revision": "a" * 40 if i % 2 == 0 else "b" * 40}
                           for i in range(6)]}
           for part, members in partitions.items()}
    report = capacity.qualification_report(raw, objective="reduce local read latency")
    assert (report["participations"], report["distinct_scenarios"]) == (18, 15)
    assert report["qualification_scope"] == "temporal_partitions_only"
    assert report["checkpoint_foreign_writer_proof"] == "separate_functional_verification_required"
    assert report["change_defaults"] is False and report["authorize_vacuum"] is False
    raw["Q5"]["runs"][0]["invocation_id"] = raw["Q1"]["runs"][0]["invocation_id"]
    with pytest.raises(ValueError, match="reused process"):
        capacity.qualification_report(raw, objective="reduce local read latency")
    raw["Q5"]["runs"][0]["invocation_id"] = "Q5-0"
    raw["Q5"]["runs"][0]["revision"] = "c" * 40
    with pytest.raises(ValueError, match="unstable source revision"):
        capacity.qualification_report(raw, objective="reduce local read latency")


def test_external_lease_and_batch_interactions_have_one_variable_controls():
    cases = [s for s in capacity.matrix(Scenario) if s.capacity_case == "external"]
    settings = {(s.identity_lease_size, s.write_rows) for s in cases}
    assert (64, 32) in settings
    for lease, batch in settings:
        if lease != 64 and batch != 32:
            assert (lease, 32) in settings
            assert (64, batch) in settings
            scenario = next(s for s in cases if (s.identity_lease_size, s.write_rows) == (lease, batch))
            assert "interaction" in capacity.contract(scenario)["comparison_scope"]
    assert capacity.comparison_scope(next(s for s in cases if
           (s.identity_lease_size, s.write_rows) == (128, 32))) == "lease_only_against_external_64_32"


def test_fixed_total_batch_group_rejects_unequal_load_or_missing_reference():
    group = [s for s in capacity.matrix(Scenario) if s.capacity_batch_size]
    assert len(group) == 4
    assert {s.capacity_batch_size for s in group} == {1, 32, 128, 512}
    assert {(s.nodes, s.write_rows, s.identity_lease_size) for s in group} == {(512, 512, 64)}
    assert len({tuple(sorted((key, value) for key, value in s.to_dict().items()
                             if key not in {"name", "capacity_batch_size"})) for s in group}) == 1
    capacity.validate_batch_selection(group)
    with pytest.raises(ValueError, match="equal 512-row load"):
        capacity.validate_batch_selection([replace(group[0], nodes=511, write_rows=511), group[1]])
    with pytest.raises(ValueError, match="32-row reference"):
        capacity.validate_batch_selection([group[0], group[2]])
    from bench.harness.campaign_schema import CampaignError, validate_campaign
    from tests.bench.test_campaign_schema import campaign
    data = campaign()
    data.update(mode="recipe", scenarios=[group[1].to_dict(),
                                          replace(group[0], nodes=511, write_rows=511).to_dict()])
    with pytest.raises(CampaignError, match="exactly one"):
        validate_campaign(data)


def test_equal_work_external_transaction_batches_have_durable_ack_and_reopen(tmp_path):
    scenario = replace(next(s for s in capacity.matrix(Scenario) if s.capacity_batch_size == 32),
                       nodes=8, write_rows=8, capacity_batch_size=2)
    with connect(tmp_path / "batch", codec="pure", vector_math="pure",
                 **capacity.connect_options(scenario)) as db:
        capacity.build_fixture(db, scenario)
        sample = capacity.measure(db, scenario, 5, measure_statement)
    proof = sample["capacity"]["external_write"]
    assert proof["total_rows"] == 8
    assert [(ack["offset"], ack["rows"]) for ack in proof["acks"]] == [
        (0, 2), (2, 2), (4, 2), (6, 2)]
    assert all(ack["durable"] and ack["ack_duration_ns"] > 0 for ack in proof["acks"])
    assert sample["capacity"]["reopen_verified"] is True
    assert sample["capacity"]["minimum_reservation_estimate"] == 64
    from bench.harness.campaign_schema import compare_campaign
    from tests.bench.test_campaign_schema import campaign
    data = campaign()
    data.update(mode="recipe", intent="exploratory", scenarios=[scenario.to_dict()])
    for run in data["runs"]:
        run["samples"] = []
        for index in range(25):
            repeated = deepcopy(sample)
            repeated.update(index=index, kind="warmup" if index < 5 else "retained")
            run["samples"].append(repeated)
    tradeoff = compare_campaign(data)["comparisons"][0]["capacity_tradeoff"]
    assert tradeoff["total_update_rows"] == 8
    assert tradeoff["transaction_batch_size"] == 2
    assert tradeoff["by_arm"]["A"]["durable_acks_per_sample"]["median_count"] == 4
    assert tradeoff["by_arm"]["A"]["external_write_elapsed_ns"] is not None
    assert tradeoff["by_arm"]["A"]["read_elapsed_ns"] is not None
    for alteration in (
        lambda item: item["capacity"]["external_write"].update(total_rows=7),
        lambda item: item["capacity"]["external_write"]["acks"][1].update(durable=False),
        lambda item: item["capacity"]["external_write"]["acks"].pop(),
        lambda item: item["capacity"].update(reopened_rows_hash="0" * 64),
    ):
        damaged = deepcopy(sample)
        alteration(damaged)
        with pytest.raises(ValueError):
            capacity.validate_sample(damaged, scenario)


def test_partial_recipe_cannot_claim_qualification_even_with_good_memory(tmp_path):
    scenario = replace(next(s for s in capacity.matrix(Scenario) if s.capacity_case == "external"
                            and s.capacity_batch_size == 0), nodes=4, write_rows=4)
    with connect(tmp_path / "quota", codec="pure", vector_math="pure",
                 **capacity.connect_options(scenario)) as db:
        capacity.build_fixture(db, scenario)
        sample = capacity.measure(db, scenario, 5, measure_statement)
    from bench.harness.campaign_schema import CampaignError, compare_campaign
    from tests.bench.test_campaign_schema import campaign
    data = campaign()
    data.update(mode="recipe", intent="qualification", scenarios=[scenario.to_dict()])
    for run in data["runs"]:
        run["samples"] = []
        for index in range(25):
            repeated = deepcopy(sample)
            repeated.update(index=index, kind="warmup" if index < 5 else "retained")
            run["samples"].append(repeated)
    with pytest.raises(CampaignError, match="exactly one"):
        compare_campaign(data)
    data["runs"][0]["samples"][0]["memory"]["logical_memory_peak_bytes"] = (1 << 20) + 1
    with pytest.raises(CampaignError, match="exactly one"):
        compare_campaign(data)


@pytest.mark.parametrize("case", ["retained", "released", "external"])
def test_real_capacity_recipe_keeps_snapshot_and_diagnostic_contract(tmp_path, case):
    scenario = capacity.matrix(Scenario)[0]
    scenario = replace(scenario, nodes=4, write_rows=4, capacity_case=case)
    with connect(tmp_path / "db", codec="pure", vector_math="pure",
                 **capacity.connect_options(scenario)) as db:
        capacity.build_fixture(db, scenario)
        sample = capacity.measure(db, scenario, 5, measure_statement)
        capacity.validate_sample(sample, scenario)
        assert sample["rows"] == len(oracle(scenario)) == 4
        assert sample["memory"]["logical_memory_peak_bytes"] is not None
        assert sample["rss_peak_bytes"] > 0
        raw = sample["capacity"]
        assert raw["before_close"]["horizon_retained_versions"] == 4
        assert raw["after_close"]["horizon_eligible_versions"] == 4
        assert raw["before_close"]["vacuum_safety_established"] is False
        assert raw["after_close"]["vacuum_safety_established"] is False
        assert raw["diagnostic_storage_unchanged"] is True
        assert raw["minimum_reservation_estimate"] == max(scenario.identity_lease_size, 4)
        assert db.verify("all").findings == ()
        damaged = dict(sample, capacity=dict(raw, diagnostic_storage_unchanged=False))
        with pytest.raises(ValueError, match="diagnostic"):
            capacity.validate_sample(damaged, scenario)


def test_decision_template_keeps_missing_or_noisy_evidence_inconclusive():
    template = capacity.decision_template(None, objective="reduce local RSS")
    assert template["decision"] == "new_research"
    assert template["change_defaults"] is False
    assert template["authorize_vacuum"] is False
    assert template["report_sha256"] is None
    noisy = capacity.decision_template({"verdict": "inconclusive"}, objective="reduce local RSS")
    assert noisy["decision"] == "new_research"
    assert noisy["report_sha256"] is not None
    review = capacity.decision_template({"decision": "tradeoff_reported",
                                         "calibration_valid": True, "noisy": False},
                                        objective="reduce local RSS")
    assert review["decision"] == "review_local_tradeoffs"
    assert review["template_only"] is True
    assert review["authorize_vacuum"] is False
    assert "fixed-512 transaction-batch controls" in review["comparison_scope"]


def test_invalid_capacity_config_and_unrelated_fields_are_rejected():
    scenario = capacity.matrix(Scenario)[0]
    for updates in ({"buffer_budget_bytes": 0}, {"identity_lease_size": True},
                    {"checkpoint_interval_records": -1}, {"capacity_case": "vacuum"},
                    {"reader": "retained"}, {"write_rows": 0}):
        with pytest.raises(ValueError):
            replace(scenario, **updates)
    with pytest.raises(ValueError):
        Scenario("bad", "constant", capacity_case="released")


def test_capacity_cannot_claim_universal_optimization():
    from bench.harness.campaign_schema import CampaignError, validate_campaign
    from tests.bench.test_campaign_schema import campaign

    data = campaign()
    data["mode"] = "optimization"
    data["scenarios"] = [capacity.matrix(Scenario)[0].to_dict()]
    with pytest.raises(CampaignError, match="local recipe"):
        validate_campaign(data)


def test_runtime_configuration_must_match_requested_recipe(tmp_path):
    scenario = replace(capacity.matrix(Scenario)[0], nodes=1, write_rows=1,
                       identity_lease_size=512)
    with connect(tmp_path / "db", codec="pure", vector_math="pure",
                 query_memory_budget_bytes=scenario.query_memory_budget_bytes) as db:
        with pytest.raises(ValueError, match="runtime configuration"):
            capacity.measure(db, scenario, 5, measure_statement)
