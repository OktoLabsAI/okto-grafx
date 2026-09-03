"""D-03: lazy owner landings preserve corruption and bounded-lifecycle contracts."""

from __future__ import annotations

import queue
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

import okto_grafx.engine.query_engine as query_engine_module
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.engine.query_engine import (
    _OWNER_LANDING_MEMO_BYTES,
    _OWNER_LANDING_TABLE_BYTES,
    _OWNER_LANDING_VIEW_BASE_BYTES,
    _OwnerLandingBudget,
    _OwnerLandingCapacity,
    _owner_landing_txn_memo,
    _owner_landing_view,
)
from tests.query.stack import QueryStack, build_query_stack


def _context(stack: QueryStack, *, txn_id: int = 71) -> object:
    """Return the narrow owner context consumed by the landing view."""
    transaction = stack.transaction(1_000)
    transaction.txn_id = txn_id
    schema = stack.catalog_store.catalog
    return SimpleNamespace(
        txn=transaction,
        snapshot=transaction.snapshot,
        schema=lambda: schema,
        staged_rows=[],
    )


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
        view.get(3)
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
        view.get(1)
    assert raised.value.details["declared"] == header.payload_len + 1
    assert raised.value.details["observed"] == header.payload_len
    stack.engine.settle_schema(71, committed=False)


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

    assert view.get(1)[0] == ref
    assert view.get(1)[0] == ref
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

    assert view.get(1)[0] == ref
    assert view._budget is None
    assert stack.engine._owner_memo[71].tables == {}
    assert stack.engine._owner_budget._used_bytes == _OWNER_LANDING_MEMO_BYTES
    assert stack.engine._owner_budget._used_entries == 1
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
        assert view.get(1)[0] == ref
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
            outcome.put(view.get(1))
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
