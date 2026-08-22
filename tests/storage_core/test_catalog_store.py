"""The catalog file: ordinary pages, a rewritable chain, and an idempotent redo.

The catalog is persisted through the buffer pool as normal pages on purpose, so that the log of
C4 covers it and the recovery of C6 replays it with the same rule as any other page. The two
properties that matter downstream are proved here: a save is a rewrite of the same pages rather
than an ever-growing file, and applying a page image twice leaves the file exactly as applying
it once did.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDurabilityBarrierFailed,
    GrafxTransactionStateError,
)
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import (
    HEADER_PAGE_INDEX,
    FileHeader,
    FileHeaderPage,
    FileKind,
    Page,
    PageType,
)
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.engine import catalog_store as catalog_module
from okto_grafx.engine.buffer_pool import (
    BARRIER_FAILURES_TOTAL,
    FSYNC_DURATION_SECONDS,
    BufferPool,
    apply_page_image,
)
from okto_grafx.engine.catalog_store import CatalogStore

from .conftest import MemoryDevice, RecordingMetrics, make_pool


@contextlib.contextmanager
def pin_ceiling(monkeypatch: pytest.MonkeyPatch, limit: int = 200) -> Iterator[None]:
    """Fail fast if a walk pins more pages than any bounded walk could need.

    Without this the only thing that stops a walk whose bound has been removed is the session
    timeout: a minute of wall clock per mutation, reported as a timeout rather than as the
    property that broke. The ceiling lives in the test, so the engine gains nothing for the sake
    of being tested.
    """
    counted = {"pins": 0}
    original = BufferPool.pin

    def counting(self: BufferPool, file: str, page_index: int) -> object:
        counted["pins"] += 1
        if counted["pins"] > limit:
            raise AssertionError(
                f"the walk pinned more than {limit} pages, so it is not going to end"
            )
        return original(self, file, page_index)

    monkeypatch.setattr(BufferPool, "pin", counting)
    yield


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


def test_redo_installs_over_a_catalog_page_that_was_allocated_and_never_written(
    pool: BufferPool, device: MemoryDevice
) -> None:
    # The regression this covers: the page exists holding the zeros allocate left, the read spent
    # the whole torn-read budget on it, and apply_page_image died on a checksum of 0x00000000
    # before it ever reached the test that would have declared the page free.
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    root = store.chain_pages()[0]
    image = image_of(pool, store.file, root)
    pool.flush()
    pool.invalidate()
    unwritten = device.allocate(store.file, 1)
    assert device.raw_page(store.file, unwritten) == bytes(pool.page_size)
    assert store.apply_page_image(unwritten, image) is True
    with pool.pinned(store.file, unwritten) as page:
        assert page.page_type == int(PageType.CATALOG)
        assert page.read_slot(0) == Page.from_bytes(image, page_size=pool.page_size).read_slot(0)


def test_a_catalog_page_that_was_never_written_reads_as_free_rather_than_damaged(
    pool: BufferPool, device: MemoryDevice
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.save()
    unwritten = device.allocate(store.file, 1)
    pool.invalidate()
    with pool.pinned(store.file, unwritten) as page:
        assert page.page_type == int(PageType.FREE)
        assert page.slot_count == 0


# --- the reserved header page is an invariant of the catalog too --------------------------------


def test_a_catalog_grown_by_redo_before_bootstrap_can_still_be_opened(
    pool: BufferPool, device: MemoryDevice
) -> None:
    # The same shape as the heap case: recovery redoes a catalog page the file does not have,
    # the file grows past page 0, and page 0 is left free. Asking only whether the file has
    # pages makes bootstrap take the load path, which then refuses a page 0 that is not a header
    # page, and the database can never be opened again.
    store = CatalogStore(pool)
    replayed = Page(int(PageType.CATALOG), page_size=pool.page_size)
    replayed.page_lsn = 50
    replayed.insert_slot(b"a chunk the redo installed")
    assert store.apply_page_image(3, pool.codec.encode_page(replayed)) is True
    assert device.page_count(store.file) == 4
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.FREE)
    assert store.is_bootstrapped() is False

    catalog = store.bootstrap()
    assert catalog.is_empty()
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.META)
        assert FileHeaderPage.read(page).kind is FileKind.CATALOG
    store.catalog.add_table(table("Person", 1))
    store.save()
    pool.flush()
    pool.invalidate()
    assert CatalogStore(pool).load().has_table("Person")


def test_a_catalog_header_page_of_the_wrong_type_is_reported(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        page.page_type = int(PageType.CATALOG)
    for call in (store.load, store.save, store.chain_pages, store.is_bootstrapped):
        with pytest.raises(GrafxCorruptionDetected):
            call()


def test_bootstrap_never_overwrites_a_catalog_header_it_cannot_understand(
    pool: BufferPool,
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    root = store.chain_pages()[0]
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        page.page_type = int(PageType.HEAP)
    with pytest.raises(GrafxCorruptionDetected):
        store.bootstrap()
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.HEAP), "the damaged page was left as it was"
    with pool.pinned(store.file, root) as page:
        assert page.page_type == int(PageType.CATALOG), "and the chain it named is untouched"


def test_a_catalog_written_with_another_page_size_is_refused(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        FileHeaderPage.write(page, FileHeader(kind=FileKind.CATALOG, page_size=1024))
    with pytest.raises(GrafxCorruptionDetected) as raised:
        store.is_bootstrapped()
    assert raised.value.details["page_size"] == 1024


def test_a_catalog_header_page_of_another_kind_of_file_is_refused(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        FileHeaderPage.write(page, FileHeader(kind=FileKind.HEAP, page_size=pool.page_size))
    with pytest.raises(GrafxCorruptionDetected) as raised:
        store.load()
    assert raised.value.details["kind"] == "HEAP"


# --- M2: page 0 never enters a chain -------------------------------------------------------------


def repoint_root(pool: BufferPool, store: CatalogStore, root_page: int) -> None:
    """Repoint the catalog root at another page, which is four damaged bytes."""
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        header = FileHeaderPage.read(page)
        FileHeaderPage.write(
            page,
            FileHeader(
                kind=header.kind,
                page_size=header.page_size,
                format_version=header.format_version,
                root_page=root_page,
                payload_length=header.payload_length,
            ),
        )


def test_a_root_that_names_the_reserved_header_page_never_reaches_a_write(
    pool: BufferPool,
) -> None:
    # Four damaged bytes in an otherwise valid, correctly checksummed page used to become
    # permanent loss: the reserved page entered the reuse list, save() cleared it and stamped it
    # CATALOG, and the file header was gone for good (A2, G6).
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    repoint_root(pool, store, HEADER_PAGE_INDEX)

    for call in (store.save, store.chain_pages, store.load):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            call()
        # Naming the field is what separates this guard from the page-type check further down:
        # both refuse the same damage, and only one of them refuses it before a pin.
        assert raised.value.details["page"] == HEADER_PAGE_INDEX
        assert raised.value.details["field"] == "root_page"

    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.META), "the header page is untouched"
        assert FileHeaderPage.read(page).kind is FileKind.CATALOG
    assert store.is_bootstrapped() is True


def test_a_chain_that_points_back_at_page_zero_is_refused(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    for index in range(1, 12):
        store.catalog.add_table(table(f"Table_{index}", index, columns=4))
    pages = store.save()
    assert len(pages) > 1
    with pool.pinned(store.file, pages[0]) as page:
        page.next_page = HEADER_PAGE_INDEX
    with pytest.raises(GrafxCorruptionDetected) as raised:
        store.save()
    assert raised.value.details["field"] == "next_page"
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.META)


def test_a_chain_page_of_the_wrong_type_is_refused_before_it_is_rewritten(
    pool: BufferPool,
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    root = store.save()[0]
    with pool.pinned(store.file, root) as page:
        page.page_type = int(PageType.HEAP)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        store.save()
    assert raised.value.details["page"] == root
    with pool.pinned(store.file, root) as page:
        assert page.page_type == int(PageType.HEAP), "nothing was rewritten"


def test_the_chain_writer_itself_refuses_the_reserved_page(pool: BufferPool) -> None:
    # The guard lives at the single choke point, so every chain of every paged file inherits it.
    from okto_grafx.engine.buffer_pool import write_chain

    pool.storage.create("scratch.dat")
    pool.storage.allocate("scratch.dat", 3)
    with pytest.raises(GrafxCorruptionDetected):
        write_chain(pool, "scratch.dat", b"payload", reuse=(HEADER_PAGE_INDEX,))
    with pytest.raises(GrafxCorruptionDetected):
        write_chain(pool, "fresh.dat", b"payload")


# --- M3: the data-file barrier of amendment A25 ---------------------------------------------------


def test_a_checkpoint_barriers_the_catalog_inside_the_timed_block(
    pool: BufferPool, device: MemoryDevice, metrics: RecordingMetrics, trace: list[str]
) -> None:
    # A25 says the barrier is timed INTO the metric, so the test reads the order of what really
    # happened rather than the fact that a timer was opened and closed. Moving the barrier out
    # of the block leaves every enter/exit assertion true and this one false.
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    assert device.barriers == [], "a save is not a barrier"
    trace.clear()
    written = pool.checkpoint(store.file)
    assert written >= 1
    assert device.barriers == [store.file]
    assert trace == [
        f"time_enter:{FSYNC_DURATION_SECONDS}",
        "barrier",
        f"time_exit:{FSYNC_DURATION_SECONDS}",
    ]
    assert metrics.timed(FSYNC_DURATION_SECONDS) == [{"target": "data"}]
    assert metrics.values_of(BARRIER_FAILURES_TOTAL) == []


def test_a_barrier_that_fails_is_counted_and_re_raised(
    pool: BufferPool, device: MemoryDevice, metrics: RecordingMetrics, trace: list[str]
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()

    def refuse(file: str | None = None) -> None:
        raise GrafxDurabilityBarrierFailed("The platter did not answer.", file=file)

    device.durable_barrier = refuse  # type: ignore[method-assign]
    trace.clear()
    with pytest.raises(GrafxDurabilityBarrierFailed):
        pool.checkpoint(store.file)
    assert metrics.values_of(BARRIER_FAILURES_TOTAL) == [1.0]
    assert trace == [
        f"time_enter:{FSYNC_DURATION_SECONDS}",
        f"time_exit:{FSYNC_DURATION_SECONDS}",
    ], "the block a caller most wants timed is the one that failed"


def test_a_checkpoint_on_the_no_op_sink_still_barriers(device: MemoryDevice) -> None:
    from okto_grafx.adapters.metrics_noop import NoOpMetricsSink

    pool = make_pool(device, NoOpMetricsSink())
    store = CatalogStore(pool)
    store.bootstrap()
    pool.checkpoint()
    assert device.barriers == [None]


def test_a_catalog_page_zero_that_was_written_to_is_never_re_initialised(
    pool: BufferPool,
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        page.page_type = int(PageType.FREE)
    with pytest.raises(GrafxCorruptionDetected):
        store.bootstrap()
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        assert page.slot_count >= 1


def test_the_reserved_page_is_never_released_when_the_catalog_shrinks(
    pool: BufferPool,
) -> None:
    # _release clears and frees every page a shrinking chain gave up. Page 0 reaching that list
    # would clear the file header, so the refusal is the last line of the same defence.
    store = CatalogStore(pool)
    store.bootstrap()
    with pytest.raises(GrafxCorruptionDetected) as raised:
        store._release((HEADER_PAGE_INDEX,))
    assert raised.value.details["page"] == HEADER_PAGE_INDEX
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.META)
        assert FileHeaderPage.read(page).kind is FileKind.CATALOG


@pytest.mark.parametrize("shape", ["self loop", "two cycle", "long cycle"])
def test_the_catalog_chain_walk_refuses_a_cycle_rather_than_walking_it(
    pool: BufferPool, shape: str
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    for index in range(1, 16):
        store.catalog.add_table(table(f"Table_{index}", index, columns=6))
    pages = store.save()
    assert len(pages) >= 4
    if shape == "self loop":
        source, target = pages[0], pages[0]
    elif shape == "two cycle":
        source, target = pages[1], pages[0]
    else:
        source, target = pages[-1], pages[0]
    with pool.pinned(store.file, source) as page:
        page.next_page = target
    with pytest.raises(GrafxCorruptionDetected) as raised:
        store.chain_pages()
    assert raised.value.details["field"] == "cycle"
    with pytest.raises(GrafxCorruptionDetected):
        store.load()


def test_a_transient_barrier_failure_is_re_raised_unchanged_for_the_caller_to_classify(
    pool: BufferPool, device: MemoryDevice, metrics: RecordingMetrics
) -> None:
    """A28 puts the transient/permanent distinction in details, not in the exception class.

    C1 has exactly one caller of durable_barrier and it does not retry: it counts the failure and
    re-raises it untouched, so whatever reads it above can switch on details["retryable"]. A
    predicate that switched on the class instead would report an antivirus or indexer touch as
    permanent (A47), which is why this test asserts the details survive the counting.
    """
    store = CatalogStore(pool)
    store.bootstrap()

    def refuse(file: str | None = None) -> None:
        raise GrafxDurabilityBarrierFailed(
            "The platter did not answer.",
            file=file,
            reason="sharing_violation",
            winerror=32,
            attempts=3,
            retryable=True,
        )

    device.durable_barrier = refuse  # type: ignore[method-assign]
    with pytest.raises(GrafxDurabilityBarrierFailed) as raised:
        pool.checkpoint(store.file)
    # A28 speaks of details["retryable"], but GrafxError.__init__ takes `retryable` as a keyword
    # and sets the ATTRIBUTE with it, so it never reaches details at all. What survives the trip
    # is the attribute plus every other key, and that is what a caller can switch on.
    assert raised.value.retryable is True
    assert "retryable" not in raised.value.details
    assert raised.value.details["reason"] == "sharing_violation"
    assert raised.value.details["winerror"] == 32
    assert metrics.values_of(BARRIER_FAILURES_TOTAL) == [1.0]
    assert device.barriers == [], "the barrier never reported success"


def test_the_catalog_chain_still_ends_when_the_visited_set_fails(
    pool: BufferPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ForgetfulSet(set):
        def __contains__(self, item: object) -> bool:
            return False

    store = CatalogStore(pool)
    store.bootstrap()
    for index in range(1, 16):
        store.catalog.add_table(table(f"Table_{index}", index, columns=6))
    pages = store.save()
    with pool.pinned(store.file, pages[-1]) as page:
        page.next_page = pages[0]
    monkeypatch.setattr(catalog_module, "visited_pages", ForgetfulSet)
    with pin_ceiling(monkeypatch):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            store.chain_pages()
    assert raised.value.details["field"] == "chain_length"


def test_a_shrinking_catalog_actually_empties_the_pages_it_surrenders(
    pool: BufferPool, device: MemoryDevice
) -> None:
    """D4: asserting that the chain shortened is not asserting that the pages were released.

    Both remain true when the surrendered tail is never emptied, so the release could be deleted
    without a sound. What only the release produces is the state of the pages themselves: free,
    empty, and pointing nowhere.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    for index in range(1, 20):
        store.catalog.add_table(table(f"Table_{index}", index, columns=6))
    long_chain = store.save()
    assert len(long_chain) > 2

    smaller = CatalogStore(pool)
    smaller.load()
    smaller._catalog = Catalog()
    smaller.catalog.add_table(table("Only", 1))
    short_chain = smaller.save()
    assert len(short_chain) < len(long_chain)

    surrendered = [page for page in long_chain if page not in short_chain]
    assert surrendered, "the chain really did give pages back"
    for index in surrendered:
        with pool.pinned(smaller.file, index) as page:
            assert page.page_type == int(PageType.FREE), f"page {index} was not released"
            assert page.slot_count == 0, f"page {index} still carries a chunk"
            assert page.next_page == NO_PAGE, f"page {index} still points into the old chain"
    pool.flush()
    pool.invalidate()
    assert [entry.name for entry in CatalogStore(pool).load().tables()] == ["Only"]


def test_a_stored_catalog_whose_counters_are_below_its_own_identifiers_is_refused(
    pool: BufferPool,
) -> None:
    """D8: the counter check had no test at all.

    A counter below the ids already in use hands the next table an id that is taken, so the
    catalog would refuse its own next write. Nothing else on this path looks at the counters.
    """
    catalog = Catalog()
    catalog.add_table(table("Person", 7))
    catalog.add_space(
        EmbeddingSpaceDef(
            space_id=5,
            name="minilm",
            dimension=8,
            metric=DistanceMetric.COSINE,
            normalized=False,
        )
    )
    raw = bytearray(catalog.serialize())
    import struct as _struct

    from okto_grafx.domain.page import crc32c

    for offset, replacement in ((20, 1), (24, 1)):
        damaged = bytearray(raw)
        _struct.pack_into("<I", damaged, offset, replacement)
        body = bytes(damaged[: len(damaged) - 4])
        damaged[len(damaged) - 4 :] = crc32c(body).to_bytes(4, "little")
        with pytest.raises(GrafxCorruptionDetected) as raised:
            Catalog.deserialize(bytes(damaged))
        assert raised.value.details["field"] == "next_table_id"
        assert raised.value.details["next_table_id"] in (1, 8)


def test_a_stored_catalog_field_that_cannot_be_served_is_reported_as_damage(
    pool: BufferPool,
) -> None:
    """D10: on the load path a bad field is damaged bytes, not a configuration mistake.

    A11-revised reserves corruption_detected for damaged bytes precisely because FR-8 and FR-10
    turn it into truncation, quarantine and a forensic entry. The store already raises it for the
    cross-record invariants; a per-field refusal on the same path has to agree, or C6 sees two
    different classifications for one kind of damage.
    """
    import struct as _struct

    from okto_grafx.domain.page import crc32c

    catalog = Catalog()
    catalog.add_table(table("Person", 1))
    raw = bytearray(catalog.serialize())
    # The table id is the first field of the first table record, right after the preamble.
    _struct.pack_into("<I", raw, 28, 0)
    body = bytes(raw[: len(raw) - 4])
    raw[len(raw) - 4 :] = crc32c(body).to_bytes(4, "little")
    with pytest.raises(GrafxCorruptionDetected) as raised:
        Catalog.deserialize(bytes(raw))
    assert raised.value.code == "corruption_detected"
    assert raised.value.details["field"] == "table_id"


def test_a_catalog_chain_shorter_than_its_declared_length_is_refused(
    pool: BufferPool, device: MemoryDevice
) -> None:
    """G6: reachable through the redo door, and it had no test.

    A chain whose pages carry fewer bytes than the header says is a catalog that cannot be
    parsed; without the check the shortfall reaches deserialize as a truncated buffer.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    for index in range(1, 14):
        store.catalog.add_table(table(f"Table_{index}", index, columns=6))
    pages = store.save()
    assert len(pages) > 2

    # Recovery replays the first chain page as one that ends the chain early.
    with pool.pinned(store.file, pages[0]) as page:
        shortened = Page.from_bytes(pool.codec.encode_page(page), page_size=pool.page_size)
    shortened.page_lsn = page.page_lsn + 10
    shortened.next_page = NO_PAGE
    assert store.apply_page_image(pages[0], pool.codec.encode_page(shortened))

    with pytest.raises(GrafxCorruptionDetected) as raised:
        store.load()
    assert raised.value.details["field"] == "payload_length"
    assert raised.value.details["observed"] < raised.value.details["declared"]


def test_releasing_the_pages_of_a_shrinking_chain_says_the_structure_changed(
    pool: BufferPool,
) -> None:
    """S4 of the A66 enumeration: pages leave the chain, and something has to say so.

    The saying is done by write_chain, which rewrote those same pages for reuse a moment earlier;
    _release deliberately does not repeat it (A67). Nothing in C1 derives a walk over catalog.dat
    today, so the property has no consumer to observe it, which is why it is asserted on the
    public counter where it is produced rather than through a reader.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    for index in range(1, 20):
        store.catalog.add_table(table(f"Table_{index}", index, columns=6))
    long_chain = store.save()
    assert len(long_chain) > 2

    smaller = CatalogStore(pool)
    smaller.load()
    smaller._catalog = Catalog()
    smaller.catalog.add_table(table("Only", 1))
    before = pool.structure_epoch(smaller.file)
    short_chain = smaller.save()
    assert len(short_chain) < len(long_chain)
    assert pool.structure_epoch(smaller.file) > before, (
        "pages left the chain without anything saying so"
    )


# --- F5: the store reads the epoch its own redo door bumps -------------------------------------


def test_saving_after_a_replay_does_not_overwrite_what_was_replayed(
    pool: BufferPool,
) -> None:
    """A catalog read before a replay describes pages the replay has since rewritten.

    Writing it back put the pre-replay schema over the replayed one with no error raised, and a
    table that recovery had just restored -- with every row of it still in the heap -- was gone.
    Only this guard refuses: the save itself is well formed and the file stays readable.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    pages = store.save()

    replayed = Page(int(PageType.CATALOG), page_size=pool.page_size)
    replayed.page_lsn = 9999
    replayed.insert_slot(b"what recovery decided this page holds")
    assert store.apply_page_image(pages[0], pool.codec.encode_page(replayed)) is True

    with pytest.raises(GrafxTransactionStateError) as raised:
        store.save()
    assert raised.value.details["field"] == "structure_epoch"
    assert raised.value.details["loaded_epoch"] != raised.value.details["current_epoch"]


def test_the_refusal_after_a_replay_names_loading_again_as_the_way_out(
    pool: BufferPool,
) -> None:
    """Refusing rather than reloading is the deliberate half: a silent reload would discard the
    caller's own additions. So the caller has to be able to act on it.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()

    # The conflict needs BOTH halves, and the order matters. The change has to be in memory and
    # unsaved BEFORE the pages move under it: a store with nothing of its own simply adopts what
    # the pages now hold, which is the whole point of the read-view fix and loses nothing.
    store.catalog.add_table(table("Invoice", 3))
    other = CatalogStore(pool)
    other.load()
    other.catalog.add_table(table("Order", 2))
    other.save()

    assert store.has_unsaved_changes()
    with pytest.raises(GrafxTransactionStateError) as raised:
        store.save()
    assert raised.value.details["field"] == "structure_epoch"

    merged = store.read_from_pages()
    merged.add_table(table("Invoice", 3))
    store.adopt(merged)
    store.save()
    reopened = CatalogStore(pool)
    reopened.load()
    assert reopened.catalog.has_table("Order")
    assert reopened.catalog.has_table("Invoice")


def test_two_saves_in_a_row_from_one_store_are_allowed(pool: BufferPool) -> None:
    """The guard must not fire on the store's own writing: a save leaves the store describing
    exactly the pages it has just written."""
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    for index in range(2, 12):
        store.catalog.add_table(table(f"Table_{index}", index, columns=6))
        store.save()
    assert CatalogStore(pool).load().has_table("Table_11")


# --- D7: the remedy must not perform the loss the guard exists to prevent ----------------------


def test_dropping_the_cache_does_not_refuse_a_save(pool: BufferPool) -> None:
    """invalidate() writes dirty frames back before forgetting them, so no link moved.

    Refusing here sent a caller to a remedy that discards its tables, over an event that changed
    nothing on the device (D7, round 8). Only reading structure_epoch rather than derived_epoch
    produces this.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()

    pool.invalidate()
    store.catalog.add_table(table("Second", 2))
    store.save()

    reopened = CatalogStore(pool)
    reopened.load()
    assert [entry.name for entry in reopened.catalog.tables()] == ["Person", "Second"]


def test_the_refusal_after_a_replay_names_the_non_destructive_remedy(pool: BufferPool) -> None:
    """The remedy has to end with the caller keeping what it had, or it is the loss itself.

    load() replaces everything in memory, so following it loses exactly what the guard refused to
    let a save overwrite. read_from_pages() and adopt() are the route that keeps both sides.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()

    store.catalog.add_table(table("Mine", 3))
    other = CatalogStore(pool)
    other.load()
    other.catalog.add_table(table("Replayed", 2))
    other.save()

    with pytest.raises(GrafxTransactionStateError):
        store.save()

    merged = store.read_from_pages()
    for entry in store._catalog.tables():
        if not merged.has_table(entry.name):
            merged.add_table(entry)
    store.adopt(merged)
    store.save()

    reopened = CatalogStore(pool)
    reopened.load()
    names = {entry.name for entry in reopened.catalog.tables()}
    assert names == {"Person", "Replayed", "Mine"}


def test_reading_from_the_pages_leaves_the_store_holding_what_it_held(pool: BufferPool) -> None:
    """The whole point of the door: asking what is on disk must not cost what is in memory."""
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    store.catalog.add_table(table("Unsaved", 2))

    from_pages = store.read_from_pages()

    assert not from_pages.has_table("Unsaved")
    assert store.catalog.has_table("Unsaved"), "reading the pages threw the caller's table away"
    assert store.catalog.has_table("Person")


# --- E1/W6: a schema change that can still be refused must not be reachable ---------------------


def commit_staged(
    pool: BufferPool,
    file: str,
    images: tuple[tuple[int, bytes], ...],
    *,
    csn: int,
) -> None:
    """Apply staged page images the way CONTRACT.md section 8.5 step 6 applies them.

    C5 owns the real thing. What it does to a page image is written down and frozen -- stamp it
    with the commit number, install it through the redo door, flush the file but do not barrier
    it -- so C1 can prove its own half against that rule without importing the component that
    happens to implement it.
    """
    for page_index, image in sorted(images):
        page = pool.codec.decode_page(image, verify=True)
        if page.page_lsn < csn:
            page.page_lsn = csn
        apply_page_image(pool, file, page_index, pool.codec.encode_page(page))
    pool.flush(file)


def images_of(device: MemoryDevice, file: str) -> list[bytes]:
    """Return every page image the device holds for this file, in order."""
    return [device.raw_page(file, index) for index in range(device.page_count(file))]


def test_a_refused_schema_change_leaves_the_catalog_file_and_its_chain_byte_identical(
    pool: BufferPool, device: MemoryDevice
) -> None:
    """The defect, stated as the property that closes it.

    save() writes through the buffer pool the moment it is called, so a statement still entitled
    to be refused had already put its pages where the next flush would carry them. The winner
    then applied its own images over a SUBSET and the file header stopped describing the chain:
    "declares 125 bytes but its chain carries 63". Staging answers with bytes instead, so a
    refusal is a value nobody kept.

    The change deliberately needs a page the file does not have, because growing the file is the
    other way a refused change stays visible: a longer file is a changed file.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    pool.flush(store.file)

    before_images = images_of(device, store.file)
    before_chain = store.chain_pages()
    before_catalog = store.read_from_pages()
    device.write_calls.clear()

    change = store.read_from_pages()
    change.add_table(table("Refused", 2, columns=60))
    staged = store.stage(change)
    assert any(
        index >= len(before_images) for index, _image in staged
    ), "the change has to need a page the file does not have, or it proves less"

    del staged  # the transaction is refused: the images are simply dropped.
    pool.flush(store.file)

    assert device.write_calls == [], "a refused schema change wrote to the device"
    assert images_of(device, store.file) == before_images
    assert device.page_count(store.file) == len(before_images)
    assert store.chain_pages() == before_chain
    assert store.read_from_pages() == before_catalog
    assert not store.catalog.has_table("Refused")


def test_the_winner_of_three_concurrent_schema_changes_leaves_a_catalog_that_reads(
    pool: BufferPool, device: MemoryDevice
) -> None:
    """The reported sequence, reproduced through C1's own surface: A commits, B and C refused."""
    store = CatalogStore(pool)
    store.bootstrap()
    pool.flush(store.file)

    def change(name: str, table_id: int, columns: int = 2) -> tuple[tuple[int, bytes], ...]:
        catalog = store.read_from_pages()
        catalog.add_table(table(name, table_id, columns))
        return store.stage(catalog)

    winner = change("Alpha", 1)
    refused_wide = change("Beta", 2, columns=60)
    refused_narrow = change("Gamma", 3)
    assert len(refused_wide) > len(winner), "B has to need a page A never touched"
    del refused_wide, refused_narrow

    commit_staged(pool, store.file, winner, csn=7)

    reopened = CatalogStore(make_pool(device, RecordingMetrics(), budget_pages=8))
    assert [entry.name for entry in reopened.load().tables()] == ["Alpha"]


def test_a_committed_schema_change_is_visible_to_a_second_connection(
    pool: BufferPool, device: MemoryDevice
) -> None:
    """A connection opened after the commit reads the schema from the device, not from a pool.

    Its own pool, its own store, nothing shared but the file -- which is the only thing two
    processes share.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    pool.flush(store.file)
    catalog = store.read_from_pages()
    catalog.add_table(table("Person", 1, columns=60))
    staged = store.stage(catalog)

    second = CatalogStore(make_pool(device, RecordingMetrics(), budget_pages=8))
    assert not second.read_from_pages().has_table(
        "Person"
    ), "nothing is visible before the commit"

    commit_staged(pool, store.file, staged, csn=11)

    third = CatalogStore(make_pool(device, RecordingMetrics(), budget_pages=8))
    loaded = third.load()
    assert loaded.has_table("Person")
    assert loaded.table("Person") == catalog.table("Person")
    assert [entry.name for entry in store.catalog.tables()] == ["Person"]


def test_a_committed_schema_change_reaches_a_store_that_had_already_read_the_catalog(
    pool: BufferPool,
) -> None:
    """L22, on the half that holds no frames.

    CatalogStore._catalog is DERIVED and caches nothing the pool can drop, so dropping frames
    says nothing to it on its own. It reads the epoch instead, and applying a page image is what
    moves the structural half of that reading -- so the commit that installs a staged schema
    change is itself the signal that the derived answer is stale.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    assert store.catalog.is_empty(), "the store has to have derived an answer first"

    catalog = store.read_from_pages()
    catalog.add_table(table("Person", 1))
    commit_staged(pool, store.file, store.stage(catalog), csn=13)

    assert store.catalog.has_table("Person")
    assert not store.has_unsaved_changes()


def test_staging_leaves_the_live_store_holding_exactly_what_it_held(
    pool: BufferPool,
) -> None:
    """L21: the marker must never be set from a catalog a refusal can still throw away.

    If staging recorded "the pages hold this", a refused change would have left the store
    believing the file carried a catalog it never received, and has_unsaved_changes() -- the one
    guard between a stale participant and a destroyed schema -- would have answered for a write
    that did not happen.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    held = store.read_from_pages()
    assert not store.has_unsaved_changes()

    change = store.read_from_pages()
    change.add_table(table("Staged", 2))
    store.stage(change)

    assert not store.has_unsaved_changes(), "staging marked an unwritten catalog as persisted"
    assert store.catalog == held
    assert not store.catalog.has_table("Staged")
    assert store.read_from_pages() == held


def test_staging_the_live_catalog_object_of_this_store_is_refused(pool: BufferPool) -> None:
    """The structural half of L21: the copy and the live object must not be one object.

    Staging the very object _catalog names would succeed, commit, and leave this store holding a
    catalog its _persisted_image no longer describes -- so has_unsaved_changes() would say True
    for good and the refresh the commit triggers would refuse instead of re-reading. A wedged
    store behind a transaction that worked is the worst shape the mistake can take, so it is
    refused at the door, naming the copy that replaces it.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    with pytest.raises(GrafxTransactionStateError) as raised:
        store.stage(store.catalog)
    assert raised.value.details["field"] == "catalog"
    assert "read_from_pages()" in raised.value.message

    # And the copy that the message names is accepted, so the guard is not satisfied by refusing
    # everything (A85).
    copy = store.read_from_pages()
    copy.add_table(table("Person", 1))
    assert store.stage(copy)


def test_staging_something_that_is_not_a_catalog_is_refused(pool: BufferPool) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    with pytest.raises(GrafxConfigurationError) as raised:
        store.stage({"tables": []})  # type: ignore[arg-type]
    assert raised.value.details["field"] == "catalog"


def test_staging_before_the_catalog_file_is_bootstrapped_is_refused(
    pool: BufferPool, device: MemoryDevice
) -> None:
    """Refused by the guard that names the remedy, not merely refused.

    Two doors below this one refuse the same input for their own reasons -- a chain would start
    at the reserved page, or the file does not exist -- and both raise the same code with the
    same file. A survivor in the battery said so: dropping this check left the suite green,
    because a downstream guard answered indistinguishably (A67, A34). What the caller needs is
    the one message that says what to DO, so the test asserts on that and the guard becomes
    demonstrable.

    The second half is the case the downstream doors genuinely cannot answer: a file a redo grew
    before anything reserved page 0 (amendment A22). It exists, it has pages, so both chain
    guards pass -- and staging a catalog into it would still be staging into a file with no
    header.
    """
    store = CatalogStore(pool)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        store.stage(Catalog())
    assert raised.value.details["file"] == store.file
    assert "bootstrap()" in raised.value.message

    device.create(store.file)
    device.allocate(store.file, 4)
    assert not store.is_bootstrapped()
    with pytest.raises(GrafxCorruptionDetected) as grown:
        store.stage(Catalog())
    assert grown.value.details["file"] == store.file
    assert "bootstrap()" in grown.value.message
    assert device.page_count(store.file) == 4, "a refused staging grew the file"


def test_staging_always_names_the_reserved_header_page(pool: BufferPool) -> None:
    """The write set of a schema change has to be total, and page 0 is the page it forgets.

    save() returns the CHAIN pages only, so a caller that logged exactly what it was handed
    logged everything except the page saying where the chain starts and how much of it means
    anything. That page then reached the device outside every log record -- invisible to redo,
    and free to be replaced by a transaction that was refused.

    It is unconditional because it also decides conflicts: TransactionContext declares interest
    in every page image staged on it, so two participants changing the schema at once always
    intersect here, whatever their chain pages do.
    """
    store = CatalogStore(pool)
    store.bootstrap()

    for number in range(1, 6):
        catalog = store.read_from_pages()
        catalog.add_table(table(f"Table_{number}", number, columns=20))
        staged = store.stage(catalog)
        assert HEADER_PAGE_INDEX in [index for index, _image in staged]
        assert staged[-1][0] == HEADER_PAGE_INDEX, "the page that makes it reachable comes last"
        commit_staged(pool, store.file, staged, csn=100 + number)

    assert len(store.catalog.tables()) == 5


def test_two_participants_staging_a_schema_change_always_name_a_page_in_common(
    pool: BufferPool,
) -> None:
    """Optimistic validation can only refuse a page both sides declared, so both must declare one.

    Two changes whose chains barely overlap still collide, because the header page is in both
    write sets. Without that, three participants each creating a table all reported a durable
    commit and two of them were lying (defect E1).
    """
    store = CatalogStore(pool)
    store.bootstrap()
    for number in range(1, 12):
        store.catalog.add_table(table(f"Seed_{number}", number, columns=20))
    store.save()
    pool.flush(store.file)

    narrow = store.read_from_pages()
    narrow.add_table(table("Narrow", 90))
    wide = store.read_from_pages()
    for number in range(91, 99):
        wide.add_table(table(f"Wide_{number}", number, columns=40))

    left = {index for index, _image in store.stage(narrow)}
    right = {index for index, _image in store.stage(wide)}
    assert left != right, "the two changes have to differ, or the test proves nothing"
    assert HEADER_PAGE_INDEX in left & right


def test_applying_a_staged_change_produces_what_a_save_would_have_produced(
    pool: BufferPool,
) -> None:
    """The two doors answer the same question by two routes; only a test stops them drifting.

    Everything a reader depends on is compared: the pages of the chain, what each of them
    carries, the root and the payload length in the file header, and the catalog that comes back
    out of the whole thing (A67).
    """
    saved_device = MemoryDevice()
    saved_pool = make_pool(saved_device, RecordingMetrics(), budget_pages=8)
    saved = CatalogStore(saved_pool)
    saved.bootstrap()
    for number in range(1, 8):
        saved.catalog.add_table(table(f"Table_{number}", number, columns=20))
    saved.save()

    store = CatalogStore(pool)
    store.bootstrap()
    catalog = store.read_from_pages()
    for number in range(1, 8):
        catalog.add_table(table(f"Table_{number}", number, columns=20))
    commit_staged(pool, store.file, store.stage(catalog), csn=17)

    assert store.chain_pages() == saved.chain_pages()
    assert store.read_from_pages() == saved.read_from_pages()
    for index in store.chain_pages():
        with pool.pinned(store.file, index) as staged_page:
            with saved_pool.pinned(saved.file, index) as saved_page:
                assert staged_page.page_type == saved_page.page_type
                assert staged_page.next_page == saved_page.next_page
                assert staged_page.read_slot(0) == saved_page.read_slot(0)
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as staged_page:
        with saved_pool.pinned(saved.file, HEADER_PAGE_INDEX) as saved_page:
            assert FileHeaderPage.read(staged_page) == FileHeaderPage.read(saved_page)


def test_staging_a_shrinking_catalog_empties_the_pages_it_surrenders(
    pool: BufferPool,
) -> None:
    """A page dropped from the chain but left carrying its old chunk is a chain that survives it.

    The staged images empty those pages exactly as _release does, so the commit that shortens
    the chain also frees what it shortened -- and the next change takes them back.
    """
    store = CatalogStore(pool)
    store.bootstrap()
    for number in range(1, 12):
        store.catalog.add_table(table(f"Table_{number}", number, columns=20))
    store.save()
    pool.flush(store.file)
    long_chain = store.chain_pages()
    assert len(long_chain) >= 3

    commit_staged(pool, store.file, store.stage(Catalog()), csn=23)

    assert len(store.chain_pages()) == 1
    for index in long_chain[1:]:
        with pool.pinned(store.file, index) as page:
            assert page.page_type == int(PageType.FREE)
            assert page.next_page == NO_PAGE
            assert page.slot_count == 0
    assert store.read_from_pages().is_empty()


def test_the_reserved_page_is_never_staged_as_a_page_to_empty(pool: BufferPool) -> None:
    """Proved at its own level, because every door in front of it refuses the same input first.

    chain_pages() refuses a root of 0 and a next_page of 0, and build_chain_images refuses a
    reuse list that names it, so nothing an ordinary caller can pass reaches this. It is kept
    anyway: a staged image that emptied page 0 would clear the file header at COMMIT time --
    after the log had already said the transaction was durable -- and take every table with it.
    When a component can destroy, the tie goes to preservation (LESSONS L17), and a guard nothing
    else can answer for is tested where nothing else can answer (A34).
    """
    store = CatalogStore(pool)
    store.bootstrap()
    with pytest.raises(GrafxCorruptionDetected) as raised:
        store._released_images((HEADER_PAGE_INDEX,))
    assert raised.value.details["page"] == HEADER_PAGE_INDEX
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        assert FileHeaderPage.read(page).kind is FileKind.CATALOG


def test_a_chain_rooted_at_the_reserved_header_page_is_refused_before_anything_is_staged(
    pool: BufferPool,
) -> None:
    store = CatalogStore(pool)
    store.bootstrap()
    store.catalog.add_table(table("Person", 1))
    store.save()
    header = store._read_file_header()
    store._write_file_header(
        FileHeader(
            kind=FileKind.CATALOG,
            page_size=header.page_size,
            format_version=header.format_version,
            root_page=HEADER_PAGE_INDEX,
            payload_length=header.payload_length,
        )
    )
    with pytest.raises(GrafxCorruptionDetected) as raised:
        store.stage(Catalog())
    assert raised.value.details["field"] == "root_page"


def test_staging_pins_nothing_when_it_returns(pool: BufferPool) -> None:
    """A door that leaves a page pinned makes the next invalidate refuse, and a participant that
    cannot drop its cache is a participant that stays stale."""
    store = CatalogStore(pool)
    store.bootstrap()
    for number in range(1, 8):
        store.catalog.add_table(table(f"Table_{number}", number, columns=20))
    store.save()
    catalog = store.read_from_pages()
    catalog.add_table(table("More", 40, columns=40))
    store.stage(catalog)
    pool.invalidate()
    assert pool.used_bytes() == 0
