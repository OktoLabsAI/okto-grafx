"""Opt-in simultaneous logical-budget observations, never RSS estimates."""

import pytest
from okto_grafx import connect

from okto_grafx.domain.query.memory import LogicalMemoryBudget, _LogicalMemoryObservation


def test_simultaneous_peak_is_not_sum_of_independent_peaks():
    observation = _LogicalMemoryObservation()
    first, second = LogicalMemoryBudget(100, operator="a"), LogicalMemoryBudget(100, operator="b")
    first._observe(observation)
    second._observe(observation)
    first.reserve(80)
    first.release(80)
    second.reserve(70)
    first.reserve(20)
    assert observation.result()["logical_memory_peak_bytes"] == 90


def test_baseline_refusal_release_and_duplicate_attachment():
    budget = LogicalMemoryBudget(100, operator="a")
    budget.reserve(40)
    observation = _LogicalMemoryObservation()
    budget._observe(observation)
    observation.freeze_baseline()
    budget._observe(observation)
    assert not budget.try_reserve(61)
    with pytest.raises(RuntimeError):
        budget.release(41)
    assert observation.result()["baseline_retained_bytes"] == 40
    assert observation.result()["logical_memory_peak_bytes"] == 40
    budget.release(40)
    assert observation.current == 0


def test_closed_observation_does_not_absorb_later_work():
    observation = _LogicalMemoryObservation()
    observation.claim(object())
    budget = LogicalMemoryBudget(100, operator="a")
    budget._observe(observation)
    budget.reserve(20)
    observation.close()
    budget.reserve(30)
    assert observation.result()["logical_memory_peak_bytes"] == 20


def test_real_query_and_cursor_pulls_have_same_peak(tmp_path):
    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        query = "UNWIND $xs AS x RETURN x ORDER BY x"
        parameters = {"xs": list(range(20, 0, -1))}
        with db._queries._observe_logical_memory() as materialized:
            expected = db.execute(query, parameters).rows
        with db._queries._observe_logical_memory() as streamed:
            with db.query(query, parameters).cursor(batch_size=1) as cursor:
                actual = tuple(cursor)
        assert actual == expected
        assert materialized.result()["unavailable_reason"] is None
        assert streamed.result()["logical_memory_peak_bytes"] == materialized.result()["logical_memory_peak_bytes"]
        assert streamed.result()["logical_memory_peak_bytes"] > 0


def test_handle_baseline_and_refused_reservation_are_observed(tmp_path):
    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        engine = db._queries
        engine._owner_budget.reserve(bytes_=40, entries=1)
        try:
            with engine._observe_logical_memory() as observation:
                db.execute("RETURN 1")
                engine._endpoint_budget.reserve(bytes_=60, entries=1)
                engine._endpoint_budget.release(bytes_=60, entries=1)
                assert observation.current == 40
            assert observation.result()["baseline_retained_bytes"] == 40
            assert observation.result()["logical_memory_peak_bytes"] == 100
        finally:
            engine._owner_budget.release(bytes_=40, entries=1)


def test_foreign_thread_budget_activity_invalidates_measurement(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        def foreign():
            db._queries._owner_budget.reserve(bytes_=10, entries=1)
            db._queries._owner_budget.release(bytes_=10, entries=1)

        with db._queries._observe_logical_memory() as observation:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(foreign).result()
        assert observation.result()["logical_memory_peak_bytes"] is None
        assert observation.result()["unavailable_reason"] == "concurrent_execution_on_handle"


def test_two_statements_and_overlapping_observations_fail_closed(tmp_path):
    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        with db._queries._observe_logical_memory() as observation:
            db.execute("RETURN 1")
            db.execute("RETURN 2")
        assert observation.result()["unavailable_reason"] == "multiple_statements_in_interval"
        with db._queries._observe_logical_memory() as overlap:
            with pytest.raises(RuntimeError):
                with db._queries._observe_logical_memory():
                    pass
        assert overlap.result()["unavailable_reason"] == "overlapping_observation"


def test_unbudgeted_coverage_is_unavailable(tmp_path):
    with connect(tmp_path / "db") as db:
        with db._queries._observe_logical_memory() as observation:
            db.execute("RETURN 1")
        assert observation.result()["logical_memory_peak_bytes"] is None
        assert observation.result()["unavailable_reason"] == "query_memory_budget_disabled"


def test_empty_interval_cannot_qualify_baseline_only(tmp_path):
    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        with db._queries._observe_logical_memory() as observation:
            pass
        assert observation.result()["logical_memory_peak_bytes"] is None
        assert observation.result()["unavailable_reason"] == "no_statement_observed"


@pytest.mark.parametrize("name", ["_owner_budget", "_endpoint_budget"])
def test_negative_handle_retention_invalidates_observation_without_changing_budget(tmp_path, name):
    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        budget = getattr(db._queries, name)
        with db._queries._observe_logical_memory() as observation:
            db.execute("RETURN 1")
            budget.release(bytes_=1, entries=0)
            assert budget._used_bytes == -1  # Existing budget semantics are preserved.
            budget.reserve(bytes_=1, entries=0)
        assert observation.result()["logical_memory_peak_bytes"] is None
        assert observation.result()["unavailable_reason"] == "negative_retained_bytes"


def test_cursor_boundary_failures_and_error_cleanup(tmp_path):
    from okto_grafx.errors import GrafxError

    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        with db.query("UNWIND [1, 2] AS x RETURN x").cursor(batch_size=1) as cursor:
            next(cursor)  # Establish execution before the observation starts.
            with db._queries._observe_logical_memory() as late:
                next(cursor)
            assert late.result()["unavailable_reason"] == "cursor_started_before_observation"
        cursor = None
        try:
            with db._queries._observe_logical_memory() as early:
                cursor = db.query("UNWIND [1, 2] AS x RETURN x").cursor(batch_size=1)
                cursor.__enter__()
                next(cursor)
            assert early.result()["unavailable_reason"] == "cursor_outlives_observation"
        finally:
            if cursor is not None:
                cursor.__exit__(None, None, None)
        with db._queries._observe_logical_memory() as failed:
            with pytest.raises(GrafxError):
                with db.query("RETURN 1 / $zero", {"zero": 0}).cursor(batch_size=1) as cursor:
                    next(cursor)
        before = failed.result()
        db.execute("RETURN 3")
        assert failed.result() == before
        with db._queries._observe_logical_memory() as fresh:
            db.execute("RETURN 4")
        assert fresh.result()["unavailable_reason"] is None


def test_concurrent_handles_have_independent_intervals(tmp_path):
    with connect(tmp_path / "a", query_memory_budget_bytes=4096) as a:
        with connect(tmp_path / "b", query_memory_budget_bytes=4096) as b:
            with a._queries._observe_logical_memory() as first:
                with b._queries._observe_logical_memory() as second:
                    a.execute("UNWIND [3, 1, 2] AS x RETURN x ORDER BY x")
                    b.execute("RETURN 1")
            assert first.result()["unavailable_reason"] is None
            assert second.result()["unavailable_reason"] is None
            assert first.result()["logical_memory_peak_bytes"] > second.result()["logical_memory_peak_bytes"]


def test_ending_observational_lifetime_does_not_release_quota():
    observation = _LogicalMemoryObservation()
    first = LogicalMemoryBudget(100, operator="merge_matches")
    second = LogicalMemoryBudget(100, operator="merge_matches")
    first._observe(observation)
    first.reserve(80)
    first._end_observation()
    assert first.retained == first.peak == 80
    assert not first.try_reserve(21)
    second._observe(observation)
    second.reserve(70)
    assert observation.result()["logical_memory_peak_bytes"] == 80


@pytest.mark.parametrize("query", [
    "UNWIND [1, 2] AS id MERGE (n:N {id:id}) RETURN n.id",
    "MATCH (n:N) WHERE n.id = 2 DELETE n",
    "MATCH (n:N) RETURN [(n)-->(m) | m.id] AS xs",
])
def test_cumulative_operator_observation_has_explicit_lifetime(tmp_path, query):
    with connect(tmp_path / "db", query_memory_budget_bytes=1048576) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("UNWIND [1, 2] AS id CREATE (:N {id:id})")
        with db.begin("write") as tx:
            with db._queries._observe_logical_memory() as observation:
                tx.execute(query)
                assert observation.current == (db._queries._owner_budget._used_bytes
                                               + db._queries._endpoint_budget._used_bytes)
        assert observation.result()["unavailable_reason"] is None
        assert observation.result()["logical_memory_peak_bytes"] > 0


def test_sequential_merge_invocations_do_not_sum_their_peaks(tmp_path):
    with connect(tmp_path / "db", query_memory_budget_bytes=1048576) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})")
        peaks = []
        with db.begin("write") as tx:
            for ids in ([1], [1, 1, 1]):
                with db._queries._observe_logical_memory() as observation:
                    result = tx.execute("UNWIND $ids AS id MERGE (n:N {id:id}) RETURN n.id", {"ids": ids})
                assert len(result.rows) == len(ids)
                peaks.append(observation.result()["logical_memory_peak_bytes"])
        assert peaks[0] == peaks[1] > 0


def test_cancelled_cursor_revokes_interval_and_preserves_next_statement(tmp_path):
    from okto_grafx import CancellationToken
    from okto_grafx.errors import GrafxQueryCancelled

    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        token = CancellationToken()
        with db._queries._observe_logical_memory() as observation:
            with db.query("UNWIND [1, 2] AS x RETURN x").cursor(batch_size=1, cancellation=token) as cursor:
                assert next(cursor) == (1,)
                token.cancel()
                with pytest.raises(GrafxQueryCancelled):
                    next(cursor)
        assert observation.result()["unavailable_reason"] is None
        before = observation.result()
        assert db.execute("RETURN 42").rows == ((42,),)
        assert observation.result() == before


def test_blocking_budget_refusal_matches_without_observer_and_cleans_up(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory
    from okto_grafx.errors import GrafxQueryBudgetExceeded

    spill_root = tmp_path / "spill"
    spill_root.mkdir()
    monkeypatch.setattr("okto_grafx.api.assembly.LocalQuerySpillFactory",
                        lambda: LocalQuerySpillFactory(str(spill_root)))
    failures = []
    with connect(tmp_path / "db", query_memory_budget_bytes=1024) as db:
        for observed in (False, True):
            scope = db._queries._observe_logical_memory() if observed else nullcontext(None)
            with scope as observation:
                with pytest.raises(GrafxQueryBudgetExceeded) as raised:
                    db.execute("UNWIND $items AS item RETURN collect(item)",
                               {"items": ["x" * 80 + str(i) for i in range(20)]})
                failures.append((raised.value.code, raised.value.details, str(raised.value)))
                assert raised.value.details["field"] == "query_memory_budget_bytes"
                assert tuple(spill_root.iterdir()) == ()
                if observation is not None:
                    assert observation.current == 0
            assert db._queries._memory_observation is None
            assert db.execute("RETURN 42").rows == ((42,),)
            if observation is not None:
                assert observation.result()["unavailable_reason"] is None
                assert observation.result()["logical_memory_peak_bytes"] > 0
        assert failures[0] == failures[1]
