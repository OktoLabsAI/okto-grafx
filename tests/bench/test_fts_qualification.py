"""Input-derived ranking, source coverage and retained-snapshot RF11 controls."""
from __future__ import annotations

import pytest

from bench.harness.campaign_schema import CampaignError, compare_campaign, validate_campaign
from bench.harness.campaign_fixtures import Scenario, matrix
from bench.harness.fts_qualification import apply_updates, apply_write_case, build_fixture, corpus_documents, oracle, rows, statistics
from bench.harness.campaign_worker import measure_statement, sample_record, unavailable_memory
from okto_grafx import connect


def test_matrix_covers_statistics_corpus_cache_and_snapshot_slices():
    scenarios = [s for s in matrix() if s.family == "fts_matrix"]
    assert {s.nodes for s in scenarios} == {256, 4096, 65536}
    assert {(s.fts_statistics_mode, s.fts_history) for s in scenarios} >= {
        ("wal", 0), ("durable", 0), ("durable", 1), ("durable", 8), ("durable", 32)}
    assert {s.cache for s in scenarios} == {"warm", "reopen"}
    assert {s.term for s in scenarios} == {"common", "rare"}
    assert {s.reader for s in scenarios} == {"short", "retained"}
    assert {(s.fts_statistics_mode, s.fts_write_case) for s in scenarios
            if s.fts_write_case != "none"} == {
                (mode, write_case) for mode in ("wal", "durable")
                for write_case in ("insert", "update", "delete", "external_mixed")}


@pytest.mark.parametrize("mode,history", [("wal", 0), ("durable", 8)])
@pytest.mark.parametrize("write_case,ack_count", [
    ("insert", 1), ("update", 1), ("delete", 1), ("external_mixed", 4)])
def test_write_cost_slices_require_durable_acks_and_reopen_exactly(
        tmp_path, mode, history, write_case, ack_count):
    scenario = Scenario(f"fts-{mode}-{write_case}", "fts_matrix", nodes=256,
                        edges_per_node=0, fts_statistics_mode=mode,
                        fts_history=history, cache="reopen", fts_write_case=write_case)
    path = tmp_path / "db"
    with connect(path) as db:
        build_fixture(db, scenario)
        durations = apply_write_case(db, scenario, path)
        assert len(durations) == ack_count
        assert all(duration > 0 for duration in durations)
    with connect(path) as reopened:
        found, result = rows(reopened, scenario)
        assert found == oracle(scenario)
        assert result.corpus_documents == corpus_documents(scenario)
        assert result.statistics_regime == ("full_census" if mode == "wal" else "durable_summary")
        assert not reopened.verify("all").findings
        reopened.checkpoint()
    with connect(path, read_only=True) as read_only:
        assert rows(read_only, scenario)[0] == oracle(scenario)
        assert not read_only.verify("all").findings


def test_write_cost_report_rejects_missing_ack_or_reopen_proof():
    from tests.bench.test_campaign_schema import campaign

    scenario = Scenario("fts-durable-insert", "fts_matrix", nodes=256,
                        edges_per_node=0, fts_statistics_mode="durable",
                        fts_history=8, cache="reopen", fts_write_case="insert")
    data = campaign()
    data["mode"] = "recipe"
    data["scenarios"] = [scenario.to_dict()]
    for run in data["runs"]:
        run["samples"] = []
        for index in range(25):
            sample = sample_record(
                scenario.to_dict(), index, oracle(scenario),
                {"postings_visited": 256, "candidates": 256,
                 "corpus_documents": 257, "statistics_wal_records": 0,
                 "statistics_from_durable_summary": 1},
                elapsed_ns=100, cpu_ns=10, prepare_ns=2,
                memory=unavailable_memory("native_fts_memory_not_covered_by_query_budget_observer"),
                rss_peak_bytes=100,
                rss_scope="process_lifetime_peak_working_set_bytes")
            sample["fts_source"] = {"statistics_regime": "durable_summary",
                                    "statistics_mode": "durable", "history_entries": 8,
                                    "hits": sample["rows"]}
            sample["fts_setup"] = {"elapsed_ns": 100, "disk_allocated_bytes": 100,
                                   "wal_allocated_bytes": 0,
                                   "scope": "seed_insert_index_creation_checkpoint_and_close",
                                   "observation_scope": "one_fixture_per_sample",
                                   "observation_id": f"{run['invocation_id']}:{scenario.name}:{index}"}
            sample["fts_maintenance"] = {"elapsed_ns": 10, "durable_acks": 1,
                                         "commit_ns": [5], "disk_allocated_delta_bytes": 1,
                                         "wal_allocated_delta_bytes": 1,
                                         "scope": "separate_durable_units_before_cold_reopen",
                                         "write_case": "insert", "writer_origin": "same_handle",
                                         "read_only_reopen_verified": True}
            run["samples"].append(sample)
    validate_campaign(data)
    tradeoff = compare_campaign(data)["comparisons"][0]["fts_tradeoff"]
    assert tradeoff["write_case"] == "insert"
    assert tradeoff["by_arm"]["A"]["write_commit_ack_ns"]["median_ns"] == 5
    assert tradeoff["by_arm"]["A"]["setup_elapsed_ns"]["observation_count"] == 20
    for key, bad in (("durable_acks", 0), ("read_only_reopen_verified", False),
                     ("writer_origin", "second_handle")):
        data["runs"][0]["samples"][0]["fts_maintenance"][key] = bad
        with pytest.raises(CampaignError):
            validate_campaign(data)
        data["runs"][0]["samples"][0]["fts_maintenance"][key] = (
            1 if key == "durable_acks" else True if key == "read_only_reopen_verified"
            else "same_handle")


def test_ranked_fts_oracle_and_source_are_real_for_wal_and_durable(tmp_path):
    for mode, history in (("wal", 0), ("durable", 8)):
        scenario = Scenario(f"fts-{mode}", "fts_matrix", nodes=256, edges_per_node=0,
                            fts_statistics_mode=mode, fts_history=history, term="common")
        path = tmp_path / mode
        with connect(path) as db:
            build_fixture(db, scenario)
        with connect(path) as db:
            found, result = rows(db, scenario)
            assert found == oracle(scenario)
            assert [row[0] for row in found] == list(range(1, 257))
            measured = statistics(result)
            assert measured["corpus_documents"] == 256
            assert measured["postings_visited"] >= len(found)
            assert measured["statistics_from_" + result.statistics_regime] == 1
            rare = Scenario(f"fts-rare-{mode}", "fts_matrix", nodes=256, edges_per_node=0,
                            fts_statistics_mode=mode, fts_history=history, term="rare")
            rare_found, _ = rows(db, rare)
            assert rare_found == oracle(rare)


@pytest.mark.parametrize("history,expected", [(0, "full_census"), (1, "full_census"),
                                              (8, "durable_summary"), (32, "durable_summary")])
def test_retained_reader_uses_before_update_corpus_and_score(tmp_path, history, expected):
    scenario = Scenario("fts-retained", "fts_matrix", nodes=256, edges_per_node=0,
                        fts_statistics_mode="durable", fts_history=history,
                        term="common", reader="retained", fts_updates=4)
    with connect(tmp_path / "db") as db:
        build_fixture(db, scenario)
        with db.begin("read") as reader:
            reader.execute("MATCH (n:N) RETURN count(n)")
            apply_updates(db, scenario)
            db._text_stats_cache.clear()
            found, result = rows(db, scenario, reader=reader)
            assert found == oracle(scenario)
            assert result.corpus_documents == 256
            assert result.statistics_regime == expected


def test_wal_delta_source_requires_cached_seed_and_exact_new_score(tmp_path):
    scenario = Scenario("fts-delta", "fts_matrix", nodes=256, edges_per_node=0,
                        fts_statistics_mode="wal", fts_history=0,
                        term="common", cache="warm", fts_updates=1)
    with connect(tmp_path / "db") as db:
        build_fixture(db, scenario)
        rows(db, scenario)
        apply_updates(db, scenario)
        found, result = rows(db, scenario)
    assert result.statistics_regime == "wal_delta"
    assert result.statistics_wal_records > 0
    assert found == oracle(scenario)
    assert found[-1][0] == 2


def test_invalid_wal_history_and_large_common_slice_rejected():
    for values in ({"fts_statistics_mode": "wal", "fts_history": 1},
                   {"nodes": 65536, "term": "common"},
                   {"fts_write_case": "external_mixed", "cache": "warm"}):
        try:
            Scenario("bad", "fts_matrix", **values)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid FTS qualification slice accepted")


def test_native_worker_records_unavailable_logical_memory_and_real_rss(tmp_path):
    scenario = Scenario("fts-sample", "fts_matrix", nodes=256, edges_per_node=0,
                        fts_statistics_mode="durable", fts_history=8, term="rare")
    with connect(tmp_path / "db") as db:
        build_fixture(db, scenario)
        rows(db, scenario)
        sample = measure_statement(db, scenario, 5)
    assert sample["result_hash"] == sample["expected_result_hash"]
    assert sample["memory"]["unavailable_reason"] == "native_fts_memory_not_covered_by_query_budget_observer"
    assert sample["memory"]["logical_memory_peak_bytes"] is None
    assert sample["rss_peak_bytes"] > 0
    assert sample["fts_source"]["history_entries"] == 8
