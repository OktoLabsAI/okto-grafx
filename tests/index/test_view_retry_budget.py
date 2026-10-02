"""An exact-index read rides out foreign page-0 publications up to a named budget (issue #12).

The certificate comparison is what makes a result trustworthy, so these tests force it to fail a
chosen number of times and check the two halves of the contract: below the budget the read
succeeds with the right row, at or above it the read raises the retryable GrafxIndexError, and
in no case is a result returned from a view whose before/after certificates differed.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxIndexError, GrafxUnsupportedOperation
from okto_grafx.engine.index_manager import INDEX_VIEW_RETRY_BUDGET, HashIndex

from .conftest import SnapshotDouble, build_database, cold_view
from .test_exact_view_fence import BORN, _insert_exact


def _lookup_with_changes(monkeypatch: pytest.MonkeyPatch, changes: int):
    """Look up the seeded row while the post-read certificate check fails ``changes`` times."""
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)
    database.pool.flush(database.heap.file)
    reader = cold_view(database)
    original = HashIndex.finish_exact_read
    calls: list[bool] = []

    def flaky(self: HashIndex, before: object, required_lsn: int) -> bool:
        if len(calls) < changes:
            calls.append(False)
            return False
        stable = original(self, before, required_lsn)
        calls.append(stable)
        return stable

    monkeypatch.setattr(HashIndex, "finish_exact_read", flaky)
    key = database.key(1, "Ada")
    outcome = reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN))
    return outcome, calls


@pytest.mark.parametrize("changes", [1, 2, 3, INDEX_VIEW_RETRY_BUDGET])
def test_a_read_below_the_budget_succeeds_with_the_right_row(
    monkeypatch: pytest.MonkeyPatch, changes: int
) -> None:
    outcome, calls = _lookup_with_changes(monkeypatch, changes)
    assert outcome, "the seeded row must be found"
    assert calls == [False] * changes + [True]


@pytest.mark.parametrize("changes", [INDEX_VIEW_RETRY_BUDGET + 1, INDEX_VIEW_RETRY_BUDGET + 4])
def test_a_read_at_or_above_the_budget_raises_the_retryable_error(
    monkeypatch: pytest.MonkeyPatch, changes: int
) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        _lookup_with_changes(monkeypatch, changes)
    assert refused.value.retryable is True
    assert refused.value.details["field"] == "index_view_changed"
    assert refused.value.details["attempts"] == INDEX_VIEW_RETRY_BUDGET + 1


def test_a_result_is_never_returned_from_a_view_whose_certificates_differ() -> None:
    writer = build_database()
    reader = cold_view(writer)
    produced: list[int] = []

    def read(certificate: object) -> str:
        produced.append(len(produced))
        # Another participant publishes page 0 between the two certificates, every time.
        writer.exact.advance_built_through(100 + len(produced))
        return "stale-view-result"

    with pytest.raises(GrafxIndexError) as refused:
        reader.exact._stable_view(0, read)
    assert refused.value.retryable is True
    assert len(produced) == INDEX_VIEW_RETRY_BUDGET + 1
    # Stable again: the very next read returns, and only then is a result handed out.
    assert reader.exact._stable_view(0, lambda certificate: "ok") == "ok"


def test_a_refusal_raised_inside_a_changed_view_is_not_a_verdict() -> None:
    writer = build_database()
    reader = cold_view(writer)
    attempts: list[int] = []

    def read(certificate: object) -> str:
        attempts.append(1)
        if len(attempts) <= 3:
            writer.exact.advance_built_through(100 + len(attempts))
            raise GrafxUnsupportedOperation("mixed view", field="slot")
        return "settled"

    assert reader.exact._stable_view(0, read) == "settled"
    assert len(attempts) == 4
