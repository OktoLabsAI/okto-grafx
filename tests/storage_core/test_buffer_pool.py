"""The buffer pool: a budget per database, and a read that survives a writer (FR-13, BR-8).

Two requirements meet in this file.

FR-13 and BR-8 say the budget belongs to one database, so pressure in one is invisible in every
other. The test that matters is not that a pool has a budget, but that a pool which has run out
of memory does not change the behaviour of a second pool built in the same process; a module
level cache or a class attribute would fail exactly there and nowhere else.

CONTRACT.md section 6.3 says a reader never blocks a writer, so a read may land on a page that
is being written. The pool answers by reading again, a bounded number of times, without ever
sleeping, and then declares the page corrupt with its location.
"""

from __future__ import annotations

import contextlib
import struct
import threading
from collections.abc import Iterator

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.domain.errors import (
    GrafxError,
    GrafxBufferBudgetExceeded,
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import PageIndex
from okto_grafx.domain.page import (
    PAGE_HEADER_SIZE,
    Page,
    PageType,
    crc32c,
    is_unwritten_image,
)
from okto_grafx.engine import buffer_pool as pool_module
from okto_grafx.engine.buffer_pool import (
    BUFFER_BUDGET_EXCEEDED_TOTAL,
    BUFFER_BUDGET_USED_BYTES,
    FSYNC_DURATION_SECONDS,
    CHECKSUM_FAILURES_TOTAL,
    CHECKSUM_VERIFICATIONS_TOTAL,
    TORN_READ_RETRY_BUDGET,
    MAX_REDO_GAP_PAGES,
    BufferPool,
    apply_page_image,
    build_chain_images,
    grow_to,
    next_seq,
    read_chain,
    refuse_endless_chain,
    write_chain,
)

from .conftest import MemoryDevice, RecordingMetrics, make_pool

FILE: str = "heap.dat"


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


def reserve_header(pool: BufferPool, file: str = FILE) -> None:
    """Give a file its reserved page 0, which every real paged file has (A2).

    A chain page may never land on page 0, so a helper that writes one has to start from a file
    that already looks like a file rather than from a bare allocation.
    """
    if not pool.storage.exists(file):
        pool.storage.create(file)
    if pool.storage.page_count(file) == 0:
        page = pool.allocate(file, int(PageType.META))
        page.insert_slot(b"header")
        pool.unpin(file, page.page_index, dirty=True)


def seed_pages(pool: BufferPool, count: int, *, file: str = FILE) -> list[PageIndex]:
    """Allocate and write count pages, returning their indices."""
    indices: list[PageIndex] = []
    pool.storage.create(file)
    for number in range(count):
        page = pool.allocate(file, int(PageType.HEAP))
        page.insert_slot(f"page-{number}".encode())
        indices.append(page.page_index)
        pool.unpin(file, page.page_index, dirty=True)
    pool.flush(file)
    return indices


# --- budget isolation --------------------------------------------------------------------------


def test_two_pools_share_nothing_and_never_see_each_other() -> None:
    first_device, second_device = MemoryDevice(), MemoryDevice()
    first_metrics, second_metrics = RecordingMetrics(), RecordingMetrics()
    first = make_pool(first_device, first_metrics, budget_pages=2, db_label="alpha")
    second = make_pool(second_device, second_metrics, budget_pages=8, db_label="beta")
    seed_pages(first, 4)
    seed_pages(second, 4)

    # Exhaust the first pool completely: every frame it can hold is pinned.
    held = [first.pin(FILE, index) for index in range(2)]
    assert first.used_bytes() == first.budget_bytes
    with pytest.raises(GrafxBufferBudgetExceeded):
        first.pin(FILE, 2)

    # The second database is untouched by that pressure: it still reads, still caches, and its
    # own accounting never moved.
    for index in range(4):
        second.pin(FILE, index)
        second.unpin(FILE, index)
    assert second.used_bytes() == 4 * second.page_size
    assert len(held) == 2

    # And the failure is reported under the label of the database that suffered it.
    labels = first_metrics.labels_of(BUFFER_BUDGET_EXCEEDED_TOTAL)
    assert labels == [{"db": "alpha"}]
    assert second_metrics.labels_of(BUFFER_BUDGET_EXCEEDED_TOTAL) == []


def test_a_second_pool_over_the_same_device_keeps_its_own_budget() -> None:
    device = MemoryDevice()
    tight = make_pool(device, RecordingMetrics(), budget_pages=1, db_label="tight")
    roomy = make_pool(device, RecordingMetrics(), budget_pages=8, db_label="roomy")
    seed_pages(tight, 5)
    tight.pin(FILE, 0)
    with pytest.raises(GrafxBufferBudgetExceeded):
        tight.pin(FILE, 1)
    # The same device, the same pages, a different budget: the roomy pool is unaffected.
    for index in range(5):
        roomy.pin(FILE, index)
        roomy.unpin(FILE, index)
    assert roomy.used_bytes() == 5 * device.page_size


def test_the_budget_is_reported_as_a_gauge_under_the_database_label() -> None:
    device, metrics = MemoryDevice(), RecordingMetrics()
    pool = make_pool(device, metrics, budget_pages=4, db_label="alpha")
    seed_pages(pool, 3)
    assert metrics.labels_of(BUFFER_BUDGET_USED_BYTES)[-1] == {"db": "alpha"}
    assert metrics.values_of(BUFFER_BUDGET_USED_BYTES)[-1] == pool.used_bytes()
    pool.invalidate()
    assert metrics.values_of(BUFFER_BUDGET_USED_BYTES)[-1] == 0.0


def test_the_pool_registers_every_metric_before_it_emits_it() -> None:
    # The property worth holding is that registration precedes emission, which is what a real
    # sink enforces. Comparing the declared set against a literal spelling of the same constants
    # it was built from would hold whatever the pool did.
    device, metrics = MemoryDevice(), RecordingMetrics()
    pool = make_pool(device, metrics)
    declared = set(metrics.registered)
    assert declared, "nothing was registered at all"
    device.create(FILE)
    page = pool.allocate(FILE, int(PageType.HEAP))
    pool.unpin(FILE, page.page_index, dirty=True)
    pool.checkpoint()
    pool.invalidate()
    pool.pin(FILE, 0)
    emitted = {name for _kind, name, _value, _labels in metrics.calls}
    assert emitted <= declared, sorted(emitted - declared)
    assert BUFFER_BUDGET_USED_BYTES in emitted
    assert FSYNC_DURATION_SECONDS in emitted


def test_a_disabled_sink_is_never_called() -> None:
    device = MemoryDevice()
    metrics = RecordingMetrics(enabled=False)
    pool = make_pool(device, metrics)
    seed_pages(pool, 2)
    pool.pin(FILE, 0)
    pool.unpin(FILE, 0)
    assert metrics.calls == []
    assert metrics.registered == {}


def test_exhaustion_is_retryable_and_names_the_database() -> None:
    device, metrics = MemoryDevice(), RecordingMetrics()
    pool = make_pool(device, metrics, budget_pages=2, db_label="alpha")
    seed_pages(pool, 4)
    pool.pin(FILE, 0)
    pool.pin(FILE, 1)
    with pytest.raises(GrafxBufferBudgetExceeded) as raised:
        pool.pin(FILE, 2)
    assert raised.value.retryable is True
    assert raised.value.code == "buffer_budget_exceeded"
    assert raised.value.details["db"] == "alpha"
    assert raised.value.details["page"] == 2
    assert raised.value.details["budget_bytes"] == pool.budget_bytes


def test_releasing_a_pin_lets_the_next_page_in() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=2)
    seed_pages(pool, 4)
    pool.pin(FILE, 0)
    pool.pin(FILE, 1)
    with pytest.raises(GrafxBufferBudgetExceeded):
        pool.pin(FILE, 2)
    pool.unpin(FILE, 0)
    page = pool.pin(FILE, 2)
    assert page.page_index == 2
    assert not pool.is_resident(FILE, 0)


def test_a_budget_below_one_page_is_refused() -> None:
    device = MemoryDevice()
    with pytest.raises(GrafxConfigurationError):
        BufferPool(
            device,
            PageCodecV1(device.page_size),
            RecordingMetrics(),
            budget_bytes=device.page_size - 1,
            db_label="alpha",
        )


@pytest.mark.parametrize(
    "label", ["", "a" * 65, "C:/data/db", "db name", "db/1", "\u00e9"]
)
def test_a_database_label_that_is_not_short_and_bounded_is_refused(label: str) -> None:
    device = MemoryDevice()
    with pytest.raises(GrafxConfigurationError):
        BufferPool(
            device,
            PageCodecV1(device.page_size),
            RecordingMetrics(),
            budget_bytes=device.page_size * 4,
            db_label=label,
        )


# --- residency and eviction -----------------------------------------------------------------


def test_a_pinned_page_is_returned_from_the_cache_without_a_second_read() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2)
    pool.invalidate()
    device.read_calls.clear()
    first = pool.pin(FILE, 0)
    second = pool.pin(FILE, 0)
    assert first is second
    assert pool.pin_count(FILE, 0) == 2
    assert len(device.read_calls) == 1
    pool.unpin(FILE, 0)
    pool.unpin(FILE, 0)
    assert pool.pin_count(FILE, 0) == 0


def test_eviction_takes_the_least_recently_used_unpinned_page() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=3)
    seed_pages(pool, 5)
    pool.invalidate()
    for index in (0, 1, 2):
        pool.pin(FILE, index)
        pool.unpin(FILE, index)
    pool.pin(FILE, 0)  # page 0 becomes the most recent again
    pool.unpin(FILE, 0)
    pool.pin(FILE, 3)
    pool.unpin(FILE, 3)
    assert not pool.is_resident(FILE, 1)
    assert pool.is_resident(FILE, 0)
    assert pool.is_resident(FILE, 2)
    assert pool.is_resident(FILE, 3)


def test_a_dirty_page_is_written_before_it_is_evicted() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=1)
    seed_pages(pool, 2)
    with pool.pinned(FILE, 0) as page:
        page.insert_slot(b"changed")
    device.write_calls.clear()
    pool.pin(FILE, 1)  # forces the eviction of page 0
    assert (FILE, 0) in device.write_calls
    pool.unpin(FILE, 1)
    pool.invalidate()
    with pool.pinned(FILE, 0) as page:
        assert page.read_slot(1) == b"changed"


def test_flush_writes_only_the_dirty_pages_and_says_how_many() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 3)
    assert pool.flush(FILE) == 0
    with pool.pinned(FILE, 1) as page:
        page.page_lsn = 42
    with pool.pinned(FILE, 2) as page:
        page.page_lsn = 43
    device.write_calls.clear()
    assert pool.flush(FILE) == 2
    assert sorted(index for _file, index in device.write_calls) == [1, 2]
    assert pool.flush() == 0


def test_flush_can_be_limited_to_one_file() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1, file="heap.dat")
    seed_pages(pool, 1, file="catalog.dat")
    with pool.pinned("heap.dat", 0) as page:
        page.page_lsn = 1
    with pool.pinned("catalog.dat", 0) as page:
        page.page_lsn = 1
    assert pool.flush("heap.dat") == 1
    assert pool.flush("catalog.dat") == 1


def test_invalidate_forces_a_re_read_and_keeps_what_was_written() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2)
    with pool.pinned(FILE, 0) as page:
        page.insert_slot(b"kept")
    pool.invalidate(FILE)
    assert not pool.is_resident(FILE, 0)
    assert pool.used_bytes() == 0
    with pool.pinned(FILE, 0) as page:
        assert page.read_slot(1) == b"kept"


def test_a_fresh_page_observation_bypasses_a_resident_frame_without_replacing_it() -> (
    None
):
    """A cross-process certificate must come from the device, not from the cache it certifies."""
    device = MemoryDevice()
    reader = make_pool(device, RecordingMetrics())
    writer = make_pool(device, RecordingMetrics())
    seed_pages(reader, 1)

    with reader.pinned(FILE, 0) as cached:
        original = cached.read_slot(0)
    with writer.pinned(FILE, 0) as changed:
        changed.update_slot(0, b"foreign")
    writer.flush(FILE)

    writes_before = tuple(device.write_calls)
    observed = reader.read_fresh_page(FILE, 0)

    assert observed.read_slot(0) == b"foreign"
    assert tuple(device.write_calls) == writes_before, (
        "a read-only observation wrote a page"
    )
    with reader.pinned(FILE, 0) as still_cached:
        assert still_cached.read_slot(0) == original, (
            "observing the device silently replaced a frame another caller may still rely on"
        )


def test_the_dirty_probe_is_file_scoped_and_clears_after_publication() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2)

    assert pool.has_dirty_pages() is False
    with pool.pinned(FILE, 0) as dirty:
        dirty.update_slot(0, b"local-unpublished")

    assert pool.has_dirty_pages() is True
    assert pool.has_dirty_pages(FILE) is True
    assert pool.has_dirty_pages("another.dat") is False
    pool.flush(FILE)
    assert pool.has_dirty_pages(FILE) is False


def test_discard_clean_file_forgets_clean_frames_without_writing_them_back() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 3)
    for page_index in range(3):
        with pool.pinned(FILE, page_index):
            pass
    writes_before = tuple(device.write_calls)
    epoch_before = pool.cache_drop_epoch(FILE)

    assert pool.discard_clean_file(FILE) == 3

    assert tuple(device.write_calls) == writes_before
    assert all(not pool.is_resident(FILE, page_index) for page_index in range(3))
    assert pool.cache_drop_epoch(FILE) == epoch_before + 1


def test_discard_clean_file_refuses_dirty_frames_atomically_and_without_writeback() -> (
    None
):
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2)
    with pool.pinned(FILE, 0) as dirty:
        dirty.update_slot(0, b"local-uncommitted")
    held_clean = pool.pin(FILE, 1)
    writes_before = tuple(device.write_calls)
    epoch_before = pool.cache_drop_epoch(FILE)

    try:
        with pytest.raises(GrafxUnsupportedOperation) as refused:
            pool.discard_clean_file(FILE)

        assert refused.value.details["field"] == "dirty"
        assert tuple(device.write_calls) == writes_before
        assert pool.cache_drop_epoch(FILE) == epoch_before
        assert pool.is_resident(FILE, 0)
        assert pool.is_resident(FILE, 1), (
            "the clean pinned sibling was doomed before the dirty refusal"
        )
        assert (FILE, 1) not in pool._doomed
    finally:
        pool.unpin(FILE, 1, page=held_clean)


def test_discard_clean_file_dooms_clean_pins_and_releases_the_exact_object_unwritten() -> (
    None
):
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2)
    held = pool.pin(FILE, 1)
    writes_before = tuple(device.write_calls)
    epoch_before = pool.cache_drop_epoch(FILE)

    assert pool.discard_clean_file(FILE) == 2

    assert tuple(device.write_calls) == writes_before
    assert pool.cache_drop_epoch(FILE) == epoch_before + 1
    assert not pool.is_resident(FILE, 0)
    assert not pool.is_resident(FILE, 1)
    assert pool._doomed[(FILE, 1)][0].page is held

    # Even a late mutation of the retired object cannot publish stale bytes over the foreign
    # certificate that caused the refresh. A new pin owns a different, device-backed object.
    held.update_slot(0, b"stale-holder")
    fresh = pool.pin(FILE, 1)
    assert fresh is not held
    assert fresh.read_slot(0) == b"page-1"
    pool.unpin(FILE, 1, dirty=True, page=held)
    assert tuple(device.write_calls) == writes_before
    assert pool.pin_count(FILE, 1) == 1
    assert (FILE, 1) not in pool._doomed
    pool.unpin(FILE, 1, page=fresh)


def test_discard_clean_file_accepts_a_dirty_frame_already_marked_discard_only() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    held = pool.pin(FILE, 0)
    writes_before = tuple(device.write_calls)
    assert pool.discard_clean_file(FILE) == 1
    held.update_slot(0, b"late-stale-holder")

    assert pool.discard_clean_file(FILE) == 0

    pool.unpin(FILE, 0, dirty=True, page=held)
    assert tuple(device.write_calls) == writes_before


def test_discard_clean_file_keeps_existing_pinned_doomed_frames_only() -> None:
    """A second refresh retires orphaned doomed frames but preserves a live holder."""
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    held = pool.pin(FILE, 0)
    assert pool.begin_read_view("foreign-1") is True
    existing = pool._doomed[(FILE, 0)][0]
    orphan = pool_module._Frame(pool.read_fresh_page(FILE, 0))
    orphan.doomed = True
    pool._doomed[(FILE, 0)].append(orphan)
    writes_before = tuple(device.write_calls)

    assert pool.discard_clean_file(FILE) == 0

    assert pool._doomed[(FILE, 0)] == [existing]
    assert existing.discard_unwritten is True
    pool.unpin(FILE, 0, page=held)
    assert (FILE, 0) not in pool._doomed
    assert tuple(device.write_calls) == writes_before


def test_discard_clean_file_forgets_local_reuse_claims_over_foreign_pages() -> None:
    """A page reclaimed locally may have acquired meaning in another participant meanwhile."""
    device = MemoryDevice()
    reader = make_pool(device, RecordingMetrics())
    writer = make_pool(device, RecordingMetrics())
    reserve_header(reader)
    reader.flush(FILE)
    abandoned = reader.allocate(FILE, int(PageType.HEAP))
    abandoned_index = abandoned.page_index
    reader.unpin(FILE, abandoned_index, page=abandoned)
    assert reader.discard(FILE, abandoned_index)

    with writer.pinned(FILE, abandoned_index) as adopted:
        adopted.page_type = int(PageType.HEAP)
        adopted.insert_slot(b"foreign-owner")
    writer.flush(FILE)

    reader.discard_clean_file(FILE)
    replacement = reader.allocate(FILE, int(PageType.HEAP))
    try:
        assert replacement.page_index != abandoned_index
    finally:
        reader.unpin(FILE, replacement.page_index, page=replacement)


def test_discard_clean_file_keeps_an_unwritten_claim_settleable() -> None:
    """A foreign cache rebase must not strand an abandoned zero page at close."""
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    pool.flush(FILE)
    abandoned = pool.allocate(FILE, int(PageType.HEAP))
    page_index = abandoned.page_index
    pool.unpin(FILE, page_index, page=abandoned)
    assert pool.discard(FILE, page_index)
    assert is_unwritten_image(device.raw_page(FILE, page_index), device.page_size)

    pool.discard_clean_file(FILE)

    assert pool.settle_abandoned(FILE) == 1
    assert not is_unwritten_image(device.raw_page(FILE, page_index), device.page_size)


def test_discard_clean_page_dooms_a_clean_pin_and_never_writes_it_on_release() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2)
    held = pool.pin(FILE, 0)
    writes_before = tuple(device.write_calls)
    epoch_before = pool.cache_drop_epoch(FILE)

    assert pool.discard_clean_page(FILE, 0) is True

    assert not pool.is_resident(FILE, 0)
    assert pool.is_resident(FILE, 1), (
        "the page-scoped refresh dropped an unrelated frame"
    )
    assert pool.cache_drop_epoch(FILE) == epoch_before + 1
    assert pool._doomed[(FILE, 0)][0].page is held
    held.update_slot(0, b"stale-header")
    fresh = pool.pin(FILE, 0)
    assert fresh is not held
    pool.unpin(FILE, 0, dirty=True, page=held)
    assert tuple(device.write_calls) == writes_before
    assert pool.pin_count(FILE, 0) == 1
    pool.unpin(FILE, 0, page=fresh)


def test_discard_clean_page_refuses_dirty_among_doomed_and_resident_atomically() -> (
    None
):
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    held_clean = pool.pin(FILE, 0)
    assert pool.begin_read_view("foreign-1") is True
    fresh_dirty = pool.pin(FILE, 0)
    fresh_dirty.update_slot(0, b"local-uncommitted")
    writes_before = tuple(device.write_calls)
    epoch_before = pool.cache_drop_epoch(FILE)

    try:
        with pytest.raises(GrafxUnsupportedOperation) as refused:
            pool.discard_clean_page(FILE, 0)

        assert refused.value.details["field"] == "dirty"
        assert tuple(device.write_calls) == writes_before
        assert pool.cache_drop_epoch(FILE) == epoch_before
        assert pool.is_resident(FILE, 0)
        assert pool._doomed[(FILE, 0)][0].page is held_clean
        assert pool._doomed[(FILE, 0)][0].discard_unwritten is True
    finally:
        pool.unpin(FILE, 0, page=held_clean)
        pool.unpin(FILE, 0, page=fresh_dirty)


def test_bounded_read_view_discards_only_proved_pages_files_and_catalog() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=8)
    index_file = "indexes/person-name.dat"
    catalog_file = "catalog/catalog.dat"
    seed_pages(pool, 3)
    seed_pages(pool, 2, file=index_file)
    seed_pages(pool, 1, file=catalog_file)
    pool.begin_read_view("old")
    for file, pages in ((FILE, 3), (index_file, 2), (catalog_file, 1)):
        for page_index in range(pages):
            page = pool.pin(file, page_index)
            pool.unpin(file, page_index, page=page)
    epochs = {
        file: pool.cache_drop_epoch(file) for file in (FILE, index_file, catalog_file)
    }
    writes_before = tuple(device.write_calls)

    assert pool.begin_read_view(
        "new",
        changed_pages={(FILE, 1)},
        changed_files={index_file},
        unfenced_file=catalog_file,
        expected_previous="old",
    )

    assert pool.read_view_token() == "new"
    assert pool.is_resident(FILE, 0)
    assert not pool.is_resident(FILE, 1)
    assert pool.is_resident(FILE, 2)
    assert not pool.is_resident(index_file, 0)
    assert not pool.is_resident(index_file, 1)
    assert not pool.is_resident(catalog_file, 0)
    assert tuple(device.write_calls) == writes_before
    for file in (FILE, index_file, catalog_file):
        assert pool.cache_drop_epoch(file) == epochs[file] + 1


def test_bounded_read_view_preflights_every_target_before_moving_token_or_frame() -> (
    None
):
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 3)
    pool.begin_read_view("old")
    for page_index in range(3):
        page = pool.pin(FILE, page_index)
        pool.unpin(FILE, page_index, page=page)
    dirty = pool.pin(FILE, 2)
    dirty.update_slot(0, b"local-work")
    pool.unpin(FILE, 2, dirty=True, page=dirty)
    epoch_before = pool.cache_drop_epoch(FILE)
    writes_before = tuple(device.write_calls)

    with pytest.raises(GrafxUnsupportedOperation) as refused:
        pool.begin_read_view(
            "new",
            changed_pages={(FILE, 0), (FILE, 2)},
            expected_previous="old",
        )

    assert refused.value.details["field"] == "dirty"
    assert pool.read_view_token() == "old"
    assert all(pool.is_resident(FILE, page_index) for page_index in range(3))
    assert pool.cache_drop_epoch(FILE) == epoch_before
    assert tuple(device.write_calls) == writes_before


def test_bounded_read_view_dooms_a_changed_pin_and_never_writes_it_later() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2)
    pool.begin_read_view("old")
    held = pool.pin(FILE, 0)
    sibling = pool.pin(FILE, 1)
    pool.unpin(FILE, 1, page=sibling)
    writes_before = tuple(device.write_calls)

    assert pool.begin_read_view(
        "new", changed_pages={(FILE, 0)}, expected_previous="old"
    )

    assert not pool.is_resident(FILE, 0)
    assert pool.is_resident(FILE, 1)
    assert pool._doomed[(FILE, 0)][0].page is held
    assert pool._doomed[(FILE, 0)][0].discard_unwritten is True
    held.update_slot(0, b"late-stale-holder")
    pool.unpin(FILE, 0, dirty=True, page=held)
    assert tuple(device.write_calls) == writes_before
    assert (FILE, 0) not in pool._doomed


def test_same_token_still_refreshes_the_explicitly_unfenced_file() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    catalog_file = "catalog/catalog.dat"
    seed_pages(pool, 1)
    seed_pages(pool, 1, file=catalog_file)
    pool.begin_read_view("stable")
    for file in (FILE, catalog_file):
        page = pool.pin(file, 0)
        pool.unpin(file, 0, page=page)
    catalog_epoch = pool.cache_drop_epoch(catalog_file)

    assert pool.begin_read_view("stable", unfenced_file=catalog_file)

    assert pool.is_resident(FILE, 0)
    assert not pool.is_resident(catalog_file, 0)
    assert pool.cache_drop_epoch(catalog_file) == catalog_epoch + 1


def test_partial_refresh_without_a_bound_baseline_falls_back_to_every_file() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    other = "other.dat"
    seed_pages(pool, 2)
    seed_pages(pool, 1, file=other)

    assert pool.begin_read_view("first", changed_pages={(FILE, 0)})

    assert not pool.is_resident(FILE, 0)
    assert not pool.is_resident(FILE, 1)
    assert not pool.is_resident(other, 0)

    for file, page_index in ((FILE, 0), (FILE, 1), (other, 0)):
        page = pool.pin(file, page_index)
        pool.unpin(file, page_index, page=page)
    assert pool.begin_read_view("second", changed_pages={(FILE, 0)})
    assert not pool.is_resident(FILE, 0)
    assert not pool.is_resident(FILE, 1)
    assert not pool.is_resident(other, 0)

    for file, page_index in ((FILE, 0), (other, 0)):
        page = pool.pin(file, page_index)
        pool.unpin(file, page_index, page=page)
    assert pool.begin_read_view(None, changed_pages=())
    assert not pool.is_resident(FILE, 0)
    assert not pool.is_resident(other, 0)


def test_stale_partial_baseline_falls_back_atomically_instead_of_regressing_partially() -> (
    None
):
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2)
    pool.begin_read_view("t1")
    for page_index in range(2):
        page = pool.pin(FILE, page_index)
        pool.unpin(FILE, page_index, page=page)
    pool.begin_read_view(
        "t3",
        changed_pages={(FILE, 1)},
        expected_previous="t1",
    )
    page = pool.pin(FILE, 1)
    pool.unpin(FILE, 1, page=page)
    assert pool.is_resident(FILE, 0)
    assert pool.is_resident(FILE, 1)

    pool.begin_read_view(
        "t2",
        changed_pages={(FILE, 0)},
        expected_previous="t1",
    )

    assert pool.read_view_token() == "t2"
    assert not pool.is_resident(FILE, 0)
    assert not pool.is_resident(FILE, 1)


def test_repeated_delta_accepts_a_dirty_doomed_frame_that_is_already_discard_only() -> (
    None
):
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    pool.begin_read_view("t0")
    held = pool.pin(FILE, 0)
    writes_before = tuple(device.write_calls)
    pool.begin_read_view("t1", changed_pages={(FILE, 0)}, expected_previous="t0")
    held.update_slot(0, b"late-stale-holder")

    pool.begin_read_view("t2", changed_pages={(FILE, 0)}, expected_previous="t1")

    assert pool.read_view_token() == "t2"
    assert pool._doomed[(FILE, 0)][0].discard_unwritten is True
    pool.unpin(FILE, 0, dirty=True, page=held)
    assert tuple(device.write_calls) == writes_before


def test_foreign_full_fallback_makes_a_clean_pin_discard_only() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    pool.begin_read_view("t0")
    held = pool.pin(FILE, 0)
    writes_before = tuple(device.write_calls)

    pool.begin_read_view("t1")

    assert pool._doomed[(FILE, 0)][0].discard_unwritten is True
    held.update_slot(0, b"late-stale-holder")
    pool.unpin(FILE, 0, dirty=True, page=held)
    assert tuple(device.write_calls) == writes_before


def test_invalid_partial_targets_refuse_before_token_frame_or_epoch_moves() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2)
    pool.begin_read_view("old")
    for page_index in range(2):
        page = pool.pin(FILE, page_index)
        pool.unpin(FILE, page_index, page=page)
    epoch_before = pool.cache_drop_epoch(FILE)
    writes_before = tuple(device.write_calls)

    with pytest.raises(GrafxConfigurationError):
        pool.begin_read_view("new", changed_pages=(), changed_files=FILE)

    assert pool.read_view_token() == "old"
    assert pool.is_resident(FILE, 0)
    assert pool.is_resident(FILE, 1)
    assert pool.cache_drop_epoch(FILE) == epoch_before
    assert tuple(device.write_calls) == writes_before


def test_partial_target_iterables_are_bounded_before_pool_state_moves() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    pool.begin_read_view("old")
    page = pool.pin(FILE, 0)
    pool.unpin(FILE, 0, page=page)
    epoch_before = pool.cache_drop_epoch(FILE)
    writes_before = tuple(device.write_calls)

    with pytest.raises(GrafxConfigurationError) as refused:
        pool.begin_read_view(
            "new",
            changed_pages=((FILE, page_index) for page_index in range(1025)),
            expected_previous="old",
        )

    assert refused.value.details == {"field": "read_view_changes", "limit": 1024}
    assert pool.read_view_token() == "old"
    assert pool.is_resident(FILE, 0)
    assert pool.cache_drop_epoch(FILE) == epoch_before
    assert tuple(device.write_calls) == writes_before


def test_discarded_header_page_is_never_settled_as_detached_abandoned_space() -> None:
    """The reserved page is withdrawn from growth tracking, never queued for settling."""
    device = MemoryDevice()
    pool = make_pool(
        device,
        RecordingMetrics(),
        page_sequence_fence=lambda _file, page_index: page_index == 0,
    )
    device.create(FILE)
    header = pool.allocate(FILE, int(PageType.META))
    pool.unpin(FILE, header.page_index, page=header)
    writes_before = tuple(device.write_calls)

    assert pool.discard(FILE, 0)
    assert pool.settle_abandoned(FILE) == 0
    assert tuple(device.write_calls) == writes_before


def test_detached_fenced_header_settlement_fails_before_device_io() -> None:
    """Even corrupted reuse bookkeeping cannot publish page 0 without its CAS base."""
    device = MemoryDevice()
    pool = make_pool(
        device,
        RecordingMetrics(),
        page_sequence_fence=lambda _file, page_index: page_index == 0,
    )
    device.create(FILE)
    device.allocate(FILE, 1)
    pool._abandoned[FILE] = [0]
    writes_before = tuple(device.write_calls)

    with pytest.raises(GrafxUnsupportedOperation) as refused:
        pool.settle_abandoned(FILE)

    assert refused.value.details["field"] == "page_sequence_base"
    assert tuple(device.write_calls) == writes_before


def test_a_stale_page_zero_writer_is_refused_without_overwriting_the_device() -> None:
    device = MemoryDevice()

    def fenced(_file: str, page_index: int) -> bool:
        return page_index == 0

    first = make_pool(device, RecordingMetrics(), page_sequence_fence=fenced)
    stale = make_pool(device, RecordingMetrics(), page_sequence_fence=fenced)
    reserve_header(first)
    first.flush(FILE)
    with stale.pinned(FILE, 0):
        pass

    with first.pinned(FILE, 0) as current:
        current.update_slot(0, b"foreign-newer")
    first.flush(FILE)
    foreign_image = device.raw_page(FILE, 0)

    with stale.pinned(FILE, 0) as cached:
        cached.update_slot(0, b"stale-overwrite")
    with pytest.raises(GrafxUnsupportedOperation) as refused:
        stale.flush(FILE)

    assert refused.value.details["field"] == "page_sequence_conflict"
    assert device.raw_page(FILE, 0) == foreign_image


def test_page_zero_redo_may_publish_an_image_ahead_of_its_device_base() -> None:
    device = MemoryDevice()
    pool = make_pool(
        device,
        RecordingMetrics(),
        page_sequence_fence=lambda _file, page_index: page_index == 0,
    )
    reserve_header(pool)
    pool.flush(FILE)
    base_seq = pool.read_fresh_page(FILE, 0).seq
    logged = pool.read_fresh_page(FILE, 0)
    logged.update_slot(0, b"redo-newer")
    logged.page_lsn = 9
    logged.seq = base_seq + 8
    image = pool.codec.encode_page(logged)

    assert apply_page_image(pool, FILE, 0, image)
    pool.flush(FILE)

    published = pool.read_fresh_page(FILE, 0)
    assert published.read_slot(0) == b"redo-newer"
    assert published.seq > logged.seq


def test_page_zero_sequence_exhaustion_refuses_before_wrap_or_write() -> None:
    device = MemoryDevice()
    device.create(FILE)
    assert device.allocate(FILE, 1) == 0
    page = Page(int(PageType.META), page_size=device.page_size, page_index=0)
    page.insert_slot(b"near-wrap")
    page.seq = pool_module.MAX_SEQ - 1
    device.write_page(FILE, 0, PageCodecV1(device.page_size).encode_page(page))
    pool = make_pool(
        device,
        RecordingMetrics(),
        page_sequence_fence=lambda _file, page_index: page_index == 0,
    )
    with pool.pinned(FILE, 0) as cached:
        cached.update_slot(0, b"must-not-wrap")
    before = device.raw_page(FILE, 0)

    with pytest.raises(GrafxUnsupportedOperation) as refused:
        pool.flush(FILE)

    assert refused.value.details["field"] == "page_sequence_exhausted"
    assert device.raw_page(FILE, 0) == before


def test_invalidating_a_pinned_page_is_refused() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2)
    pool.pin(FILE, 0)
    with pytest.raises(GrafxUnsupportedOperation):
        pool.invalidate(FILE)
    assert pool.is_resident(FILE, 0)


def test_unpinning_a_page_that_is_not_pinned_is_refused() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    with pytest.raises(GrafxUnsupportedOperation):
        pool.unpin(FILE, 0)
    with pytest.raises(GrafxUnsupportedOperation):
        pool.unpin(FILE, 99)


def test_the_context_manager_unpins_even_when_the_body_raises() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    with pytest.raises(ValueError):
        with pool.pinned(FILE, 0) as page:
            page.insert_slot(b"applied before the failure")
            raise ValueError("the body failed")
    assert pool.pin_count(FILE, 0) == 0
    # A change applied before the failure is still a change: the page carries its own flag, so
    # the pool cannot forget it just because the block did not finish.
    assert pool.flush(FILE) == 1


def test_an_allocated_page_comes_back_pinned_and_survives_pressure() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=1)
    device.create(FILE)
    page = pool.allocate(FILE, int(PageType.HEAP))
    assert pool.pin_count(FILE, page.page_index) == 1
    page.insert_slot(b"written into a fresh page")
    with pytest.raises(GrafxBufferBudgetExceeded):
        pool.allocate(FILE, int(PageType.HEAP))
    pool.unpin(FILE, page.page_index, dirty=True)
    pool.flush(FILE)
    pool.invalidate()
    with pool.pinned(FILE, 0) as reread:
        assert reread.read_slot(0) == b"written into a fresh page"


def test_the_pool_exposes_the_ports_the_stores_need() -> None:
    device = MemoryDevice()
    codec = PageCodecV1(device.page_size)
    pool = BufferPool(
        device,
        codec,
        RecordingMetrics(),
        budget_bytes=device.page_size * 4,
        db_label="alpha",
    )
    assert pool.storage is device
    assert pool.codec is codec
    assert pool.page_size == device.page_size
    assert pool.capacity_pages == 4
    assert pool.db_label == "alpha"
    assert "alpha" in repr(pool)


# --- torn reads ------------------------------------------------------------------------------


def torn_image(raw: bytes) -> bytes:
    """Return the image with an odd sequence counter and a checksum that still matches."""
    image = bytearray(raw)
    struct.pack_into("<I", image, 16, 7)
    struct.pack_into("<I", image, 0, crc32c(bytes(image[4:])))
    return bytes(image)


def test_a_page_being_written_is_read_again_until_it_settles() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    pool.invalidate()
    attempts = {"count": 0}

    def reader(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        attempts["count"] += 1
        return torn_image(raw) if attempts["count"] <= 3 else raw

    device.page_reader = reader
    page = pool.pin(FILE, 0)
    assert attempts["count"] == 4
    assert page.seq % 2 == 0
    assert page.read_slot(0) == b"page-0"


def test_a_page_that_never_settles_is_declared_corrupt_with_its_location() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    pool.invalidate()
    device.page_reader = lambda file, page_index, raw: torn_image(raw)
    device.read_calls.clear()
    with pytest.raises(GrafxCorruptionDetected) as raised:
        pool.pin(FILE, 0)
    assert raised.value.details["file"] == FILE
    assert raised.value.details["page"] == 0
    assert raised.value.details["attempts"] == TORN_READ_RETRY_BUDGET + 1
    assert len(device.read_calls) == TORN_READ_RETRY_BUDGET + 1
    assert not pool.is_resident(FILE, 0)


def test_a_checksum_failure_is_retried_and_then_reported() -> None:
    device, metrics = MemoryDevice(), RecordingMetrics()
    pool = make_pool(device, metrics)
    seed_pages(pool, 1)
    pool.invalidate()

    def scramble(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        image = bytearray(raw)
        image[PAGE_HEADER_SIZE] ^= 0xFF
        return bytes(image)

    device.page_reader = scramble
    with pytest.raises(GrafxCorruptionDetected) as raised:
        pool.pin(FILE, 0)
    assert raised.value.details["page"] == 0
    assert len(metrics.values_of(CHECKSUM_FAILURES_TOTAL)) == TORN_READ_RETRY_BUDGET + 1
    assert metrics.labels_of(CHECKSUM_FAILURES_TOTAL)[0] == {"kind": "page"}


def test_a_transient_checksum_failure_is_survived() -> None:
    device, metrics = MemoryDevice(), RecordingMetrics()
    pool = make_pool(device, metrics)
    seed_pages(pool, 1)
    pool.invalidate()
    attempts = {"count": 0}

    def flaky(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        attempts["count"] += 1
        if attempts["count"] > 2:
            return raw
        image = bytearray(raw)
        image[PAGE_HEADER_SIZE] ^= 0xFF
        return bytes(image)

    device.page_reader = flaky
    page = pool.pin(FILE, 0)
    assert page.read_slot(0) == b"page-0"
    assert len(metrics.values_of(CHECKSUM_VERIFICATIONS_TOTAL)) == 3
    assert len(metrics.values_of(CHECKSUM_FAILURES_TOTAL)) == 2


def test_every_write_advances_the_sequence_counter_by_two() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    page = pool.allocate(FILE, int(PageType.HEAP))
    pool.unpin(FILE, page.page_index, dirty=True)
    for expected in (2, 4, 6):
        pool.flush(FILE)
        stored = Page.from_bytes(device.raw_page(FILE, 0), page_size=device.page_size)
        assert stored.seq == expected
        assert stored.seq % 2 == 0
        with pool.pinned(FILE, 0) as resident:
            resident.page_lsn += 1


# --- page chains ------------------------------------------------------------------------------


def test_a_payload_larger_than_a_page_survives_a_chain() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=2)
    reserve_header(pool)
    payload = bytes((index * 7) % 256 for index in range(3000))
    pages = write_chain(pool, FILE, payload)
    assert len(pages) == 7
    assert read_chain(pool, FILE, pages[0], page_type=int(PageType.OVERFLOW)) == payload


def test_a_chain_reuses_the_pages_it_is_given_before_it_grows_the_file() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    first = write_chain(pool, FILE, b"x" * 2000)
    before = device.page_count(FILE)
    second = write_chain(pool, FILE, b"y" * 2000, reuse=first)
    assert second == first
    assert device.page_count(FILE) == before
    assert read_chain(pool, FILE, second[0]) == b"y" * 2000


def test_an_empty_payload_still_occupies_one_page() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    pages = write_chain(pool, FILE, b"")
    assert len(pages) == 1
    assert read_chain(pool, FILE, pages[0]) == b""


def test_a_chain_of_the_wrong_page_type_is_refused() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    pages = write_chain(pool, FILE, b"payload", page_type=int(PageType.CATALOG))
    with pytest.raises(GrafxCorruptionDetected):
        read_chain(pool, FILE, pages[0], page_type=int(PageType.OVERFLOW))


def test_a_chain_that_loops_back_is_reported_rather_than_followed() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    pages = write_chain(pool, FILE, b"z" * 2000)
    with pool.pinned(FILE, pages[-1]) as page:
        page.next_page = pages[0]
    with pytest.raises(GrafxCorruptionDetected) as raised:
        read_chain(pool, FILE, pages[0])
    assert raised.value.details["page"] == pages[0]


def test_a_refused_invalidate_leaves_every_page_where_it_was() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 4)
    with pool.pinned(FILE, 0) as page:
        page.insert_slot(b"changed")
    pool.pin(FILE, 3)
    resident = pool.used_bytes()
    with pytest.raises(GrafxUnsupportedOperation):
        pool.invalidate()
    assert pool.used_bytes() == resident
    assert all(pool.is_resident(FILE, index) for index in range(4))
    assert pool.flush(FILE) == 1


def test_a_failing_write_back_never_loses_the_page_it_could_not_write() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=2)
    seed_pages(pool, 3)
    pool.invalidate()
    with pool.pinned(FILE, 0) as page:
        page.insert_slot(b"a change that the device will refuse")

    def refuse(file: str, page_index: PageIndex, data: bytes) -> None:
        raise GrafxDeviceFull("The device refused to grow.", file=file, page=page_index)

    device.write_page = refuse  # type: ignore[method-assign]
    with pytest.raises(GrafxDeviceFull):
        pool.flush(FILE)
    assert pool.is_resident(FILE, 0)
    with pool.pinned(FILE, 0) as page:
        assert page.dirty
        assert page.read_slot(1) == b"a change that the device will refuse"


# --- review fixes ------------------------------------------------------------------------------


def test_a_refused_allocation_does_not_grow_the_file() -> None:
    # The refusal is retryable, so a retry loop that grew the file once per attempt would inflate
    # the data file without bound, and G6 offers no sanctioned way to shrink it back.
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=2)
    device.create(FILE)
    held = [pool.allocate(FILE, int(PageType.HEAP)) for _ in range(2)]
    before = device.page_count(FILE)
    for _attempt in range(5):
        with pytest.raises(GrafxBufferBudgetExceeded):
            pool.allocate(FILE, int(PageType.HEAP))
    assert device.page_count(FILE) == before
    assert len(held) == 2


def test_a_refused_pin_does_not_grow_the_file_either() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=2)
    seed_pages(pool, 4)
    pool.invalidate()
    pool.pin(FILE, 0)
    pool.pin(FILE, 1)
    before = device.page_count(FILE)
    with pytest.raises(GrafxBufferBudgetExceeded):
        pool.pin(FILE, 2)
    assert device.page_count(FILE) == before


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (0, 2),
        (2, 4),
        (7, 8),
        (1, 2),
        (0xFFFFFFFE, 0),
        (0xFFFFFFFF, 0),
        (0xFFFFFFFD, 0xFFFFFFFE),
    ],
)
def test_the_sequence_counter_always_lands_on_an_even_value(
    current: int, expected: int
) -> None:
    assert next_seq(current) == expected
    assert next_seq(current) % 2 == 0


def test_an_odd_sequence_counter_is_normalised_by_the_next_write() -> None:
    # Preserving the parity it found would leave a page unreadable for good: every later write
    # would keep it odd, and every read would spend the whole retry budget and then fail.
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    page = pool.allocate(FILE, int(PageType.HEAP))
    page.seq = 7
    page.insert_slot(b"payload")
    pool.unpin(FILE, page.page_index, dirty=True)
    pool.flush(FILE)
    stored = Page.from_bytes(device.raw_page(FILE, 0), page_size=device.page_size)
    assert stored.seq == 8
    assert stored.seq % 2 == 0
    pool.invalidate()
    assert pool.pin(FILE, 0).read_slot(0) == b"payload"


# --- an allocated page nobody has written -------------------------------------------------------


def test_a_page_that_was_allocated_and_never_written_reads_as_free() -> None:
    # This is the crash window between a PAGE_ALLOC record and the WRITE_PAGE that was to follow
    # it. The device zero-fills what it allocates, so the page is free, not damaged.
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    device.allocate(FILE, 1)
    device.read_calls.clear()
    page = pool.pin(FILE, 0)
    assert page.page_type == int(PageType.FREE)
    assert page.slot_count == 0
    assert page.page_lsn == 0
    assert not page.dirty
    assert len(device.read_calls) == 1, (
        "the retry budget is not spent on a page nobody wrote"
    )


def test_a_written_page_can_never_look_like_an_unwritten_one() -> None:
    # The two states must be disjoint, or reading zeros as free would be a way of hiding damage.
    from okto_grafx.domain.page import MAX_PAGE_SIZE, MIN_PAGE_SIZE, is_unwritten_image

    size = MIN_PAGE_SIZE
    while size <= MAX_PAGE_SIZE:
        for page_type in PageType:
            page = Page(int(page_type), page_size=size)
            assert not is_unwritten_image(page.to_bytes(), size)
            page.insert_slot(b"")
            assert not is_unwritten_image(page.to_bytes(), size)
        size *= 2


def test_a_short_read_is_still_corruption_and_not_an_unwritten_page() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 1)
    pool.invalidate()
    device.page_reader = lambda file, page_index, raw: bytes(len(raw) - 1)
    with pytest.raises(GrafxCorruptionDetected):
        pool.pin(FILE, 0)


# --- the redo rule -------------------------------------------------------------------------------


def image_with_lsn(
    pool: BufferPool, page_lsn: int, payload: bytes, seq: int = 0
) -> bytes:
    """Return an encoded page image carrying that log position and payload."""
    page = Page(int(PageType.HEAP), page_size=pool.page_size)
    page.page_lsn = page_lsn
    page.seq = seq
    page.insert_slot(payload)
    return pool.codec.encode_page(page)


def test_redo_installs_an_image_over_a_page_that_was_never_written() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    device.allocate(FILE, 3)
    assert (
        apply_page_image(pool, FILE, 2, image_with_lsn(pool, 500, b"replayed")) is True
    )
    with pool.pinned(FILE, 2) as page:
        assert page.page_lsn == 500
        assert page.read_slot(0) == b"replayed"


def test_redo_grows_the_file_for_a_page_that_is_not_there_at_all() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    assert apply_page_image(pool, FILE, 4, image_with_lsn(pool, 10, b"beyond")) is True
    assert device.page_count(FILE) == 5
    with pool.pinned(FILE, 4) as page:
        assert page.read_slot(0) == b"beyond"


def test_redo_is_idempotent_and_never_moves_a_page_backwards() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    device.allocate(FILE, 1)
    newer = image_with_lsn(pool, 300, b"newer")
    assert apply_page_image(pool, FILE, 0, newer) is True
    assert apply_page_image(pool, FILE, 0, newer) is False
    assert apply_page_image(pool, FILE, 0, image_with_lsn(pool, 100, b"older")) is False
    with pool.pinned(FILE, 0) as page:
        assert page.page_lsn == 300
        assert page.read_slot(0) == b"newer"


def test_redo_never_installs_an_odd_sequence_counter() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    device.allocate(FILE, 1)
    assert (
        apply_page_image(pool, FILE, 0, image_with_lsn(pool, 9, b"odd", seq=7)) is True
    )
    with pool.pinned(FILE, 0) as page:
        assert page.seq % 2 == 0
    pool.flush(FILE)
    pool.invalidate()
    assert pool.pin(FILE, 0).read_slot(0) == b"odd", (
        "an odd counter would be unreadable for good"
    )


def test_redo_refuses_a_damaged_image() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    device.allocate(FILE, 1)
    damaged = bytearray(image_with_lsn(pool, 10, b"payload"))
    damaged[40] ^= 0xFF
    with pytest.raises(GrafxCorruptionDetected):
        apply_page_image(pool, FILE, 0, bytes(damaged))


def test_a_page_image_that_is_not_bytes_is_refused_as_a_grafx_error() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    device.allocate(FILE, 1)
    for image in ("an image", 42, None, ["bytes"], Page(page_size=device.page_size)):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            apply_page_image(pool, FILE, 0, image)  # type: ignore[arg-type]
        assert raised.value.details["file"] == FILE
        assert raised.value.details["page"] == 0


def test_a_page_image_may_be_any_byte_buffer() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    device.allocate(FILE, 1)
    image = image_with_lsn(pool, 10, b"payload")
    assert apply_page_image(pool, FILE, 0, bytearray(image)) is True
    assert apply_page_image(pool, FILE, 0, memoryview(image)) is False


@pytest.mark.parametrize("page_index", [True, False, "0", 1.0, None, -1, 1 << 32])
def test_a_page_index_that_is_not_an_unsigned_integer_is_refused(
    page_index: object,
) -> None:
    # A bool is the dangerous one: True would silently mean page 1, so a flag passed where an
    # index belongs would grow a file and write a page nobody asked for.
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    device.allocate(FILE, 1)
    image = image_with_lsn(pool, 10, b"payload")
    before = device.page_count(FILE)
    with pytest.raises(GrafxCorruptionDetected):
        apply_page_image(pool, FILE, page_index, image)  # type: ignore[arg-type]
    with pytest.raises(GrafxCorruptionDetected):
        grow_to(pool, FILE, page_index)  # type: ignore[arg-type]
    with pytest.raises(GrafxCorruptionDetected):
        pool.pin(FILE, page_index)  # type: ignore[arg-type]
    with pytest.raises(GrafxCorruptionDetected):
        pool.unpin(FILE, page_index)  # type: ignore[arg-type]
    assert device.page_count(FILE) == before


def test_a_chain_written_into_a_file_with_no_pages_leaves_no_page_behind() -> None:
    # The refusal has to precede the allocation. Allocating first stamps a chain image onto
    # page 0 and leaves the file permanently un-bootstrappable, which is the opposite of what
    # refusing was for. C7 index files use this same helper.
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create("index.idx")
    with pytest.raises(GrafxCorruptionDetected) as raised:
        write_chain(pool, "index.idx", b"payload")
    assert raised.value.details["page"] == 0
    assert device.page_count("index.idx") == 0
    assert not pool.is_resident("index.idx", 0)


def test_a_chain_written_into_a_file_that_does_not_exist_is_refused() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    with pytest.raises(GrafxCorruptionDetected):
        write_chain(pool, "absent.idx", b"payload")
    assert not device.exists("absent.idx")


def test_an_overflow_pointer_read_from_a_negative_offset_is_refused() -> None:
    from okto_grafx.domain.model.record import decode_overflow_pointer

    with pytest.raises(GrafxCorruptionDetected):
        decode_overflow_pointer(bytes(44), -100)
    with pytest.raises(GrafxCorruptionDetected):
        decode_overflow_pointer(bytes(44), 41)
    assert decode_overflow_pointer(bytes(44), 40) == 0


@pytest.mark.parametrize("shape", ["self loop", "two cycle", "long cycle"])
def test_a_page_chain_that_closes_is_refused_rather_than_walked(shape: str) -> None:
    # The overflow reader is a walker too, and an unterminated walk there blocks the process.
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    pages = write_chain(pool, FILE, b"z" * 2000)
    assert len(pages) >= 4
    if shape == "self loop":
        source, target = pages[0], pages[0]
    elif shape == "two cycle":
        source, target = pages[1], pages[0]
    else:
        source, target = pages[-1], pages[0]
    with pool.pinned(FILE, source) as page:
        page.next_page = target
    with pytest.raises(GrafxCorruptionDetected) as raised:
        read_chain(pool, FILE, pages[0])
    assert raised.value.details["field"] == "cycle"


def test_the_chain_bound_refuses_a_walk_the_file_cannot_justify() -> None:
    """The bound is tested where nothing else can answer for it (A34).

    While the visited set works, this guard can never fire, so no end-to-end test reaches it --
    which is exactly why the mutation removing it survived until it had a test of its own. Its
    job is to end a walk when the visited set does NOT work, so that a broken guard is a failing
    test rather than a hung machine.
    """
    refuse_endless_chain("heap.dat", 5, 5)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        refuse_endless_chain("heap.dat", 6, 5)
    assert raised.value.details["field"] == "chain_length"
    assert raised.value.details["steps"] == 6
    assert raised.value.details["file"] == "heap.dat"
    assert "does not end" in raised.value.message


def test_the_chain_bound_is_the_size_of_the_file_and_not_of_a_hint() -> None:
    # A bound taken from a stored count would refuse a chain that had merely drifted (A40); this
    # one comes from the device, so it can only ever refuse a chain longer than the file itself.
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    pages = write_chain(pool, FILE, b"z" * 2000)
    assert len(pages) + 1 <= device.page_count(FILE) + 1
    refuse_endless_chain(FILE, device.page_count(FILE), device.page_count(FILE) + 1)
    assert read_chain(pool, FILE, pages[0]) == b"z" * 2000


def test_the_overflow_reader_still_ends_when_the_visited_set_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForgetfulSet(set):
        def __contains__(self, item: object) -> bool:
            return False

    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    pages = write_chain(pool, FILE, b"z" * 2000)
    with pool.pinned(FILE, pages[-1]) as page:
        page.next_page = pages[0]
    monkeypatch.setattr(pool_module, "visited_pages", ForgetfulSet)
    with pin_ceiling(monkeypatch):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            read_chain(pool, FILE, pages[0])
    assert raised.value.details["field"] == "chain_length"


# --- DEF-1 and its siblings: every door that says a derived walk may no longer hold -------------


def test_dropping_the_cache_of_a_file_that_has_nothing_resident_still_says_so() -> None:
    """The sibling of the existing invalidate test, and the one that was never enumerated.

    Deriving the names to bump from the frames made this door do nothing whenever ordinary budget
    eviction had already emptied it -- which is the case where the next read comes from the
    device and the derived walk is most in need of being distrusted.

    Nothing here may call invalidate() before it measures: doing so seeded the per-file counter
    and hid that the every-file branch had no reading of its own (A72).
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=2)
    seed_pages(pool, 3, file="resident.dat")
    empty = "no-frames.dat"
    assert not pool.is_resident(empty, 0), (
        "the file under test must hold no frame at all"
    )
    before = pool.cache_drop_epoch(empty)

    pool.invalidate(empty)
    assert pool.cache_drop_epoch(empty) == before + 1, (
        "an empty cache still had to be dropped"
    )
    pool.invalidate()
    assert pool.cache_drop_epoch(empty) == before + 2


def test_dropping_every_cache_moves_the_epoch_of_a_pool_that_has_never_been_read() -> (
    None
):
    """The every-file branch, measured from a pool whose counters have never been written.

    invalidate(file) and invalidate() must be the same strength, and only the branch under test
    can move this reading: nothing else in the test touches a page at all.
    """
    pool = make_pool(MemoryDevice(), RecordingMetrics())
    assert pool.cache_drop_epoch(FILE) == 0
    pool.invalidate()
    assert pool.cache_drop_epoch(FILE) == 1, "invalidate() spoke for no file at all"
    pool.invalidate()
    assert pool.cache_drop_epoch(FILE) == 2


def test_dropping_every_cache_speaks_for_a_file_this_pool_has_never_touched() -> None:
    """A walk derived over a file is held by its own store, not by this pool's frame table.

    So the names this door must cover cannot be read off the frames or off any dict this pool
    happens to have written -- including files that do not exist when the door is used.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2, file="heap.dat")
    stranger_before = pool.cache_drop_epoch("never-touched.dat")

    pool.invalidate()

    assert pool.cache_drop_epoch("never-touched.dat") == stranger_before + 1
    seed_pages(pool, 2, file="created-later.dat")
    created_later = pool.cache_drop_epoch("created-later.dat")
    pool.invalidate()
    assert pool.cache_drop_epoch("created-later.dat") == created_later + 1


def test_the_every_file_branch_and_the_one_file_branch_move_a_reading_by_the_same_step() -> (
    None
):
    """Two spellings of one statement must not have two strengths (A66.1)."""
    one_file = make_pool(MemoryDevice(), RecordingMetrics())
    every_file = make_pool(MemoryDevice(), RecordingMetrics())
    for pool in (one_file, every_file):
        seed_pages(pool, 2)
    one_file.invalidate(FILE)
    every_file.invalidate()
    assert one_file.cache_drop_epoch(FILE) == every_file.cache_drop_epoch(FILE) == 1


def test_dropping_one_file_does_not_speak_for_another() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 2, file="heap.dat")
    seed_pages(pool, 2, file="catalog.dat")
    heap_before = pool.cache_drop_epoch("heap.dat")
    catalog_before = pool.cache_drop_epoch("catalog.dat")
    pool.invalidate("heap.dat")
    assert pool.cache_drop_epoch("heap.dat") == heap_before + 1
    assert pool.cache_drop_epoch("catalog.dat") == catalog_before


def test_a_chain_that_takes_pages_from_another_structure_says_so() -> None:
    # Freshly allocated pages belong to no chain; pages offered for reuse were part of one until
    # this call rewrote their next_page.
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    first = write_chain(pool, FILE, b"x" * 2000)
    after_fresh = pool.structure_epoch(FILE)
    write_chain(pool, FILE, b"y" * 2000, reuse=first)
    assert pool.structure_epoch(FILE) == after_fresh + 1


def test_a_redo_says_the_structure_of_the_file_may_have_changed() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    device.allocate(FILE, 1)
    before = pool.structure_epoch(FILE)
    assert (
        apply_page_image(pool, FILE, 0, image_with_lsn(pool, 500, b"replayed")) is True
    )
    assert pool.structure_epoch(FILE) == before + 1
    # An image that is refused changes nothing, so it says nothing.
    assert apply_page_image(pool, FILE, 0, image_with_lsn(pool, 100, b"older")) is False
    assert pool.structure_epoch(FILE) == before + 1


def test_a_refused_redo_names_the_page_it_could_not_apply() -> None:
    """DEF-4: the codec port carries no page index, so a decode failure names no location.

    Section 8.6 asks a finding for file, page and the rest; without them C6 has nothing to build
    one from. The caller of this door knows both.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    device.allocate(FILE, 2)
    damaged = bytearray(image_with_lsn(pool, 10, b"payload"))
    damaged[40] ^= 0xFF
    with pytest.raises(GrafxCorruptionDetected) as raised:
        apply_page_image(pool, FILE, 1, bytes(damaged))
    assert raised.value.details["file"] == FILE
    assert raised.value.details["page"] == 1
    assert raised.value.details["field"] == "checksum"


def test_a_chain_refuses_a_reuse_list_that_names_one_page_twice() -> None:
    """Each chunk clears its page before writing, so a page named twice keeps only the last.

    The call returned successfully carrying a fraction of what it was given -- 1024 bytes in, a
    single chunk readable -- with no error at all. write_chain is exported, so C4, C6, C7 and C9
    reach it; today's only caller passes chain_pages(), which refuses cycles, so the in-repo path
    was safe and nothing exercised the aliased list.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    device.allocate(FILE, 4)
    payload = bytes(range(256)) * 4
    before = device.page_count(FILE)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        write_chain(
            pool, FILE, payload, page_type=int(PageType.OVERFLOW), reuse=(1, 1, 1)
        )
    assert raised.value.details["field"] == "reuse"
    assert raised.value.details["page"] == 1
    assert device.page_count(FILE) == before, "a refused chain grew the file"

    # A list of distinct pages is written whole, which is what the refusal is protecting.
    pages = write_chain(
        pool, FILE, payload, page_type=int(PageType.OVERFLOW), reuse=(1, 2, 3)
    )
    assert read_chain(pool, FILE, pages[0], page_type=int(PageType.OVERFLOW)) == payload


# --- F4: a page index off a log record is disk-sourced, so the growth it asks for is bounded ----


def test_a_redo_may_bridge_a_gap_left_by_an_interrupted_allocation() -> None:
    """The bound has to admit the case it exists for: a small gap from one crashed operation."""
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    present = pool.storage.page_count(FILE)
    allocated = grow_to(pool, FILE, present + 3)
    assert allocated == 4
    assert pool.storage.page_count(FILE) == present + 4


def test_a_page_index_far_past_the_end_is_refused_instead_of_allocated() -> None:
    """Only the gap bound produces this: without it the call succeeds and the file grows.

    The index comes off a log record, so a damaged one is a number this door would otherwise
    spend hours honouring -- and G6 forbids ever shrinking the zero-filled pages away again.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    before = pool.storage.page_count(FILE)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        grow_to(pool, FILE, before + MAX_REDO_GAP_PAGES)

    assert raised.value.details["field"] == "page_index"
    assert raised.value.details["limit"] == MAX_REDO_GAP_PAGES
    assert raised.value.details["page_count"] == before
    assert pool.storage.page_count(FILE) == before, "the refusal still grew the file"


def test_the_gap_bound_names_both_numbers_it_compared() -> None:
    """A refusal that says only 'too far' cannot be acted on by C6, which has to decide whether
    the record is damaged or the file is the wrong one."""
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    grow_to(pool, FILE, 40)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        grow_to(pool, FILE, 1 << 31)
    details = raised.value.details
    assert details["page"] == 1 << 31
    assert details["page_count"] == 41
    assert details["limit"] == MAX_REDO_GAP_PAGES


# --- D7: dropping a cache is not relinking pages, and one counter could not say both -----------


def test_dropping_the_cache_moves_no_structure_reading() -> None:
    """invalidate() writes every dirty frame back before it forgets it, so the links on the
    device afterwards are exactly the links a walk found before it.

    Counting a drop as a relink made CatalogStore.save() refuse after a plain invalidate() and
    sent the caller to a remedy that would have discarded its tables (D7, round 8). Only the
    split produces this reading: before it, both numbers moved together.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 3)
    structural = pool.structure_epoch(FILE)

    pool.invalidate()
    pool.invalidate(FILE)

    assert pool.structure_epoch(FILE) == structural, "dropping a cache relinked nothing"
    assert pool.cache_drop_epoch(FILE) == 2
    assert pool.derived_epoch(FILE) == structural + 2


def test_a_relink_moves_both_the_structure_reading_and_the_conservative_one() -> None:
    """The other half: the split must not have made structural change unsayable."""
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    first = write_chain(pool, FILE, b"x" * 2000)
    structural = pool.structure_epoch(FILE)
    conservative = pool.derived_epoch(FILE)
    drops = pool.cache_drop_epoch(FILE)

    write_chain(pool, FILE, b"y" * 2000, reuse=first)

    assert pool.structure_epoch(FILE) == structural + 1
    assert pool.derived_epoch(FILE) == conservative + 1
    assert pool.cache_drop_epoch(FILE) == drops, "a relink is not a cache drop either"


# --- C6: a decoded page must not name page zero -------------------------------------------------


def test_a_page_decoded_through_the_port_reports_page_zero_until_it_is_stamped() -> (
    None
):
    """The defect C6 met, pinned as a property of the PORT rather than of any C1 door.

    decode_page cannot take a page index -- section 4 is frozen -- so every page it returns
    claims index 0, and page 0 is a real page: the reserved file header. A message naming it is
    not vague, it is wrong. This test does not claim C1's redo door is what fixes that; it pins
    what the port does, and that the pool's own pages always name themselves.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    seed_pages(pool, 4)
    target = 3
    image = Page(int(PageType.HEAP), page_size=pool.page_size, page_index=target)
    image.page_lsn = 500
    image.insert_slot(b"a chunk the redo installed")
    raw = pool.codec.encode_page(image)

    decoded = pool.codec.decode_page(raw, verify=True)
    assert decoded.page_index == 0, "the port really does hand back an unstamped page"

    assert apply_page_image(pool, FILE, target, raw) is True
    with pool.pinned(FILE, target) as page:
        assert page.page_index == target
        with pytest.raises(GrafxError) as raised:
            page.read_slot(9)
    assert raised.value.details["page"] == target, "a pooled page named the wrong page"


def test_a_stamped_page_names_itself_in_every_refusal() -> None:
    """The stamp is the remedy a caller reaching this through the port has to apply."""
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    image = Page(int(PageType.HEAP), page_size=pool.page_size, page_index=11)
    slot = image.insert_slot(b"payload")
    image.free_slot(slot)
    decoded = pool.codec.decode_page(pool.codec.encode_page(image), verify=True)
    assert decoded.page_index == 0

    decoded.page_index = 11

    with pytest.raises(GrafxError) as raised:
        decoded.read_slot(slot)
    assert raised.value.details["page"] == 11
    assert raised.value.details["field"] == "freed_slot"


# --- the offline twin of write_chain: images, no writes, no allocations -------------------------


def test_building_chain_images_writes_nothing_and_allocates_nothing() -> None:
    """The whole reason the door exists: a change that can still be refused must be a VALUE.

    write_chain makes the chain reachable the moment it runs -- the frames are in the pool and
    the next flush of that file carries them to the device, whether or not the caller was ever
    allowed to commit. This one answers with bytes and leaves the file exactly as it found it.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    pool.flush(FILE)
    before = [device.raw_page(FILE, index) for index in range(device.page_count(FILE))]
    device.write_calls.clear()

    images = build_chain_images(pool, FILE, b"z" * 2000)

    assert len(images) == 5
    assert device.page_count(FILE) == len(before), "building images grew the file"
    pool.flush(FILE)
    after = [device.raw_page(FILE, index) for index in range(device.page_count(FILE))]
    assert after == before, "building images left something behind for the next flush"
    assert device.write_calls == []


def test_the_images_a_chain_would_write_carry_the_chain_the_chain_would_have() -> None:
    """Applying the images has to produce exactly the chain write_chain produces.

    The two doors answer the same question by two routes, so nothing but a test can stop them
    drifting apart (A67). Everything a reader depends on is compared: which pages, in what
    order, linked how, carrying which bytes.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=16)
    reserve_header(pool)
    payload = bytes((index * 11) % 256 for index in range(2500))

    planned = build_chain_images(pool, FILE, payload, page_type=int(PageType.CATALOG))
    written = write_chain(pool, FILE, payload, page_type=int(PageType.CATALOG))

    assert [index for index, _image in planned] == list(written)
    for index, image in planned:
        staged = pool.codec.decode_page(image, verify=True)
        with pool.pinned(FILE, index) as live:
            assert staged.page_type == live.page_type
            assert staged.next_page == live.next_page
            assert staged.slot_count == live.slot_count
            assert staged.read_slot(0) == live.read_slot(0)


def test_a_growing_chain_names_the_pages_the_file_does_not_have_yet() -> None:
    """Not growing the file is what keeps a refused change invisible, and it costs nothing.

    apply_page_image grows a file to reach a page an image names, so the commit that applies
    these allocates exactly the pages the change turned out to need -- and a commit that never
    happens allocates none.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=16)
    reserve_header(pool)
    first = write_chain(pool, FILE, b"x" * 400)
    pool.flush(FILE)
    present = device.page_count(FILE)

    images = build_chain_images(pool, FILE, b"y" * 2000, reuse=first)

    assert images[0][0] == first[0], "the page already in the chain is reused first"
    grown = [index for index, _image in images[1:]]
    assert grown == list(range(present, present + len(grown))), grown
    assert device.page_count(FILE) == present, "planning the change grew the file"
    assert grown, "the payload has to need a page the file does not have"

    for index, image in images:
        apply_page_image(pool, FILE, index, _later(pool, image))
    assert read_chain(pool, FILE, images[0][0]) == b"y" * 2000


def _later(pool: BufferPool, image: bytes) -> bytes:
    """Return the image stamped with a log position above anything the file holds.

    The redo rule installs an image only when it is newer than the page, so a test that applies
    an unstamped image is testing the refusal rather than the apply. The commit protocol stamps
    at the number the log assigned (CONTRACT.md section 8.5 step 6); this stands in for it.
    """
    page = pool.codec.decode_page(image, verify=True)
    page.page_lsn = 4096
    return pool.codec.encode_page(page)


def test_building_images_refuses_a_reuse_list_that_names_the_reserved_header_page() -> (
    None
):
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    device.allocate(FILE, 2)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        build_chain_images(pool, FILE, b"payload", reuse=(0,))
    assert raised.value.details["page"] == 0


def test_building_images_refuses_a_reuse_list_that_names_one_page_twice() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    device.allocate(FILE, 3)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        build_chain_images(pool, FILE, bytes(range(256)) * 4, reuse=(1, 1, 1))
    assert raised.value.details["field"] == "reuse"


def test_building_images_refuses_a_reuse_page_the_file_does_not_have() -> None:
    """A reuse index past the end collides with a prospective one: two images, one page.

    The caller keeps whichever it staged last, so the chain silently carries less than it was
    given -- the same harm the duplicate list has, arriving by the one route write_chain cannot
    take, because write_chain gets its new indices from the device.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    device.allocate(FILE, 1)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        build_chain_images(pool, FILE, b"x" * 2000, reuse=(1, 2, 3))
    assert raised.value.details["field"] == "reuse"
    assert raised.value.details["page"] == 2
    assert raised.value.details["page_count"] == 2


def test_building_images_for_a_file_with_no_pages_is_refused() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create("index.idx")
    with pytest.raises(GrafxCorruptionDetected) as raised:
        build_chain_images(pool, "index.idx", b"payload")
    assert raised.value.details["page"] == 0
    assert device.page_count("index.idx") == 0


def test_building_images_for_a_file_that_does_not_exist_is_refused() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    with pytest.raises(GrafxCorruptionDetected):
        build_chain_images(pool, "absent.idx", b"payload")
    assert not device.exists("absent.idx")


def test_an_empty_payload_still_gets_one_image() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    reserve_header(pool)
    images = build_chain_images(pool, FILE, b"")
    assert len(images) == 1
    for index, image in images:
        apply_page_image(pool, FILE, index, _later(pool, image))
    assert read_chain(pool, FILE, images[0][0]) == b""


# --- threads of one participant: the guard, and frames doomed by a read view ---------------------


def test_page_write_fence_orders_guard_then_section_and_allows_nested_write_back() -> (
    None
):
    """A rebuild can hold one page section while a nested page-0 CAS re-enters both locks."""
    entered: list[str] = []

    class RecordingReentrantLock:
        def __init__(self, name: str) -> None:
            self.name = name
            self.depth = 0
            self.lock = threading.RLock()

        def __enter__(self) -> None:
            self.lock.acquire()
            self.depth += 1
            entered.append(f"{self.name}+{self.depth}")

        def __exit__(self, *exc: object) -> None:
            entered.append(f"{self.name}-{self.depth}")
            self.depth -= 1
            self.lock.release()

    guard = RecordingReentrantLock("guard")
    section = RecordingReentrantLock("section")

    def page_section(_file: str, _page_index: PageIndex) -> RecordingReentrantLock:
        assert guard.depth > 0, (
            "the cross-process section was requested before the local guard"
        )
        return section

    device = MemoryDevice()
    pool = BufferPool(
        device,
        PageCodecV1(device.page_size),
        RecordingMetrics(),
        budget_bytes=device.page_size * 2,
        db_label="fenced",
        guard=guard,
        page_write_section=page_section,
        page_sequence_fence=lambda _file, page_index: page_index == 0,
    )
    device.create(FILE)
    header = pool.allocate(FILE, int(PageType.META))
    pool.unpin(FILE, 0, dirty=True, page=header)
    entered.clear()

    with pool.page_write_fence(FILE, 0):
        entered.append("body")
        assert pool.write_back(FILE, 0) is True

    assert entered == [
        "guard+1",
        "section+1",
        "body",
        "guard+2",
        "section+2",
        "section-2",
        "guard-2",
        "section-1",
        "guard-1",
    ]


def test_page_write_fence_validates_the_index_before_acquiring_either_lock() -> None:
    entered: list[str] = []

    @contextlib.contextmanager
    def guard() -> Iterator[None]:
        entered.append("guard")
        yield

    @contextlib.contextmanager
    def section(_file: str, _page_index: PageIndex) -> Iterator[None]:
        entered.append("section")
        yield

    device = MemoryDevice()
    pool = BufferPool(
        device,
        PageCodecV1(device.page_size),
        RecordingMetrics(),
        budget_bytes=device.page_size,
        db_label="validated",
        guard=guard(),
        page_write_section=section,
    )

    with pytest.raises(GrafxCorruptionDetected):
        with pool.page_write_fence(FILE, True):
            raise AssertionError("an invalid page index entered the fenced body")

    assert entered == []


def test_every_door_of_the_pool_runs_under_the_injected_guard() -> None:
    """The pool imports no mechanism; the composition root hands the lock in. Every door enters it.

    The doors are sequences of dictionary steps that are individually atomic and jointly not: a
    reader's eviction between a writer's lookup and its pin handed the same page out twice as two
    objects, and the writer's change landed in the orphan -- an index entry lost with nobody
    refused. The guard is what makes the doors atomic against each other.
    """
    entered = {"n": 0}

    class CountingGuard:
        def __enter__(self) -> None:
            entered["n"] += 1

        def __exit__(self, *exc: object) -> None:
            return None

    device = MemoryDevice()
    pool = BufferPool(
        device,
        PageCodecV1(device.page_size),
        RecordingMetrics(),
        budget_bytes=device.page_size * 4,
        db_label="guarded",
        guard=CountingGuard(),
    )
    seed_pages(pool, 2)
    before = entered["n"]
    pool.pin(FILE, 0)
    pool.is_resident(FILE, 0)
    pool.pin_count(FILE, 0)
    pool.unpin(FILE, 0)
    pool.flush()
    pool.begin_read_view("t1")
    pool.discard(FILE, 1)
    assert entered["n"] - before >= 7


def test_a_read_view_dooms_a_pinned_frame_instead_of_refusing_the_begin() -> None:
    """A searching thread holds a page pinned while another thread's begin() takes a read view.

    Refusing failed that begin -- and every thread's next begin -- for as long as anyone was
    reading, which turned a concurrent reader into a writer's refusal. The pinned frame is
    DOOMED instead: it leaves the table so the next pin reads the device, the holder keeps its
    object and releases exactly that object, and the last release drops it unwritten.
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=4)
    seed_pages(pool, 2)
    held = pool.pin(FILE, 0)  # a reader, outside any section
    assert pool.begin_read_view("another participant committed") is True
    # The frame left the table; the holder's object is still the holder's.
    assert not pool.is_resident(FILE, 0)
    fresh = pool.pin(FILE, 0)  # the next pin reads the device into a NEW frame
    assert fresh is not held
    assert pool.pin_count(FILE, 0) == 1
    pool.unpin(FILE, 0, page=held)  # releases the doomed one, not the fresh one
    assert pool.pin_count(FILE, 0) == 1
    pool.unpin(FILE, 0, page=fresh)
    assert pool.pin_count(FILE, 0) == 0
    # And an explicit invalidate still refuses over a pinned page: its callers mean it.
    pool.pin(FILE, 1)
    with pytest.raises(GrafxUnsupportedOperation):
        pool.invalidate()
    pool.unpin(FILE, 1)
