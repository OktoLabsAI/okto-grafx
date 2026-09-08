"""Index certification validates old payloads without retaining all their decoded values."""

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, HeapVersion
from okto_grafx.domain.verify.findings import FindingKind
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.verifier import Verifier, _CanonicalIndexVerification

from .conftest import HEAP_FILE, Stack
from .test_verifier import _builtin_indexes, _count_heap_reads, _rewrite_page
from .test_verifier_version_suffixes import _history


@pytest.mark.parametrize("size", [8, 64])
def test_history_values_are_released_but_every_reference_remains_proven(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, size: int,
) -> None:
    table, refs = _history(stack, size)
    rows = tuple(stack.heap.scan_all(table))
    indexes = _builtin_indexes(stack, table, rows)
    verifier = Verifier(stack.pool, stack.metrics, heap=stack.heap,
                        catalog=stack.catalog, indexes=indexes)
    observed = []
    original = Verifier._canonical_versions

    def inspect(self, identity, selected, shared):
        result = original(self, identity, selected, shared)
        observed.append((len(result), len(shared.scanned_refs[identity])))
        return result

    monkeypatch.setattr(Verifier, "_canonical_versions", inspect)
    reads = _count_heap_reads(stack, monkeypatch)
    optimized = verifier.verify("all")
    assert optimized.findings == ()
    assert optimized.records_checked == size
    assert optimized.index_entries_checked == 2 * size
    assert observed and set(observed) == {(1, size)}
    assert reads[0] == 0
    assert stack.heap.version_chain(refs[-1]) == tuple(reversed(refs))
    with monkeypatch.context() as patch:
        patch.setattr(Verifier, "_canonical_index_verification", lambda self: None)
        assert verifier.verify("all") == optimized
    # Nothing is cached across public calls.
    assert verifier.verify("all") == optimized


def test_corrupt_ended_tuple_still_fails_resolution_and_coverage(stack, monkeypatch):
    table, refs = _history(stack, 8)
    indexes = _builtin_indexes(stack, table, tuple(stack.heap.scan_all(table)))
    historical = refs[0]

    def corrupt(page):
        payload = page.read_slot(historical.slot)
        page.update_slot(historical.slot, payload[:RECORD_HEADER_SIZE]
                         + b"\xff" * (len(payload) - RECORD_HEADER_SIZE))

    _rewrite_page(stack, HEAP_FILE, historical.page, corrupt)
    verifier = Verifier(stack.pool, stack.metrics, heap=stack.heap,
                        catalog=stack.catalog, indexes=indexes)
    optimized = verifier.verify("all")
    assert optimized.findings_of(FindingKind.INDEX_UNREADABLE)
    assert optimized.findings_of(FindingKind.INDEX_ENTRY_UNRESOLVED)
    with monkeypatch.context() as patch:
        patch.setattr(Verifier, "_canonical_index_verification", lambda self: None)
        assert verifier.verify("all") == optimized


def test_late_scan_failure_never_publishes_partial_reference_proofs(stack, monkeypatch):
    table, _refs = _history(stack, 8)
    verifier = stack.verifier()
    shared = _CanonicalIndexVerification()
    original = HeapStore.scan_all
    calls = []

    def fail_after_one(self, selected):
        calls.append(selected.table_id)
        yield next(original(self, selected))
        raise GrafxCorruptionDetected("late invalid historical payload", field="tuple")

    monkeypatch.setattr(HeapStore, "scan_all", fail_after_one)
    identity = (table.table_id, table.name)
    for _ in range(2):
        with pytest.raises(GrafxCorruptionDetected, match="late invalid"):
            verifier._canonical_versions(identity, table, shared)
    assert calls == [table.table_id]
    assert shared.versions == {}
    assert shared.scanned_refs == {}
    assert shared.resolved_refs == {}


def test_table_state_released_at_last_index(stack):
    table, _refs = _history(stack, 8)
    indexes = _builtin_indexes(stack, table, tuple(stack.heap.scan_all(table)))
    shared = _CanonicalIndexVerification()
    for index in indexes:
        shared.register(index)
    identity = (table.table_id, table.name)
    stack.verifier()._canonical_versions(identity, table, shared)
    shared.release(indexes[0])
    assert len(shared.scanned_refs[identity]) == 8
    shared.release(indexes[1])
    assert shared.scanned_refs == {}
    assert shared.versions == {}


def test_foreign_version_live_property_is_not_evaluated_during_collection(stack, monkeypatch):
    table, _refs = _history(stack, 2)
    ref, source = next(stack.heap.scan_all(table))
    observed = []

    class ForeignVersion(HeapVersion):
        @property
        def live(self):
            observed.append("live")
            return False

    foreign = ForeignVersion(source.record_id, source.xmin, source.xmax, source.values,
                             source.prev, source.schema_version, source.deleted, source.table_id)
    monkeypatch.setattr(HeapStore, "scan_all", lambda self, selected: iter(((ref, foreign),)))
    shared = _CanonicalIndexVerification()
    assert stack.verifier()._canonical_versions((table.table_id, table.name), table, shared) == (
        (ref, foreign),
    )
    assert observed == []
