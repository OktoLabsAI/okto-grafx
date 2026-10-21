"""The RF06 timing exception is confined to quota-bounded landing targets."""
from copy import deepcopy

import pytest

from bench.harness.campaign_fixtures import Scenario, oracle, query_for
from bench.harness.campaign_schema import CampaignError, canonical_hash, compare_campaign
from bench.harness.campaign_worker import sample_record
from bench.harness.quota_qualification import SPLIT_MEMORY_REGIME, qualification_matrix
from tests.bench.test_campaign_schema import campaign


def split_campaign():
    data = campaign()
    scenarios = qualification_matrix(Scenario)
    data.update(mode="optimization", memory_regime=SPLIT_MEMORY_REGIME,
                scenarios=[item.to_dict() for item in scenarios])
    for run in data["runs"]:
        arm = run["arm"]
        run["memory_regime"] = SPLIT_MEMORY_REGIME
        run["samples"] = []
        proofs = {}
        for scenario in scenarios:
            target = scenario.role == "target"
            memory = ({"logical_memory_peak_bytes": None, "baseline_retained_bytes": 0,
                       "coverage": "unavailable", "unavailable_reason": "query_memory_budget_disabled"}
                      if target else
                      {"logical_memory_peak_bytes": 100, "baseline_retained_bytes": 0,
                       "coverage": "simultaneous_query_budget_and_handle_owner_endpoint_tariffs_v1",
                       "unavailable_reason": None})
            stats = {"certificate_attempts": (128 if arm == "A" else 2) if target else 128,
                     "landing_candidates": 128,
                     "landing_batches": 2 if target and arm == "B" else 0}
            for index in range(25):
                run["samples"].append(sample_record(
                    scenario.to_dict(), index, oracle(scenario), stats,
                    elapsed_ns=(80 if arm == "B" else 100) if target else 100,
                    cpu_ns=10, prepare_ns=2, memory=memory.copy(), rss_peak_bytes=100,
                    rss_scope="process_lifetime_peak_working_set_bytes"))
            if not target:
                proofs[scenario.name] = {"scope": "untimed same-handle scalar control",
                                         "arm": arm, "revision": run["revision"],
                                         "invocation_id": run["invocation_id"],
                                         "process_id": run["process_id"],
                                         "scenario_id": scenario.name,
                                         "scenario_hash": canonical_hash(scenario.to_dict()),
                                         "query_memory_budget_bytes": scenario.query_memory_budget_bytes,
                                         "records": [{"index": index, "retained_before_bytes": 0,
                                                      "retained_after_bytes": 0,
                                                      "open_transactions_after": 0,
                                                      "memory": memory.copy()}
                                                     for index in range(5)]}
        run["rf06_memory_proofs"] = proofs
    return data


def test_fixed_split_matrix_has_same_traversal_quota_in_each_pair():
    cases = qualification_matrix(Scenario)
    assert len(cases) == 6
    for target, control in zip(cases[::2], cases[1::2]):
        assert target.family == control.family == "landing"
        assert target.nodes == control.nodes == 128
        assert target.edges_per_node == control.edges_per_node == 0
        assert target.fanout == control.fanout == 128
        assert target.quota_field == control.quota_field
        assert target.quota_limit == control.quota_limit == 128
        assert query_for(target) == query_for(control)
        assert target.query_memory_budget_bytes is None
        assert control.query_memory_budget_bytes == 1 << 20
        assert target.work_counter == "certificate_attempts"


def test_real_scalar_control_releases_budget_after_repeated_reads(tmp_path):
    from bench.harness.campaign_fixtures import build_fixture
    from bench.harness.campaign_worker import rf06_memory_retention_proof
    from bench.harness.quota_qualification import connection_options
    from okto_grafx import connect

    scenario = qualification_matrix(Scenario)[1]
    root = tmp_path / "rf06-control"
    with connect(root) as setup:
        build_fixture(setup, scenario)
    with connect(root, query_memory_budget_bytes=scenario.query_memory_budget_bytes,
                 **connection_options(scenario)) as db:
        records = rf06_memory_retention_proof(db, scenario)["records"]
    assert len(records) == 5
    assert {record["retained_before_bytes"] for record in records} == {0}
    assert all(record["memory"]["logical_memory_peak_bytes"] >= 0 for record in records)


def test_split_campaign_qualifies_time_without_claiming_target_memory():
    report = compare_campaign(split_campaign())
    assert report["decision"] == "passed"
    assert report["comparative_logical_memory"] is False
    assert report["rf06_scalar_control_memory_verified"] is True
    assert report["rf06_memory_scope"]["target"].startswith("logical peak unavailable")
    assert len(report["comparisons"]) == 18
    assert report["comparisons"][0]["rf06_tradeoff"]["by_arm"]["B"]["landing_batches"]["median_count"] == 2


@pytest.mark.parametrize("tamper", [
    lambda data: data["scenarios"].pop(),
    lambda data: data["scenarios"][1].update(nodes=127),
    lambda data: data["scenarios"][1].update(quota_limit=127),
    lambda data: data["runs"][1]["samples"][0]["memory"].update(logical_memory_peak_bytes=0),
    lambda data: data["runs"][1]["samples"][0]["statistics"].update(landing_batches=0),
    lambda data: data["runs"][1]["samples"][25]["statistics"].update(landing_batches=1),
    lambda data: data["runs"][1]["rf06_memory_proofs"].pop(next(iter(data["runs"][1]["rf06_memory_proofs"]))),
    lambda data: next(iter(data["runs"][1]["rf06_memory_proofs"].values()))["records"][4].update(
        retained_after_bytes=1),
    lambda data: next(iter(data["runs"][1]["rf06_memory_proofs"].values())).update(
        invocation_id=data["runs"][0]["invocation_id"]),
    lambda data: next(iter(data["runs"][1]["rf06_memory_proofs"].values()))["records"][0]["memory"].update(
        logical_memory_peak_bytes=1048577),
    lambda data: data["runs"][1]["samples"][25]["memory"].update(logical_memory_peak_bytes=None),
    lambda data: data["runs"][1]["samples"][25]["memory"].update(logical_memory_peak_bytes=1048577),
])
def test_split_campaign_fails_closed_on_missing_or_fabricated_evidence(tamper):
    data = deepcopy(split_campaign())
    tamper(data)
    with pytest.raises(CampaignError):
        compare_campaign(data)
