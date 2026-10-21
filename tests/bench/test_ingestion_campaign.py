"""RF09 deterministic recipe oracles; these tests do not qualify timing."""
import copy

import pytest

from bench.harness.campaign_fixtures import Scenario, build_fixture, oracle
from bench.harness import campaign_fixtures, campaign_schema, campaign_worker
from okto_grafx import connect
from okto_grafx.errors import GrafxQueryError


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
    assert audit["confirmed_prefix"] == [{"csn": 7, "durable": True, "wrote": True}]
    assert audit["failed_transaction"]["offset"] == 1
    assert audit["cleanup_error"]["type"] == "OSError"
    calls.clear()
    db = SimpleNamespace(begin=lambda _: Tx(), _queries=SimpleNamespace())
    with pytest.raises(campaign_worker.WriteCampaignFailure) as measured:
        campaign_worker._measure_writer(db, scenario, [{"id": 1}, {"id": 2}])
    import json
    artifact = json.loads(json.dumps(campaign_worker.error_record(measured.value)))
    assert artifact["write_failure"]["confirmed_prefix"] == audit["confirmed_prefix"]
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
    assert write["business_rows"] == 3
    assert write["rows_per_second"] == 3e9 / sample["elapsed_ns"]
    assert write["durable_acks_per_second"] == commits * 1e9 / sample["elapsed_ns"]
    assert sample["memory"]["logical_memory_peak_bytes"] is not None
    campaign_schema.validate_write_sample(sample, scenario, qualification=True)
    broken = copy.deepcopy(sample)
    broken["write"]["durable_acks"] += 1
    with pytest.raises(campaign_schema.CampaignError):
        campaign_schema.validate_write_sample(broken, scenario, qualification=True)


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
