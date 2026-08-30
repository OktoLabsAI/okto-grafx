"""Cross-participant exact-index views are complete or refuse; they never answer short."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import (
    GrafxBufferBudgetExceeded,
    GrafxIndexError,
    GrafxStorageError,
    GrafxUnsupportedOperation,
)
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.index_manager import (
    INDEX_FLAG_STALE,
    INDEX_READ_RETRY_BUDGET,
    HashIndex,
    IndexStore,
)

from .conftest import (
    SnapshotDouble,
    TransactionDouble,
    build_database,
    cold_view,
    header_on_device,
)

BORN = 10
LATER = 20


def _insert_exact(database: object, record_id: int, name: str, csn: int, txn_id: int):
    ref = database.insert(record_id, name, csn)
    txn = TransactionDouble(txn_id=txn_id)
    database.exact.stage_insert(txn, database.key(record_id, name), ref, 0)
    database.exact.commit(txn, csn)
    return ref


def _rebuild(database: object, through: int, txn_id: int) -> None:
    txn = TransactionDouble(txn_id=txn_id)
    database.manager.rebuild(database.exact.name, txn, through)
    database.exact.commit(txn, through)
    database.manager.clear_stale(database.exact.name, through)


def _two_row_rebuild_fixture():
    database = build_database()
    first = _insert_exact(database, 1, "Ada", BORN, 1)
    database.pool.flush(database.heap.file)
    reader = cold_view(database)
    second = database.insert(2, "Ada", LATER)
    database.pool.flush(database.heap.file)
    key = database.key(1, "Ada")
    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN)) == (first,)
    return database, reader, key, first, second


def test_a_completed_foreign_rebuild_invalidates_preexisting_bucket_frames() -> None:
    database, reader, key, first, second = _two_row_rebuild_fixture()

    _rebuild(database, LATER, 2)

    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(LATER)) == (
        first,
        second,
    )


def test_same_lsn_healthy_stale_healthy_aba_is_detected_by_page_zero_sequence() -> None:
    database = build_database()
    first = _insert_exact(database, 1, "Ada", BORN, 1)
    second = database.insert(2, "Ada", BORN)
    database.pool.flush(database.heap.file)
    reader = cold_view(database)
    key = database.key(1, "Ada")
    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN)) == (first,)
    header_before = header_on_device(database.device, database.exact.file)
    seq_before = reader.pool.read_fresh_page(database.exact.file, 0).seq

    _rebuild(database, BORN, 2)

    header_after = header_on_device(database.device, database.exact.file)
    seq_after = reader.pool.read_fresh_page(database.exact.file, 0).seq
    assert header_after == header_before, "the header payload must form a real ABA"
    assert seq_after > seq_before
    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN)) == (
        first,
        second,
    )


def test_direct_exact_doors_rebase_after_same_lsn_healthy_aba() -> None:
    database = build_database()
    first = _insert_exact(database, 1, "Ada", BORN, 1)
    second = database.insert(2, "Ada", BORN)
    database.pool.flush(database.heap.file)
    reader = cold_view(database)
    key = database.key(1, "Ada")

    assert reader.exact.lookup(key, SnapshotDouble(BORN)) == (first,)
    assert tuple(entry.ref for entry in reader.exact.candidates(key)) == (first,)

    _rebuild(database, BORN, 2)

    assert reader.exact.lookup(key, SnapshotDouble(BORN)) == (first, second)
    assert tuple(entry.ref for entry in reader.exact.candidates(key)) == (first, second)


def test_an_older_rebuild_generation_cannot_overwrite_a_newer_complete_rebuild() -> None:
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)
    older = TransactionDouble(txn_id=2)
    database.manager.rebuild(database.exact.name, older, BORN)
    second = database.insert(2, "Grace", LATER)
    database.pool.flush(database.heap.file)
    newer = TransactionDouble(txn_id=3)
    database.manager.rebuild(database.exact.name, newer, LATER)
    database.exact.commit(newer, LATER)

    with pytest.raises(GrafxIndexError) as superseded:
        database.exact.commit(older, BORN)

    assert superseded.value.details["field"] == "rebuild_superseded"
    assert database.manager.lookup(
        database.exact.name,
        database.key(2, "Grace"),
        SnapshotDouble(LATER),
    ) == (second,)


def test_completed_rebuild_reset_replay_is_idempotent() -> None:
    database = build_database()
    first = _insert_exact(database, 1, "Ada", BORN, 1)
    rebuild = TransactionDouble(txn_id=2)
    database.manager.rebuild(database.exact.name, rebuild, BORN)
    records = rebuild.with_lsns(100)
    database.exact.commit(rebuild, BORN)

    assert database.manager.apply(records[0]) is True
    for record in records[1:]:
        assert database.manager.apply(record) is True
    database.pool.flush(database.exact.file)
    database.pool.flush(database.heap.file)

    assert database.manager.lookup(
        database.exact.name,
        database.key(1, "Ada"),
        SnapshotDouble(BORN),
    ) == (first,)


def test_same_lsn_redo_repair_moves_page_zero_and_refreshes_a_warm_reader() -> None:
    database = build_database()
    empty_buckets = tuple(
        database.device.raw_page(database.exact.file, page_index)
        for page_index in range(1, database.exact.definition.bucket_count + 1)
    )
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=4)
    database.exact.stage_insert(txn, database.key(1, "Ada"), ref, 0)
    record = txn.with_lsns(BORN)[0]
    database.exact.commit(txn, BORN)
    for page_index, image in enumerate(empty_buckets, start=1):
        database.device.poke_page(database.exact.file, page_index, image)
    database.pool.invalidate(database.exact.file)
    reader = cold_view(database)
    key = database.key(1, "Ada")
    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN)) == ()
    seq_before = reader.pool.read_fresh_page(reader.exact.file, 0).seq
    recoverer = cold_view(database)

    assert recoverer.manager.apply(record) is True
    recoverer.pool.flush(recoverer.exact.file)

    seq_after = reader.pool.read_fresh_page(reader.exact.file, 0).seq
    assert seq_after > seq_before
    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN)) == (ref,)


def test_a_rebuild_during_heap_validation_retries_the_whole_exact_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, reader, key, first, _second = _two_row_rebuild_fixture()
    original = HashIndex._candidates_unchecked
    triggered = False
    traversals = 0

    def rebuilding(self: HashIndex, wanted: bytes):
        nonlocal triggered, traversals
        if self is reader.exact:
            traversals += 1
        candidates = original(self, wanted)
        if self is reader.exact and not triggered:
            triggered = True
            _rebuild(database, LATER, 3)
        return candidates

    monkeypatch.setattr(HashIndex, "_candidates_unchecked", rebuilding)

    # The statement snapshot remains BORN, so the later row is correctly invisible; what the
    # assertion pins is that the first traversal is discarded and the whole operation repeats.
    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN)) == (first,)
    assert triggered
    assert traversals == 2


def test_a_persistent_foreign_stale_mark_mid_lookup_refuses_instead_of_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)
    reader = cold_view(database)
    key = database.key(1, "Ada")
    original = HashIndex._candidates_unchecked
    marked = False

    def marking(self: HashIndex, wanted: bytes):
        nonlocal marked
        candidates = original(self, wanted)
        if self is reader.exact and not marked:
            marked = True
            database.exact.mark_stale("persistent foreign mark")
        return candidates

    monkeypatch.setattr(HashIndex, "_candidates_unchecked", marking)
    with pytest.raises(GrafxIndexError) as refused:
        reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN))

    assert refused.value.details["field"] == "index_view_unavailable"
    assert marked


def test_a_mark_after_the_final_fence_linearizes_first_result_but_refuses_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    ref = _insert_exact(database, 1, "Ada", BORN, 1)
    reader = cold_view(database)
    key = database.key(1, "Ada")
    original = HashIndex.finish_exact_read
    marked = False

    def marking_after(self: HashIndex, before: object, required_lsn: int) -> bool:
        nonlocal marked
        stable = original(self, before, required_lsn)
        if self is reader.exact and stable and not marked:
            marked = True
            database.exact.mark_stale("after reader linearization")
        return stable

    monkeypatch.setattr(HashIndex, "finish_exact_read", marking_after)
    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN)) == (ref,)
    with pytest.raises(GrafxIndexError) as refused:
        reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN))
    assert refused.value.details["field"] == "index_view_unavailable"


def test_an_exact_lookup_is_observational_and_writes_no_page() -> None:
    database = build_database()
    ref = _insert_exact(database, 1, "Ada", BORN, 1)
    reader = cold_view(database)
    writes_before = tuple(database.device.write_calls)

    assert reader.manager.lookup(
        reader.exact.name, database.key(1, "Ada"), SnapshotDouble(BORN)
    ) == (ref,)

    assert tuple(database.device.write_calls) == writes_before


def test_steady_exact_lookup_collects_exactly_two_fresh_page_zero_observations() -> None:
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)
    reader = cold_view(database)
    key = database.key(1, "Ada")
    database.device.read_calls.clear()

    reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN))
    first_use = sum(
        call == (reader.exact.file, 0) for call in database.device.read_calls
    )
    database.device.read_calls.clear()
    reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN))
    steady = sum(call == (reader.exact.file, 0) for call in database.device.read_calls)

    assert first_use == 2
    assert steady == 2


def test_repeated_certificate_changes_fail_closed_after_the_bounded_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)
    database.pool.flush(database.heap.file)
    calls = 0

    def always_changed(self: HashIndex, before: object, required_lsn: int) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(HashIndex, "finish_exact_read", always_changed)

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.lookup(
            database.exact.name, database.key(1, "Ada"), SnapshotDouble(BORN)
        )

    assert refused.value.details["field"] == "index_view_changed"
    assert refused.value.retryable is True
    assert calls == INDEX_READ_RETRY_BUDGET + 1


def test_a_snapshot_beyond_the_durable_build_position_is_refused_explicitly() -> None:
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.lookup(
            database.exact.name, database.key(1, "Ada"), SnapshotDouble(LATER)
        )

    assert refused.value.details["field"] == "index_view_unavailable"
    assert refused.value.details["built_through_lsn"] == BORN
    assert refused.value.details["required_lsn"] == LATER


def test_an_exact_snapshot_without_read_lsn_is_refused_without_weakening_snapshotlike() -> None:
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)

    class PredicateOnly:
        def visible(self, xmin: int, xmax: int) -> bool:
            return True

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.lookup(
            database.exact.name, database.key(1, "Ada"), PredicateOnly()
        )

    assert refused.value.details["field"] == "snapshot.read_lsn"


def test_reset_rechecks_durable_stale_before_mutating_its_first_bucket() -> None:
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)
    database.exact.mark_stale("repair requested")
    txn = TransactionDouble(txn_id=8)
    database.exact.stage_reset(txn, LATER)
    database.exact.clear_stale(LATER)
    before = tuple(
        database.device.raw_page(database.exact.file, page_index)
        for page_index in range(1, database.exact.definition.bucket_count + 1)
    )

    with pytest.raises(GrafxIndexError) as refused:
        database.exact.commit(txn, LATER)

    assert refused.value.details["field"] == "rebuild_superseded"
    assert tuple(
        database.device.raw_page(database.exact.file, page_index)
        for page_index in range(1, database.exact.definition.bucket_count + 1)
    ) == before


def test_logged_reset_redo_requires_the_durable_mark_and_rebuilds_completely() -> None:
    database = build_database()
    first = _insert_exact(database, 1, "Ada", BORN, 1)
    second = database.insert(2, "Ada", LATER)
    txn = TransactionDouble(txn_id=9)
    database.manager.rebuild(database.exact.name, txn, LATER)

    for record in txn.with_lsns(100):
        assert database.manager.apply(record)
    # Recovery publishes replayed bucket pages before it lifts the durable refusal.
    database.pool.flush(database.exact.file)
    database.pool.flush(database.heap.file)
    database.manager.clear_stale(database.exact.name, LATER)

    assert database.manager.lookup(
        database.exact.name, database.key(1, "Ada"), SnapshotDouble(LATER)
    ) == (first, second)


def test_a_stale_mark_rebases_page_zero_and_preserves_a_foreign_advance() -> None:
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)
    participant = cold_view(database)
    assert participant.exact.header.built_through_lsn == BORN
    _insert_exact(database, 2, "Grace", LATER, 2)

    participant.exact.mark_stale("foreign advance must survive")

    header = header_on_device(database.device, database.exact.file)
    assert header.built_through_lsn == LATER
    assert header.flags & INDEX_FLAG_STALE


def test_final_page_zero_conflict_discards_the_attempt_before_a_later_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)
    participant = cold_view(database)
    original = BufferPool.write_back

    def conflict_each_attempt(self: BufferPool, file: str, page_index: int) -> bool:
        if self is database.pool and file == database.exact.file and page_index == 0:
            participant.exact._claim_rebuild("advance every competing generation")
        return original(self, file, page_index)

    monkeypatch.setattr(BufferPool, "write_back", conflict_each_attempt)
    with pytest.raises(GrafxUnsupportedOperation) as conflict:
        database.exact._claim_rebuild("losing claimant")
    assert conflict.value.details["field"] == "page_sequence_conflict"
    monkeypatch.setattr(BufferPool, "write_back", original)

    token = database.exact._claim_rebuild("retry after contention")

    assert token == database.pool.read_fresh_page(database.exact.file, 0).seq


def test_storage_refusal_during_page_zero_publish_does_not_poison_the_handle() -> None:
    database = build_database()
    _insert_exact(database, 1, "Ada", BORN, 1)
    database.device.refuse_write_number(
        1, GrafxStorageError("injected transient page-0 refusal", file=database.exact.file)
    )

    with pytest.raises(GrafxStorageError) as refused:
        database.exact._claim_rebuild("first attempt")
    assert refused.value.retryable is True
    database.device.disarm()

    token = database.exact._claim_rebuild("retry after storage recovers")

    assert token == database.pool.read_fresh_page(database.exact.file, 0).seq


def test_a_successful_exact_commit_publishes_buckets_before_its_header_certificate() -> None:
    database = build_database()
    database.device.write_calls.clear()

    _insert_exact(database, 1, "Ada", BORN, 1)

    writes = [
        page for file, page in database.device.write_calls if file == database.exact.file
    ]
    assert any(page != 0 for page in writes)
    assert writes[-1] == 0, "page 0 advertised the build before its bucket reached the device"


def test_short_commit_retry_never_clears_stale_when_bucket_flush_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    first = database.insert(1, "Ada", BORN)
    second = database.insert(2, "Grace", BORN)
    txn = TransactionDouble(txn_id=21)
    database.exact.stage_insert(txn, database.key(1, "Ada"), first, 0)
    database.exact.stage_insert(txn, database.key(2, "Grace"), second, 0)
    original_apply = IndexStore._apply_change
    calls = 0

    def fail_second(store: IndexStore, change: object, lsn: int) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GrafxBufferBudgetExceeded("injected partial apply")
        return original_apply(store, change, lsn)  # type: ignore[arg-type]

    monkeypatch.setattr(IndexStore, "_apply_change", fail_second)
    with pytest.raises(GrafxBufferBudgetExceeded):
        database.exact.commit(txn, BORN)
    monkeypatch.setattr(IndexStore, "_apply_change", original_apply)
    original_flush = BufferPool.flush

    def refuse_bucket_publish(self: BufferPool, file: str | None = None) -> int:
        if self is database.pool and file == database.exact.file:
            raise GrafxStorageError("injected bucket publication refusal", file=file)
        return original_flush(self, file)

    monkeypatch.setattr(BufferPool, "flush", refuse_bucket_publish)
    with pytest.raises(GrafxStorageError):
        database.exact.commit(txn, BORN)

    assert database.exact.stale
    assert header_on_device(database.device, database.exact.file).flags & INDEX_FLAG_STALE


def test_late_replay_prefix_cannot_overwrite_a_newer_healthy_rebuild() -> None:
    database = build_database()
    first = _insert_exact(database, 1, "Ada", BORN, 31)
    second = _insert_exact(database, 2, "Ada", BORN, 32)
    older = TransactionDouble(txn_id=33)
    database.manager.rebuild(database.exact.name, older, BORN)
    records = older.with_lsns(100)

    assert database.manager.apply(records[0])  # RESET and its empty prefix are durable.
    assert database.manager.apply(records[1])  # One rebuilt bucket prefix is durable too.

    third = database.insert(3, "Ada", LATER)
    database.pool.flush(database.heap.file)
    newer = cold_view(database)
    _rebuild(newer, LATER, 34)

    with pytest.raises(GrafxIndexError) as superseded:
        database.manager.apply(records[2])
    assert superseded.value.details["field"] == "rebuild_superseded"

    # No refused frame may survive and write the old prefix over the winner later.
    database.pool.flush(database.exact.file)
    winner = cold_view(database)
    assert winner.manager.lookup(
        winner.exact.name, database.key(1, "Ada"), SnapshotDouble(LATER)
    ) == (first, second, third)


def test_handle_opened_while_stale_heals_after_a_foreign_rebuild() -> None:
    database = build_database()
    ref = _insert_exact(database, 1, "Ada", BORN, 35)
    database.exact.mark_stale("foreign repair is required")
    reader = cold_view(database)

    with pytest.raises(GrafxIndexError):
        reader.manager.lookup(
            reader.exact.name, database.key(1, "Ada"), SnapshotDouble(BORN)
        )

    _rebuild(database, BORN, 36)

    assert reader.manager.lookup(
        reader.exact.name, database.key(1, "Ada"), SnapshotDouble(BORN)
    ) == (ref,)
    assert reader.exact.stale is False


def test_completed_then_interrupted_rebuild_drops_phantom_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    empty_buckets = tuple(
        database.device.raw_page(database.exact.file, page_index)
        for page_index in range(1, database.exact.definition.bucket_count + 1)
    )
    ref = _insert_exact(database, 1, "Ada", BORN, 37)
    rebuild = TransactionDouble(txn_id=38)
    database.manager.rebuild(database.exact.name, rebuild, BORN)
    records = rebuild.with_lsns(BORN - 1)
    original = BufferPool.write_back
    interrupt_final_header = True

    def complete_then_interrupt(
        self: BufferPool, file: str, page_index: int
    ) -> bool:
        nonlocal interrupt_final_header
        if (
            interrupt_final_header
            and self is database.pool
            and file == database.exact.file
            and page_index == 0
        ):
            interrupt_final_header = False
            original(self, file, page_index)
            raise KeyboardInterrupt("after the device accepted the healthy header")
        return original(self, file, page_index)

    monkeypatch.setattr(BufferPool, "write_back", complete_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        database.exact.commit(rebuild, BORN)
    monkeypatch.setattr(BufferPool, "write_back", original)

    durable = database.pool.read_fresh_page(database.exact.file, 0)
    assert not header_on_device(database.device, database.exact.file).flags & INDEX_FLAG_STALE
    assert database.exact._rebuild_authority is None

    # Recreate a missing same-LSN bucket and prove redo publishes a new page-0 clock. A phantom
    # authority used to suppress that touch and left every warm reader on the short generation.
    for page_index, image in enumerate(empty_buckets, start=1):
        database.device.poke_page(database.exact.file, page_index, image)
    database.pool.invalidate(database.exact.file)
    reader = cold_view(database)
    key = database.key(1, "Ada")
    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN)) == ()

    assert database.manager.apply(records[1])
    database.pool.flush(database.exact.file)
    repaired = database.pool.read_fresh_page(database.exact.file, 0)

    assert repaired.seq > durable.seq
    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN)) == (ref,)


def test_proximity_lookup_rebases_after_a_foreign_rebuild() -> None:
    database = build_database()
    first = database.insert(1, "Ada", BORN)
    staged = TransactionDouble(txn_id=39)
    key = database.key(1, "Ada")
    database.proximity.stage_insert(staged, key, first, BORN)
    database.proximity.commit(staged, BORN)
    reader = cold_view(database)
    assert reader.proximity.lookup(key, SnapshotDouble(BORN)) == (first,)

    second = database.insert(2, "Ada", LATER)
    database.pool.flush(database.heap.file)
    rebuild = TransactionDouble(txn_id=40)
    database.manager.rebuild(database.proximity.name, rebuild, LATER)
    database.proximity.commit(rebuild, LATER)
    database.manager.clear_stale(database.proximity.name, LATER)

    assert reader.proximity.lookup(key, SnapshotDouble(LATER)) == (first, second)


def test_exact_validation_rebases_heap_frames_after_a_foreign_delete() -> None:
    database = build_database()
    ref = _insert_exact(database, 1, "Ada", BORN, 41)
    reader = cold_view(database)
    key = database.key(1, "Ada")
    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(BORN)) == (ref,)

    # An exact index may retain a deleted candidate, but its page-0 clock still moves with the
    # maintained commit. Heap validation must therefore leave the reader's warm pre-delete page.
    database.heap.delete(database.table, ref, LATER)
    database.pool.flush(database.heap.file)
    database.exact.advance_built_through(LATER)

    assert reader.manager.lookup(reader.exact.name, key, SnapshotDouble(LATER)) == ()
