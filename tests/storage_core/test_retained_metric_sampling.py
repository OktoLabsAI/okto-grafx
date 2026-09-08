"""Bound residency telemetry work without changing explicit diagnostics or admission."""
from __future__ import annotations

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.graph_guard import ConditionGuard
from okto_grafx.domain.page import Page, PageType
from okto_grafx.engine.buffer_pool import (
    BUFFER_BUDGET_USED_BYTES,
    BUFFER_RETAINED_ESTIMATE_BYTES,
    BufferPool,
)

from .conftest import MemoryDevice, RecordingMetrics


def seeded_pool(count, *, concurrent, enabled=True):
    device = MemoryDevice(page_size=8192)
    device.create("heap.dat")
    device.allocate("heap.dat", count)
    codec = PageCodecV1(device.page_size)
    for index in range(count):
        page = Page(int(PageType.HEAP), page_size=device.page_size, page_index=index)
        page.insert_slot(b"seed")
        device.write_page("heap.dat", index, codec.encode_page(page))
    metrics = RecordingMetrics(enabled=enabled)
    pool = BufferPool(device, codec, metrics, budget_bytes=count * device.page_size,
                      db_label="sample", guard=ConditionGuard() if concurrent else None)
    return pool, metrics


@pytest.mark.parametrize("concurrent", [False, True])
def test_cold_admission_does_not_walk_every_resident_frame_per_page(monkeypatch, concurrent):
    count = 512
    pool, metrics = seeded_pool(count, concurrent=concurrent)
    walks = []
    original = BufferPool._retained_bytes_estimate

    def measured(self):
        walks.append(len(self._frames))
        return original(self)

    monkeypatch.setattr(BufferPool, "_retained_bytes_estimate", measured)
    for index in range(count):
        page = pool.pin("heap.dat", index)
        pool.unpin("heap.dat", index, page=page)
    # Full every-page sampling visits N*(N+1)/2 frames. The diagnostic now
    # amortizes over retained page equivalents; this pins work, not wall time.
    assert sum(walks) < count * 3
    assert len(walks) < count // 8
    assert metrics.values_of(BUFFER_BUDGET_USED_BYTES)[-1] == count * pool.page_size
    assert len(metrics.values_of(BUFFER_RETAINED_ESTIMATE_BYTES)) == len(walks) + 1
    assert pool.used_bytes() == pool.budget_bytes


def test_explicit_diagnostic_is_fresh_and_does_not_exhaust_sample_cadence(monkeypatch):
    pool, metrics = seeded_pool(8, concurrent=True)
    page = pool.pin("heap.dat", 0)
    before = pool.retained_bytes_estimate()
    countdown = pool._retained_sample_countdown
    emissions = len(metrics.values_of(BUFFER_RETAINED_ESTIMATE_BYTES))
    page.insert_slot(b"additional")
    assert pool.retained_bytes_estimate() > before
    assert pool._retained_sample_countdown == countdown
    assert len(metrics.values_of(BUFFER_RETAINED_ESTIMATE_BYTES)) == emissions
    pool.unpin("heap.dat", 0, page=page)


def test_next_due_sample_is_current_and_skipped_samples_are_not_reemitted():
    pool, metrics = seeded_pool(16, concurrent=False)
    for index in range(2):
        page = pool.pin("heap.dat", index)
        pool.unpin("heap.dat", index, page=page)
    countdown = pool._retained_sample_countdown
    assert countdown > 0
    previous = len(metrics.values_of(BUFFER_RETAINED_ESTIMATE_BYTES))
    for _ in range(countdown):
        pool._report_usage()
    assert len(metrics.values_of(BUFFER_RETAINED_ESTIMATE_BYTES)) == previous
    expected = pool.retained_bytes_estimate()
    pool._report_usage()
    assert metrics.values_of(BUFFER_RETAINED_ESTIMATE_BYTES)[-1] == expected
    assert len(metrics.values_of(BUFFER_RETAINED_ESTIMATE_BYTES)) == previous + 1


def test_disabled_pool_does_not_sample_or_advance_cadence(monkeypatch):
    pool, metrics = seeded_pool(4, concurrent=True, enabled=False)

    def forbidden(_):
        pytest.fail("disabled telemetry walked retained objects")

    monkeypatch.setattr(BufferPool, "_retained_bytes_estimate", forbidden)
    for index in range(4):
        page = pool.pin("heap.dat", index)
        pool.unpin("heap.dat", index, page=page)
    assert pool._retained_sample_countdown == 0
    assert metrics.calls == []


def test_sampling_state_is_per_pool():
    first, _ = seeded_pool(8, concurrent=True)
    second, _ = seeded_pool(8, concurrent=True)
    for index in range(2):
        page = first.pin("heap.dat", index)
        first.unpin("heap.dat", index, page=page)
    assert first._retained_sample_countdown > 0
    assert second._retained_sample_countdown == 0
