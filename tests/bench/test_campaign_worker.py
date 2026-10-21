from bench.harness.campaign_fixtures import Scenario, build_fixture, oracle
from bench.harness.campaign_worker import measure_statement
from okto_grafx import connect
import pytest


def test_default_sink_metadata_uses_underlying_noop_sink(tmp_path):
    with connect(tmp_path / "db") as db:
        assert type(db._queries._metrics).__name__ == "MetricEmitter"
        assert type(db._queries._metrics.sink).__name__ == "ContainedMetricsSink"
        assert type(db._queries._metrics.sink.inner).__name__ == "NoOpMetricsSink"
        assert db._queries._metrics.enabled is False


@pytest.mark.parametrize("family", ["write", "fts"])
def test_write_ack_reopen_and_native_fts_membership(tmp_path, family):
    scenario = Scenario(name=family, family=family, nodes=4, write_rows=2, term="rare")
    with connect(tmp_path / "db") as db:
        build_fixture(db, scenario)
        sample = measure_statement(db, scenario, 0)
    assert sample["result_hash"] == sample["expected_result_hash"]
    assert (sample["commit_ns"] is not None) == (family == "write")
    if family == "write":
        with connect(tmp_path / "db") as db:
            assert db.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id").rows == oracle(scenario)


def test_retained_reader_observes_prechurn_rows(tmp_path):
    from bench.harness.campaign_fixtures import apply_churn
    scenario = Scenario(name="old", family="node_scan", nodes=8, reader="retained", churn_percent=25)
    with connect(tmp_path / "db") as db:
        build_fixture(db, scenario, apply_updates=False)
        with db.begin("read") as reader:
            reader.execute("MATCH (n:N) RETURN count(n)")
            apply_churn(db, scenario)
            sample = measure_statement(db, scenario, 0, reader=reader)
        assert sample["rows"] == 8
        assert len(db.execute("MATCH (n:N) RETURN n.id").rows) == 7


@pytest.mark.multiprocess
def test_two_writer_processes_persist_disjoint_exact_rows(tmp_path):
    from bench.harness.campaign_worker import measure_concurrent_write
    scenario = Scenario(name="writers", family="write", nodes=2, write_rows=1, writers=2)
    with connect(tmp_path / "db") as db:
        build_fixture(db, scenario)
    sample = measure_concurrent_write(tmp_path / "db", scenario, 0)
    assert sample["rows"] == 2
    assert len({writer["process_id"] for writer in sample["writers"]}) == 2
    assert all(writer["commit_ns"] > 0 for writer in sample["writers"])
    assert sample["memory"]["logical_memory_peak_bytes"] is None


def test_separate_candidate_memory_producer_observes_real_lifecycle(tmp_path):
    from bench.harness.campaign_worker import collect_candidate_memory
    from bench.harness.campaign_schema import validate_candidate_memory
    run = {"revision": "b" * 40, "process_id": 123, "invocation_id": "test-only",
           "engine_source_hashes": {"query": "b" * 64}}
    evidence = collect_candidate_memory(run, {4096}, tmp_path)
    validate_candidate_memory(dict(run, candidate_memory_evidence=evidence), {4096})
    records = evidence["body"]["budgets"][0]["records"]
    assert len(records) == 60
    assert {r["mode"] for r in records} == {"success", "error", "cancel"}
    assert all(r["memory"]["logical_memory_peak_bytes"] > 0 for r in records)
    assert all(r["live_before_bytes"] == r["live_after_bytes"] == 0 for r in records)
    assert all(r["handle_before_bytes"] == r["handle_after_bytes"] == 0 for r in records)


def test_real_cursor_reports_first_row_consumption_and_exact_oracle(tmp_path):
    scenario = Scenario(name="cursor", family="node_scan", nodes=8, cursor_batch_size=1)
    with connect(tmp_path / "db") as db:
        build_fixture(db, scenario)
        sample = measure_statement(db, scenario, 0)
    assert sample["rows"] == len(oracle(scenario))
    assert 0 <= sample["first_row_ns"] <= sample["consume_all_ns"] <= sample["elapsed_ns"]
    assert sample["commit_ns"] is None
    assert sample["result_hash"] == sample["expected_result_hash"]
    assert sample["prepare_boundary"] == "explain_parse_plan_not_lazy_expression_compile"


def test_real_scan_reports_physical_work(tmp_path):
    scenario = Scenario(name="edges", family="edge_scan", nodes=8, edges_per_node=2)
    with connect(tmp_path / "db") as db:
        build_fixture(db, scenario)
        sample = measure_statement(db, scenario, 0)
    assert sample["statistics"]["edge_scans"] == 1
    assert sample["statistics"]["physical_edge_versions_examined"] > 1
    assert sample["first_row_ns"] is None
def test_real_unbudgeted_landing_reports_unavailable_memory_coverage(tmp_path):
    from bench.harness.campaign_fixtures import matrix
    scenario = next(s for s in matrix() if s.name == "landing-control")
    assert scenario.query_memory_budget_bytes is None
    with connect(tmp_path / "landing") as db:
        build_fixture(db, scenario)
        sample = measure_statement(db, scenario, 0)
    assert sample["memory"] == {
        "logical_memory_peak_bytes": None,
        "baseline_retained_bytes": 0,
        "coverage": "unavailable",
        "unavailable_reason": "query_memory_budget_disabled",
    }
    assert sample["memory_budget_bytes"] is None
    assert sample["statistics"]["landing_batches"] > 0
    assert sample["statistics"]["landing_candidates"] > 0
    assert sample["result_hash"] == sample["expected_result_hash"]
