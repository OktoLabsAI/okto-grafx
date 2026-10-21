"""A reused FTS fixture cannot become 25 independent setup-cost samples."""
from copy import deepcopy

import pytest

from bench.harness.campaign_fixtures import Scenario, oracle
from bench.harness.campaign_schema import CampaignError, compare_campaign
from bench.harness.campaign_worker import sample_record, unavailable_memory
from tests.bench.test_campaign_schema import campaign


def _shared_setup_campaign():
    scenario = Scenario("fts-shared-setup", "fts_matrix", nodes=256,
                        edges_per_node=0, fts_statistics_mode="wal",
                        fts_history=0, term="rare", cache="reopen")
    data = campaign()
    data.update(mode="recipe", intent="qualification", scenarios=[scenario.to_dict()])
    for run in data["runs"]:
        run["samples"] = []
        setup = {"elapsed_ns": 100 + 10 * run["pair"] + (run["arm"] == "B"),
                 "disk_allocated_bytes": 1000 + run["pair"],
                 "wal_allocated_bytes": 100 + run["pair"],
                 "scope": "seed_insert_index_creation_checkpoint_and_close",
                 "observation_scope": "one_fixture_per_process",
                 "observation_id": f"{run['invocation_id']}:{scenario.name}:shared"}
        for index in range(25):
            sample = sample_record(
                scenario.to_dict(), index, oracle(scenario),
                {"postings_visited": 1, "candidates": 1,
                 "corpus_documents": 256, "statistics_wal_records": 0,
                 "statistics_from_full_census": 1},
                elapsed_ns=1000, cpu_ns=100, prepare_ns=20,
                memory=unavailable_memory("native_fts_memory_not_covered_by_query_budget_observer"),
                rss_peak_bytes=1000,
                rss_scope="process_lifetime_peak_working_set_bytes",
            )
            sample["fts_source"] = {"statistics_regime": "full_census",
                                    "statistics_mode": "wal", "history_entries": 0,
                                    "hits": sample["rows"]}
            sample["fts_setup"] = dict(setup)
            run["samples"].append(sample)
    return data


def test_report_counts_one_shared_setup_per_process_and_three_per_arm():
    report = compare_campaign(_shared_setup_campaign())
    pair = report["comparisons"][0]["fts_tradeoff"]["by_arm"]["A"]
    setup = pair["setup_elapsed_ns"]
    assert setup["observation_count"] == 1
    assert setup["observation_scope"] == "one_fixture_per_process"
    assert setup["p95_ns"] is None and setup["stdev_ns"] is None
    overall = report["overall"][0]["fts_setup_by_arm"]["A"]["elapsed_ns"]
    assert overall["observation_count"] == 3
    assert overall["p95_ns"] is None
    assert overall["median_ns"] == 110


def test_qualification_rejects_forged_independence_and_changed_shared_setup():
    data = _shared_setup_campaign()
    forged = deepcopy(data)
    setup = forged["runs"][0]["samples"][5]["fts_setup"]
    setup["observation_scope"] = "one_fixture_per_sample"
    setup["observation_id"] = f"{forged['runs'][0]['invocation_id']}:fts-shared-setup:5"
    with pytest.raises(CampaignError, match="observation identity"):
        compare_campaign(forged)

    changed = deepcopy(data)
    changed["runs"][0]["samples"][5]["fts_setup"]["elapsed_ns"] += 1
    with pytest.raises(CampaignError, match="changed between samples"):
        compare_campaign(changed)

    missing = deepcopy(data)
    del missing["runs"][0]["samples"][5]["fts_setup"]["observation_id"]
    with pytest.raises(CampaignError, match="FTS fixture setup"):
        compare_campaign(missing)
