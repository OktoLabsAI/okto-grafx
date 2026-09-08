"""Bounded validation of already-published physical history, not crash-cut redo."""
from __future__ import annotations

import math

import pytest

from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected, GrafxRecoveryRefused
from okto_grafx.domain.page import Page
from okto_grafx.domain.recovery.decision import committed_replay
from okto_grafx.domain.txn.commit_catalog import MAX_COMMIT_RECORD_BYTES
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.commit_catalog_store import (
    COMMIT_DIRECTORY_FILE as DIR, COMMIT_STREAM_FILE as DATA, CommitCatalogStore,
)

from test_commit_catalog_store import UUID, Images, entry, mutate_slot, stack


def published(count: int = 1, *, page_size: int = 512, large: bool = False) -> tuple[Images, CommitCatalogStore, int, dict[str, int]]:
    images, store = stack(page_size)
    for i in range(count):
        sequence = 10 + 2 * i
        plan = store.plan_append(entry(sequence, large=large and i == count - 1))
        images.apply(plan)
        touched = {(image.file, image.page_index) for image in plan.images}
        if i == 0:
            # The first native append includes the immutable stream header.
            touched.add((DATA, 0))
        for address in touched:
            page = Page.from_bytes(images.pages[address])
            page.page_lsn = sequence
            page.seq = 2
            images.pages[address] = page.to_bytes()
    sizes = {file: (1 + max(index for name, index in images.pages if name == file)) * page_size for file in (DIR, DATA)}
    return images, store, 8 + 2 * count, sizes


@pytest.mark.parametrize("page_size", [512, 8192])
@pytest.mark.parametrize("count", [1, 8, 14, 128])
@pytest.mark.parametrize("large", [False, True])
def test_published_head_validation_has_history_independent_read_bound(page_size: int, count: int, large: bool) -> None:
    images, store, sequence, sizes = published(count, page_size=page_size, large=large)
    before = dict(images.pages)
    images.reads.clear()
    head = store.validate_published_head(sequence=sequence, activation_sequence=7, file_size=sizes.__getitem__)
    assert head.entry_count == count and head.last_sequence == sequence
    bound = math.ceil(MAX_COMMIT_RECORD_BYTES / (page_size - 76)) + 5 if large else 6
    assert len(images.reads) <= bound
    assert images.pages == before


@pytest.mark.parametrize("file", [DIR, DATA])
@pytest.mark.parametrize("size_kind", ["missing", "partial", "truncated", "trailing", "bool"])
def test_physical_extent_mismatch_is_not_history_absence(file: str, size_kind: str) -> None:
    images, store, sequence, sizes = published(14)
    values = {"missing": 0, "partial": sizes[file] - 1, "truncated": sizes[file] - 512,
              "trailing": sizes[file] + 512, "bool": True}
    sizes[file] = values[size_kind]
    before = dict(images.pages)
    with pytest.raises(GrafxCorruptionDetected) as failure:
        store.validate_published_head(sequence=sequence, activation_sequence=7, file_size=sizes.__getitem__)
    assert failure.value.details["field"] == "physical_extent"
    assert images.pages == before


@pytest.mark.parametrize("damage", ["uuid", "activation", "directory_stamp", "stream_header_stamp", "first_directory_stamp", "tail_stamp", "record_crc", "missing_tail", "cross_page_clock"])
def test_checksum_valid_physical_boundary_damage_refuses(damage: str) -> None:
    images, store, sequence, sizes = published(14)
    tail = max(index for file, index in images.pages if file == DATA)
    if damage == "uuid":
        mutate_slot(images, DIR, 0, 1, 12, bytes(16))
    elif damage == "activation":
        for file in (DIR, DATA):
            mutate_slot(images, file, 0, 1, 28, (8).to_bytes(8, "little"))
    elif damage == "record_crc":
        raw = Page.from_bytes(images.pages[DATA, tail]).read_slot(1)
        mutate_slot(images, DATA, tail, 1, len(raw) - 1, bytes([raw[-1] ^ 1]))
    elif damage == "missing_tail":
        del images.pages[DATA, tail]
    elif damage == "cross_page_clock":
        mutate_slot(images, DIR, 1, 1, 12 * 32 + 24, sequence.to_bytes(8, "little", signed=True))
    else:
        address = {"directory_stamp": (DIR, 0), "stream_header_stamp": (DATA, 0),
                   "first_directory_stamp": (DIR, 1), "tail_stamp": (DATA, tail)}[damage]
        page = Page.from_bytes(images.pages[address])
        page.page_lsn = sequence - 1
        images.pages[address] = page.to_bytes()
    with pytest.raises(GrafxCorruptionDetected):
        store.validate_published_head(sequence=sequence, activation_sequence=7, file_size=sizes.__getitem__)


@pytest.mark.parametrize("sequence,activation", [(True, 7), (0, 7), (7, 7), (10, True)])
def test_published_coordinates_are_admitted_before_any_provider(sequence: int, activation: int) -> None:
    images, store, _sequence, _sizes = published()
    images.reads.clear()

    def forbidden_size(file: str) -> int:
        raise AssertionError("Coordinate admission must precede storage calls.")

    with pytest.raises(GrafxConfigurationError):
        store.validate_published_head(sequence=sequence, activation_sequence=activation, file_size=forbidden_size)
    assert images.reads == []


@pytest.mark.parametrize("uuid", [UUID, bytes(16), None])
def test_native_idle_preflight_requires_qualified_published_history(uuid: bytes | None) -> None:
    images, _store, sequence, sizes = published(14)
    device = MemoryStorageDevice(page_size=512)
    try:
        pool = BufferPool(device, PageCodecV1(page_size=512), NoOpMetricsSink(), budget_bytes=8192, db_label="physical-history-test")
        catalog = CatalogStore(pool)
        catalog.bootstrap()
        schema = catalog.read_from_pages().upgrade_index_catalog().enable_commit_catalog(7)
        catalog.adopt(schema)
        catalog.save()
        pool.flush()
        for file, size in sizes.items():
            device.create(file)
            device.allocate(file, size // 512)
        for (file, index), raw in images.pages.items():
            device.write_page(file, index, raw)
        before = {name: device.read_log(name, 0, device.file_size(name)) for name in device.list_files("")}
        redo = CommitRedo(pool, database_uuid=uuid)
        replay = committed_replay(())
        if uuid == UUID:
            passage = object()
            proof = redo.preflight(replay, _checkpoint_lsn=sequence, _passage=passage)
            assert redo.apply(replay, _checkpoint_lsn=sequence, _passage=passage, _preflighted=proof).effects_replayed == 0
        else:
            with pytest.raises((GrafxCorruptionDetected, GrafxRecoveryRefused)):
                redo.preflight(replay, _checkpoint_lsn=sequence)
        after = {name: device.read_log(name, 0, device.file_size(name)) for name in device.list_files("")}
        assert after == before
    finally:
        device.close()
