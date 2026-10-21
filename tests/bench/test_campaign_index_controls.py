"""Executed physical counters, not explain labels, qualify these control shapes."""
import pytest

from okto_grafx import connect
from bench.harness.campaign_fixtures import Scenario, build_fixture, contract, matrix, oracle, query_for


@pytest.mark.parametrize("family", ["pk_seek", "landing"])
def test_index_control_executes_certified_work_with_independent_oracle(tmp_path, family):
    spec = Scenario(family, family, nodes=128, edges_per_node=0, fanout=64,
                    keys=(2, 2, 7, 999))
    with connect(tmp_path / "db") as db:
        build_fixture(db, spec)
        query, parameters = query_for(spec)
        for _ in range(2):  # Prepared/warm execution must still exercise the stated work.
            result = db.execute(query, parameters)
            expected = ((2,), (2,), (7,)) if family == "pk_seek" else tuple((0, i) for i in range(64))
            assert oracle(spec) == expected
            assert result.rows == expected
            assert result.statistics.get("index_probe_keys", 0) > 0
            assert result.statistics.get("certificate_attempts", 0) > 0
            assert result.statistics.get("physical_node_versions_examined", 0) > 0
            if family == "landing":
                assert result.statistics.get("landing_candidates", 0) >= 64
                assert result.statistics.get("landing_batches", 0) > 0


def test_budgeted_landing_is_explicitly_a_fallback_not_batch_evidence(tmp_path):
    spec = Scenario("guard", "landing", nodes=32, edges_per_node=0, fanout=8,
                    query_memory_budget_bytes=4096)
    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        build_fixture(db, spec)
        result = db.execute(*query_for(spec))
        assert result.rows == oracle(spec)
        assert result.statistics["fallback_reason_memory_guard"] == 1
        assert result.statistics.get("landing_batches", 0) == 0


def test_catalog_explicitly_names_certified_controls():
    catalog = {spec.name: spec for spec in matrix()}
    assert catalog["pk-seek-control"].family == "pk_seek"
    assert catalog["landing-control"].family == "landing"
    assert "diagnostic control only" in contract(catalog["landing-control"])["qualification_limit"]
