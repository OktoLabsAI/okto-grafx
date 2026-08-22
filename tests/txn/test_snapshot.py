"""The visibility predicate (CONTRACT.md section 8.5, SPEC-M1 BR-9).

Three terms, and each one is probed on its own: a term that cannot be shown to matter is a term
nobody can prove is there (A34, A62).
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.txn import Snapshot


def test_a_version_committed_at_or_below_the_snapshot_is_visible() -> None:
    snapshot = Snapshot(read_lsn=10)
    assert snapshot.visible(1, 0) is True
    assert snapshot.visible(10, 0) is True


def test_a_version_committed_above_the_snapshot_is_not_visible() -> None:
    """The second term. Without ``xmin <= read_lsn`` a snapshot would see later commits."""
    assert Snapshot(read_lsn=10).visible(11, 0) is False


def test_a_version_with_no_commit_number_is_never_visible() -> None:
    """The first term, and it is not implied by the second: zero is below every read LSN."""
    assert Snapshot(read_lsn=10).visible(0, 0) is False
    assert Snapshot(read_lsn=0).visible(0, 0) is False


def test_a_version_ended_at_or_below_the_snapshot_is_hidden() -> None:
    """The third term. ``xmax > read_lsn`` is what keeps a deleted row out of a later view."""
    assert Snapshot(read_lsn=10).visible(2, 10) is False
    assert Snapshot(read_lsn=10).visible(2, 5) is False


def test_a_version_ended_above_the_snapshot_is_still_visible() -> None:
    """The other side of the third term: an update after the snapshot must not hide the old row."""
    assert Snapshot(read_lsn=10).visible(2, 11) is True


def test_a_live_version_is_marked_by_a_zero_end() -> None:
    """Zero means live, so the end term must special-case it rather than compare it."""
    assert Snapshot(read_lsn=10).visible(2, 0) is True


def test_the_empty_database_snapshot_sees_nothing() -> None:
    snapshot = Snapshot(read_lsn=0)
    assert snapshot.visible(1, 0) is False
    assert snapshot.visible(0, 0) is False


@pytest.mark.parametrize("value", [-1, "5", 5.0, None, True])
def test_an_unusable_read_lsn_is_refused_as_configuration(value: object) -> None:
    """A caller mistake is never corruption_detected (amendment A11-revised)."""
    with pytest.raises(GrafxConfigurationError) as raised:
        Snapshot(read_lsn=value)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "read_lsn"
    assert raised.value.retryable is False


def test_a_snapshot_cannot_be_moved_after_it_is_taken() -> None:
    """BR-9: the view is fixed at the moment the transaction opened."""
    snapshot = Snapshot(read_lsn=4)
    with pytest.raises(Exception) as raised:
        snapshot.read_lsn = 9  # type: ignore[misc]
    assert isinstance(raised.value, (AttributeError, TypeError))


def test_the_predicate_is_a_pure_function_of_its_three_numbers() -> None:
    """Determinism: the same three numbers give the same answer every time."""
    snapshot = Snapshot(read_lsn=7)
    answers = {snapshot.visible(3, 0) for _ in range(50)}
    assert answers == {True}
