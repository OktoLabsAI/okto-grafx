"""Focused proofs for HNSW stores joining the composed logical replay batch (E-3).

Before this batch every ``VectorHnswIndex`` record replayed through the scalar per-record
protocol -- one page-0 read, one bucket mutation and one page-0 write per record -- because the
batch doors excluded any store overriding :meth:`IndexStore.apply`.  A store now declares, for its
exact type and for this moment, whether the batch protocol is complete for it: an HNSW store
without a published picture is, and it hears about the batch exactly once after the composed
header decision.  Everything the scalar protocol proved about bytes, order, failure and foreign
generations must still hold, which is what these tests pin down.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.index import IndexChange, IndexOperation
from okto_grafx.domain.index.header import INDEX_HEADER_SLOT, IndexHeader
from okto_grafx.domain.page import HEADER_PAGE_INDEX
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.wal.record import WalRecord
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.index_manager import INDEX_FLAG_STALE, IndexManager, IndexStore
from okto_grafx.engine.vector_engine import VectorHnswIndex

from .conftest import Database, build_database, header_on_device
from .test_common_replay_batch import _LegacyManager, _effect, _replay


def _vector_store(database: Database) -> VectorHnswIndex:
    """Register an HNSW store over the proximity definition, exactly as the engine would."""
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
        resolve=lambda _ref: (1, (1.0, 0.0)),
    )
    database.manager._indexes[vector.definition.registry_key] = vector  # noqa: SLF001
    return vector


def _mixed_records(
    database: Database, vector: VectorHnswIndex
) -> tuple[WalRecord, ...]:
    return (
        _effect(database.exact, IndexOperation.INSERT, 10, ordinal=1),
        _effect(vector, IndexOperation.INSERT, 11, ordinal=2),
        _effect(database.exact, IndexOperation.INSERT, 12, ordinal=3),
        _effect(vector, IndexOperation.INSERT, 13, ordinal=4),
    )


def _rewrite_device_header(database: Database, file: str, transform) -> IndexHeader:
    """Replace the index header on the DEVICE the way a foreign participant would, and drop the
    resident clean frame so the next pin sees the device, as a descriptor revalidation would."""
    codec = PageCodecV1(database.device.page_size)
    page = codec.decode_page(
        database.device.raw_page(file, HEADER_PAGE_INDEX), page_index=HEADER_PAGE_INDEX
    )
    foreign = transform(IndexHeader.decode(page.read_slot(INDEX_HEADER_SLOT)))
    page.update_slot(INDEX_HEADER_SLOT, foreign.encode())
    database.device.poke_page(file, HEADER_PAGE_INDEX, codec.encode_page(page))
    database.pool.discard_clean_page(file, HEADER_PAGE_INDEX)
    return foreign


def test_vector_store_without_picture_is_batched_once_per_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    vector = _vector_store(database)
    vector._live_count = 7  # noqa: SLF001 - a derived count the batch must forget
    records = _mixed_records(database, vector)
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    settled: list[bool] = []
    original_read = IndexStore._read_header
    original_write = IndexStore._write_header
    original_settled = VectorHnswIndex._batch_replay_settled

    def counted_read(store: IndexStore, *, proved_present: bool = False) -> IndexHeader:
        reads[store.name] = reads.get(store.name, 0) + 1
        return original_read(store, proved_present=proved_present)

    def counted_write(store: IndexStore, header: IndexHeader) -> None:
        writes[store.name] = writes.get(store.name, 0) + 1
        original_write(store, header)

    def scalar_forbidden(store: VectorHnswIndex, record: WalRecord) -> None:
        pytest.fail("an HNSW store without a picture must not replay record by record")

    def counted_settled(store: VectorHnswIndex, *, moved: bool) -> None:
        settled.append(moved)
        original_settled(store, moved=moved)

    monkeypatch.setattr(IndexStore, "_read_header", counted_read)
    monkeypatch.setattr(IndexStore, "_write_header", counted_write)
    monkeypatch.setattr(VectorHnswIndex, "apply", scalar_forbidden)
    monkeypatch.setattr(VectorHnswIndex, "_batch_replay_settled", counted_settled)

    report = CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert reads == {database.exact.name: 1, vector.name: 1}
    assert writes == {database.exact.name: 1, vector.name: 1}
    assert settled == [True]
    assert vector._live_count is None  # noqa: SLF001
    assert vector._snapshot is None  # noqa: SLF001
    assert len(tuple(database.exact.walk())) == 2
    assert len(tuple(vector.walk())) == 2
    assert vector.built_through_lsn == 13
    assert report.index_effects_dispatched == len(records)
    assert report.touched_files == (database.exact.file, vector.file)


def test_vector_store_with_a_published_picture_keeps_the_scalar_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    vector = _vector_store(database)
    vector.snapshot()
    records = _mixed_records(database, vector)
    scalar_calls = 0
    settled = 0
    original_apply = VectorHnswIndex.apply

    def counted_apply(store: VectorHnswIndex, record: WalRecord) -> None:
        nonlocal scalar_calls
        scalar_calls += 1
        original_apply(store, record)

    def counted_settled(store: VectorHnswIndex, *, moved: bool) -> None:
        nonlocal settled
        settled += 1

    monkeypatch.setattr(VectorHnswIndex, "apply", counted_apply)
    monkeypatch.setattr(VectorHnswIndex, "_batch_replay_settled", counted_settled)

    assert database.manager.apply_common_replay_batch(records[1:2]) is None
    CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert scalar_calls == 2
    assert settled == 0
    assert len(tuple(database.exact.walk())) == 2
    assert len(tuple(vector.walk())) == 2


def test_subclass_of_the_vector_store_never_declares_the_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Specialised(VectorHnswIndex):
        pass

    database = build_database()
    vector = _Specialised(
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
        resolve=lambda _ref: (1, (1.0, 0.0)),
    )
    database.manager._indexes[vector.definition.registry_key] = vector  # noqa: SLF001
    assert vector._batch_replay_admits() is False  # noqa: SLF001
    assert IndexManager._batch_replay_compatible(vector) is False  # noqa: SLF001
    scalar_calls = 0
    original_apply = VectorHnswIndex.apply

    def counted_apply(store: VectorHnswIndex, record: WalRecord) -> None:
        nonlocal scalar_calls
        scalar_calls += 1
        original_apply(store, record)

    monkeypatch.setattr(VectorHnswIndex, "apply", counted_apply)
    CommitRedo(database.pool, database.manager).apply(
        _replay(_mixed_records(database, vector))
    )
    assert scalar_calls == 2


def test_picture_born_during_the_batch_is_replaced_and_never_certified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    vector = _vector_store(database)
    generation_before = vector._graph_generation  # noqa: SLF001
    records = _mixed_records(database, vector)
    original_change = IndexStore._apply_change
    born = False

    def build_a_picture_mid_batch(
        store: IndexStore, change: IndexChange, lsn: int
    ) -> bool:
        nonlocal born
        moved = original_change(store, change, lsn)
        if store is vector and not born:
            born = True
            # A reader thread asks for the graph while buckets already moved and the header
            # has not: the picture it builds may be ahead of its mark.
            vector.snapshot()
            assert vector._snapshot is not None  # noqa: SLF001
        return moved

    def certify_forbidden(
        store: VectorHnswIndex, picture: object, mark: object
    ) -> None:
        pytest.fail("a picture born under an unmoved header must never be certified")

    monkeypatch.setattr(IndexStore, "_apply_change", build_a_picture_mid_batch)
    monkeypatch.setattr(VectorHnswIndex, "_certify", certify_forbidden)

    CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert born is True
    assert vector._snapshot is None  # noqa: SLF001
    assert vector._graph_generation is not generation_before  # noqa: SLF001
    assert len(tuple(vector.walk())) == 2


def test_batched_vector_replay_matches_the_scalar_protocol_byte_for_byte() -> None:
    batched = build_database(name="batched")
    legacy = build_database(name="legacy")
    batched_vector = _vector_store(batched)
    legacy_vector = _vector_store(legacy)

    def records(database: Database, vector: VectorHnswIndex) -> tuple[WalRecord, ...]:
        return (
            _effect(vector, IndexOperation.INSERT, 10, ordinal=1),
            _effect(database.exact, IndexOperation.INSERT, 11, ordinal=2),
            _effect(vector, IndexOperation.INSERT, 12, ordinal=3),
            _effect(vector, IndexOperation.TOMBSTONE, 13, ordinal=1, csn=13),
            _effect(database.exact, IndexOperation.TOMBSTONE, 14, ordinal=2, csn=14),
        )

    batched_redo = CommitRedo(batched.pool, batched.manager)
    legacy_redo = CommitRedo(legacy.pool, _LegacyManager(legacy.manager))  # type: ignore[arg-type]
    batched_redo.flush(batched_redo.apply(_replay(records(batched, batched_vector))))
    legacy_redo.flush(legacy_redo.apply(_replay(records(legacy, legacy_vector))))

    assert batched.entries() == legacy.entries()
    assert batched_vector.header == legacy_vector.header
    assert header_on_device(batched.device, batched_vector.file) == header_on_device(
        legacy.device, legacy_vector.file
    )
    page_count = batched.device.page_count(batched_vector.file)
    assert page_count == legacy.device.page_count(legacy_vector.file)
    for index in range(page_count):
        assert batched.device.raw_page(
            batched_vector.file, index
        ) == legacy.device.raw_page(legacy_vector.file, index), index


@pytest.mark.parametrize(
    ("label", "transform"),
    (
        (
            "generation",
            lambda header: replace(header, artifact_nonce=header.artifact_nonce + 1),
        ),
        (
            "stale",
            lambda header: replace(header, flags=header.flags | INDEX_FLAG_STALE),
        ),
    ),
)
def test_foreign_change_of_page_zero_between_effects_refuses_publication(
    monkeypatch: pytest.MonkeyPatch, label: str, transform
) -> None:
    database = build_database()
    vector = _vector_store(database)
    database.pool.flush(vector.file)
    records = (
        _effect(vector, IndexOperation.INSERT, 10, ordinal=1),
        _effect(vector, IndexOperation.INSERT, 11, ordinal=2),
    )
    original_change = IndexStore._apply_change
    foreign: list[IndexHeader] = []

    def swap_after_first_effect(
        store: IndexStore, change: IndexChange, lsn: int
    ) -> bool:
        moved = original_change(store, change, lsn)
        if not foreign:
            foreign.append(_rewrite_device_header(database, vector.file, transform))
        return moved

    monkeypatch.setattr(IndexStore, "_apply_change", swap_after_first_effect)

    with pytest.raises(GrafxCorruptionDetected, match="changed identity") as failure:
        CommitRedo(database.pool, database.manager).apply(_replay(records))

    assert failure.value.details["field"] == "replay_identity"
    assert vector.stale is True
    assert vector._snapshot is None  # noqa: SLF001
    on_device = header_on_device(database.device, vector.file)
    assert on_device.artifact_nonce == foreign[0].artifact_nonce, label
    assert on_device.flags & INDEX_FLAG_STALE
    # The composed image (built through 11) was never published over the foreign page.
    assert on_device.built_through_lsn != 11
