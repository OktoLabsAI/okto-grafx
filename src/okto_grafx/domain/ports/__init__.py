"""The ports of Okto Grafx (CONTRACT.md section 4, SPEC-M1 TR-2).

Everything the pure core needs from the outside world is declared here as a runtime checkable
Protocol: storage, time, cross-process coordination, page encoding, metrics, vector arithmetic
and events. An adapter satisfies the shape or the composition root refuses to start, which is
the fail-closed half of the hexagonal contract (BR-8, G5).
"""

from __future__ import annotations

from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.codec import PageCodec
from okto_grafx.domain.ports.coordination import (
    DeadOwnerReport,
    Lease,
    ProcessCoordinator,
    ReaderHandle,
)
from okto_grafx.domain.ports.events import EventSink
from okto_grafx.domain.ports.metrics import (
    FORBIDDEN_LABEL_NAMES,
    METRIC_NAME_PREFIX,
    METRIC_UNIT_SUFFIXES,
    UNBOUNDED_LABEL_CARDINALITY_LIMIT,
    LabelSpec,
    MetricDescriptor,
    MetricKind,
    MetricsSink,
)
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.ports.vectormath import DistanceMetric, VectorMath

__all__ = [
    "Clock",
    "DeadOwnerReport",
    "DistanceMetric",
    "EventSink",
    "FORBIDDEN_LABEL_NAMES",
    "LabelSpec",
    "Lease",
    "METRIC_NAME_PREFIX",
    "METRIC_UNIT_SUFFIXES",
    "MetricDescriptor",
    "MetricKind",
    "MetricsSink",
    "PageCodec",
    "ProcessCoordinator",
    "ReaderHandle",
    "StorageDevice",
    "UNBOUNDED_LABEL_CARDINALITY_LIMIT",
    "VectorMath",
]
