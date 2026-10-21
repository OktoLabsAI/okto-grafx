"""Input-derived ranking, source coverage and retained-snapshot RF11 controls."""
from __future__ import annotations

import pytest

from bench.harness.campaign_fixtures import Scenario, matrix
from bench.harness.fts_qualification import apply_updates, build_fixture, oracle, rows, statistics
from bench.harness.campaign_worker import measure_statement
from okto_grafx import connect


def test_matrix_covers_statistics_corpus_cache_and_snapshot_slices():
    scenarios = [s for s in matrix() if s.family == "fts_matrix"]
    assert {s.nodes for s in scenarios} == {256, 4096, 65536}
    assert {(s.fts_statistics_mode, s.fts_history) for s in scenarios} >= {
        ("wal", 0), ("durable", 0), ("durable", 1), ("durable", 8), ("durable", 32)}
    assert {s.cache for s in scenarios} == {"warm", "reopen"}
    assert {s.term for s in scenarios} == {"common", "rare"}
    assert {s.reader for s in scenarios} == {"short", "retained"}


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
                   {"nodes": 65536, "term": "common"}):
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
