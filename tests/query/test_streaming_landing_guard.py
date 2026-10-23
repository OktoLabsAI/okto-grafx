"""RF07: existing streaming guards protect demand, errors and published prefixes.

These are protection tests, not defect fixes: they pass on the existing scalar
implementation. The explicit unsafe-admission counterexample demonstrates why
removing the guard is observable. Public iterator batching is caller demand;
single-row early-close cases deliberately use batch_size=1.
"""
import pytest

from okto_grafx import CancellationToken
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexStore, INDEX_READ_RETRY_BUDGET
from okto_grafx.errors import (
    GrafxCorruptionDetected, GrafxIndexError, GrafxQueryCancelled,
    GrafxQueryDeadlineExceeded,
)
from . import test_batched_landing_without_vectors as fixture
from .test_batched_landing_without_vectors import PREFIX, _identity_index, _port

graph = fixture.graph


def _destination_fault(monkeypatch, graph):
    """Observe both real heap doors without disabling the canonical batch fence."""
    table_id = _identity_index(graph).definition.table_id
    seen = []
    for name in ("read", "read_landing"):
        original = getattr(HeapStore, name)

        def read(heap, ref, _original=original):
            version = _original(heap, ref)
            if version.table_id == table_id:
                seen.append(version.values[0])
                if version.values[0] != "b0":
                    raise GrafxCorruptionDetected("Undemanded destination fault", field="rf07.destination")
            return version

        monkeypatch.setattr(HeapStore, name, read)
    monkeypatch.setattr(qe, "_VECTOR_FREE_CANONICAL_READ", HeapStore.read)
    return seen


def _clean(graph, cursor=None):
    if cursor is not None:
        assert cursor.closed and not cursor._buffer
    assert graph.transactions.open_transactions == 0
    assert graph._queries._owner_budget._used_bytes == 0


@pytest.mark.parametrize("suffix,expected", [
    ("RETURN b.id LIMIT 0", ()),
    ("RETURN b.id LIMIT 1", (("b0",),)),
    ("AND r.rank=1 RETURN b.id LIMIT 1", (("b0",),)),
    ("RETURN DISTINCT b.id LIMIT 1", (("b0",),)),
])
def test_limit_filter_distinct_never_validate_later_destination(graph, monkeypatch, suffix, expected):
    seen = _destination_fault(monkeypatch, graph)
    counts, _ = _port(monkeypatch)
    assert graph.execute(PREFIX + suffix).rows == expected
    assert set(seen) == ({"b0"} if expected else set())
    assert counts["many"] == 0
    _clean(graph)


def test_close_after_first_pull_does_not_validate_next_destination(graph, monkeypatch):
    seen = _destination_fault(monkeypatch, graph)
    cursor = graph.query(PREFIX + "RETURN b.id").cursor(batch_size=1)
    assert next(cursor) == ("b0",)
    cursor.close()
    cursor.close()
    assert seen == ["b0"]
    assert cursor.fetchone() is None
    _clean(graph, cursor)


def test_duplicate_identity_beyond_limit_is_only_rejected_when_demanded(graph, monkeypatch):
    index = _identity_index(graph)
    original = IndexStore._candidates_unchecked
    keys = []

    def duplicate_later(store, key):
        entries = original(store, key)
        if store.file == index.file:
            keys.append(key)
            if key != keys[0]:
                return entries + entries[:1]
        return entries

    monkeypatch.setattr(IndexStore, "_candidates_unchecked", duplicate_later)
    assert graph.execute(PREFIX + "RETURN b.id LIMIT 1").rows == (("b0",),)
    assert len(keys) == 1
    _clean(graph)
    with pytest.raises(GrafxCorruptionDetected):
        graph.execute(PREFIX + "RETURN b.id")
    assert len(set(keys)) == 2
    _clean(graph)


def test_unsafe_prefetch_exposes_fault_before_required_first_row(graph, monkeypatch):
    """Counterexample: changing only admission moves a later error before LIMIT 1."""
    seen = _destination_fault(monkeypatch, graph)
    counts, _ = _port(monkeypatch)
    monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_: True)
    with pytest.raises(GrafxCorruptionDetected) as failure:
        graph.execute(PREFIX + "RETURN b.id LIMIT 1")
    assert failure.value.details["field"] == "rf07.destination"
    assert counts["many"] == 1 and "b1" in seen
    _clean(graph)


@pytest.mark.parametrize("control", ["cancel", "deadline"])
def test_control_between_pulls_wins_over_undemanded_destination_fault(graph, monkeypatch, control):
    class Clock:
        now = 10.0

        def monotonic(self):
            return self.now

    clock = Clock()
    graph._clock = clock
    token = CancellationToken()
    seen = _destination_fault(monkeypatch, graph)
    cursor = graph.query(PREFIX + "RETURN b.id").cursor(
        batch_size=1, cancellation=token, timeout_seconds=1,
    )
    assert next(cursor) == ("b0",)
    if control == "cancel":
        token.cancel()
    else:
        clock.now += 2
    with pytest.raises(GrafxQueryCancelled if control == "cancel" else GrafxQueryDeadlineExceeded):
        cursor.fetchone()
    assert seen == ["b0"]
    _clean(graph, cursor)


def test_generation_failure_after_prefix_never_replays_it(graph, monkeypatch):
    index = _identity_index(graph)
    cursor = graph.query(PREFIX + "RETURN DISTINCT b.id").cursor(batch_size=1)
    assert cursor.fetchone() == ("b0",)
    original = HashIndex.finish_exact_read
    attempts = []

    def changed(store, before, lsn):
        answer = original(store, before, lsn)
        if store.file == index.file:
            attempts.append(1)
            return False
        return answer

    monkeypatch.setattr(HashIndex, "finish_exact_read", changed)
    with pytest.raises(GrafxIndexError) as failure:
        cursor.fetchone()
    assert failure.value.details["field"] == "index_view_changed"
    assert len(attempts) == INDEX_READ_RETRY_BUDGET + 1
    assert cursor.fetchone() is None
    _clean(graph, cursor)


def test_blocking_consumer_retains_existing_batched_delivery(graph, monkeypatch):
    counts, keys = _port(monkeypatch)
    assert graph.execute(PREFIX + "RETURN b.id, count(r) ORDER BY b.id").rows == (
        ("b0", 2), ("b1", 2), ("b2", 2),
    )
    assert counts["many"] == 1 and keys == [3]
    _clean(graph)
