"""A read view whose token moved only by this pool's own commit keeps its clean frames."""

from __future__ import annotations

from okto_grafx.domain.page import PageType
from okto_grafx.engine.buffer_pool import READ_VIEW_DROPS_TOTAL, BufferPool

from .conftest import MemoryDevice, RecordingMetrics, make_pool

FILE = "heap.dat"
CATALOG = "catalog.dat"


def _seed(pool: BufferPool, count: int, *, file: str = FILE) -> None:
    pool.storage.create(file)
    for number in range(count):
        page = pool.allocate(file, int(PageType.HEAP))
        page.insert_slot(f"page-{number}".encode())
        pool.unpin(file, page.page_index, dirty=True)
    pool.flush(file)


def _drop_labels(metrics: RecordingMetrics) -> list[str]:
    return [labels["view_origin"] for labels in metrics.labels_of(READ_VIEW_DROPS_TOTAL)]


def test_an_own_token_change_keeps_clean_frames_resident() -> None:
    # CQ-2/QW-4: the token moved, but only because THIS participant committed -- the frames
    # ARE the committed state this pool just produced, and dropping them re-reads every page
    # from the device for no new information. A foreign token still drops everything.
    device = MemoryDevice()
    metrics = RecordingMetrics()
    pool = make_pool(device, metrics)
    _seed(pool, 2)
    assert pool.begin_read_view("commit-1") is True  # foreign default: establishes
    pool.pin(FILE, 0)
    pool.unpin(FILE, 0)
    assert pool.is_resident(FILE, 0)
    reads_before = len(device.read_calls)

    assert pool.begin_read_view("commit-2", own=True) is False

    assert pool.is_resident(FILE, 0)  # nothing dropped
    pool.pin(FILE, 0)
    pool.unpin(FILE, 0)
    assert len(device.read_calls) == reads_before  # served from the kept frame
    # And the token WAS updated: repeating it is a no-op whichever way it is spelled.
    assert pool.begin_read_view("commit-2") is False
    assert pool.begin_read_view("commit-2", own=True) is False


def test_a_foreign_token_still_drops_everything_as_today() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    _seed(pool, 2)
    assert pool.begin_read_view("commit-1", own=True) is True  # first view: nothing proven yet
    pool.pin(FILE, 1)
    pool.unpin(FILE, 1)
    assert pool.is_resident(FILE, 1)

    assert pool.begin_read_view("foreign-2") is True

    assert not pool.is_resident(FILE, 1)


def test_an_own_view_with_a_dirty_frame_elsewhere_falls_back_to_the_full_drop() -> None:
    # The full drop writes dirty frames back before forgetting them. A dirty frame outside the
    # commit's image set that survived an exempted view would make the NEXT foreign
    # discard_clean_file refuse -- so the exemption applies only to a provably clean pool.
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    _seed(pool, 2)
    assert pool.begin_read_view("commit-1") is True
    page = pool.pin(FILE, 1)
    page.update_slot(0, b"locally-dirty")
    pool.unpin(FILE, 1, dirty=True)
    writes_before = len(device.write_calls)

    assert pool.begin_read_view("commit-2", own=True) is True

    assert not pool.is_resident(FILE, 1)
    assert len(device.write_calls) > writes_before  # the dirty frame was written back


def test_an_own_view_still_drops_the_unfenced_catalog_file() -> None:
    # catalog.dat has no page-0 sequence fence: a vector space save moves its device state
    # without moving the commit token. Today's full drop is what surfaced those changes to the
    # catalog's epoch door, so the exempted view must keep exactly that behaviour for exactly
    # that file -- frames dropped and its drop epoch bumped -- while the rest of the pool
    # keeps its frames.
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    _seed(pool, 2)
    _seed(pool, 1, file=CATALOG)
    assert pool.begin_read_view("commit-1") is True
    pool.pin(FILE, 0)
    pool.unpin(FILE, 0)
    pool.pin(CATALOG, 0)
    pool.unpin(CATALOG, 0)
    heap_epoch = pool.cache_drop_epoch(FILE)
    catalog_epoch = pool.cache_drop_epoch(CATALOG)

    assert pool.begin_read_view("commit-2", own=True, unfenced_file=CATALOG) is True

    assert pool.is_resident(FILE, 0)  # kept
    assert not pool.is_resident(CATALOG, 0)  # dropped
    assert pool.cache_drop_epoch(CATALOG) > catalog_epoch
    assert pool.cache_drop_epoch(FILE) == heap_epoch


def test_an_own_view_bumps_the_unfenced_epoch_even_with_no_resident_frame() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    _seed(pool, 1)
    pool.storage.create(CATALOG)
    assert pool.begin_read_view("commit-1") is True
    catalog_epoch = pool.cache_drop_epoch(CATALOG)

    pool.begin_read_view("commit-2", own=True, unfenced_file=CATALOG)

    assert pool.cache_drop_epoch(CATALOG) > catalog_epoch


def test_the_same_token_early_returns_before_any_own_decision() -> None:
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    _seed(pool, 1)
    assert pool.begin_read_view("commit-1") is True
    pool.pin(FILE, 0)
    pool.unpin(FILE, 0)

    assert pool.begin_read_view("commit-1", own=True) is False
    assert pool.begin_read_view("commit-1") is False

    assert pool.is_resident(FILE, 0)


def test_read_view_drops_are_counted_by_origin() -> None:
    device = MemoryDevice()
    metrics = RecordingMetrics()
    pool = make_pool(device, metrics)
    _seed(pool, 1)

    assert pool.begin_read_view("commit-1") is True  # foreign (first view)
    assert pool.begin_read_view("commit-2", own=True) is False  # own, exempted
    assert pool.begin_read_view("commit-2", own=True) is False  # same token: no event
    assert pool.begin_read_view("foreign-3") is True  # foreign again

    assert _drop_labels(metrics) == ["foreign", "own", "foreign"]
