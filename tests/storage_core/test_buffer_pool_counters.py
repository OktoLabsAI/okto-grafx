"""Monotonic per-handle pool pressure counters (issue #13).

The counters answer one operator question, is this pool too small for its working set, so the
tests pin exact numbers on a known access pattern, on both pool compositions (the condition-backed
production one and the compatibility one), and then prove the public view carries them and that
concurrent pins do not lose a count.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.engine.buffer_pool import BufferPool

from .conftest import MemoryDevice, RecordingMetrics, make_pool
from .test_buffer_pool_single_flight import (
    FILE,
    concurrent_pool,
    join_all,
    stored_pages,
)

NAMES = ("hits", "misses", "evictions", "dirty_evictions", "load_waits")


def _pool(kind: str, pages: int, budget_pages: int) -> tuple[BufferPool, MemoryDevice]:
    device = MemoryDevice()
    stored_pages(device, pages)
    if kind == "single-flight":
        pool, _guard = concurrent_pool(device, budget_pages=budget_pages)
    else:
        pool = make_pool(device, RecordingMetrics(), budget_pages=budget_pages)
    return pool, device


def _counts(pool: BufferPool) -> dict[str, int]:
    return dict(zip(NAMES, pool.counters(), strict=True))


def _touch(pool: BufferPool, index: int, *, dirty: bool = False) -> None:
    pool.pin(FILE, index)
    pool.unpin(FILE, index, dirty=dirty)


@pytest.mark.parametrize("kind", ["single-flight", "compatibility"])
def test_counters_follow_a_known_access_pattern(kind: str) -> None:
    pool, device = _pool(kind, pages=4, budget_pages=2)
    assert _counts(pool) == dict.fromkeys(NAMES, 0)

    _touch(pool, 0)  # miss
    assert _counts(pool) == {**dict.fromkeys(NAMES, 0), "misses": 1}
    _touch(pool, 0)  # hit
    _touch(pool, 1)  # miss, pool now full: [0, 1]
    _touch(pool, 2)  # miss, evicts clean 0: [1, 2]
    _touch(pool, 0)  # miss, evicts clean 1: [2, 0]
    assert _counts(pool) == {
        "hits": 1,
        "misses": 4,
        "evictions": 2,
        "dirty_evictions": 0,
        "load_waits": 0,
    }

    _touch(pool, 2, dirty=True)  # hit, now dirty: [0, 2]
    _touch(pool, 3)  # miss, evicts clean 0: [2, 3]
    device.write_calls.clear()
    _touch(pool, 1)  # miss, evicts dirty 2 with a write-back: [3, 1]
    assert device.write_calls == [(FILE, 2)]
    assert _counts(pool) == {
        "hits": 2,
        "misses": 6,
        "evictions": 3,
        "dirty_evictions": 1,
        "load_waits": 0,
    }


@pytest.mark.parametrize("kind", ["single-flight", "compatibility"])
def test_counters_never_decrease(kind: str) -> None:
    pool, _device = _pool(kind, pages=4, budget_pages=2)
    previous = _counts(pool)
    for index in (0, 1, 0, 2, 3, 1, 1, 0):
        _touch(pool, index, dirty=index == 2)
        current = _counts(pool)
        assert all(current[name] >= previous[name] for name in NAMES)
        previous = current
    pool.invalidate()
    assert all(_counts(pool)[name] >= previous[name] for name in NAMES)


def test_a_waiter_on_another_threads_load_is_counted() -> None:
    device = MemoryDevice()
    stored_pages(device, 1)
    entered = threading.Event()
    release = threading.Event()

    def block_first_read(file: str, page_index: int, raw: bytes) -> bytes:
        entered.set()
        assert release.wait(timeout=5)
        return raw

    device.page_reader = block_first_read
    pool, guard = concurrent_pool(device)
    threads = [threading.Thread(target=_touch, args=(pool, 0)) for _ in range(2)]
    threads[0].start()
    assert entered.wait(timeout=5)
    threads[1].start()
    assert guard.waited.wait(timeout=5)
    release.set()
    join_all(threads)
    assert _counts(pool) == {
        "hits": 1,
        "misses": 1,
        "evictions": 0,
        "dirty_evictions": 0,
        "load_waits": 1,
    }


def test_concurrent_pins_do_not_lose_counts() -> None:
    threads_count, pins_each = 8, 500
    pool, _device = _pool("single-flight", pages=2, budget_pages=4)
    _touch(pool, 0)  # the one miss
    start = threading.Barrier(threads_count)

    def worker() -> None:
        start.wait(timeout=5)
        for _ in range(pins_each):
            _touch(pool, 0)

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    join_all(threads)
    counts = _counts(pool)
    assert counts["hits"] == threads_count * pins_each
    assert counts["misses"] == 1
    assert counts["evictions"] == counts["dirty_evictions"] == counts["load_waits"] == 0


def test_the_public_pool_view_carries_the_counters(tmp_path: Path) -> None:
    with connect(tmp_path / "db", buffer_budget_bytes=16 * 8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T(id INT64, pad STRING, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            for i in range(200):
                tx.execute("CREATE (:T {id: $i, pad: $p})", {"i": i, "p": "x" * 500})
        first = db.pool
        assert all(isinstance(getattr(first, name), int) for name in NAMES)
        assert first.hits + first.misses > 0
        assert db.execute("MATCH (n:T) RETURN count(*)").rows == ((200,),)
        second = db.pool
        assert all(getattr(second, name) >= getattr(first, name) for name in NAMES)
        assert second.hits + second.misses > first.hits + first.misses
        assert second.evictions + second.dirty_evictions > 0, "a 16-page pool must be under pressure"
