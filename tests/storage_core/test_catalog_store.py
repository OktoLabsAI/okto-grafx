"""The catalog file: ordinary pages, a rewritable chain, and an idempotent redo.

The catalog is persisted through the buffer pool as normal pages on purpose, so that the log of
C4 covers it and the recovery of C6 replays it with the same rule as any other page. The two
properties that matter downstream are proved here: a save is a rewrite of the same pages rather
than an ever-growing file, and applying a page image twice leaves the file exactly as applying
it once did.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import (
    HEADER_PAGE_INDEX,
    FileHeaderPage,
    FileKind,
    Page,
    PageType,
)
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore

from .conftest import MemoryDevice, RecordingMetrics, make_pool


def table(name: str, table_id: int, columns: int = 2) -> TableDef:
    """Return a node table with a controllable number of columns."""
    return TableDef(
        table_id=table_id,
        name=name,
        kind="node",
        columns=tuple(
            ColumnDef(name=f"column_{index}", type=ValueType.STRING) for index in range(columns)
        ),
    )


def test_bootstrapping_creates_the_file_the_header_page_and_an_empty_catalog(
    pool: BufferPool,
) -> None:
    store = CatalogStore(pool)
    assert not store.is_bootstrapped()
    catalog = store.bootstrap()
    assert store.is_bootstrapped()
    assert catalog.is_empty()
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        header = FileHeaderPage.read(page)
    assert page.page_type == int(PageType.META)
    assert header.kind is FileKind.CATALOG
    assert header.page_size == pool.page_size
    assert header.root_page != NO_PAGE


def test_bootstrapping_an_existing_catalog_loads_it_instead_of_replacing_it(
    pool: BufferPool,
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    reopened = CatalogStore(pool)
    loaded = reopened.bootstrap()
    assert loaded.has_table("Person")
    assert loaded == store.catalog


def test_a_catalog_round_trips_through_its_file(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_space(
        EmbeddingSpaceDef(
            space_id=1,
            name="minilm",
            dimension=384,
            metric=DistanceMetric.COSINE,
            normalized=True,
            created_at_wall=1_700_000_000.0,
        )
    )
    store.catalog.add_table(
        TableDef(
            table_id=1,
            name="Chunk",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="embedding", type=ValueType.VECTOR_F32, vector_space="minilm"),
            ),
            primary_key="id",
        )
    )
    store.save()
    pool.flush()
    pool.invalidate()
    reopened = CatalogStore(pool)
    loaded = reopened.load()
    assert loaded == store.catalog
    assert loaded.space("minilm").dimension == 384
    assert loaded.table("Chunk").column("embedding").vector_space == "minilm"


def test_a_retired_space_survives_the_file(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_space(
        EmbeddingSpaceDef(
            space_id=1,
            name="e5",
            dimension=8,
            metric=DistanceMetric.DOT,
            normalized=False,
        )
    )
    store.catalog.retire_space("e5")
    store.save()
    pool.flush()
    pool.invalidate()
    loaded = CatalogStore(pool).load()
    assert not loaded.space("e5").is_active


def test_a_catalog_larger_than_one_page_spans_a_chain(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    for index in range(1, 25):
        store.catalog.add_table(table(f"Table_{index}", index, columns=6))
    store.save()
    pages = store.chain_pages()
    assert len(pages) > 3
    assert len(store.catalog.serialize()) > store.chunk_capacity
    pool.flush()
    pool.invalidate()
    assert CatalogStore(pool).load() == store.catalog


def test_saving_again_rewrites_the_same_pages(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    for index in range(1, 12):
        store.catalog.add_table(table(f"Table_{index}", index, columns=4))
    first = store.save()
    size = pool.storage.page_count(store.file)
    store.catalog.add_table(table("One_more", 99))
    second = store.save()
    assert second[: len(first)] == first
    assert pool.storage.page_count(store.file) <= size + len(second) - len(first) + 1
    pool.flush()
    pool.invalidate()
    assert CatalogStore(pool).load() == store.catalog


def test_a_catalog_that_shrinks_keeps_reading_correctly(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    for index in range(1, 20):
        store.catalog.add_table(table(f"Table_{index}", index, columns=6))
    store.save()
    long_chain = store.chain_pages()
    smaller = CatalogStore(pool)
    smaller.load()
    smaller._catalog = Catalog()
    smaller.catalog.add_table(table("Only", 1))
    smaller.save()
    assert len(smaller.chain_pages()) < len(long_chain)
    pool.flush()
    pool.invalidate()
    loaded = CatalogStore(pool).load()
    assert [entry.name for entry in loaded.tables()] == ["Only"]


def test_loading_a_catalog_that_was_never_created_is_refused(pool: BufferPool) -> None:
    store = CatalogStore(pool, file="absent.dat")
    with pytest.raises(GrafxCorruptionDetected):
        store.load()
    with pytest.raises(GrafxCorruptionDetected):
        store.save()
    assert store.chain_pages() == ()


def test_a_budget_too_small_for_the_catalog_is_refused(device: MemoryDevice) -> None:
    tight = make_pool(device, RecordingMetrics(), budget_pages=1)
    with pytest.raises(GrafxConfigurationError):
        CatalogStore(tight)


def test_a_corrupted_catalog_page_is_reported_rather_than_guessed(
    pool: BufferPool, device: MemoryDevice
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    pool.flush()
    pool.invalidate()
    root = store.chain_pages()[0]
    damaged = bytearray(device.raw_page(store.file, root))
    damaged[40] ^= 0xFF
    device.poke_page(store.file, root, bytes(damaged))
    pool.invalidate()
    with pytest.raises(GrafxCorruptionDetected):
        CatalogStore(pool).load()


# --- the redo path -----------------------------------------------------------------------------


def image_of(pool: BufferPool, file: str, page_index: int) -> bytes:
    """Return the encoded image of a page as recovery would have captured it."""
    with pool.pinned(file, page_index) as page:
        return pool.codec.encode_page(page)


def test_applying_a_newer_image_installs_it(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    root = store.chain_pages()[0]
    with pool.pinned(store.file, root) as page:
        page.page_lsn = 100
    newer = Page.from_bytes(image_of(pool, store.file, root), page_size=pool.page_size)
    newer.page_lsn = 200
    newer.clear()
    newer.page_type = int(PageType.CATALOG)
    newer.insert_slot(b"a replayed catalog chunk")
    assert store.apply_page_image(root, pool.codec.encode_page(newer)) is True
    with pool.pinned(store.file, root) as page:
        assert page.page_lsn == 200
        assert page.read_slot(0) == b"a replayed catalog chunk"


def test_applying_the_same_image_twice_changes_nothing_the_second_time(
    pool: BufferPool,
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    root = store.chain_pages()[0]
    with pool.pinned(store.file, root) as page:
        page.page_lsn = 50
    replayed = Page.from_bytes(image_of(pool, store.file, root), page_size=pool.page_size)
    replayed.page_lsn = 150
    image = pool.codec.encode_page(replayed)
    assert store.apply_page_image(root, image) is True
    assert store.apply_page_image(root, image) is False
    with pool.pinned(store.file, root) as page:
        assert page.page_lsn == 150


def test_an_older_image_is_refused_so_replay_never_moves_backwards(
    pool: BufferPool,
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.save()
    root = store.chain_pages()[0]
    with pool.pinned(store.file, root) as page:
        page.page_lsn = 500
        page.insert_slot(b"the current content")
    older = Page.from_bytes(image_of(pool, store.file, root), page_size=pool.page_size)
    older.page_lsn = 100
    assert store.apply_page_image(root, pool.codec.encode_page(older)) is False
    with pool.pinned(store.file, root) as page:
        assert page.page_lsn == 500


def test_applying_an_image_grows_the_file_when_the_page_is_missing(
    pool: BufferPool,
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.save()
    beyond = pool.storage.page_count(store.file) + 3
    fresh = Page(int(PageType.CATALOG), page_size=pool.page_size, page_index=beyond)
    fresh.page_lsn = 900
    fresh.insert_slot(b"a page the crash never wrote")
    assert store.apply_page_image(beyond, pool.codec.encode_page(fresh)) is True
    assert pool.storage.page_count(store.file) == beyond + 1
    with pool.pinned(store.file, beyond) as page:
        assert page.read_slot(0) == b"a page the crash never wrote"
        assert page.page_lsn == 900


def test_applying_a_damaged_image_is_refused(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.save()
    root = store.chain_pages()[0]
    damaged = bytearray(image_of(pool, store.file, root))
    damaged[40] ^= 0xFF
    with pytest.raises(GrafxCorruptionDetected):
        store.apply_page_image(root, bytes(damaged))


def test_the_catalog_can_be_reloaded_after_a_replay(pool: BufferPool) -> None:
    source = CatalogStore(pool)
    source.bootstrap()
    source.catalog.add_table(table("Person", 1))
    source.save()
    pool.flush()
    images = {
        index: image_of(pool, source.file, index)
        for index in range(pool.storage.page_count(source.file))
    }

    # A second database replays exactly those page images and ends up with the same catalog.
    replica_pool = make_pool(MemoryDevice(), RecordingMetrics(), db_label="replica")
    replica = CatalogStore(replica_pool)
    replica.bootstrap()
    for index, image in images.items():
        page = Page.from_bytes(image, page_size=replica_pool.page_size)
        page.page_lsn = 1000
        replica.apply_page_image(index, replica_pool.codec.encode_page(page))
    assert replica.load() == source.catalog


def test_the_repr_says_what_it_holds(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    assert "catalog.dat" in repr(store)
    assert "Catalog(tables=0, spaces=0)" in repr(store)
