"""RF03 independent topology/oracles and native small database checks."""
from dataclasses import replace

import pytest
import okto_grafx

from bench.harness.campaign_fixtures import Scenario, build_fixture, matrix, oracle, query_for
from bench.harness.connected_qualification import fixture_data, matrix as connected_matrix


def test_exact_rf03_fixture_and_membership_oracle():
    scenario = next(s for s in connected_matrix() if s.name == "rf03-connected-n1024-k8")
    starts, destinations, edges = fixture_data(scenario)
    assert (len(starts), len(destinations), len(edges)) == (1024, 128, 1151)
    assert fixture_data(scenario) == fixture_data(scenario)
    assert len(oracle(scenario)) == 16
    assert oracle(replace(scenario, keys=scenario.keys + scenario.keys)) == oracle(scenario)
    assert Scenario.from_dict(scenario.to_dict()) == scenario
    assert "MATCH (a:N)-[r:E]->(b:B) WHERE a.id IN $keys" in query_for(scenario)[0]
    assert {s.name for s in connected_matrix()} <= {s.name for s in matrix()}


@pytest.mark.parametrize("family", ["connected_in", "connected_with", "isolated_in"])
@pytest.mark.parametrize("distribution", ["uniform", "hub"])
def test_connected_oracle_executes_on_real_graph(tmp_path, family, distribution):
    scenario = Scenario("small", family, nodes=16, destination_nodes=4,
                        parallel_edges=7, keys=(0, 1, 1, 15, 99), distribution=distribution)
    with okto_grafx.connect(tmp_path / "db", codec="pure", vector_math="pure") as db:
        build_fixture(db, scenario)
        query, parameters = query_for(scenario)
        assert db.execute(query, parameters).rows == oracle(scenario)


@pytest.mark.parametrize("changes", [dict(destination_nodes=0), dict(parallel_edges=-1),
                                     dict(churn_percent=25), dict(reader="retained"),
                                     dict(keys=tuple(range(1025)))])
def test_connected_unsupported_configurations_fail_closed(changes):
    with pytest.raises(ValueError):
        Scenario("bad", "connected_in", **changes)


def test_selectivity_control_crosses_existing_threshold_within_api_cap():
    broad = next(s for s in connected_matrix() if s.name == "rf03-broad-n16-k1024")
    assert len(set(broad.keys)) >= 2 * broad.nodes
    assert len(broad.keys) <= 1024
    assert broad.role == "control"


def test_exact_fixture_builds_across_public_parameter_chunks(tmp_path):
    scenario = next(s for s in connected_matrix() if s.name == "rf03-connected-n1024-k8")
    with okto_grafx.connect(tmp_path / "db", codec="pure", vector_math="pure") as db:
        build_fixture(db, scenario)
        assert db.execute("MATCH (a:N) RETURN count(a)").rows == ((1024,),)
        assert db.execute("MATCH (b:B) RETURN count(b)").rows == ((128,),)
        assert db.execute("MATCH (:N)-[r:E]->(:B) RETURN count(r)").rows == ((1151,),)
        query, parameters = query_for(scenario)
        result = db.execute(query, parameters)
        assert result.rows == oracle(scenario)
        assert result.statistics.get("rows_scanned", 0) < 1024
