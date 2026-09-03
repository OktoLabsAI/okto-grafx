"""Deterministic concurrency regressions for BufferPool cold-miss single-flight."""

from __future__ import annotations

import threading
from collections.abc import Mapping

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.graph_guard import ConditionGuard
from okto_grafx.domain.errors import (
    GrafxBufferBudgetExceeded,
    GrafxDeviceFull,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import PageIndex
from okto_grafx.domain.page import Page, PageType
from okto_grafx.domain.ports.metrics import MetricDescriptor
from okto_grafx.engine import buffer_pool as buffer_pool_module
from okto_grafx.engine.buffer_pool import BufferPool

from .conftest import MemoryDevice, RecordingMetrics, SMALL_PAGE_SIZE

FILE = "heap.dat"


class ObservedConditionGuard(ConditionGuard):
    """A production condition with deterministic evidence that a waiter reached it."""

    __slots__ = ("waited",)

    def __init__(self) -> None:
        super().__init__()
        self.waited = threading.Event()

    def wait_for(self, predicate: object, timeout: float | None = None) -> bool:
        self.waited.set()
        return super().wait_for(predicate, timeout)  # type: ignore[arg-type]

    def owned_by_current_thread(self) -> bool:
        """Expose only to tests whether a collaborator is running below the pool guard."""

        owned = getattr(self._condition, "_is_owned")
        return bool(owned())


class GuardCheckingCodec:
    """Delegate codec which refuses encode/decode beneath the pool guard."""

    def __init__(
        self,
        guard: ObservedConditionGuard,
        *,
        decode_barrier: threading.Barrier | None = None,
    ) -> None:
        self._delegate = PageCodecV1(SMALL_PAGE_SIZE)
        self._guard = guard
        self._decode_barrier = decode_barrier
        self.armed = False
        self.encoded = 0
        self.decoded = 0

    @property
    def format_version(self) -> int:
        return self._delegate.format_version

    def checksum(self, payload: bytes) -> int:
        return self._delegate.checksum(payload)

    def encode_page(self, page: Page) -> bytes:
        if self.armed:
            assert not self._guard.owned_by_current_thread()
            self.encoded += 1
        return self._delegate.encode_page(page)

    def decode_page(self, raw: bytes, *, verify: bool = True) -> Page:
        if self.armed:
            assert not self._guard.owned_by_current_thread()
            self.decoded += 1
            if self._decode_barrier is not None:
                self._decode_barrier.wait(timeout=5)
        return self._delegate.decode_page(raw, verify=verify)


class GuardCheckingMetrics(RecordingMetrics):
    """Recording sink which proves cold-path emissions happen after guard release."""

    def __init__(self, guard: ObservedConditionGuard) -> None:
        super().__init__()
        self._guard = guard
        self.armed = False
        self.checked = 0

    def _check(self) -> None:
        if self.armed:
            assert not self._guard.owned_by_current_thread()
            self.checked += 1

    def register(self, descriptor: MetricDescriptor) -> None:
        self._check()
        super().register(descriptor)

    def increment(
        self,
        name: str,
        value: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._check()
        super().increment(name, value, labels)

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._check()
        super().set_gauge(name, value, labels)


class BlockingWriteDevice(MemoryDevice):
    """Memory device with one deterministic write boundary for dirty eviction."""

    def __init__(self) -> None:
        super().__init__()
        self.block_next_write = False
        self.write_entered = threading.Event()
        self.release_write = threading.Event()

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        if self.block_next_write:
            self.block_next_write = False
            self.write_entered.set()
            assert self.release_write.wait(timeout=5)
        super().write_page(file, page_index, data)


class GuardCheckingDevice(MemoryDevice):
    """Memory device which proves detached reads and dirty writes own no pool guard."""

    def __init__(self, guard: ObservedConditionGuard) -> None:
        super().__init__()
        self._guard = guard
        self.armed = False
        self.checked = 0

    def _check(self) -> None:
        if self.armed:
            assert not self._guard.owned_by_current_thread()
            self.checked += 1

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        self._check()
        return super().read_page(file, page_index)

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        self._check()
        super().write_page(file, page_index, data)


def stored_pages(device: MemoryDevice, count: int) -> PageCodecV1:
    """Put ``count`` valid images directly on the device without warming a pool."""

    codec = PageCodecV1(device.page_size)
    device.create(FILE)
    for index in range(count):
        assert device.allocate(FILE) == index
        page = Page(int(PageType.HEAP), page_size=device.page_size, page_index=index)
        page.insert_slot(f"page-{index}".encode())
        device.write_page(FILE, index, codec.encode_page(page))
    device.read_calls.clear()
    device.write_calls.clear()
    return codec


def concurrent_pool(
    device: MemoryDevice,
    *,
    budget_pages: int = 4,
    codec: object | None = None,
    metrics: object | None = None,
    guard: ObservedConditionGuard | None = None,
) -> tuple[BufferPool, ObservedConditionGuard]:
    """Build the same condition-backed pool used by public assembly."""

    selected_guard = ObservedConditionGuard() if guard is None else guard
    pool = BufferPool(
        device,
        PageCodecV1(device.page_size) if codec is None else codec,  # type: ignore[arg-type]
        RecordingMetrics() if metrics is None else metrics,  # type: ignore[arg-type]
        budget_bytes=device.page_size * budget_pages,
        db_label="single-flight",
        guard=selected_guard,
    )
    return pool, selected_guard


def join_all(threads: list[threading.Thread]) -> None:
    """Join without sleeping and make a deadlock an immediate, legible assertion."""

    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)


def test_same_cold_key_has_one_loader_and_one_published_page() -> None:
    device = MemoryDevice()
    stored_pages(device, 1)
    entered = threading.Event()
    release = threading.Event()

    def block_first_read(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        entered.set()
        assert release.wait(timeout=5)
        return raw

    device.page_reader = block_first_read
    pool, guard = concurrent_pool(device)
    start = threading.Barrier(3)
    pages: list[Page] = []
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            start.wait(timeout=5)
            pages.append(pool.pin(FILE, 0))
        except BaseException as failure:  # noqa: BLE001 - test captures thread outcome
            failures.append(failure)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    assert entered.wait(timeout=5)
    assert guard.waited.wait(timeout=5)
    assert device.read_calls == [(FILE, 0)]
    release.set()
    join_all(threads)

    assert failures == []
    assert len(pages) == 2
    assert pages[0] is pages[1]
    assert pool.pin_count(FILE, 0) == 2
    for page in pages:
        pool.unpin(FILE, 0, page=page)
    assert pool.pin_count(FILE, 0) == 0


def test_loader_failure_is_not_cached_and_wakes_a_waiter_to_retry() -> None:
    device = MemoryDevice()
    stored_pages(device, 1)
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_guard = threading.Lock()

    def fail_first_read(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        nonlocal calls
        with calls_guard:
            calls += 1
            attempt = calls
        if attempt == 1:
            entered.set()
            assert release.wait(timeout=5)
            raise RuntimeError("injected read failure")
        return raw

    device.page_reader = fail_first_read
    pool, guard = concurrent_pool(device)
    start = threading.Barrier(3)
    pages: list[Page] = []
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            start.wait(timeout=5)
            pages.append(pool.pin(FILE, 0))
        except BaseException as failure:  # noqa: BLE001 - exact thread outcome is asserted
            failures.append(failure)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    assert entered.wait(timeout=5)
    assert guard.waited.wait(timeout=5)
    release.set()
    join_all(threads)

    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert len(pages) == 1
    assert calls == 2
    assert device.read_calls == [(FILE, 0), (FILE, 0)]
    assert pool.is_resident(FILE, 0)
    pool.unpin(FILE, 0, page=pages[0])


def test_failed_load_releases_its_retained_memory_reservation() -> None:
    device = MemoryDevice()
    stored_pages(device, 1)
    entered = threading.Event()
    release = threading.Event()

    def fail_after_observation(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        entered.set()
        assert release.wait(timeout=5)
        raise RuntimeError("injected read failure")

    device.page_reader = fail_after_observation
    pool, _guard = concurrent_pool(device)
    baseline = pool.retained_bytes_estimate()
    failures: list[BaseException] = []

    def loader() -> None:
        try:
            pool.pin(FILE, 0)
        except BaseException as failure:  # noqa: BLE001
            failures.append(failure)

    thread = threading.Thread(target=loader)
    thread.start()
    assert entered.wait(timeout=5)
    assert pool.used_bytes() == 0
    assert pool.retained_bytes_estimate() > baseline
    release.set()
    join_all([thread])

    assert len(failures) == 1
    assert pool._loads == {}
    assert pool._evictions == {}
    assert pool.used_bytes() == 0
    assert pool.retained_bytes_estimate() == baseline


def test_distinct_cold_storage_reads_really_overlap() -> None:
    device = MemoryDevice()
    stored_pages(device, 2)
    overlap = threading.Barrier(2)
    device.page_reader = lambda _file, _page_index, raw: (overlap.wait(timeout=5), raw)[
        1
    ]
    pool, _guard = concurrent_pool(device, budget_pages=2)
    pages: list[Page] = []
    failures: list[BaseException] = []

    def worker(page_index: int) -> None:
        try:
            pages.append(pool.pin(FILE, page_index))
        except BaseException as failure:  # noqa: BLE001
            failures.append(failure)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    join_all(threads)

    assert failures == []
    assert {page.page_index for page in pages} == {0, 1}
    assert len(device.read_calls) == 2
    for page in pages:
        pool.unpin(FILE, page.page_index, page=page)


def test_distinct_cold_decodes_really_overlap() -> None:
    device = MemoryDevice()
    stored_pages(device, 2)
    guard = ObservedConditionGuard()
    codec = GuardCheckingCodec(guard, decode_barrier=threading.Barrier(2))
    pool, _guard = concurrent_pool(device, budget_pages=2, codec=codec, guard=guard)
    codec.armed = True
    pages: list[Page] = []
    failures: list[BaseException] = []

    def worker(page_index: int) -> None:
        try:
            pages.append(pool.pin(FILE, page_index))
        except BaseException as failure:  # noqa: BLE001
            failures.append(failure)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    join_all(threads)

    assert failures == []
    assert codec.decoded == 2
    for page in pages:
        pool.unpin(FILE, page.page_index, page=page)


def test_read_view_during_io_discards_the_old_result_and_retries() -> None:
    device = MemoryDevice()
    codec = stored_pages(device, 1)
    old_captured = threading.Event()
    release_old = threading.Event()
    reads = 0

    def hold_old_image(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 1:
            old_captured.set()
            assert release_old.wait(timeout=5)
        return raw

    device.page_reader = hold_old_image
    pool, _guard = concurrent_pool(device)
    result: list[Page] = []
    failures: list[BaseException] = []

    def loader() -> None:
        try:
            result.append(pool.pin(FILE, 0))
        except BaseException as failure:  # noqa: BLE001
            failures.append(failure)

    thread = threading.Thread(target=loader)
    thread.start()
    assert old_captured.wait(timeout=5)

    replacement = Page(int(PageType.HEAP), page_size=device.page_size, page_index=0)
    replacement.insert_slot(b"new-view")
    device.poke_page(FILE, 0, codec.encode_page(replacement))
    assert pool.begin_read_view("foreign-commit") is True
    release_old.set()
    join_all([thread])

    assert failures == []
    assert reads == 2
    assert result[0].read_slot(0) == b"new-view"
    pool.unpin(FILE, 0, page=result[0])


def test_own_read_view_keeps_resident_cache_but_revokes_an_older_cold_load() -> None:
    device = MemoryDevice()
    codec = stored_pages(device, 1)
    pool, _guard = concurrent_pool(device)
    assert pool.begin_read_view("baseline") is True
    old_captured = threading.Event()
    release_old = threading.Event()
    reads = 0

    def hold_old_image(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 1:
            old_captured.set()
            assert release_old.wait(timeout=5)
        return raw

    device.page_reader = hold_old_image
    result: list[Page] = []
    failures: list[BaseException] = []

    def loader() -> None:
        try:
            result.append(pool.pin(FILE, 0))
        except BaseException as failure:  # noqa: BLE001
            failures.append(failure)

    thread = threading.Thread(target=loader)
    thread.start()
    assert old_captured.wait(timeout=5)

    replacement = Page(int(PageType.HEAP), page_size=device.page_size, page_index=0)
    replacement.insert_slot(b"own-new-view")
    device.poke_page(FILE, 0, codec.encode_page(replacement))
    assert pool.begin_read_view("own-commit", own=True) is False
    release_old.set()
    join_all([thread])

    assert failures == []
    assert reads == 2
    assert result[0].read_slot(0) == b"own-new-view"
    pool.unpin(FILE, 0, page=result[0])


def test_structure_epoch_during_io_also_prevents_late_publication() -> None:
    device = MemoryDevice()
    stored_pages(device, 1)
    entered = threading.Event()
    release = threading.Event()
    reads = 0

    def pause_once(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            assert release.wait(timeout=5)
        return raw

    device.page_reader = pause_once
    pool, _guard = concurrent_pool(device)
    result: list[Page] = []
    thread = threading.Thread(target=lambda: result.append(pool.pin(FILE, 0)))
    thread.start()
    assert entered.wait(timeout=5)
    pool._bump_structure_epoch(FILE)
    release.set()
    join_all([thread])

    assert reads == 2
    assert len(result) == 1
    pool.unpin(FILE, 0, page=result[0])


def test_inflight_reservation_never_overcommits_a_one_page_budget() -> None:
    device = MemoryDevice()
    stored_pages(device, 2)
    entered = threading.Event()
    release = threading.Event()

    def pause_page_zero(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        if page_index == 0:
            entered.set()
            assert release.wait(timeout=5)
        return raw

    device.page_reader = pause_page_zero
    pool, guard = concurrent_pool(device, budget_pages=1)
    pages: list[Page] = []
    failures: list[BaseException] = []

    def worker(page_index: int) -> None:
        try:
            pages.append(pool.pin(FILE, page_index))
        except BaseException as failure:  # noqa: BLE001
            failures.append(failure)

    first = threading.Thread(target=worker, args=(0,))
    first.start()
    assert entered.wait(timeout=5)
    assert pool.used_bytes() == 0
    assert pool._occupied_slots() == pool.capacity_pages == 1

    second = threading.Thread(target=worker, args=(1,))
    second.start()
    assert guard.waited.wait(timeout=5)
    assert pool._occupied_slots() == 1
    release.set()
    join_all([first, second])

    assert len(pages) == 1
    assert pages[0].page_index == 0
    assert len(failures) == 1
    assert isinstance(failures[0], GrafxBufferBudgetExceeded)
    assert pool.used_bytes() == pool.budget_bytes
    assert pool._occupied_slots() == 1
    pool.unpin(FILE, 0, page=pages[0])


def test_pressure_counts_reservation_and_dirty_eviction_callbacks_are_unguarded() -> (
    None
):
    guard = ObservedConditionGuard()
    device = GuardCheckingDevice(guard)
    stored_pages(device, 2)
    codec = GuardCheckingCodec(guard)
    metrics = GuardCheckingMetrics(guard)
    pool, _guard = concurrent_pool(
        device,
        budget_pages=1,
        codec=codec,
        metrics=metrics,
        guard=guard,
    )

    first = pool.pin(FILE, 0)
    first.insert_slot(b"dirty")
    pool.unpin(FILE, 0, dirty=True, page=first)
    codec.armed = True
    metrics.armed = True
    device.armed = True
    second = pool.pin(FILE, 1)

    assert pool.used_bytes() == pool.budget_bytes == device.page_size
    assert pool._occupied_slots() == pool.capacity_pages
    assert codec.encoded == 1
    assert codec.decoded == 1
    assert device.checked == 2
    assert metrics.checked > 0
    assert (FILE, 0) in pool.modified_pages()
    assert (
        PageCodecV1(device.page_size)
        .decode_page(device.raw_page(FILE, 0), verify=True)
        .read_slot(1)
        == b"dirty"
    )
    pool.unpin(FILE, 1, page=second)


def test_failed_dirty_eviction_restores_the_frame_and_wakes_capacity_waiters() -> None:
    device = BlockingWriteDevice()
    stored_pages(device, 2)
    pool, guard = concurrent_pool(device, budget_pages=1)
    first = pool.pin(FILE, 0)
    first.insert_slot(b"local-work")
    pool.unpin(FILE, 0, dirty=True, page=first)
    retained_before_flight = pool.retained_bytes_estimate()
    device.refuse_write_number(1, GrafxDeviceFull("injected full device"))
    device.block_next_write = True
    pages: list[Page] = []
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            pages.append(pool.pin(FILE, 1))
        except BaseException as failure:  # noqa: BLE001
            failures.append(failure)

    first_loader = threading.Thread(target=worker)
    first_loader.start()
    assert device.write_entered.wait(timeout=5)
    second_loader = threading.Thread(target=worker)
    second_loader.start()
    assert guard.waited.wait(timeout=5)
    assert pool.used_bytes() == 0
    assert pool._occupied_slots() == pool.capacity_pages == 1
    assert pool.retained_bytes_estimate() > retained_before_flight
    device.release_write.set()
    join_all([first_loader, second_loader])

    assert len(failures) == 1
    assert isinstance(failures[0], GrafxDeviceFull)
    assert device.writes_attempted == 2
    assert len(pages) == 1
    assert pages[0].page_index == 1
    assert pool._occupied_slots() == pool.capacity_pages == 1
    assert not pool.is_resident(FILE, 0)
    assert pool.is_resident(FILE, 1)
    assert (FILE, 0) in pool.modified_pages()
    pool.unpin(FILE, 1, page=pages[0])


def test_eviction_ticket_memory_error_never_orphans_a_dirty_victim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = MemoryDevice()
    stored_pages(device, 2)
    pool, _guard = concurrent_pool(
        device, budget_pages=1, metrics=RecordingMetrics(enabled=False)
    )
    dirty = pool.pin(FILE, 0)
    dirty.insert_slot(b"work-that-must-remain-owned")
    pool.unpin(FILE, 0, dirty=True, page=dirty)
    expected = MemoryError("injected eviction-ticket allocation failure")

    class RefusingEvictionTicket:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise expected

    monkeypatch.setattr(buffer_pool_module, "_PageEviction", RefusingEvictionTicket)

    with pytest.raises(MemoryError) as raised:
        pool.pin(FILE, 1)

    assert raised.value is expected
    assert pool.is_resident(FILE, 0)
    assert pool._frames[(FILE, 0)].page is dirty
    assert pool.has_dirty_pages(FILE)
    assert pool.modified_pages(FILE) == frozenset({(FILE, 0)})
    assert pool._loads == {}
    assert pool._evictions == {}


def test_frame_memory_error_removes_the_load_and_wakes_a_same_key_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = MemoryDevice()
    stored_pages(device, 1)
    entered = threading.Event()
    release = threading.Event()
    reads = 0

    def block_first_read(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            assert release.wait(timeout=5)
        return raw

    device.page_reader = block_first_read
    pool, guard = concurrent_pool(device, metrics=RecordingMetrics(enabled=False))
    real_frame = buffer_pool_module._Frame
    expected = MemoryError("injected frame allocation failure")
    allocations = 0

    class FailFirstFrameAllocation:
        def __new__(cls, *args: object, **kwargs: object) -> object:
            nonlocal allocations
            allocations += 1
            if allocations == 1:
                raise expected
            return real_frame(*args, **kwargs)

    monkeypatch.setattr(buffer_pool_module, "_Frame", FailFirstFrameAllocation)
    pages: list[Page] = []
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            pages.append(pool.pin(FILE, 0))
        except BaseException as failure:  # noqa: BLE001 - exact thread outcome is asserted
            failures.append(failure)

    first = threading.Thread(target=worker)
    first.start()
    assert entered.wait(timeout=5)
    waiter = threading.Thread(target=worker)
    waiter.start()
    assert guard.waited.wait(timeout=5)
    release.set()
    first.join(timeout=5)
    assert not first.is_alive()
    waiter.join(timeout=1)
    waiter_stuck = waiter.is_alive()
    if waiter_stuck:
        # Leave no non-daemon waiter behind when this regression is run red-first or mutated.
        with pool._guard:
            pool._signal_flight_state()
        waiter.join(timeout=5)
    assert not waiter_stuck
    assert not waiter.is_alive()

    assert failures == [expected]
    assert len(pages) == 1
    assert reads == 2
    assert pool._loads == {}
    assert pool._evictions == {}
    assert pool.is_resident(FILE, 0)
    pool.unpin(FILE, 0, page=pages[0])


def test_post_write_epoch_failure_releases_both_flights_and_keeps_change_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful dirty write cannot strand tickets if its target rebase fails."""
    device = MemoryDevice()
    codec = stored_pages(device, 2)
    pool, _guard = concurrent_pool(
        device, budget_pages=1, metrics=RecordingMetrics(enabled=False)
    )
    dirty = pool.pin(FILE, 0)
    dirty.insert_slot(b"published-before-rebase-failure")
    pool.unpin(FILE, 0, dirty=True, page=dirty)
    real_load_epoch = BufferPool._load_epoch
    expected = MemoryError("injected post-write epoch allocation failure")
    calls = 0

    def fail_the_rebase(self: BufferPool, file: str) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise expected
        return real_load_epoch(self, file)

    monkeypatch.setattr(BufferPool, "_load_epoch", fail_the_rebase)

    with pytest.raises(MemoryError) as raised:
        pool.pin(FILE, 1)

    assert raised.value is expected
    assert pool._loads == {}
    assert pool._evictions == {}
    assert not pool.is_resident(FILE, 0)
    assert (FILE, 0) in pool.modified_pages()
    written = codec.decode_page(device.raw_page(FILE, 0), verify=True)
    assert written.read_slot(1) == b"published-before-rebase-failure"

    # The failed target reservation owns no capacity and a subsequent cold pin progresses.
    target = pool.pin(FILE, 1)
    assert target.read_slot(0) == b"page-1"
    pool.unpin(FILE, 1, page=target)


def test_page_fence_waits_for_detached_eviction_before_taking_its_section() -> None:
    device = BlockingWriteDevice()
    stored_pages(device, 2)
    guard = ObservedConditionGuard()
    page_section = threading.RLock()
    pool = BufferPool(
        device,
        PageCodecV1(device.page_size),
        RecordingMetrics(),
        budget_bytes=device.page_size,
        db_label="fenced-flight",
        guard=guard,
        page_write_section=lambda _file, _page_index: page_section,
        page_sequence_fence=lambda _file, page_index: page_index == 0,
    )
    first = pool.pin(FILE, 0)
    first.insert_slot(b"dirty-header")
    pool.unpin(FILE, 0, dirty=True, page=first)
    device.block_next_write = True
    loaded: list[Page] = []
    failures: list[BaseException] = []

    def evict() -> None:
        try:
            loaded.append(pool.pin(FILE, 1))
        except BaseException as failure:  # noqa: BLE001
            failures.append(failure)

    fence_entered = threading.Event()

    def fence() -> None:
        try:
            with pool.page_write_fence(FILE, 0):
                fence_entered.set()
        except BaseException as failure:  # noqa: BLE001
            failures.append(failure)

    evictor = threading.Thread(target=evict)
    evictor.start()
    assert device.write_entered.wait(timeout=5)
    fencer = threading.Thread(target=fence)
    fencer.start()
    assert guard.waited.wait(timeout=5)
    assert not fence_entered.is_set()
    device.release_write.set()
    join_all([evictor, fencer])

    assert failures == []
    assert fence_entered.is_set()
    assert len(loaded) == 1
    pool.unpin(FILE, 1, page=loaded[0])


def test_reentrant_invalidation_during_read_forces_a_safe_retry() -> None:
    device = MemoryDevice()
    stored_pages(device, 1)
    pool, guard = concurrent_pool(device)
    callbacks = 0

    def invalidate_once(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        nonlocal callbacks
        assert not guard.owned_by_current_thread()
        callbacks += 1
        if callbacks == 1:
            pool.invalidate(file)
        return raw

    device.page_reader = invalidate_once
    page = pool.pin(FILE, 0)

    assert callbacks == 2
    assert device.read_calls == [(FILE, 0), (FILE, 0)]
    pool.unpin(FILE, 0, page=page)


def test_same_key_reentrant_storage_callback_is_refused_without_deadlock() -> None:
    device = MemoryDevice()
    stored_pages(device, 1)
    pool, guard = concurrent_pool(device)
    refusals: list[GrafxUnsupportedOperation] = []

    def reenter(file: str, page_index: PageIndex, raw: bytes) -> bytes:
        assert not guard.owned_by_current_thread()
        try:
            pool.pin(file, page_index)
        except GrafxUnsupportedOperation as refusal:
            refusals.append(refusal)
        return raw

    device.page_reader = reenter
    page = pool.pin(FILE, 0)

    assert len(refusals) == 1
    assert refusals[0].details["field"] == "buffer_flight_reentrant"
    assert refusals[0].details["operation"] == "load"
    assert device.read_calls == [(FILE, 0)]
    pool.unpin(FILE, 0, page=page)
