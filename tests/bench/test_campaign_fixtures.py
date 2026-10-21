"""Independent synthetic graph oracle and explicit matrix coverage."""
from bench.harness.campaign_fixtures import Scenario, fixture_data, matrix, oracle, query_for


def test_seeded_data_and_hashes_are_reproducible():
    scenario = Scenario(name="edge", family="edge_scan", nodes=16, edges_per_node=8,
                        distribution="hub", seed=12)
    assert fixture_data(scenario) == fixture_data(scenario)
    rows = oracle(scenario)
    assert len(rows) == 128
    assert rows == tuple(sorted(rows))
    assert sum(a == 0 for a, _ in rows) >= 102


def test_key_oracle_keeps_unwind_duplicates_and_empty():
    scenario = Scenario(name="keys", family="keys", nodes=16, keys=(2, 2, 3, 999))
    assert oracle(scenario) == ((2,), (2,), (3,))
    assert oracle(Scenario(name="empty", family="keys", nodes=16, keys=())) == ()
    assert "$keys" in query_for(scenario)[0]


def test_catalog_has_required_dimensions_without_cartesian_explosion():
    catalog = matrix()
    assert {1024, 16384} <= {s.nodes for s in catalog}
    assert {0, 1, 8, 16, 64} <= {len(s.keys) for s in catalog if s.family == "keys"}
    assert {0, 1, 63, 64, 65, 128} <= {s.fanout for s in catalog if s.family == "fanout"}
    assert {1, 16, 128} <= {s.page for s in catalog if s.family == "page"}
    assert {1, 32, 128, 512} <= {s.write_rows for s in catalog if s.family == "write"}
    assert {64, 4096} <= {s.payload_bytes for s in catalog if s.family == "write"}
    assert {1, 2, 4} <= {s.writers for s in catalog if s.family == "write"}
    assert {"short", "retained"} <= {s.reader for s in catalog}
    assert {0, 25} <= {s.churn_percent for s in catalog}
    assert {"rare", "common"} <= {s.term for s in catalog if s.family == "fts"}


def test_churn_oracle_reflects_updates_and_deletes():
    scenario = Scenario(name="churn", family="node_scan", nodes=16, churn_percent=25)
    assert len(oracle(scenario)) == 14
