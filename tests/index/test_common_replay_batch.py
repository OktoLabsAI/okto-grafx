"""Focused proofs for the narrow common logical-index replay batch."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.ids import PROVISIONAL_CSN, RecordRef
from okto_grafx.domain.index import (
    IndexChange,
    IndexEntry,
    IndexOperation,
    wal_record_for,
)
from okto_grafx.domain.index.header import IndexHeader
from okto_grafx.domain.index.keys import bucket_of
from okto_grafx.domain.page import Page, PageFullError
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.recovery.decision import CommittedReplay
from okto_grafx.domain.txn.records import encode_page_write
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.index_manager import (
    INDEX_FLAG_STALE,
    IndexManager,
    IndexStore,
)
from okto_grafx.engine.vector_engine import VectorHnswIndex
import okto_grafx.engine.index_manager as index_manager_module

from .conftest import build_database


def _effect(
    store: IndexStore,
    operation: IndexOperation,
    lsn: int,
    *,
    ordinal: int,
    csn: int | None = None,
) -> WalRecord:
    stamp = (
        lsn
        if csn is None
        and (store.definition.versioned or operation is not IndexOperation.INSERT)
        else (0 if csn is None else csn)
    )
    return wal_record_for(
        IndexChange(
            index=store.name,
            operation=operation,
            key=f"key-{ordinal}".encode(),
            ref=RecordRef(ordinal + 1, 1),
            csn=stamp,
            versioned=store.definition.versioned,
        )
    ).with_lsn(lsn)


class _LegacyManager:
    """Expose the existing per-record protocol without the optional batch capability."""

    def __init__(self, manager: IndexManager) -> None:
        self._manager = manager

    def index(self, name: str) -> IndexStore:
        return self._manager.active_index(name)

    def active_index(self, name: str) -> IndexStore:
        return self._manager.active_index(name)

    def apply(self, record: WalRecord) -> bool:
        return self._manager.apply(record)


def _replay(records: Sequence[WalRecord]) -> CommittedReplay:
    return CommittedReplay(
        effects=tuple(records),
        last_committed_lsn=max(record.lsn for record in records) + 1,
    )


def _keys_in_bucket(
    store: IndexStore,
    bucket: int,
    count: int,
    *,
    prefix: bytes = b"replay-key-",
    width: int = 0,
) -> tuple[bytes, ...]:
    keys: list[bytes] = []
    candidate = 0
    while len(keys) < count:
        key = prefix + str(candidate).encode()
        if width:
            key = key.ljust(width, b"x")
        if bucket_of(key, store.definition.bucket_count) == bucket:
            keys.append(key)
        candidate += 1
    return tuple(keys)


def _effect_for(
    store: IndexStore,
    operation: IndexOperation,
    lsn: int,
    key: bytes,
    ref: RecordRef,
) -> WalRecord:
    return wal_record_for(
        IndexChange(
            index=store.name,
            operation=operation,
            key=key,
            ref=ref,
            csn=(
                lsn
                if store.definition.versioned
                or operation is not IndexOperation.INSERT
                else 0
            ),
            versioned=store.definition.versioned,
        )
    ).with_lsn(lsn)


def test_common_batch_seeds_and_writes_page_zero_once_per_interleaved_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    records = (
        _effect(database.exact, IndexOperation.INSERT, 10, ordinal=1),
        _effect(database.proximity, IndexOperation.INSERT, 11, ordinal=2),
        _effect(database.exact, IndexOperation.INSERT, 12, ordinal=3),
        _effect(database.proximity, IndexOperation.INSERT, 13, ordinal=4),
    )
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    original_read = IndexStore._read_header
    original_write = IndexStore._write_header

    def counted_read(store: IndexStore, *, proved_present: bool = False) -> IndexHeader:
        reads[store.name] = reads.get(store.name, 0) + 1
        return original_read(store, proved_present=proved_present)

    def counted_write(store: IndexStore, header: IndexHeader) -> None:
        writes[store.name] = writes.get(store.name, 0) + 1
        original_write(store, header)

    monkeypatch.setattr(IndexStore, "_read_header", counted_read)
    monkeypatch.setattr(IndexStore, "_write_header", counted_write)
    writes_before = len(database.device.write_calls)

    report = CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert reads == {database.exact.name: 1, database.proximity.name: 1}
    assert writes == {database.exact.name: 1, database.proximity.name: 1}
    assert report.touched_files == (database.exact.file, database.proximity.file)
    assert report.index_effects_dispatched == len(records)
    assert len(database.device.write_calls) == writes_before


def test_common_batch_exposes_no_reusable_prepared_plan() -> None:
    database = build_database()

    assert not hasattr(database.manager, "prepare_common_replay_batch")


def test_repeating_replay_after_stale_never_republishes_captured_healthy_header() -> (
    None
):
    database = build_database()
    records = (_effect(database.exact, IndexOperation.INSERT, 10, ordinal=1),)
    redo = CommitRedo(database.pool, database.manager)
    first = redo.apply(_replay(records))
    redo.flush(first)
    database.exact.mark_stale("a later generation must remain authoritative")

    assert database.exact.header.flags & INDEX_FLAG_STALE
    assert database.manager.apply_common_replay_batch(records) is None
    assert database.exact.header.flags & INDEX_FLAG_STALE

    redo.apply(_replay(records))

    assert database.exact.header.flags & INDEX_FLAG_STALE
    assert database.exact.stale is True


def test_common_batch_matches_legacy_and_reapplication_is_idempotent() -> None:
    batched = build_database(name="batched")
    legacy = build_database(name="legacy")
    batched_records = (
        _effect(batched.exact, IndexOperation.INSERT, 10, ordinal=1),
        _effect(batched.proximity, IndexOperation.INSERT, 11, ordinal=2),
        _effect(batched.exact, IndexOperation.TOMBSTONE, 12, ordinal=1, csn=12),
        _effect(batched.proximity, IndexOperation.TOMBSTONE, 13, ordinal=2, csn=13),
    )
    legacy_records = (
        _effect(legacy.exact, IndexOperation.INSERT, 10, ordinal=1),
        _effect(legacy.proximity, IndexOperation.INSERT, 11, ordinal=2),
        _effect(legacy.exact, IndexOperation.TOMBSTONE, 12, ordinal=1, csn=12),
        _effect(legacy.proximity, IndexOperation.TOMBSTONE, 13, ordinal=2, csn=13),
    )
    redo = CommitRedo(batched.pool, batched.manager)

    redo.apply(_replay(batched_records))
    CommitRedo(legacy.pool, _LegacyManager(legacy.manager)).apply(
        _replay(legacy_records)
    )  # type: ignore[arg-type]
    once = batched.entries()
    redo.apply(_replay(batched_records))

    assert batched.entries() == once == legacy.entries()
    assert batched.exact.header == legacy.exact.header
    assert batched.proximity.header == legacy.proximity.header


def test_remove_composes_built_and_reconciled_horizons_and_touches_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    records = (
        _effect(database.proximity, IndexOperation.INSERT, 10, ordinal=1, csn=10),
        _effect(database.proximity, IndexOperation.TOMBSTONE, 11, ordinal=1, csn=11),
        _effect(database.proximity, IndexOperation.REMOVE, 12, ordinal=1, csn=50),
    )
    writes = 0
    original_write = IndexStore._write_header

    def counted_write(store: IndexStore, header: IndexHeader) -> None:
        nonlocal writes
        writes += 1
        original_write(store, header)

    monkeypatch.setattr(IndexStore, "_write_header", counted_write)

    CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert writes == 1
    assert database.proximity.header.built_through_lsn == 12
    assert database.proximity.header.reconciled_through_lsn == 50
    assert tuple(database.proximity.walk()) == ()


def test_a_moved_bucket_touches_page_zero_even_when_built_position_is_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    redo = CommitRedo(database.pool, database.manager)
    redo.apply(
        _replay((_effect(database.exact, IndexOperation.INSERT, 100, ordinal=1),))
    )
    writes = 0
    original_write = IndexStore._write_header

    def counted_write(store: IndexStore, header: IndexHeader) -> None:
        nonlocal writes
        writes += 1
        original_write(store, header)

    monkeypatch.setattr(IndexStore, "_write_header", counted_write)
    redo.apply(
        _replay((_effect(database.exact, IndexOperation.INSERT, 50, ordinal=2),))
    )

    assert writes == 1
    assert database.exact.header.built_through_lsn == 100
    assert len(tuple(database.exact.walk())) == 2


def test_a_late_invalid_position_refuses_before_the_first_bucket_mutates() -> None:
    database = build_database()
    first = _effect(database.exact, IndexOperation.INSERT, 10, ordinal=1)
    invalid = _effect(
        database.exact,
        IndexOperation.INSERT,
        PROVISIONAL_CSN,
        ordinal=2,
    )
    before = database.entries()

    with pytest.raises(GrafxIndexError) as refused:
        CommitRedo(database.pool, database.manager).apply(_replay((first, invalid)))

    assert refused.value.details["field"] == "built_through_lsn"
    assert database.entries() == before


def test_partial_failure_marks_touched_store_stale_and_retry_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    records = (
        _effect(database.exact, IndexOperation.INSERT, 10, ordinal=1),
        _effect(database.exact, IndexOperation.INSERT, 11, ordinal=2),
    )
    original = IndexStore._apply_change
    calls = 0

    def fail_second(store: IndexStore, change: IndexChange, lsn: int) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GrafxIndexError("injected replay failure", field="injected")
        return original(store, change, lsn)

    monkeypatch.setattr(IndexStore, "_apply_change", fail_second)
    redo = CommitRedo(database.pool, database.manager)
    with pytest.raises(GrafxIndexError, match="injected replay failure"):
        redo.apply(_replay(records))
    assert database.exact.stale is True

    monkeypatch.setattr(IndexStore, "_apply_change", original)
    redo.apply(_replay(records))

    assert len(tuple(database.exact.walk())) == 2
    assert database.exact.stale is True


def test_final_header_failure_marks_every_touched_store_without_masking_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    records = (
        _effect(database.exact, IndexOperation.INSERT, 10, ordinal=1),
        _effect(database.proximity, IndexOperation.INSERT, 11, ordinal=2),
    )
    original = IndexStore._write_header
    refused = False

    def fail_second_store(store: IndexStore, header: IndexHeader) -> None:
        nonlocal refused
        if store is database.proximity and not refused:
            refused = True
            raise GrafxIndexError(
                "injected final publication failure", field="publication"
            )
        original(store, header)

    monkeypatch.setattr(IndexStore, "_write_header", fail_second_store)

    with pytest.raises(
        GrafxIndexError, match="injected final publication failure"
    ) as failure:
        CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert failure.value.details["field"] == "publication"
    assert database.exact.stale is True
    assert database.proximity.stale is True


@pytest.mark.parametrize("signal", (KeyboardInterrupt, SystemExit))
def test_process_control_signal_does_not_publish_a_new_stale_verdict(
    monkeypatch: pytest.MonkeyPatch,
    signal: type[BaseException],
) -> None:
    database = build_database()
    records = (
        _effect(database.exact, IndexOperation.INSERT, 10, ordinal=1),
        _effect(database.exact, IndexOperation.INSERT, 11, ordinal=2),
    )
    original = IndexStore._apply_change
    calls = 0

    def interrupt(store: IndexStore, change: IndexChange, lsn: int) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise signal()
        return original(store, change, lsn)

    monkeypatch.setattr(IndexStore, "_apply_change", interrupt)

    with pytest.raises(signal):
        CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert database.exact.stale is False
    assert database.exact._replaying is False  # noqa: SLF001


def test_mixed_replay_bypasses_the_batch_capability_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    database.pool.flush("heap.dat")
    page = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write(
            "heap.dat", 0, database.device.raw_page("heap.dat", 0)
        ),
        lsn=9,
    )
    logical = _effect(database.exact, IndexOperation.INSERT, 10, ordinal=1)
    calls = 0

    def unexpected_batch(
        _manager: IndexManager, _records: Sequence[WalRecord]
    ) -> object | None:
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(IndexManager, "apply_common_replay_batch", unexpected_batch)

    CommitRedo(database.pool, database.manager).apply(_replay((page, logical)))

    assert calls == 0
    assert len(tuple(database.exact.walk())) == 1


def test_reset_active_rebuild_and_vector_store_fall_back_for_the_whole_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    reset = wal_record_for(
        IndexChange(
            index=database.exact.name,
            operation=IndexOperation.RESET,
            csn=10,
            versioned=False,
        )
    ).with_lsn(10)
    assert database.manager.apply_common_replay_batch((reset,)) is None

    token = database.exact._claim_rebuild("test active replay generation")  # noqa: SLF001
    assert token > 0
    ordinary = _effect(database.exact, IndexOperation.INSERT, 11, ordinal=1)
    assert database.manager.apply_common_replay_batch((ordinary,)) is None
    legacy_calls: list[str] = []
    original_apply = IndexStore.apply

    def counted_apply(store: IndexStore, record: WalRecord) -> None:
        legacy_calls.append(store.name)
        original_apply(store, record)

    monkeypatch.setattr(IndexStore, "apply", counted_apply)
    CommitRedo(database.pool, database.manager).apply(_replay((ordinary,)))
    assert legacy_calls == [database.exact.name]

    vector = VectorHnswIndex(
        database.proximity.definition,
        database.pool,
        database.metrics,
        space_id=1,
        space_name="test",
        dimension=2,
        metric_of_space=DistanceMetric.COSINE,
        storage_dtype="f32",
        normalized=False,
        math=PureVectorMath(),
        resolve=lambda _ref: (1.0, 0.0),
    )
    database.manager._indexes[vector.definition.registry_key] = vector  # noqa: SLF001
    vector_record = _effect(database.proximity, IndexOperation.INSERT, 12, ordinal=2)
    assert database.manager.apply_common_replay_batch((vector_record,)) is None
    vector_calls = 0
    original_vector_apply = VectorHnswIndex.apply

    def counted_vector_apply(store: VectorHnswIndex, record: WalRecord) -> None:
        nonlocal vector_calls
        vector_calls += 1
        original_vector_apply(store, record)

    monkeypatch.setattr(VectorHnswIndex, "apply", counted_vector_apply)
    CommitRedo(database.pool, database.manager).apply(_replay((vector_record,)))
    assert vector_calls == 1


def test_only_buckets_at_the_eight_effect_threshold_bypass_scalar_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    hot_keys = _keys_in_bucket(database.exact, 0, 8, prefix=b"hot-")
    cold_keys = _keys_in_bucket(database.exact, 1, 7, prefix=b"cold-")
    records = tuple(
        _effect_for(
            database.exact,
            IndexOperation.INSERT,
            10 + offset,
            key,
            RecordRef(offset + 1, 1),
        )
        for offset, key in enumerate((*hot_keys, *cold_keys))
    )
    scalar_calls: list[bytes] = []
    original = IndexStore._apply_change

    def counted(store: IndexStore, change: IndexChange, lsn: int) -> bool:
        scalar_calls.append(change.key)
        return original(store, change, lsn)

    monkeypatch.setattr(IndexStore, "_apply_change", counted)

    CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert scalar_calls == list(cold_keys)
    assert len(tuple(database.exact.walk())) == 15


def test_hot_buckets_preserve_global_wal_order_across_interleaved_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    exact_keys = _keys_in_bucket(database.exact, 0, 8, prefix=b"ordered-exact-")
    proximity_keys = _keys_in_bucket(
        database.proximity, 0, 8, prefix=b"ordered-proximity-"
    )
    records: list[WalRecord] = []
    expected: list[tuple[str, bytes]] = []
    for ordinal, (exact_key, proximity_key) in enumerate(
        zip(exact_keys, proximity_keys, strict=True)
    ):
        for store, key in (
            (database.exact, exact_key),
            (database.proximity, proximity_key),
        ):
            records.append(
                _effect_for(
                    store,
                    IndexOperation.INSERT,
                    10 + len(records),
                    key,
                    RecordRef(ordinal + 1, 1),
                )
            )
            expected.append((store.name, key))
    observed: list[tuple[str, bytes]] = []
    original = IndexStore._apply_common_replay_hot_change

    def ordered(
        store: IndexStore,
        bucket: object,
        change: IndexChange,
        lsn: int,
    ) -> bool:
        observed.append((store.name, change.key))
        return original(store, bucket, change, lsn)  # type: ignore[arg-type]

    monkeypatch.setattr(IndexStore, "_apply_common_replay_hot_change", ordered)

    CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert observed == expected


def test_hot_bucket_matches_legacy_bytes_first_fit_and_derived_counters() -> None:
    batched = build_database(name="hot-batched", budget_pages=64)
    legacy = build_database(name="hot-legacy", budget_pages=64)
    keys = _keys_in_bucket(
        batched.proximity,
        0,
        20,
        prefix=b"wide-hot-",
        width=96,
    )
    refs = tuple(RecordRef(100 + ordinal, 1) for ordinal in range(len(keys)))
    operations: list[tuple[IndexOperation, int]] = [
        *((IndexOperation.INSERT, ordinal) for ordinal in range(12)),
        *((IndexOperation.TOMBSTONE, ordinal) for ordinal in range(4)),
        *((IndexOperation.REMOVE, ordinal) for ordinal in range(4)),
        *((IndexOperation.INSERT, ordinal) for ordinal in range(12, 16)),
        (IndexOperation.TOMBSTONE, 16),
        (IndexOperation.REMOVE, 17),
    ]

    def records_for(store: IndexStore) -> tuple[WalRecord, ...]:
        return tuple(
            _effect_for(store, operation, 10 + offset, keys[ordinal], refs[ordinal])
            for offset, (operation, ordinal) in enumerate(operations)
        )

    # Seed the lazy diagnostic so every live/dead transition must maintain it incrementally.
    assert batched.proximity._tombstone_backlog() == 0  # noqa: SLF001
    assert legacy.proximity._tombstone_backlog() == 0  # noqa: SLF001
    batched_redo = CommitRedo(batched.pool, batched.manager)
    legacy_redo = CommitRedo(legacy.pool, _LegacyManager(legacy.manager))  # type: ignore[arg-type]
    batched_result = batched_redo.apply(_replay(records_for(batched.proximity)))
    legacy_result = legacy_redo.apply(_replay(records_for(legacy.proximity)))
    batched_redo.flush(batched_result)
    legacy_redo.flush(legacy_result)
    once = batched.entries()
    batched_redo.flush(
        batched_redo.apply(_replay(records_for(batched.proximity)))
    )
    legacy_redo.flush(legacy_redo.apply(_replay(records_for(legacy.proximity))))

    assert batched.entries() == once == legacy.entries()
    assert batched.proximity.header == legacy.proximity.header
    assert batched.proximity.missing_targets == legacy.proximity.missing_targets == 4
    assert batched.proximity._tombstone_backlog_count == 0  # noqa: SLF001
    assert legacy.proximity._tombstone_backlog_count == 0  # noqa: SLF001
    assert batched.device.page_count(batched.proximity.file) == legacy.device.page_count(
        legacy.proximity.file
    )
    assert tuple(
        batched.device.raw_page(batched.proximity.file, page)
        for page in range(batched.device.page_count(batched.proximity.file))
    ) == tuple(
        legacy.device.raw_page(legacy.proximity.file, page)
        for page in range(legacy.device.page_count(legacy.proximity.file))
    )


def test_duplicate_physical_target_declines_the_hot_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    key = _keys_in_bucket(database.exact, 0, 1, prefix=b"duplicate-")[0]
    ref = RecordRef(77, 1)
    entry = IndexEntry(key=key, ref=ref, versioned=False)
    pages = database.exact._bucket_pages(0)  # noqa: SLF001
    assert database.exact._place(pages, entry, 1) is True  # noqa: SLF001
    assert database.exact._place(pages, entry, 2) is True  # noqa: SLF001
    records = tuple(
        _effect_for(database.exact, IndexOperation.TOMBSTONE, 10 + offset, key, ref)
        for offset in range(8)
    )
    scalar_calls = 0
    original = IndexStore._apply_change

    def counted(store: IndexStore, change: IndexChange, lsn: int) -> bool:
        nonlocal scalar_calls
        scalar_calls += 1
        return original(store, change, lsn)

    monkeypatch.setattr(IndexStore, "_apply_change", counted)

    CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert scalar_calls == len(records)
    matching = tuple(entry for entry in database.exact.walk() if entry.matches(key, ref))
    assert len(matching) == 2
    assert sum(entry.live for entry in matching) == 1


def test_hot_preparation_materializes_entries_only_for_target_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database(budget_pages=64)
    keys = _keys_in_bucket(database.exact, 0, 21, prefix=b"scan-target-")
    refs = tuple(RecordRef(100 + ordinal, 1) for ordinal in range(len(keys)))
    for ordinal in range(20):
        ref = refs[7] if ordinal == 0 else refs[ordinal]
        assert database.exact._apply_change(  # noqa: SLF001
            IndexChange(
                index=database.exact.name,
                operation=IndexOperation.INSERT,
                key=keys[ordinal],
                ref=ref,
                versioned=False,
            ),
            1 + ordinal,
        )
    original_init = IndexEntry.__init__
    materialized: list[tuple[bytes, RecordRef]] = []

    def counted_init(self: IndexEntry, *args: object, **kwargs: object) -> None:
        key = kwargs.get("key")
        ref = kwargs.get("ref")
        assert isinstance(key, bytes)
        assert isinstance(ref, RecordRef)
        materialized.append((key, ref))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(IndexEntry, "__init__", counted_init)
    target = (keys[7], refs[7])
    missing = (keys[20], refs[20])

    prepared = database.exact._prepare_common_replay_hot_bucket(  # noqa: SLF001
        0,
        {target, missing},
        page_limit=100,
    )

    assert prepared is not None
    assert materialized == [target]
    assert prepared.entries[target] is not None
    assert prepared.entries[missing] is None


def test_late_hot_bucket_corruption_refuses_before_an_earlier_bucket_mutates() -> None:
    database = build_database()
    first_keys = _keys_in_bucket(database.exact, 0, 8, prefix=b"first-hot-")
    corrupt_keys = _keys_in_bucket(database.exact, 1, 8, prefix=b"corrupt-hot-")
    records = tuple(
        _effect_for(
            database.exact,
            IndexOperation.INSERT,
            10 + offset,
            key,
            RecordRef(offset + 1, 1),
        )
        for offset, key in enumerate((*first_keys, *corrupt_keys))
    )
    corrupt_page = database.exact._bucket_head(1)  # noqa: SLF001
    with database.pool.pinned(database.exact.file, corrupt_page) as page:
        page.insert_slot(b"not-an-index-entry")

    with pytest.raises(GrafxCorruptionDetected):
        CommitRedo(database.pool, database.manager).apply(_replay(records))

    for page_index in database.exact._bucket_pages(0):  # noqa: SLF001
        with database.pool.pinned(database.exact.file, page_index) as page:
            assert not page.live_slots()


def test_hot_bucket_partial_failure_marks_stale_without_scalar_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    keys = _keys_in_bucket(database.exact, 0, 8, prefix=b"fail-hot-")
    records = tuple(
        _effect_for(
            database.exact,
            IndexOperation.INSERT,
            10 + offset,
            key,
            RecordRef(offset + 1, 1),
        )
        for offset, key in enumerate(keys)
    )
    original_hot = IndexStore._apply_common_replay_hot_change
    hot_calls = 0
    scalar_calls = 0

    def fail_second(
        store: IndexStore,
        bucket: object,
        change: IndexChange,
        lsn: int,
    ) -> bool:
        nonlocal hot_calls
        hot_calls += 1
        if hot_calls == 2:
            raise GrafxIndexError("injected hot replay failure", field="injected")
        return original_hot(store, bucket, change, lsn)  # type: ignore[arg-type]

    def unexpected_scalar(
        _store: IndexStore, _change: IndexChange, _lsn: int
    ) -> bool:
        nonlocal scalar_calls
        scalar_calls += 1
        return False

    monkeypatch.setattr(IndexStore, "_apply_common_replay_hot_change", fail_second)
    monkeypatch.setattr(IndexStore, "_apply_change", unexpected_scalar)

    with pytest.raises(GrafxIndexError, match="injected hot replay failure"):
        CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert hot_calls == 2
    assert scalar_calls == 0
    assert database.exact.stale is True


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    (
        ("_COMMON_REPLAY_HOT_TARGET_LIMIT", 7),
        ("_COMMON_REPLAY_HOT_PAGE_LIMIT", 0),
        ("_COMMON_REPLAY_HOT_BUCKET_LIMIT", 0),
    ),
)
def test_hot_directory_budget_declines_before_scalar_mutation(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
) -> None:
    database = build_database()
    keys = _keys_in_bucket(database.exact, 0, 8, prefix=b"bounded-hot-")
    records = tuple(
        _effect_for(
            database.exact,
            IndexOperation.INSERT,
            10 + offset,
            key,
            RecordRef(offset + 1, 1),
        )
        for offset, key in enumerate(keys)
    )
    scalar_calls = 0
    original = IndexStore._apply_change

    def counted(store: IndexStore, change: IndexChange, lsn: int) -> bool:
        nonlocal scalar_calls
        scalar_calls += 1
        return original(store, change, lsn)

    monkeypatch.setattr(index_manager_module, limit_name, limit)
    monkeypatch.setattr(IndexStore, "_apply_change", counted)

    CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert scalar_calls == len(records)
    assert len(tuple(database.exact.walk())) == len(records)


def test_pagefull_remains_authoritative_for_a_hot_first_fit_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    keys = _keys_in_bucket(database.exact, 0, 8, prefix=b"pagefull-hot-")
    records = tuple(
        _effect_for(
            database.exact,
            IndexOperation.INSERT,
            10 + offset,
            key,
            RecordRef(offset + 1, 1),
        )
        for offset, key in enumerate(keys)
    )
    head = database.exact._bucket_head(0)  # noqa: SLF001
    original_insert = Page.insert_slot
    refused = False
    scalar_calls = 0

    def refuse_first_hint(page: Page, payload: bytes) -> int:
        nonlocal refused
        if not refused and page.page_index == head:
            refused = True
            raise PageFullError("injected authoritative page refusal", page=head)
        return original_insert(page, payload)

    def unexpected_scalar(
        _store: IndexStore, _change: IndexChange, _lsn: int
    ) -> bool:
        nonlocal scalar_calls
        scalar_calls += 1
        return False

    monkeypatch.setattr(Page, "insert_slot", refuse_first_hint)
    monkeypatch.setattr(IndexStore, "_apply_change", unexpected_scalar)

    CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert refused is True
    assert scalar_calls == 0
    assert len(tuple(database.exact.walk())) == len(records)
