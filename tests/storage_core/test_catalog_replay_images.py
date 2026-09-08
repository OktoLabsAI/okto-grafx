"""Native preflight proves whole schema-catalog after-images before any page apply."""
from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxRecoveryRefused
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import FileHeaderPage, Page, PageType
from okto_grafx.domain.recovery.decision import committed_replay
from okto_grafx.domain.txn.records import encode_page_write_record
from okto_grafx.domain.wal.commit import CommitPayload
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore, read_catalog_page_images
from okto_grafx.engine.commit_redo import CommitRedo

from .conftest import MemoryDevice, RecordingMetrics, make_pool


def catalog(horizon: int | None, *, large: bool = False) -> Catalog:
    result = Catalog().upgrade_index_catalog()
    if horizon is not None:
        result.enable_commit_catalog(horizon)
    if large:
        result.add_table(TableDef(table_id=1, name="Large", kind="node", columns=tuple(
            ColumnDef(name=f"field_{i}", type=ValueType.STRING) for i in range(100)
        )))
    return result


def staged(value: Catalog, sequence: int, *, old: Catalog | None = None) -> tuple[BufferPool, CatalogStore, tuple[tuple[int, bytes], ...]]:
    device = MemoryDevice(page_size=512)
    pool = make_pool(device, RecordingMetrics())
    store = CatalogStore(pool)
    store.bootstrap()
    if old is not None:
        store.adopt(old)
        store.save()
    result = []
    for index, raw in store.stage(value):
        page = Page.from_bytes(raw)
        page.page_lsn = sequence
        page.seq = 2
        result.append((index, page.to_bytes()))
    return pool, store, tuple(result)


def records(images: tuple[tuple[int, bytes], ...], sequence: int) -> tuple[WalRecord, ...]:
    result = []
    for i, (index, raw) in enumerate(images):
        encoded = encode_page_write_record("catalog.dat", index, raw, compress=True)
        result.append(WalRecord(WalRecordType.WRITE_PAGE, encoded.payload,
            lsn=sequence-len(images)+i, epoch=sequence, txn_id=7,
            format_version=encoded.format_version, flags=encoded.flags))
    result.append(WalRecord(WalRecordType.COMMIT, CommitPayload.build(snapshot_lsn=0).encode(),
        lsn=sequence, epoch=sequence, txn_id=7))
    return tuple(result)


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("shrink", [False, True])
def test_complete_staged_catalog_decodes_without_host_reads_or_adoption(large: bool, shrink: bool) -> None:
    value = catalog(1000, large=large)
    pool, store, images = staged(value, 1000, old=catalog(None, large=True) if shrink else None)
    device = pool.storage
    assert isinstance(device, MemoryDevice)
    before = store.persisted_image()
    writes = list(device.write_calls)
    device.read_calls.clear()
    assert read_catalog_page_images(images, page_size=512, sequence=1000).serialize() == value.serialize()
    assert device.read_calls == [] and device.write_calls == writes
    assert store.persisted_image() == before


@pytest.mark.parametrize("damage", ["missing_header", "missing_chunk", "cycle", "extra_live", "stamp", "length", "duplicate"])
@pytest.mark.parametrize("door", ["preflight", "apply"])
def test_native_preflight_refuses_partial_catalog_before_any_apply(damage: str, door: str, monkeypatch: pytest.MonkeyPatch) -> None:
    pool, _store, images = staged(catalog(1000, large=True), 1000)
    offered = list(images)
    if damage == "missing_header":
        offered = [pair for pair in offered if pair[0] != 0]
    elif damage == "missing_chunk":
        offered = offered[1:]
    elif damage == "duplicate":
        offered.append(offered[0])
    else:
        position = -1 if damage == "length" else 0
        index, raw = offered[position]
        page = Page.from_bytes(raw)
        if damage == "cycle":
            page.next_page = index
        elif damage == "stamp":
            page.page_lsn = 999
        elif damage == "length":
            header = FileHeaderPage.read(page)
            FileHeaderPage.write(page, replace(header, payload_length=header.payload_length + 1))
        else:
            offered.append((99, raw))
        offered[position] = index, page.to_bytes()
    calls: list[int] = []

    def forbidden_apply(*args: object, **kwargs: object) -> bool:
        calls.append(1)
        raise AssertionError("Catalog preflight must finish before any effect applies.")

    monkeypatch.setattr("okto_grafx.engine.commit_redo.apply_page_image", forbidden_apply)
    replay = committed_replay(records(tuple(offered), 1000))
    with pytest.raises(GrafxCorruptionDetected):
        getattr(CommitRedo(pool), door)(replay)
    assert calls == []


@pytest.mark.parametrize("later_horizon", [None, 2001, 999])
def test_native_preflight_never_drops_or_retargets_an_observed_activation(later_horizon: int | None) -> None:
    pool, _store, first = staged(catalog(None), 1000)
    _pool2, _store2, activation = staged(catalog(2000), 2000)
    _pool3, _store3, later = staged(catalog(later_horizon), 3000)
    replay = committed_replay((*records(first, 1000), *records(activation, 2000), *records(later, 3000)))
    with pytest.raises(GrafxRecoveryRefused) as failure:
        CommitRedo(pool).apply(replay)
    assert failure.value.details["field"] == "commit_catalog_activation"


@pytest.mark.parametrize("horizon", [999, 2001])
def test_activation_from_a_legacy_snapshot_names_exactly_its_own_commit(horizon: int) -> None:
    pool, _store, first = staged(catalog(None), 1000)
    _pool2, _store2, activation = staged(catalog(horizon), 2000)
    replay = committed_replay((*records(first, 1000), *records(activation, 2000)))
    with pytest.raises(GrafxRecoveryRefused):
        CommitRedo(pool).apply(replay)


def test_valid_native_catalog_replay_can_start_from_a_torn_header_and_repeat() -> None:
    value = catalog(1000, large=True)
    pool, store, images = staged(value, 1000)
    device = pool.storage
    assert isinstance(device, MemoryDevice)
    pool.flush()
    pool.invalidate("catalog.dat")
    device.write_page("catalog.dat", 0, bytes(512))
    replay = committed_replay(records(images, 1000))
    redo = CommitRedo(pool)
    proof = redo.preflight(replay, _passage=store)
    result = redo.apply(replay, _preflighted=proof, _passage=store)
    assert result.page_images_applied == len(images)
    assert store.read_from_pages().serialize() == value.serialize()
    assert redo.apply(replay).page_images_applied == 0
    assert Page.from_bytes(images[-1][1]).next_page == NO_PAGE
    assert Page.from_bytes(images[0][1]).page_type == PageType.CATALOG


@pytest.mark.parametrize("include_legacy", [False, True])
def test_valid_catalog_horizon_survives_later_schema_change(include_legacy: bool) -> None:
    pool, store, first = staged(catalog(None), 1000)
    _pool2, _store2, activation = staged(catalog(2000), 2000)
    value = catalog(2000, large=True)
    _pool3, _store3, later = staged(value, 3000)
    prefix = records(first, 1000) if include_legacy else ()
    replay = committed_replay((*prefix, *records(activation, 2000), *records(later, 3000)))
    redo = CommitRedo(pool)
    proof = redo.preflight(replay)
    redo.apply(replay, _preflighted=proof)
    assert store.read_from_pages().serialize() == value.serialize()
    assert redo.apply(replay).page_images_applied == 0
