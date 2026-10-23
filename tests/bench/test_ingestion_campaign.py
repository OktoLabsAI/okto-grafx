"""RF09 deterministic recipe oracles; these tests do not qualify timing."""
import copy
from dataclasses import replace

import pytest

from bench.harness.campaign_fixtures import Scenario, build_fixture, oracle
from bench.harness import campaign_fixtures, campaign_schema, campaign_worker
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.errors import GrafxQueryError, GrafxWriteConflict


MIB = 1 << 20
READBACK = "MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id"


@pytest.mark.parametrize("shape", ("unwind", "statements", "executemany", "chunks", "transactions"))
def test_matrix_central_recipes_preserve_rows_payload_acks_and_one_mib(tmp_path, shape):
    scenario = replace(next(s for s in campaign_fixtures.ingestion_matrix()
                            if (s.write_shape, s.write_rows, s.payload_bytes, s.writers)
                            == (shape, 32, 64, 1)), query_memory_budget_bytes=MIB)
    root = tmp_path / shape
    with connect(root, query_memory_budget_bytes=MIB) as db:
        build_fixture(db, scenario)
        sample = campaign_worker.measure_statement(db, scenario, 0)
        assert db.execute(READBACK).rows == oracle(scenario)
        assert db.transactions.open_transactions == 0
        assert db._queries._owner_budget._used_bytes == 0
        db.checkpoint()
    with connect(root, read_only=True) as reopened:
        rows = reopened.execute(READBACK).rows
        assert rows == oracle(scenario)
        assert all(len(payload) == 64 for _, payload in rows)
        assert reopened.verify("all").findings == ()

    assert sample["rows"] == 32
    assert sample["result_hash"] == sample["expected_result_hash"]
    assert sample["write"]["business_rows"] == 32
    expected_acks = 32 if shape == "transactions" else 1
    reports = sample["write"]["commit_reports"]
    assert sample["write"]["commits"] == sample["write"]["durable_acks"] == expected_acks
    assert len(reports) == expected_acks
    assert all(report["durable"] is True and report["wrote"] is True for report in reports)
    assert len({report["csn"] for report in reports}) == expected_acks
    assert sum(report["rows"] for report in reports) == 32
    assert [report["offset"] for report in reports] == (
        list(range(32)) if shape == "transactions" else [0])
    assert 0 <= sample["memory"]["logical_memory_peak_bytes"] <= MIB
    assert sample["memory"]["baseline_retained_bytes"] == 0
    assert sample["memory"]["coverage"] != "unavailable"
    assert sample["memory"]["unavailable_reason"] is None
    campaign_schema.validate_write_sample(sample, scenario, qualification=True)


def test_matrix_chunk_recipe_keeps_each_durable_prefix_atomic(tmp_path):
    scenario = replace(next(s for s in campaign_fixtures.ingestion_matrix()
                            if (s.write_shape, s.write_rows, s.payload_bytes, s.writers)
                            == ("chunks", 128, 4096, 1)), query_memory_budget_bytes=MIB)
    root = tmp_path / "chunks"
    with connect(root, query_memory_budget_bytes=MIB) as db:
        build_fixture(db, scenario)
        sample = campaign_worker.measure_statement(db, scenario, 0)
        assert db.transactions.open_transactions == 0
        db.checkpoint()
    with connect(root, read_only=True) as reopened:
        assert reopened.execute(READBACK).rows == oracle(scenario)
        assert reopened.verify("all").findings == ()
    reports = sample["write"]["commit_reports"]
    assert [(r["offset"], r["rows"]) for r in reports] == [(0, 32), (32, 32), (64, 32), (96, 32)]
    assert sample["write"]["durable_acks"] == 4
    assert 0 <= sample["memory"]["logical_memory_peak_bytes"] <= MIB
    assert sample["memory"]["coverage"] != "unavailable"
    campaign_schema.validate_write_sample(sample, scenario, qualification=True)


def test_recipe_occ_conflict_rolls_back_without_implicit_replay_then_explicit_retry(
    tmp_path, monkeypatch,
):
    scenario = Scenario("occ-recipe", "write", nodes=1, edges_per_node=0,
                        write_rows=2, write_shape="statements", query_memory_budget_bytes=MIB)
    records = [{"id": 0, "payload": "a"}, {"id": 1, "payload": "b"}]
    root = tmp_path / "occ"
    with connect(root, query_memory_budget_bytes=MIB) as db:
        build_fixture(db, scenario)
        original_commit = Transaction.commit
        attempted = []

        def conflicting_commit(tx):
            attempted.append(tx)
            with db.begin("write") as contender:
                contender.execute("CREATE (:W {id:0,payload:'contender'})")
                ack = original_commit(contender)
                assert ack.durable is True and ack.wrote is True
            return original_commit(tx)

        audit = {}
        with monkeypatch.context() as patch:
            patch.setattr(Transaction, "commit", conflicting_commit)
            with pytest.raises(GrafxWriteConflict) as failure:
                campaign_worker.execute_write_recipe(db, scenario, records,
                                                     failure_evidence=audit)
        assert failure.value.retryable is True
        assert len(attempted) == 1
        assert audit["confirmed_prefix"] == []
        assert audit["failed_transaction"]["phase"] == "commit"
        assert db.transactions.open_transactions == 0
        assert db.execute(READBACK).rows == ((0, "contender"),)
        db.checkpoint()
    with connect(root, query_memory_budget_bytes=MIB) as db:
        assert db.execute(READBACK).rows == ((0, "contender"),)
        with db.begin("write") as tx:
            tx.execute("MATCH (n:W {id:0}) DELETE n")
        stats, reports, _ = campaign_worker.execute_write_recipe(db, scenario, records)
        assert stats["rows_created"] == 2
        assert len(reports) == 1 and reports[0]["durable"] is True
        assert db.execute(READBACK).rows == ((0, "a"), (1, "b"))
        assert db.transactions.open_transactions == 0
        db.checkpoint()
    with connect(root, read_only=True) as reopened:
        assert reopened.execute(READBACK).rows == ((0, "a"), (1, "b"))
        assert reopened.verify("all").findings == ()


def test_sequence_observes_retained_growth_and_partial_call_cleanup(tmp_path):
    with connect(tmp_path / "sequence", query_memory_budget_bytes=65536) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE W(id INT64,payload STRING,PRIMARY KEY(id))")
        with db._queries._observe_logical_memory_sequence() as observation:
            with db.begin("write") as tx:
                tx.execute("CREATE (:W {id:9,payload:'prior'})")
                before = observation.current
                tx.executemany("CREATE (:W {id:$id,payload:$payload})",
                               ({"id": i, "payload": "x" * 512} for i in range(3)))
                after = observation.current
                with pytest.raises(GrafxQueryError, match="primary key"):
                    tx.executemany("CREATE (:W {id:$id,payload:$payload})",
                                   ({"id": 10, "payload": "y"}, {"id": 9, "payload": "duplicate"}))
                assert tx.execute("MATCH (n:W) RETURN n.id ORDER BY n.id").rows == ((0,), (1,), (2,), (9,))
        proof = observation.result()
        assert after >= before
        assert proof["unavailable_reason"] is None
        assert proof["logical_memory_peak_bytes"] >= after
        assert db.transactions.open_transactions == 0
        with db._queries._observe_logical_memory() as single:
            db.execute("RETURN 1")
            db.execute("RETURN 2")
        assert single.result()["unavailable_reason"] == "multiple_statements_in_interval"


def test_sequence_keeps_live_ledger_between_statements_without_summing_peaks():
    from okto_grafx.domain.query.memory import LogicalMemoryBudget, _LogicalMemoryObservation
    observation = _LogicalMemoryObservation(sequence=True)
    retained = LogicalMemoryBudget(100, operator="retained")
    retained.reserve(40)
    retained._observe(observation)
    observation.freeze_baseline()
    first, second = LogicalMemoryBudget(100, operator="first"), LogicalMemoryBudget(100, operator="second")
    observation.claim(object())
    first._observe(observation)
    first.reserve(60)
    first.release(60)
    observation.claim(object())
    second._observe(observation)
    second.reserve(70)
    assert not second.try_reserve(31)
    second.release(70)
    retained.release(40)
    observation.close()
    assert observation.result()["logical_memory_peak_bytes"] == 110
    assert observation.result()["baseline_retained_bytes"] == 40
    assert observation.current == 0


def test_sequence_foreign_owner_and_overlapping_scope_remain_unavailable(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with connect(tmp_path / "isolation", query_memory_budget_bytes=4096) as db:
        with db._queries._observe_logical_memory_sequence() as observation:
            db.execute("RETURN 1")
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(db._queries._memory_observation_current).result()
        assert observation.result()["unavailable_reason"] == "concurrent_execution_on_handle"
        with db._queries._observe_logical_memory_sequence() as observation:
            with pytest.raises(RuntimeError):
                with db._queries._observe_logical_memory_sequence():
                    pass
            db.execute("RETURN 1")
        assert observation.result()["unavailable_reason"] == "overlapping_observation"


def test_chunk_failure_keeps_only_confirmed_prefix_and_closes_failed_transaction(tmp_path):
    scenario = Scenario("partial", "write", nodes=1, edges_per_node=0, write_rows=3,
                        write_shape="chunks", chunk_rows=1, partial_progress=True)
    with connect(tmp_path / "prefix", query_memory_budget_bytes=65536) as db:
        build_fixture(db, scenario)
        with db._queries._observe_logical_memory_sequence() as observation:
            with pytest.raises(GrafxQueryError, match="primary key"):
                campaign_worker.execute_write_recipe(db, scenario,
                    [{"id": i, "payload": "x"} for i in (0, 1, 1, 2)])
        assert observation.result()["unavailable_reason"] is None
        assert db.transactions.open_transactions == 0
        assert db.execute("MATCH (n:W) RETURN n.id ORDER BY n.id").rows == ((0,), (1,))
    with connect(tmp_path / "prefix") as db:
        assert db.execute("MATCH (n:W) RETURN n.id ORDER BY n.id").rows == ((0,), (1,))


def test_refused_ack_is_not_counted_or_replayed():
    from types import SimpleNamespace
    calls = []
    class Transaction:
        active = True
        def execute(self, *_):
            return SimpleNamespace(statistics={})
        def commit(self):
            calls.append("commit")
            self.active = False
            return SimpleNamespace(csn=1, durable=False, wrote=True)
        def rollback(self):
            calls.append("rollback")
    db = SimpleNamespace(begin=lambda _: Transaction())
    with pytest.raises(campaign_schema.CampaignError, match="no durable ACK"):
        campaign_worker.execute_write_recipe(db, Scenario("refused", "write"), [{"id": 1, "payload": "x"}])
    assert calls == ["commit"]


def test_partial_ack_audit_preserves_primary_and_cleanup_failure():
    from types import SimpleNamespace
    calls, audit = [], {}
    primary = ValueError("primary execution failure")
    class Tx:
        active = True
        def execute(self, *_):
            if calls:
                raise primary
            return SimpleNamespace(statistics={})
        def commit(self):
            calls.append("commit")
            self.active = False
            return SimpleNamespace(csn=7, durable=True, wrote=True)
        def rollback(self):
            raise OSError("cleanup failure")
    scenario = Scenario("failure", "write", write_shape="chunks", chunk_rows=1, partial_progress=True)
    with pytest.raises(ValueError) as caught:
        campaign_worker.execute_write_recipe(SimpleNamespace(begin=lambda _: Tx()), scenario,
                                              [{"id": 1}, {"id": 2}], failure_evidence=audit)
    assert caught.value is primary
    assert len(audit["confirmed_prefix"]) == 1
    assert {key: audit["confirmed_prefix"][0][key] for key in ("csn", "durable", "wrote",
                                                                 "writer", "offset", "rows")} == {
        "csn": 7, "durable": True, "wrote": True, "writer": 0, "offset": 0, "rows": 1}
    assert audit["confirmed_prefix"][0]["ack_duration_ns"] > 0
    assert audit["failed_transaction"]["offset"] == 1
    assert audit["cleanup_error"]["type"] == "OSError"
    calls.clear()
    db = SimpleNamespace(begin=lambda _: Tx(), _queries=SimpleNamespace())
    with pytest.raises(campaign_worker.WriteCampaignFailure) as measured:
        campaign_worker._measure_writer(db, scenario, [{"id": 1}, {"id": 2}])
    import json
    artifact = json.loads(json.dumps(campaign_worker.error_record(measured.value)))
    assert [{key: report[key] for key in ("csn", "durable", "wrote", "writer", "offset", "rows")}
            for report in artifact["write_failure"]["confirmed_prefix"]] == [
                {key: report[key] for key in ("csn", "durable", "wrote", "writer", "offset", "rows")}
                for report in audit["confirmed_prefix"]]
    assert artifact["write_failure"]["confirmed_prefix"][0]["ack_duration_ns"] > 0
    assert artifact["write_failure"]["primary_error"]["type"] == "ValueError"
    assert artifact["write_failure"]["cleanup_error"]["type"] == "OSError"


def test_barrier_action_records_time_before_any_participant_returns():
    import threading
    import time
    from types import SimpleNamespace
    stamp = SimpleNamespace(value=0)
    barrier = threading.Barrier(2, action=campaign_worker._BarrierTimestamp(stamp))
    observed = []
    def worker():
        barrier.wait()
        observed.append(time.perf_counter_ns())
    thread = threading.Thread(target=worker)
    thread.start()
    barrier.wait()
    thread.join()
    assert 0 < stamp.value <= observed[0]


@pytest.mark.parametrize("shape,commits", [("unwind", 1), ("statements", 1), ("executemany", 1), ("chunks", 2), ("transactions", 3)])
def test_recipe_confirms_real_atomic_units_and_reopens(tmp_path, shape, commits):
    scenario = Scenario("recipe", "write", nodes=1, edges_per_node=0, write_rows=3,
                        write_shape=shape, chunk_rows=2 if shape == "chunks" else 0,
                        partial_progress=shape in ("chunks", "transactions"),
                        query_memory_budget_bytes=65536)
    with connect(tmp_path / "db", query_memory_budget_bytes=65536) as db:
        build_fixture(db, scenario)
        sample = campaign_worker.measure_statement(db, scenario, 0)
        assert db.transactions.open_transactions == 0
    with connect(tmp_path / "db") as db:
        assert db.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id").rows == oracle(scenario)
    write = sample["write"]
    assert write["commits"] == write["durable_acks"] == commits
    assert len(write["commit_reports"]) == commits
    assert all(r["durable"] is True and r["wrote"] is True for r in write["commit_reports"])
    assert len({r["csn"] for r in write["commit_reports"]}) == commits
    assert [r["writer"] for r in write["commit_reports"]] == [0] * commits
    expected_offsets = ([0, 2] if shape == "chunks" else [0, 1, 2]
                        if shape == "transactions" else [0])
    assert [r["offset"] for r in write["commit_reports"]] == expected_offsets
    assert sum(r["rows"] for r in write["commit_reports"]) == 3
    assert all(r["ack_duration_ns"] > 0 for r in write["commit_reports"])
    assert sum(r["ack_duration_ns"] for r in write["commit_reports"]) == sample["commit_ns"]
    assert write["business_rows"] == 3
    assert write["rows_per_second"] == 3e9 / sample["elapsed_ns"]
    assert write["durable_acks_per_second"] == commits * 1e9 / sample["elapsed_ns"]
    assert sample["memory"]["logical_memory_peak_bytes"] is not None
    campaign_schema.validate_write_sample(sample, scenario, qualification=True)
    broken = copy.deepcopy(sample)
    broken["write"]["durable_acks"] += 1
    with pytest.raises(campaign_schema.CampaignError):
        campaign_schema.validate_write_sample(broken, scenario, qualification=True)
    broken = copy.deepcopy(sample)
    broken["statistics"].pop("index_probe_keys")
    with pytest.raises(campaign_schema.CampaignError, match="physical work counters"):
        campaign_schema.validate_write_sample(broken, scenario, qualification=True)
    broken = copy.deepcopy(sample)
    broken["wal_delta_bytes"] = "unmeasured"
    with pytest.raises(campaign_schema.CampaignError, match="allocated-file cost"):
        campaign_schema.validate_write_sample(broken, scenario, qualification=True)
    for field, value in (("offset", 1), ("rows", 99), ("ack_duration_ns", 0)):
        broken = copy.deepcopy(sample)
        broken["write"]["commit_reports"][0][field] = value
        with pytest.raises(campaign_schema.CampaignError):
            campaign_schema.validate_write_sample(broken, scenario, qualification=True)
    sample["wal_delta_bytes"] = 17
    sample["disk_delta_bytes"] = 19
    observed = campaign_schema.write_tradeoff({"A": [sample], "B": [sample]}, scenario)
    assert observed["by_arm"]["A"]["runtime_statement_counters_per_sample"]["rows_created"]["median_count"] == 3
    assert observed["by_arm"]["A"]["physical_work_per_sample"]["index_probe_keys"]["median_count"] == 3
    assert "physical_node_versions_examined" in observed["by_arm"]["A"]["physical_work_unavailable"]
    sample["statistics"]["index_probe_keys"] = 5
    tradeoff = campaign_schema.write_tradeoff({"A": [sample], "B": [sample]}, scenario)
    arm = tradeoff["by_arm"]["A"]
    assert len(arm["ack_duration_by_atomic_unit"]) == commits
    assert arm["allocated_file_bytes_per_sample"]["wal_delta_bytes"]["median_bytes"] == 17
    assert arm["physical_work_per_sample"]["index_probe_keys"]["median_count"] == 5
    assert arm["amortized_cost_per_atomic_unit"]["disk_allocated_delta_bytes"]["median_bytes"] == 19 / commits
    assert "not individual ACK latencies" in tradeoff["amortized_cost_scope"]
    loads = campaign_schema.matched_write_loads(
        [scenario], [{"scenario_id": scenario.name, "pair": 0, "write_tradeoff": tradeoff}])
    assert loads[0]["write_rows_per_writer"] == 3
    assert loads[0]["shapes"][0]["atomic_unit"] == write["atomic_unit"]
    assert loads[0]["shapes"][0]["pairs"][0]["by_arm"]["A"]["durable_acks_per_sample"] == commits


def test_matrix_covers_dimensions_and_refuses_implicit_partial_progress():
    scenarios = campaign_fixtures.ingestion_matrix()
    assert {s.write_rows for s in scenarios} == {1, 32, 128, 512}
    assert {s.payload_bytes for s in scenarios} == {64, 4096}
    assert {s.writers for s in scenarios} == {1, 2, 4}
    assert {s.write_shape for s in scenarios} == {"unwind", "statements", "executemany", "chunks", "transactions"}
    with pytest.raises(ValueError, match="partial"):
        Scenario("bad", "write", write_shape="chunks", chunk_rows=2)


@pytest.mark.multiprocess
@pytest.mark.parametrize("writers", [2, 4])
def test_concurrent_proof_has_individual_memory_and_actual_acks(tmp_path, writers):
    scenario = Scenario("concurrent", "write", nodes=1, edges_per_node=0,
                        write_rows=1, writers=writers, query_memory_budget_bytes=65536)
    with connect(tmp_path / "db", query_memory_budget_bytes=65536) as db:
        build_fixture(db, scenario)
    sample = campaign_worker.measure_concurrent_write(tmp_path / "db", scenario, 0)
    assert sample["memory"]["logical_memory_peak_bytes"] is None
    assert sample["write"]["commits"] == sample["write"]["durable_acks"] == writers
    assert len({w["process_id"] for w in sample["writers"]}) == writers
    assert all(sample["barrier_release_ns"] <= w["started_ns"] for w in sample["writers"])
    assert all(all(report["writer"] == writer["writer"] and report["offset"] == 0
                   and report["rows"] == 1 for report in writer["commit_reports"])
               for writer in sample["writers"])
    campaign_schema.validate_write_sample(sample, scenario, qualification=True)
    for change in ({"rss_peak_bytes": 0}, {"rss_peak_bytes": -1}, {"rss_scope": "coordinator"}):
        invalid = copy.deepcopy(sample)
        invalid["writers"][0].update(change)
        with pytest.raises(campaign_schema.CampaignError, match="RSS"):
            campaign_schema.validate_write_sample(invalid, scenario, qualification=True)
    broken = copy.deepcopy(sample)
    broken["writers"][0]["memory"]["logical_memory_peak_bytes"] = None
    with pytest.raises(campaign_schema.CampaignError, match="memory"):
        campaign_schema.validate_write_sample(broken, scenario, qualification=True)
