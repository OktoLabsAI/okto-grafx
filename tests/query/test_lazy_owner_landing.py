"""D-03: lazy owner landings preserve corruption and bounded-lifecycle contracts."""

from __future__ import annotations

import gc
import queue
import threading
import weakref
from dataclasses import replace

import pytest

import okto_grafx.engine.query_engine as query_engine_module
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.model.schema import encode_tuple
from okto_grafx.engine.query_engine import (
    _OWNER_LANDING_FINGERPRINT_ENTRY_BYTES,
    _OWNER_LANDING_MEMO_BYTES,
    _OWNER_LANDING_OVERLAY_ENTRY_BYTES,
    _OWNER_LANDING_PAYLOAD_MULTIPLIER,
    _OWNER_LANDING_TABLE_BYTES,
    _OWNER_LANDING_VIEW_BASE_BYTES,
    _OwnerLandingBudget,
    _OwnerLandingCapacity,
    _owner_landing_result_bytes,
    _owner_landing_txn_memo,
    _owner_landing_view,
)
from tests.query.stack import QueryStack, build_query_stack


class _LandingContext:
    """The minimal weak-referenceable statement authority consumed by a landing view."""

    __slots__ = ("_schema", "snapshot", "staged_rows", "txn", "__weakref__")

    def __init__(
        self,
        stack: QueryStack,
        *,
        txn: object | None = None,
        txn_id: int = 71,
    ) -> None:
        transaction = stack.transaction(1_000) if txn is None else txn
        transaction.txn_id = txn_id
        self.txn = transaction
        self.snapshot = transaction.snapshot
        self._schema = stack.catalog_store.catalog
        self.staged_rows: list[object] = []

    def schema(self) -> object:
        """Return the exact catalog picture shared by sibling statement contexts."""
        return self._schema


def _context(stack: QueryStack, *, txn_id: int = 71) -> _LandingContext:
    """Return the narrow owner context consumed by the landing view."""
    return _LandingContext(stack, txn_id=txn_id)


def _insert_people(stack: QueryStack, count: int) -> list[RecordRef]:
    table = stack.table("Person")
    return [
        stack.heap.insert(
            table,
            identity,
            (identity, f"person-{identity}", identity, "city"),
            xmin=1,
        )
        for identity in range(1, count + 1)
    ]


def test_header_corruption_before_the_requested_landing_is_not_hidden() -> None:
    stack = build_query_stack()
    refs = _insert_people(stack, 3)
    assert refs[0].page == refs[-1].page
    with stack.pool.pinned(stack.heap.file, refs[0].page) as page:
        page.update_slot(refs[0].slot, bytes(RECORD_HEADER_SIZE - 1))

    context = _context(stack)
    view = _owner_landing_view(
        stack.engine, context, stack.table("Person"), frozenset()
    )
    with pytest.raises(GrafxCorruptionDetected) as raised:
        view.get(3, context)
    assert raised.value.details["field"] == "record_header"
    stack.engine.settle_schema(71, committed=False)


def test_requested_landing_payload_corruption_is_not_hidden() -> None:
    stack = build_query_stack()
    ref = _insert_people(stack, 1)[0]
    with stack.pool.pinned(stack.heap.file, ref.page) as page:
        content = page.read_slot(ref.slot)
        header = RecordHeader.decode(content)
        page.update_slot(
            ref.slot,
            replace(header, payload_len=header.payload_len + 1).encode()
            + content[RECORD_HEADER_SIZE:],
        )

    context = _context(stack)
    view = _owner_landing_view(
        stack.engine, context, stack.table("Person"), frozenset()
    )
    with pytest.raises(GrafxCorruptionDetected) as raised:
        view.get(1, context)
    assert raised.value.details["declared"] == header.payload_len + 1
    assert raised.value.details["observed"] == header.payload_len
    stack.engine.settle_schema(71, committed=False)


def test_on_disk_landing_reuses_the_authenticated_payload_length_for_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_query_stack()
    ref = _insert_people(stack, 1)[0]
    table = stack.table("Person")
    version = stack.heap.read(ref)
    expected_payload_bytes = len(encode_tuple(table, version.values))
    assert version.stored_payload_bytes == expected_payload_bytes

    def unexpected_encode(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("an authenticated on-disk payload must not be re-encoded")

    monkeypatch.setattr(query_engine_module, "encode_tuple", unexpected_encode)

    assert _owner_landing_result_bytes(table, (ref, version)) == (
        query_engine_module._OWNER_LANDING_RESULT_BASE_BYTES
        + expected_payload_bytes * _OWNER_LANDING_PAYLOAD_MULTIPLIER
    )


def test_changed_or_synthetic_landing_keeps_the_canonical_encoding_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_query_stack()
    ref = _insert_people(stack, 1)[0]
    table = stack.table("Person")
    changed = replace(stack.heap.read(ref), values=(1, "changed", 9, "city"))
    assert changed.stored_payload_bytes is None
    original = encode_tuple
    calls = 0

    def observed_encode(table_arg: object, values: object) -> bytes:
        nonlocal calls
        calls += 1
        return original(table_arg, values)

    monkeypatch.setattr(query_engine_module, "encode_tuple", observed_encode)

    assert _owner_landing_result_bytes(table, (ref, changed)) is not None
    assert calls == 1


@pytest.mark.parametrize(
    ("max_bytes", "max_entries"),
    (
        (
            _OWNER_LANDING_MEMO_BYTES
            + _OWNER_LANDING_TABLE_BYTES
            + _OWNER_LANDING_VIEW_BASE_BYTES,
            1_000,
        ),
        (64 * 1024 * 1024, 3),
    ),
    ids=("byte-ceiling", "entry-ceiling"),
)
def test_result_quota_discards_retention_but_never_denies_the_landing(
    monkeypatch: pytest.MonkeyPatch,
    max_bytes: int,
    max_entries: int,
) -> None:
    stack = build_query_stack()
    ref = _insert_people(stack, 1)[0]
    stack.engine._owner_budget = _OwnerLandingBudget(
        max_bytes=max_bytes,
        max_entries=max_entries,
        guard=stack.engine._endpoint_guard,
    )
    calls = 0
    original = query_engine_module._visible_identity_with_ref

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(query_engine_module, "_visible_identity_with_ref", counted)
    context = _context(stack)
    view = _owner_landing_view(
        stack.engine, context, stack.table("Person"), frozenset()
    )

    assert view.get(1, context)[0] == ref
    assert view.get(1, context)[0] == ref
    assert calls == 2, "the saturated result cache fell back without retaining either payload"
    memo = stack.engine._owner_memo[71]
    slot = memo.tables[stack.table("Person").table_id]
    assert slot.view is view
    assert view._cache == {}
    assert view._cache_enabled is False
    assert stack.engine._owner_budget._used_bytes == (
        _OWNER_LANDING_MEMO_BYTES
        + _OWNER_LANDING_TABLE_BYTES
        + _OWNER_LANDING_VIEW_BASE_BYTES
    )
    assert stack.engine._owner_budget._used_entries == 3

    stack.engine.settle_schema(71, committed=False)
    assert stack.engine._owner_budget._used_bytes == 0
    assert stack.engine._owner_budget._used_entries == 0


def test_view_admission_failure_releases_its_table_slot_and_uses_local_fallback() -> None:
    stack = build_query_stack()
    ref = _insert_people(stack, 1)[0]
    stack.engine._owner_budget = _OwnerLandingBudget(
        max_bytes=_OWNER_LANDING_MEMO_BYTES + _OWNER_LANDING_TABLE_BYTES,
        max_entries=2,
        guard=stack.engine._endpoint_guard,
    )
    context = _context(stack)

    view = _owner_landing_view(
        stack.engine, context, stack.table("Person"), frozenset()
    )

    assert view.get(1, context)[0] == ref
    assert view._budget is None
    assert stack.engine._owner_memo[71].tables == {}
    assert stack.engine._owner_budget._used_bytes == _OWNER_LANDING_MEMO_BYTES
    assert stack.engine._owner_budget._used_entries == 1
    stack.engine.settle_schema(71, committed=False)
    assert stack.engine._owner_budget._used_bytes == 0
    assert stack.engine._owner_budget._used_entries == 0


def test_many_updates_of_one_ref_are_charged_by_full_fingerprint_history() -> None:
    stack = build_query_stack()
    ref = _insert_people(stack, 1)[0]
    table = stack.table("Person")
    context = _context(stack)
    for revision in range(32):
        context.txn.stage_row_update(
            table,
            ref,
            (1, f"revision-{revision}-{'x' * 32}", revision, "city"),
        )

    # This is exactly what the former accounting charged: one reduced changed row.  The complete
    # 32-entry fingerprint must no longer fit behind that O(1) overlay allowance.
    reduced_allowance = (
        _OWNER_LANDING_MEMO_BYTES
        + _OWNER_LANDING_TABLE_BYTES
        + _OWNER_LANDING_VIEW_BASE_BYTES
        + _OWNER_LANDING_OVERLAY_ENTRY_BYTES
    )
    stack.engine._owner_budget = _OwnerLandingBudget(
        max_bytes=reduced_allowance,
        max_entries=10_000,
        guard=stack.engine._endpoint_guard,
    )

    view = _owner_landing_view(stack.engine, context, table, frozenset())

    assert view._budget is None
    assert view._fingerprint == ()
    assert stack.engine._owner_memo[71].tables == {}
    assert stack.engine._owner_budget._used_bytes == _OWNER_LANDING_MEMO_BYTES
    found = view.get(1, context)
    assert found is not None
    assert found[1].values == (1, f"revision-31-{'x' * 32}", 31, "city")
    stack.engine.settle_schema(71, committed=False)
    assert stack.engine._owner_budget._used_bytes == 0
    assert stack.engine._owner_budget._used_entries == 0


def test_a_memoized_view_does_not_retain_its_statement_context() -> None:
    stack = build_query_stack()
    ref = _insert_people(stack, 1)[0]
    table = stack.table("Person")
    context = _context(stack)
    values = (1, "updated", 9, "city")
    context.txn.stage_row_update(table, ref, values)
    context_ref = weakref.ref(context)
    view = _owner_landing_view(stack.engine, context, table, frozenset())
    expected_bytes = (
        _OWNER_LANDING_MEMO_BYTES
        + _OWNER_LANDING_TABLE_BYTES
        + _OWNER_LANDING_VIEW_BASE_BYTES
        + _OWNER_LANDING_OVERLAY_ENTRY_BYTES
        + _OWNER_LANDING_FINGERPRINT_ENTRY_BYTES
        + len(encode_tuple(table, values)) * _OWNER_LANDING_PAYLOAD_MULTIPLIER
    )
    assert stack.engine._owner_budget._used_bytes == expected_bytes
    assert stack.engine._owner_budget._used_entries == 5

    del context
    gc.collect()

    assert context_ref() is None
    assert view._fingerprint != ()
    stack.engine.settle_schema(71, committed=False)
    assert view._fingerprint == ()
    assert stack.engine._owner_budget._used_bytes == 0
    assert stack.engine._owner_budget._used_entries == 0


def test_concurrent_callers_supply_their_own_statement_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_query_stack()
    refs = _insert_people(stack, 2)
    versions = {identity: stack.heap.read(refs[identity - 1]) for identity in (1, 2)}
    first = _context(stack)
    second = _LandingContext(stack, txn=first.txn, txn_id=71)
    first_view = _owner_landing_view(
        stack.engine, first, stack.table("Person"), frozenset()
    )
    second_view = _owner_landing_view(
        stack.engine, second, stack.table("Person"), frozenset()
    )
    assert second_view is first_view
    rendezvous = threading.Barrier(2)
    outcomes: queue.SimpleQueue[object] = queue.SimpleQueue()

    def observed_lookup(
        engine: object,
        context: object,
        table: object,
        identity: int,
    ) -> object:
        expected = first if identity == 1 else second
        assert context is expected
        rendezvous.wait(5)
        return refs[identity - 1], versions[identity]

    monkeypatch.setattr(
        query_engine_module, "_visible_identity_with_ref", observed_lookup
    )

    def resolve(identity: int, context: _LandingContext) -> None:
        try:
            outcomes.put(first_view.get(identity, context))
        except BaseException as failure:  # pragma: no cover - rendered by parent assertion
            outcomes.put(failure)

    threads = (
        threading.Thread(target=resolve, args=(1, first)),
        threading.Thread(target=resolve, args=(2, second)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()

    found = tuple(outcomes.get_nowait() for _thread in threads)
    for result in found:
        if isinstance(result, BaseException):
            raise result
    assert {result[1].record_id for result in found} == {1, 2}
    stack.engine.settle_schema(71, committed=False)
    assert stack.engine._owner_budget._used_bytes == 0
    assert stack.engine._owner_budget._used_entries == 0


def test_saturation_cannot_grow_an_unaccounted_owner_transaction_registry() -> None:
    stack = build_query_stack()
    ref = _insert_people(stack, 1)[0]
    stack.engine._owner_budget = _OwnerLandingBudget(
        max_bytes=_OWNER_LANDING_MEMO_BYTES - 1,
        max_entries=10_000,
        guard=stack.engine._endpoint_guard,
    )

    for txn_id in range(1, 33):
        context = _context(stack, txn_id=txn_id)
        view = _owner_landing_view(
            stack.engine, context, stack.table("Person"), frozenset()
        )
        assert view.get(1, context)[0] == ref
        view.close()
        stack.engine.settle_schema(txn_id, committed=False)

    assert stack.engine._owner_memo == {}
    assert stack.engine._owner_budget._used_bytes == 0
    assert stack.engine._owner_budget._used_entries == 0


class _RefusingMemoMap(dict[int, object]):
    """A mapping allocation failure at the exact post-reservation boundary."""

    def __setitem__(self, key: int, value: object) -> None:
        raise MemoryError("injected owner memo mapping allocation failure")


def test_owner_txn_registry_failure_returns_its_prepaid_budget() -> None:
    stack = build_query_stack()
    _insert_people(stack, 1)
    stack.engine._owner_memo = _RefusingMemoMap()

    with pytest.raises(MemoryError, match="owner memo mapping"):
        _owner_landing_view(
            stack.engine, _context(stack), stack.table("Person"), frozenset()
        )

    assert stack.engine._owner_memo == {}
    assert stack.engine._owner_budget._used_bytes == 0
    assert stack.engine._owner_budget._used_entries == 0


def test_owner_table_registry_failure_returns_its_prepaid_budget() -> None:
    stack = build_query_stack()
    _insert_people(stack, 1)
    context = _context(stack)
    memo = _owner_landing_txn_memo(stack.engine, context)
    assert memo is not None
    memo.tables = _RefusingMemoMap()  # type: ignore[assignment]

    with pytest.raises(MemoryError, match="owner memo mapping"):
        _owner_landing_view(
            stack.engine, context, stack.table("Person"), frozenset()
        )

    assert stack.engine._owner_budget._used_bytes == _OWNER_LANDING_MEMO_BYTES
    assert stack.engine._owner_budget._used_entries == 1
    stack.engine.settle_schema(71, committed=False)
    assert stack.engine._owner_budget._used_bytes == 0
    assert stack.engine._owner_budget._used_entries == 0


class _ObservedRLock:
    """A re-entrant injected guard that exposes ownership only to this test."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()
        self.entries = 0

    def __enter__(self) -> _ObservedRLock:
        self._lock.acquire()
        self._local.depth = getattr(self._local, "depth", 0) + 1
        self.entries += 1
        return self

    def __exit__(self, *exception: object) -> bool:
        self._local.depth -= 1
        self._lock.release()
        return False

    def held_here(self) -> bool:
        return bool(getattr(self._local, "depth", 0))


def test_owner_budget_competing_reservations_are_atomic() -> None:
    guard = _ObservedRLock()
    budget = _OwnerLandingBudget(max_bytes=3, max_entries=3, guard=guard)
    ready = threading.Barrier(17)
    outcomes: queue.SimpleQueue[bool] = queue.SimpleQueue()

    def reserve_one() -> None:
        ready.wait()
        try:
            budget.reserve(bytes_=1, entries=1)
        except _OwnerLandingCapacity:
            outcomes.put(False)
        else:
            outcomes.put(True)

    threads = [threading.Thread(target=reserve_one) for _ in range(16)]
    for thread in threads:
        thread.start()
    ready.wait()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()

    assert sum(outcomes.get_nowait() for _ in threads) == 3
    assert budget._used_bytes == 3
    assert budget._used_entries == 3
    budget.release(bytes_=3, entries=3)
    assert guard.entries == 17


def test_settlement_retires_an_active_view_without_holding_the_guard_for_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_query_stack()
    ref = _insert_people(stack, 1)[0]
    version = stack.heap.read(ref)
    guard = _ObservedRLock()
    stack.engine._endpoint_guard = guard
    stack.engine._owner_budget = _OwnerLandingBudget(
        max_bytes=64 * 1024 * 1024,
        max_entries=1_000_000,
        guard=guard,
    )
    context = _context(stack)
    view = _owner_landing_view(
        stack.engine, context, stack.table("Person"), frozenset()
    )
    entered = threading.Event()
    proceed = threading.Event()
    outcome: queue.SimpleQueue[object] = queue.SimpleQueue()

    def paused_lookup(*args: object, **kwargs: object) -> object:
        assert not guard.held_here(), "heap identity lookup ran inside the owner memo guard"
        entered.set()
        assert proceed.wait(5)
        return ref, version

    monkeypatch.setattr(
        query_engine_module, "_visible_identity_with_ref", paused_lookup
    )

    def resolve() -> None:
        try:
            outcome.put(view.get(1, context))
        except BaseException as failure:  # pragma: no cover - rendered by parent assertion
            outcome.put(failure)

    worker = threading.Thread(target=resolve)
    worker.start()
    assert entered.wait(5)
    stack.engine.settle_schema(71, committed=False)
    assert stack.engine._owner_memo == {}
    assert stack.engine._owner_budget._used_bytes == _OWNER_LANDING_VIEW_BASE_BYTES
    assert stack.engine._owner_budget._used_entries == 1
    proceed.set()
    worker.join(5)
    assert not worker.is_alive()

    found = outcome.get_nowait()
    if isinstance(found, BaseException):
        raise found
    assert found is not None and found[0] == ref
    assert stack.engine._owner_budget._used_bytes == 0
    assert stack.engine._owner_budget._used_entries == 0
