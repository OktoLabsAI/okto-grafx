"""Bounded telemetry for the local descriptor cache."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
import threading

from okto_grafx import connect
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.metrics_contained import ContainedMetricsSink
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.storage_local import (
    DESCRIPTOR_CACHE_EVICTIONS_TOTAL,
    DESCRIPTOR_CACHE_HITS_TOTAL,
    DESCRIPTOR_CACHE_MISSES_TOTAL,
    DescriptorCacheStats,
    LocalStorageDevice,
)
from okto_grafx.domain.ports.metrics import MetricDescriptor
from okto_grafx.domain.page import PageType
from okto_grafx.engine.buffer_pool import BufferPool

PAGE_SIZE = 512


class _GuardCheckingMetrics:
    """Record calls and fail if the device invokes host code while its guard is owned."""

    def __init__(
        self, device: LocalStorageDevice, *, pool_guard: threading.RLock | None = None
    ) -> None:
        self._device = device
        self._pool_guard = pool_guard
        self.registered: dict[str, MetricDescriptor] = {}
        self.increments: list[tuple[str, float, Mapping[str, str] | None]] = []

    @property
    def enabled(self) -> bool:
        self._assert_outside_guard()
        return True

    def register(self, descriptor: MetricDescriptor) -> None:
        self._assert_outside_guard()
        self.registered[descriptor.name] = descriptor

    def increment(
        self,
        name: str,
        value: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._assert_outside_guard()
        self.increments.append((name, value, labels))

    def set_gauge(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        self._assert_outside_guard()

    def observe(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        self._assert_outside_guard()

    def time(self, name: str, labels: Mapping[str, str] | None = None) -> object:
        self._assert_outside_guard()
        return nullcontext()

    def snapshot(self) -> Mapping[str, object]:
        self._assert_outside_guard()
        return {}

    def total(self, name: str) -> float:
        """Return the cumulative increment recorded for one counter."""
        return sum(
            value for emitted, value, _labels in self.increments if emitted == name
        )

    def _assert_outside_guard(self) -> None:
        assert not self._device._lock._is_owned(), (
            "host metrics callback ran under storage guard"
        )
        if self._pool_guard is not None:
            assert not self._pool_guard._is_owned(), (
                "host metrics callback ran under buffer-pool guard"
            )


def _exercise_cache(device: LocalStorageDevice) -> DescriptorCacheStats:
    for name in ("a.bin", "b.bin"):
        device.create(name)
    assert device.read_log("a.bin", 0, 0) == b""  # one valid warm hit
    device.create("c.bin")  # b is the LRU victim
    assert device.read_log("b.bin", 0, 0) == b""  # one miss, evicting a
    return device.descriptor_cache_stats()


def test_descriptor_cache_counters_are_cumulative_bounded_and_outside_the_guard(
    tmp_path: Path,
) -> None:
    device = LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE, max_open_files=2)
    metrics = _GuardCheckingMetrics(device)
    try:
        device.bind_metrics(metrics)
        stats = _exercise_cache(device)

        assert stats == DescriptorCacheStats(hits=1, misses=1, evictions=2)
        assert metrics.total(DESCRIPTOR_CACHE_HITS_TOTAL) == 1.0
        assert metrics.total(DESCRIPTOR_CACHE_MISSES_TOTAL) == 1.0
        assert metrics.total(DESCRIPTOR_CACHE_EVICTIONS_TOTAL) == 2.0
        assert set(metrics.registered) == {
            DESCRIPTOR_CACHE_HITS_TOTAL,
            DESCRIPTOR_CACHE_MISSES_TOTAL,
            DESCRIPTOR_CACHE_EVICTIONS_TOTAL,
        }
        assert all(
            descriptor.labels == () for descriptor in metrics.registered.values()
        )
        assert all(labels is None for _name, _value, labels in metrics.increments)
    finally:
        device.close()


def test_descriptor_cache_counter_lifecycle_resets_with_the_device(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    first = LocalStorageDevice(root, page_size=PAGE_SIZE, max_open_files=2)
    try:
        assert _exercise_cache(first) == DescriptorCacheStats(1, 1, 2)
    finally:
        first.close()

    second = LocalStorageDevice(root, page_size=PAGE_SIZE, max_open_files=2)
    try:
        assert second.descriptor_cache_stats() == DescriptorCacheStats(0, 0, 0)
    finally:
        second.close()


def test_fused_read_publishes_its_cache_hit_only_after_releasing_the_guard(
    tmp_path: Path,
) -> None:
    device = LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE)
    metrics = _GuardCheckingMetrics(device)
    try:
        device.create("control/state")
        device.bind_metrics(metrics)

        assert device.read_log_if_exists("control/state", 0, 0) == b""

        assert metrics.total(DESCRIPTOR_CACHE_HITS_TOTAL) == 1.0
        assert metrics.total(DESCRIPTOR_CACHE_MISSES_TOTAL) == 0.0
    finally:
        device.close()


def test_disabled_metrics_add_no_adapter_callbacks(tmp_path: Path) -> None:
    device = LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE, max_open_files=2)
    try:
        device.bind_metrics(NoOpMetricsSink())
        assert "read_log" not in device.__dict__
        assert _exercise_cache(device) == DescriptorCacheStats(1, 1, 2)
    finally:
        device.close()


def test_descriptor_metrics_leave_both_nested_storage_and_pool_guards(
    tmp_path: Path,
) -> None:
    device = LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE, max_open_files=2)
    pool_guard = threading.RLock()
    host = _GuardCheckingMetrics(device, pool_guard=pool_guard)
    metrics = ContainedMetricsSink(host)
    try:
        device.bind_metrics(metrics)
        pool = BufferPool(
            device,
            PageCodecV1(PAGE_SIZE),
            metrics,
            budget_bytes=PAGE_SIZE,
            db_label="nested-guards",
            guard=pool_guard,
            metrics_defer=metrics.defer,
        )
        device.create("heap.dat")

        page = pool.allocate("heap.dat", int(PageType.HEAP))
        pool.unpin("heap.dat", page.page_index)

        assert host.total(DESCRIPTOR_CACHE_HITS_TOTAL) >= 1.0
    finally:
        device.close()


def test_default_composition_exposes_cache_and_retained_memory_health(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "composed", metrics="openmetrics") as database:
        metrics = database.snapshot_metrics()
        storage = database.storage
        pool = database.pool

        assert DESCRIPTOR_CACHE_HITS_TOTAL in metrics
        assert DESCRIPTOR_CACHE_MISSES_TOTAL in metrics
        assert DESCRIPTOR_CACHE_EVICTIONS_TOTAL in metrics
        assert "oktografx_buffer_retained_estimate_bytes" in metrics
        assert storage.descriptor_cache_hits is not None
        assert storage.descriptor_cache_misses is not None
        assert storage.descriptor_cache_evictions is not None
        assert pool.retained_bytes_estimator == "python-v2"
        assert pool.retained_bytes_estimate() >= pool.used_bytes()
