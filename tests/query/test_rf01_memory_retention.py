"""Candidate-only bounded logical-memory proof; not a timing qualification.

Private, sequential handles and a fixed quota delimit this proof. Independent
row oracles and filesystem cleanup complement the observer's own accounting;
this makes no claim about Python allocation or process RSS retention.
"""

import pytest

from okto_grafx import CancellationToken, connect
from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory
from okto_grafx.errors import GrafxQueryBudgetExceeded, GrafxQueryCancelled


@pytest.mark.parametrize("ending", ["exhaust", "close", "cancel", "budget_error"])
def test_repeated_statements_leave_no_logical_retention_or_spill(tmp_path, monkeypatch, ending):
    quota = 1024
    spill_root = tmp_path / "spill"
    spill_root.mkdir()
    monkeypatch.setattr(
        "okto_grafx.api.assembly.LocalQuerySpillFactory",
        lambda: LocalQuerySpillFactory(str(spill_root)),
    )
    values = list(range(48, 0, -1))
    expected = tuple((value,) for value in sorted(values))
    peaks = []
    baselines = []
    completed = []
    with connect(tmp_path / "db", query_memory_budget_bytes=quota) as db:
        for _ in range(8):
            token = CancellationToken()
            with db._queries._observe_logical_memory() as observation:
                if ending == "budget_error":
                    with pytest.raises(GrafxQueryBudgetExceeded) as raised:
                        db.execute("UNWIND $xs AS x RETURN collect(x)",
                                   {"xs": ["x" * 80 + str(i) for i in range(20)]})
                    assert raised.value.details["field"] == "query_memory_budget_bytes"
                else:
                    with db.query("UNWIND $xs AS x RETURN x ORDER BY x", {"xs": values}).cursor(
                        batch_size=1, cancellation=token,
                    ) as cursor:
                        assert next(cursor) == expected[0]
                        # This is a real spill, not a small all-in-memory sort.
                        assert tuple(spill_root.iterdir())
                        if ending == "exhaust":
                            assert (expected[0], *tuple(cursor)) == expected
                        elif ending == "cancel":
                            token.cancel()
                            with pytest.raises(GrafxQueryCancelled):
                                next(cursor)
                assert observation.current == 0
                assert tuple(spill_root.iterdir()) == ()
            sample = observation.result()
            assert sample["unavailable_reason"] is None
            assert sample["baseline_retained_bytes"] == 0
            assert 0 < sample["logical_memory_peak_bytes"] <= quota
            peaks.append(sample["logical_memory_peak_bytes"])
            completed.append((observation, sample))

            # A new, successful statement observes the handle after settlement.
            # It must not inherit the prior query's retained quota or observer.
            with db._queries._observe_logical_memory() as probe:
                assert db.execute("RETURN 42").rows == ((42,),)
            state = probe.result()
            assert state["unavailable_reason"] is None
            baselines.append(state["baseline_retained_bytes"])
            assert state["logical_memory_peak_bytes"] == 0
            assert tuple(spill_root.iterdir()) == ()
            assert all(old.result() == frozen for old, frozen in completed)

    assert baselines == [0] * 8
    assert len(set(peaks)) == 1  # Identical work cannot accumulate across intervals.
    assert tuple(spill_root.iterdir()) == ()
