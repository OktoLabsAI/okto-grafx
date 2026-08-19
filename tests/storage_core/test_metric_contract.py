"""The storage core against the real metric catalog and a real sink (G7, FR-14, TR-7).

A recording sink refuses an emission under a name it was not given, and refuses a second
declaration of a name with a different shape. That makes this file the integration proof that
the buffer pool emits inside the frozen catalog of CONTRACT.md section 9 rather than alongside
it: if the pool declared its own descriptor for a name the catalog already owns, registering
both on one sink would fail here.
"""

from __future__ import annotations

import pytest

from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.metrics_openmetrics import OpenMetricsSink
from okto_grafx.domain.errors import GrafxBufferBudgetExceeded
from okto_grafx.domain.page import PageType
from okto_grafx.engine.buffer_pool import (
    BUFFER_BUDGET_EXCEEDED_TOTAL,
    BUFFER_BUDGET_USED_BYTES,
    BUFFER_POOL_METRICS,
    CHECKSUM_FAILURES_TOTAL,
    CHECKSUM_VERIFICATIONS_TOTAL,
)
from okto_grafx.engine.metrics_catalog import metric, metric_names, register_catalog

from .conftest import MemoryDevice, make_pool

EMITTED_NAMES: tuple[str, ...] = (
    BUFFER_BUDGET_USED_BYTES,
    BUFFER_BUDGET_EXCEEDED_TOTAL,
    CHECKSUM_VERIFICATIONS_TOTAL,
    CHECKSUM_FAILURES_TOTAL,
)
"""Every metric the storage core emits."""


@pytest.mark.parametrize("name", EMITTED_NAMES)
def test_every_metric_the_pool_emits_is_in_the_frozen_catalog(name: str) -> None:
    assert name in metric_names()
    assert metric(name) in BUFFER_POOL_METRICS


def test_the_pool_declares_the_catalog_descriptor_and_not_a_copy() -> None:
    for descriptor in BUFFER_POOL_METRICS:
        assert descriptor is metric(descriptor.name)


def test_a_pool_and_the_catalog_can_be_registered_on_the_same_sink() -> None:
    sink = OpenMetricsSink()
    register_catalog(sink)
    device = MemoryDevice()
    pool = make_pool(device, sink, budget_pages=2, db_label="alpha")
    device.create("heap.dat")
    page = pool.allocate("heap.dat", int(PageType.HEAP))
    page.insert_slot(b"payload")
    pool.unpin("heap.dat", page.page_index, dirty=True)
    pool.flush()
    pool.invalidate()
    pool.pin("heap.dat", 0)
    rendered = sink.render()
    assert BUFFER_BUDGET_USED_BYTES in rendered
    assert 'db="alpha"' in rendered
    assert f'{CHECKSUM_VERIFICATIONS_TOTAL}{{kind="page"}}' in rendered


def test_the_budget_failure_is_counted_under_the_database_label() -> None:
    sink = OpenMetricsSink()
    register_catalog(sink)
    device = MemoryDevice()
    pool = make_pool(device, sink, budget_pages=1, db_label="alpha")
    device.create("heap.dat")
    page = pool.allocate("heap.dat", int(PageType.HEAP))
    pool.unpin("heap.dat", page.page_index, dirty=True)
    pool.pin("heap.dat", 0)
    with pytest.raises(GrafxBufferBudgetExceeded):
        pool.allocate("heap.dat", int(PageType.HEAP))
    samples = sink.snapshot()[BUFFER_BUDGET_EXCEEDED_TOTAL]["samples"]
    assert [(dict(sample["labels"]), sample["value"]) for sample in samples] == [
        ({"db": "alpha"}, 1.0)
    ]


def test_the_pool_costs_nothing_on_the_no_op_sink() -> None:
    device = MemoryDevice()
    pool = make_pool(device, NoOpMetricsSink(), budget_pages=2, db_label="alpha")
    device.create("heap.dat")
    page = pool.allocate("heap.dat", int(PageType.HEAP))
    page.insert_slot(b"payload")
    pool.unpin("heap.dat", page.page_index, dirty=True)
    pool.flush()
    pool.invalidate()
    assert pool.pin("heap.dat", 0).read_slot(0) == b"payload"
    assert NoOpMetricsSink().enabled is False
