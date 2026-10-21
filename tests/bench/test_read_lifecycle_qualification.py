"""RF10 recipe measurements exercise ownership; no timing thresholds in CI."""
from copy import deepcopy
from dataclasses import replace

import pytest
import okto_grafx

from bench.harness import read_lifecycle_qualification as lifecycle
from bench.harness.campaign_fixtures import Scenario, build_fixture, matrix, oracle
from bench.harness.campaign_worker import measure_statement
from bench.harness.campaign_schema import CampaignError, SEQUENCE_LOGICAL_COVERAGE, compare_campaign
from bench.harness.campaign_worker import sample_record
from tests.bench.test_campaign_schema import campaign


@pytest.mark.parametrize("family", sorted(lifecycle.FAMILIES))
def test_read_recipes_and_separate_diagnostics(tmp_path, family):
    scenario = Scenario("case", family, nodes=16, edges_per_node=0, keys=tuple(range(16)))
    with okto_grafx.connect(tmp_path / "db", codec="pure", vector_math="pure") as db:
        build_fixture(db, scenario)
        sample = measure_statement(db, scenario, 0)
        assert sample["rows"] == 16
        assert sample["first_row_ns"] is not None
        assert sample["first_row_ns"] <= sample["elapsed_ns"]
        stats = sample["statistics"]
        expected = 16 if family == "read_autocommit" else 1
        assert stats["diagnostic_transactions_begin"] == expected
        assert stats["diagnostic_transactions_commit"] + stats["diagnostic_transactions_rollback"] == expected
        assert stats["diagnostic_partial_owned_transactions"] == 1
        assert stats["diagnostic_owned_transactions_after_close"] == 0
        assert stats["diagnostic_physical_pins_after_close"] >= 0
        assert db.transactions.open_transactions == 0
        assert lifecycle.execute(db, scenario)[0] == oracle(scenario)
        # Reusable manager pins are allowed. Participant disappears with handle.
        with okto_grafx.connect(tmp_path / "db", codec="pure", vector_math="pure") as observer:
            db.close()
            assert observer._transactions._coordinator.reader_horizon() is None


def test_registry_and_closed_configuration():
    scenarios = lifecycle.matrix()
    assert {s.name for s in scenarios} <= {s.name for s in matrix()}
    for scenario in scenarios:
        assert Scenario.from_dict(scenario.to_dict()) == scenario
        assert scenario.role == "control"
        assert "after timing" in lifecycle.contract(scenario)["diagnostic_scope"]
        for changes in (dict(keys=(1,)), dict(nodes=8), dict(churn_percent=25), dict(reader="retained")):
            with pytest.raises(ValueError):
                replace(scenario, **changes)


@pytest.mark.parametrize("family", ["read_autocommit", "read_shared"])
def test_multi_statement_recipe_uses_sequence_memory_observer(tmp_path, family):
    scenario = Scenario("case", family, nodes=16, edges_per_node=0,
                        keys=tuple(range(16)), query_memory_budget_bytes=1 << 20)
    with okto_grafx.connect(tmp_path / "budgeted", query_memory_budget_bytes=1 << 20) as db:
        build_fixture(db, scenario)
        sample = measure_statement(db, scenario, 0)
    assert sample["memory"]["coverage"] == SEQUENCE_LOGICAL_COVERAGE
    assert sample["memory"]["logical_memory_peak_bytes"] is not None
    assert sample["memory"]["unavailable_reason"] is None


def _qualified_shared_read():
    data = campaign()
    scenario = Scenario("rf10-read_shared-warm", "read_shared", nodes=16,
                        edges_per_node=0, keys=tuple(range(16)),
                        query_memory_budget_bytes=1 << 20)
    data.update(mode="recipe", scenarios=[scenario.to_dict()])
    diagnostics = {"diagnostic_transactions_begin": 1,
                   "diagnostic_transactions_commit": 1,
                   "diagnostic_transactions_rollback": 0,
                   "diagnostic_partial_first_row_ns": 1,
                   "diagnostic_partial_close_ns": 1,
                   "diagnostic_partial_owned_transactions": 1,
                   "diagnostic_partial_resident_physical_pins": 0,
                   "diagnostic_owned_transactions_after_close": 0,
                   "diagnostic_physical_pins_after_close": 0,
                   "diagnostic_pool_retained_estimate_bytes": 0,
                   "diagnostic_standing_reader_horizon_present": 0,
                   "diagnostic_handle_close_complete": 1,
                   "lifecycle_explicit_close_ns": 1}
    memory = {"logical_memory_peak_bytes": 100, "baseline_retained_bytes": 0,
              "coverage": SEQUENCE_LOGICAL_COVERAGE, "unavailable_reason": None}
    for run in data["runs"]:
        run["samples"] = [sample_record(scenario.to_dict(), index, lifecycle.oracle(scenario),
                                        diagnostics, elapsed_ns=100, cpu_ns=10, prepare_ns=2,
                                        first_row_ns=5, memory=memory.copy(), rss_peak_bytes=100)
                          for index in range(25)]
    return data


def test_qualified_read_recipe_accepts_first_row_and_bound_ownership_proof():
    assert compare_campaign(_qualified_shared_read())["decision"] == "tradeoff_reported"


@pytest.mark.parametrize("tamper", [
    lambda d: d["runs"][0]["samples"][0].update(first_row_ns=None),
    lambda d: d["runs"][0]["samples"][0]["statistics"].update(diagnostic_transactions_begin=16),
    lambda d: d["runs"][0]["samples"][0]["statistics"].update(diagnostic_handle_close_complete=0),
    lambda d: d["runs"][0]["samples"][0]["statistics"].pop("diagnostic_partial_owned_transactions"),
    lambda d: d["runs"][0]["samples"][0]["memory"].update(coverage="single_statement_only"),
    lambda d: d["runs"][0]["samples"][0].update(memory_budget_bytes=None),
])
def test_qualified_read_recipe_rejects_missing_or_misattributed_evidence(tamper):
    data = deepcopy(_qualified_shared_read())
    tamper(data)
    with pytest.raises(CampaignError):
        compare_campaign(data)
