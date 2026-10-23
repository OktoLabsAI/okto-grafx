"""Deterministic RF02 campaign slices; timing is never asserted here."""
from bench.harness.campaign_fixtures import Scenario, matrix, oracle, query_for
from bench.harness.rf02_qualification import matrix as rf02_matrix


def test_rf02_slices_preserve_occurrences_and_roles():
    scenarios = rf02_matrix()
    target = next(s for s in scenarios if s.name == "rf02-scalar-n1024-occ16-dist8")
    assert target.role == "target"
    assert target.work_counter == "physical_node_versions_examined"
    assert len(target.keys) == 16 and len(set(target.keys)) == 8
    assert len(oracle(target)) == 16
    assert "{id:k}" in query_for(target)[0]
    assert Scenario.from_dict(target.to_dict()) == target
    assert {s.name for s in scenarios} <= {s.name for s in matrix()}
    assert any(s.family == "pk_seek" and s.role == "control" for s in scenarios)
    assert any(s.family == "node_scan" and s.role == "control" for s in scenarios)
    assert len({s.name for s in matrix()}) == len(matrix())
