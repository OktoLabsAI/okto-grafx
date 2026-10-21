"""RF10 recipe measurements exercise ownership; no timing thresholds in CI."""
from dataclasses import replace

import pytest
import okto_grafx

from bench.harness import read_lifecycle_qualification as lifecycle
from bench.harness.campaign_fixtures import Scenario, build_fixture, matrix, oracle
from bench.harness.campaign_worker import measure_statement


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
