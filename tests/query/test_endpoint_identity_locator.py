"""Focused regressions for the bounded canonical endpoint identity locator."""

from __future__ import annotations

import inspect
import multiprocessing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.model.schema import encode_tuple
from okto_grafx.domain.page import PageType
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.query_engine import (
    _EndpointLocatorBudget,
    _require_physical_endpoint,
    _visible_identity_with_ref,
)
from tests.query.stack import QueryStack, build_query_stack


def _context(stack: QueryStack, *, read_lsn: int = 1000, txn_id: int = 1) -> object:
    """Return the narrow statement context the generic identity door consumes."""
    transaction = stack.transaction(read_lsn)
    transaction.txn_id = txn_id
    schema = stack.catalog_store.catalog
    return SimpleNamespace(
        txn=transaction,
        snapshot=transaction.snapshot,
        schema=lambda: schema,
    )


def _insert_people(stack: QueryStack, count: int, *, width: int = 0) -> list[RecordRef]:
    """Arrange committed heap rows without exercising the query path under test."""
    table = stack.table("Person")
    return [
        stack.heap.insert(
            table,
            identity,
            (identity, f"person-{identity}-{'x' * width}", identity, "city"),
            xmin=1,
        )
        for identity in range(1, count + 1)
    ]


def test_locator_walks_each_header_at_most_once_when_the_prefix_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_query_stack(budget_pages=128)
    count = 60
    refs = _insert_people(stack, count, width=40)
    context = _context(stack)
    calls = 0
    descriptor = inspect.getattr_static(RecordHeader, "peek")
    assert isinstance(descriptor, classmethod)
    original = descriptor.__func__

    def counted(cls: type, raw: bytes) -> object:
        nonlocal calls
        calls += 1
        return original(cls, raw)

    monkeypatch.setattr(RecordHeader, "peek", classmethod(counted))

    tail = _visible_identity_with_ref(stack.engine, context, stack.table("Person"), count)
    assert tail is not None and tail[0] == refs[-1]
    for identity in range(count, 0, -1):
        found = _visible_identity_with_ref(
            stack.engine, context, stack.table("Person"), identity
        )
        assert found is not None and found[1].record_id == identity

    assert calls == count, "the table headers made one canonical pass, not one pass per edge"
    stack.engine.settle_schema(1, committed=False)
    assert stack.engine._endpoint_memo == {}
    assert stack.engine._endpoint_budget._used_bytes == 0
    assert stack.engine._endpoint_budget._used_entries == 0


def test_locator_finishes_the_match_page_before_decoding_the_candidate() -> None:
    stack = build_query_stack()
    refs = _insert_people(stack, 2)
    assert refs[0].page == refs[1].page
    with stack.pool.pinned(stack.heap.file, refs[1].page) as page:
        page.update_slot(refs[1].slot, bytes(RECORD_HEADER_SIZE - 1))

    with pytest.raises(GrafxCorruptionDetected) as raised:
        _visible_identity_with_ref(
            stack.engine, _context(stack), stack.table("Person"), 1
        )
    assert raised.value.details["field"] == "record_header"


def test_corrupt_first_duplicate_wins_before_the_expected_later_ref() -> None:
    stack = build_query_stack()
    table = stack.table("Person")
    first = stack.heap.insert(table, 7, (7, "first", 1, "x"), xmin=1)
    second = stack.heap.insert(table, 7, (7, "second", 2, "x"), xmin=2)
    with stack.pool.pinned(stack.heap.file, first.page) as page:
        content = page.read_slot(first.slot)
        header = RecordHeader.decode(content)
        broken = replace(header, payload_len=header.payload_len + 1)
        page.update_slot(first.slot, broken.encode() + content[RECORD_HEADER_SIZE:])

    snapshot = stack.snapshot()
    with pytest.raises(GrafxCorruptionDetected) as canonical:
        stack.heap.lookup(table, 7, snapshot)
    with pytest.raises(GrafxCorruptionDetected) as located:
        _require_physical_endpoint(
            stack.engine,
            _context(stack),
            stack.table("Knows"),
            table,
            7,
            second,
            "_from",
        )
    assert located.value.message == canonical.value.message
    assert located.value.details == canonical.value.details


def test_later_visible_duplicate_is_refused_only_when_it_is_the_binding_witness() -> None:
    stack = build_query_stack()
    table = stack.table("Person")
    first = stack.heap.insert(table, 7, (7, "first", 1, "x"), xmin=1)
    second = stack.heap.insert(table, 7, (7, "second", 2, "x"), xmin=2)
    context = _context(stack)

    accepted = _require_physical_endpoint(
        stack.engine, context, stack.table("Knows"), table, 7, first, "_from"
    )
    assert accepted.values[1] == "first"
    with pytest.raises(GrafxCorruptionDetected) as raised:
        _require_physical_endpoint(
            stack.engine, context, stack.table("Knows"), table, 7, second, "_from"
        )
    assert raised.value.details["field"] == "record_id"
    assert raised.value.details["canonical_ref"] == first.encode()
    assert raised.value.details["observed_ref"] == second.encode()


def test_a_visible_ref_outside_the_canonical_chain_is_not_accepted() -> None:
    stack = build_query_stack()
    table = stack.table("Person")
    stack.heap.insert(table, 1, (1, "canonical", 1, "x"), xmin=1)
    orphan = stack.pool.allocate(stack.heap.file, int(PageType.HEAP))
    try:
        stack.heap._initialize_data_page(orphan, table.table_id)
        payload = encode_tuple(table, (99, "orphan", 1, "x"))
        slot = orphan.insert_slot(
            RecordHeader(
                record_id=99,
                xmin=1,
                payload_len=len(payload),
                schema_version=table.schema_version,
            ).encode()
            + payload
        )
        orphan_ref = RecordRef(page=orphan.page_index, slot=slot)
    finally:
        stack.pool.unpin(stack.heap.file, orphan.page_index, dirty=True)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        _require_physical_endpoint(
            stack.engine,
            _context(stack),
            stack.table("Knows"),
            table,
            99,
            orphan_ref,
            "_from",
        )
    assert raised.value.details["reason"] == "outside_canonical_chain"


def test_cycle_proof_survives_cursor_resumption_and_is_released_on_failure() -> None:
    stack = build_query_stack(budget_pages=128)
    _insert_people(stack, 50, width=80)
    table = stack.table("Person")
    chain = stack.heap.pages_of(table)
    assert len(chain) >= 3
    with stack.pool.pinned(stack.heap.file, chain[-1]) as page:
        page.next_page = chain[0]
    context = _context(stack)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        _visible_identity_with_ref(stack.engine, context, table, 999_999)
    assert raised.value.details["field"] == "cycle"
    assert stack.engine._endpoint_memo[1].locators == {}
    assert stack.engine._endpoint_budget._used_bytes == 0
    assert stack.engine._endpoint_budget._used_entries == 0


def test_capacity_discards_the_partial_locator_and_uses_canonical_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_query_stack()
    ref = _insert_people(stack, 1)[0]
    stack.engine._endpoint_budget = _EndpointLocatorBudget(max_bytes=1, max_entries=1)
    calls = 0
    original = inspect.getattr_static(HeapStore, "_lookup_with_ref")

    def counted(
        store: HeapStore, table: object, record_id: int, snapshot: object
    ) -> object:
        nonlocal calls
        calls += 1
        return original(store, table, record_id, snapshot)

    monkeypatch.setattr(HeapStore, "_lookup_with_ref", counted)
    context = _context(stack)
    found = _visible_identity_with_ref(stack.engine, context, stack.table("Person"), 1)

    assert found is not None and found[0] == ref
    assert calls == 1
    memo = stack.engine._endpoint_memo[1]
    assert memo.locators == {}
    assert memo.disabled_tables == {stack.table("Person").table_id}
    assert stack.engine._endpoint_budget._used_bytes == 0
    assert stack.engine._endpoint_budget._used_entries == 0


def test_snapshot_and_epoch_changes_never_reuse_a_derived_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_query_stack()
    table = stack.table("Person")
    old = stack.heap.insert(table, 1, (1, "old", 1, "x"), xmin=10)
    new = stack.heap.update(table, old, (1, "new", 2, "x"), xmin=20)

    before = _context(stack, read_lsn=15, txn_id=1)
    found_old = _visible_identity_with_ref(stack.engine, before, table, 1)
    assert found_old is not None and found_old[0] == old

    after = _context(stack, read_lsn=30, txn_id=1)
    found_new = _visible_identity_with_ref(stack.engine, after, table, 1)
    assert found_new is not None and found_new[0] == new
    assert stack.engine._endpoint_memo[1].snapshot is after.snapshot

    calls = 0
    original = inspect.getattr_static(HeapStore, "_lookup_with_ref")

    def counted(
        store: HeapStore, target: object, record_id: int, snapshot: object
    ) -> object:
        nonlocal calls
        calls += 1
        return original(store, target, record_id, snapshot)

    monkeypatch.setattr(HeapStore, "_lookup_with_ref", counted)
    stack.pool.invalidate()
    assert _visible_identity_with_ref(stack.engine, after, table, 1) is not None
    assert calls == 1
    assert stack.engine._endpoint_memo[1].locators == {}


def test_mixed_pending_physical_self_loop_update_and_owner_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "overlays"
    with okto_grafx.connect(root) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
            schema.execute("CREATE REL TABLE Knows(FROM Person TO Person, note STRING)")
        with database.begin("write") as seed:
            seed.execute("CREATE (:Person {id: 1, name: 'one'}), (:Person {id: 2, name: 'two'})")

        calls: list[RecordRef] = []
        original = inspect.getattr_static(HeapStore, "_revalidate_visible_ref")

        def counted(
            store: HeapStore,
            table: object,
            ref: RecordRef,
            record_id: int,
            snapshot: object,
        ) -> object:
            calls.append(ref)
            return original(store, table, ref, record_id, snapshot)

        monkeypatch.setattr(HeapStore, "_revalidate_visible_ref", counted)
        with database.begin("write") as writer:
            writer.execute("MATCH (p:Person {id: 1}) SET p.name = 'updated'")
            writer.execute("CREATE (:Person {id: 3, name: 'pending'})")
            writer.execute(
                "MATCH (a:Person {id: 1}), (b:Person {id: 3}) "
                "CREATE (a)-[:Knows {note: 'mixed'}]->(b)"
            )
            writer.execute(
                "MATCH (a:Person {id: 1}) CREATE (a)-[:Knows {note: 'loop'}]->(a)"
            )
            accepted = tuple(writer._context.row_intents)
            with pytest.raises(GrafxConfigurationError):
                writer.execute(
                    "MATCH (a:Person {id: 1}), (b:Person {id: 2}) DELETE a "
                    "CREATE (a)-[:Knows {note: 'ended'}]->(b)"
                )
            assert tuple(writer._context.row_intents) == accepted

        # Pending refs never reached the heap.  Mixed validates one physical side; the self-loop
        # validates its one exact physical witness once; the refused owner-ended edge validates
        # neither endpoint.
        assert len(calls) == 2
        assert all(type(ref) is RecordRef for ref in calls)


def _foreign_insert(root: str, result: object) -> None:
    """Commit one node from a second process and report any failure without hiding it."""
    try:
        with okto_grafx.connect(root) as database:
            with database.begin("write") as writer:
                writer.execute("CREATE (:Person {id: 3})")
        result.put(None)
    except BaseException as failure:  # pragma: no cover - the parent renders the child failure
        result.put(repr(failure))


@pytest.mark.multiprocess
def test_locator_never_crosses_a_foreign_process_read_view(tmp_path: Path) -> None:
    root = tmp_path / "two-processes"
    with okto_grafx.connect(root) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE REL TABLE Knows(FROM Person TO Person)")
        with database.begin("write") as seed:
            seed.execute("CREATE (:Person {id: 1}), (:Person {id: 2})")
        with database.begin("write") as first:
            first.execute(
                "MATCH (a:Person {id: 1}), (b:Person {id: 2}) CREATE (a)-[:Knows]->(b)"
            )
        assert database._queries._endpoint_memo == {}

        spawn = multiprocessing.get_context("spawn")
        result = spawn.Queue()
        process = spawn.Process(target=_foreign_insert, args=(str(root), result))
        process.start()
        process.join(30)
        if process.is_alive():
            process.terminate()
            process.join(5)
            pytest.fail("the foreign writer did not terminate")
        assert process.exitcode == 0
        assert result.get(timeout=5) is None

        with database.begin("write") as second:
            second.execute(
                "MATCH (a:Person {id: 1}), (b:Person {id: 3}) CREATE (a)-[:Knows]->(b)"
            )
        assert database.execute(
            "MATCH (a:Person)-[:Knows]->(b:Person) RETURN a.id, b.id ORDER BY b.id"
        ).rows == ((1, 2), (1, 3))
