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

import struct

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.domain.errors import (
    GrafxBufferBudgetExceeded,
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import PageIndex
from okto_grafx.domain.page import PAGE_HEADER_SIZE, Page, PageType, crc32c
from okto_grafx.engine.buffer_pool import (
    BUFFER_BUDGET_EXCEEDED_TOTAL,
    BUFFER_BUDGET_USED_BYTES,
    BUFFER_POOL_METRICS,
    CHECKSUM_FAILURES_TOTAL,
    CHECKSUM_VERIFICATIONS_TOTAL,
    TORN_READ_RETRY_BUDGET,
    BufferPool,
    read_chain,
    write_chain,
)

from .conftest import MemoryDevice, RecordingMetrics, make_pool

FILE: str = "heap.dat"


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


def test_the_pool_registers_the_metrics_it_emits() -> None:
    device, metrics = MemoryDevice(), RecordingMetrics()
    make_pool(device, metrics)
    for descriptor in BUFFER_POOL_METRICS:
        assert metrics.registered[descriptor.name] is descriptor
    assert {descriptor.name for descriptor in BUFFER_POOL_METRICS} == {
        BUFFER_BUDGET_USED_BYTES,
        BUFFER_BUDGET_EXCEEDED_TOTAL,
        CHECKSUM_VERIFICATIONS_TOTAL,
        CHECKSUM_FAILURES_TOTAL,
    }


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


@pytest.mark.parametrize("label", ["", "a" * 65, "C:/data/db", "db name", "db/1", "\u00e9"])
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
        device, codec, RecordingMetrics(), budget_bytes=device.page_size * 4, db_label="alpha"
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
    device.create(FILE)
    payload = bytes((index * 7) % 256 for index in range(3000))
    pages = write_chain(pool, FILE, payload)
    assert len(pages) == 7
    assert read_chain(pool, FILE, pages[0], page_type=int(PageType.OVERFLOW)) == payload


def test_a_chain_reuses_the_pages_it_is_given_before_it_grows_the_file() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    first = write_chain(pool, FILE, b"x" * 2000)
    before = device.page_count(FILE)
    second = write_chain(pool, FILE, b"y" * 2000, reuse=first)
    assert second == first
    assert device.page_count(FILE) == before
    assert read_chain(pool, FILE, second[0]) == b"y" * 2000


def test_an_empty_payload_still_occupies_one_page() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    pages = write_chain(pool, FILE, b"")
    assert len(pages) == 1
    assert read_chain(pool, FILE, pages[0]) == b""


def test_a_chain_of_the_wrong_page_type_is_refused() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
    pages = write_chain(pool, FILE, b"payload", page_type=int(PageType.CATALOG))
    with pytest.raises(GrafxCorruptionDetected):
        read_chain(pool, FILE, pages[0], page_type=int(PageType.OVERFLOW))


def test_a_chain_that_loops_back_is_reported_rather_than_followed() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    device.create(FILE)
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
