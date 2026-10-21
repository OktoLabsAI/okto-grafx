"""RF05 campaign fixture keeps exact order and observes bounded/fallback work."""

import pytest
from copy import deepcopy

from bench.harness.campaign_fixtures import Scenario, build_fixture, contract, matrix, oracle, query_for
from bench.harness.pagination_qualification import validate_qualification_sample
from bench.harness.campaign_worker import measure_statement
from bench.harness.campaign_schema import canonical_hash
from okto_grafx import connect
from okto_grafx.domain.model.value import Timestamp


@pytest.mark.parametrize("case", ["first", "membership", "dynamic_membership", "keyset", "skip", "no_index"])
def test_rf05_case_has_independent_oracle_and_runtime_regime(tmp_path, case):
    scenario = Scenario(f"rf05-{case}", "pagination", nodes=64, edges_per_node=0,
                        page=16, pagination_case=case)
    query, params = query_for(scenario)
    with connect(tmp_path / "db", codec="pure") as db:
        build_fixture(db, scenario)
        result = db.execute(query, params)
        assert result.rows == oracle(scenario)
        assert len(result.rows) == 16
        sample = measure_statement(db, scenario, 5)
        assert sample["result_hash"] == sample["expected_result_hash"]
        assert sample["contract_hash"]
        assert sample["first_row_ns"] is None  # db.execute materializes the page.
        assert sample["consume_all_ns"] == sample["elapsed_ns"]
        validate_qualification_sample(sample, scenario, arm="B")
        if case in ("first", "membership", "dynamic_membership", "keyset"):
            assert result.statistics.get("ordered_candidates_examined") == (
                32 if case in ("membership", "dynamic_membership") else 16
            )
            assert result.statistics.get("ordered_merge_rows") == 16
            if case == "first":
                assert 0 < result.statistics["physical_node_versions_examined"] < scenario.nodes
                assert sample["statistics"]["physical_node_versions_examined"] == result.statistics[
                    "physical_node_versions_examined"]
        else:
            assert result.statistics.get("ordered_merge_rows", 0) == 0
        false_work = deepcopy(sample)
        if case in ("first", "membership", "dynamic_membership", "keyset"):
            false_work["statistics"]["ordered_candidates_examined"] += 1
        else:
            false_work["statistics"]["ordered_merge_rows"] = 1
        with pytest.raises(ValueError, match="RF05"):
            validate_qualification_sample(false_work, scenario, arm="B")
        false_first_row = deepcopy(sample)
        false_first_row["first_row_ns"] = 1
        with pytest.raises(ValueError, match="first-row"):
            validate_qualification_sample(false_first_row, scenario, arm="B")


def test_rf05_catalog_is_explicit_and_rejects_wrong_dimensions():
    cases = [item for item in matrix() if item.family == "pagination"]
    assert {item.pagination_case for item in cases} == {
        "first", "membership", "dynamic_membership", "keyset", "skip", "no_index"
    }
    assert all(item.nodes == 1024 and item.page == 16 for item in cases)
    assert len({item.name for item in cases}) == len(cases)
    assert all(Scenario.from_dict(item.to_dict()) == item for item in cases)
    assert contract(cases[1])["rejected_top_candidates"] == 16
    assert canonical_hash(Timestamp(1336)) != canonical_hash(Timestamp(1337))
    with pytest.raises(ValueError):
        Scenario("invalid", "pagination", nodes=32, page=32, pagination_case="first")
