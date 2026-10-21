"""RF12 recipes retain semantic oracles and never authorize maintenance."""
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
