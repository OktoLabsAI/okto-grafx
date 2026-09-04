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
    BARRIER_FAILURES_TOTAL,
    BUFFER_BUDGET_EXCEEDED_TOTAL,
    BUFFER_BUDGET_USED_BYTES,
    BUFFER_POOL_METRICS,
    BUFFER_RETAINED_ESTIMATE_BYTES,
    CHECKSUM_FAILURES_TOTAL,
    CHECKSUM_VERIFICATIONS_TOTAL,
    FSYNC_DURATION_SECONDS,
)
from okto_grafx.engine.metrics_catalog import metric_names, register_catalog

from .conftest import MemoryDevice, RecordingMetrics, make_pool

EMITTED_NAMES: tuple[str, ...] = (
    BUFFER_BUDGET_USED_BYTES,
    BUFFER_RETAINED_ESTIMATE_BYTES,
    BUFFER_BUDGET_EXCEEDED_TOTAL,
    CHECKSUM_VERIFICATIONS_TOTAL,
    CHECKSUM_FAILURES_TOTAL,
    FSYNC_DURATION_SECONDS,
    BARRIER_FAILURES_TOTAL,
)
"""Every metric the storage core emits.

The retained-estimate gauge is the bounded buffer telemetry of 283cffa (CONTRACT.md section 9,
``estimator=python-v2``), emitted beside the budget gauge whenever the pool re-measures itself.
The last two are the data-file half of amendment A25. They are listed here because a roster that
omits a metric a component owns does not merely fail to test it: it ratifies the gap, and the
dashboard gate cannot catch it, because that gate checks that a panel exists and not that a
series is ever populated.
"""


@pytest.mark.parametrize("name", EMITTED_NAMES)
def test_every_metric_the_pool_emits_is_in_the_frozen_catalog(name: str) -> None:
    assert name in metric_names()
    assert name in {descriptor.name for descriptor in BUFFER_POOL_METRICS}


def test_every_name_the_pool_actually_emits_was_declared_first() -> None:
    """Run a workload and check what came out, rather than what the module says will come out.

    Comparing the declared tuple against the catalog it was built from proves nothing: it is the
    same objects on both sides. What can actually be wrong is an emission under a name nobody
    registered, which a recording sink refuses in production and which only a workload can find.
    """
    device = MemoryDevice()
    sink = RecordingMetrics()
    pool = make_pool(device, sink, budget_pages=2, db_label="alpha")
    device.create("heap.dat")
    first = pool.allocate("heap.dat", int(PageType.HEAP))
    first.insert_slot(b"payload")
    pool.unpin("heap.dat", first.page_index, dirty=True)
    pool.checkpoint()
    pool.invalidate()
    pool.pin("heap.dat", 0)
    pool.unpin("heap.dat", 0)
    second = pool.allocate("heap.dat", int(PageType.HEAP))
    pool.unpin("heap.dat", second.page_index, dirty=True)
    pool.pin("heap.dat", 0)
    pool.pin("heap.dat", 1)
    with pytest.raises(GrafxBufferBudgetExceeded):
        pool.allocate("heap.dat", int(PageType.HEAP))

    emitted = {name for _kind, name, _value, _labels in sink.calls}
    assert emitted, "the workload emitted nothing at all"
    assert emitted <= metric_names(), sorted(emitted - metric_names())
    assert emitted <= set(EMITTED_NAMES), sorted(emitted - set(EMITTED_NAMES))
    assert {
        BUFFER_BUDGET_USED_BYTES,
        BUFFER_BUDGET_EXCEEDED_TOTAL,
        CHECKSUM_VERIFICATIONS_TOTAL,
        FSYNC_DURATION_SECONDS,
    } <= emitted


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
