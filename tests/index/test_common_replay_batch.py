"""Focused proofs for the narrow common logical-index replay batch."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.ids import PROVISIONAL_CSN, RecordRef
from okto_grafx.domain.index import IndexChange, IndexOperation, wal_record_for
from okto_grafx.domain.index.header import IndexHeader
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
