"""The JSON metrics adapter: the same numbers, written where a scraper cannot reach (FR-14, OR-6).

Not every deployment of an embedded database can host an HTTP endpoint. A batch job, a desktop
application or a continuous integration runner still needs the numbers, so this adapter emits the
whole aggregated state as one JSON document through an injected callable. The callable keeps the
adapter testable and file-free by default; ``RotatingFileWriter`` is the small piece of mechanism
that turns it into a real file when the host wants one.

The document is built from the very same ``MetricAggregator.collect()`` output the OpenMetrics
text body is built from, so the two adapters can never disagree about what was measured. The
registration contract is identical too, and for the same reason: it lives in the aggregator.

Document shape (version 1)::

    {"format": "okto-grafx-metrics",
     "version": 1,
     "metrics": [{"name": ..., "kind": ..., "unit": ..., "description": ...,
                  "samples": [{"labels": {...}, "value": 1.0},
                              {"labels": {...}, "count": 2.0, "sum": 0.5,
                               "buckets": [{"le": "0.005", "count": 1.0}, ...]}]}]}

The array of metrics is sorted by name and every sample list is sorted by its labels, so two
snapshots of the same state are byte-identical and a difference between two documents is a real
difference in the measurements.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager

from okto_grafx.adapters.metrics_openmetrics import (
    MetricAggregator,
    MetricSample,
    OpenMetricsSink,
    format_number,
)
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.events import EventSink
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricKind

__all__ = [
    "DOCUMENT_FORMAT",
    "DOCUMENT_VERSION",
    "JsonMetricsSink",
    "RotatingFileWriter",
    "build_document",
]

DOCUMENT_FORMAT: str = "okto-grafx-metrics"
"""The self-identifying tag of the JSON document, so a reader can refuse a foreign file."""

DOCUMENT_VERSION: int = 1
"""The version of the document shape, incremented whenever the shape stops being compatible."""


def build_document(
    collected: tuple[tuple[MetricDescriptor, tuple[MetricSample, ...]], ...],
) -> dict[str, object]:
    """Build the JSON-serialisable document of one collected metric state."""
    metrics: list[dict[str, object]] = []
    for descriptor, samples in collected:
        entries: list[dict[str, object]] = []
        for sample in samples:
            labels = {name: value for name, value in sample.labels}
            if descriptor.kind is MetricKind.HISTOGRAM:
                boundaries = [format_number(edge) for edge in descriptor.buckets]
                boundaries.append("+Inf")
                entries.append(
                    {
                        "labels": labels,
                        "count": sample.count,
                        "sum": sample.total,
                        "buckets": [
                            {"le": boundary, "count": cumulative}
                            for boundary, cumulative in zip(boundaries, sample.buckets, strict=True)
                        ],
                    }
                )
            else:
                entries.append({"labels": labels, "value": sample.value})
        metrics.append(
            {
                "name": descriptor.name,
                "kind": descriptor.kind.value,
                "unit": descriptor.unit,
                "description": descriptor.description,
                "samples": entries,
            }
        )
    return {"format": DOCUMENT_FORMAT, "version": DOCUMENT_VERSION, "metrics": metrics}


class RotatingFileWriter:
    """Append JSON documents to a file, one document per line, rotating on size.

    Rotation is the plain scheme an operator can reason about without a manual: when the file
    would grow past ``max_bytes`` it becomes ``<path>.1``, the previous ``<path>.1`` becomes
    ``<path>.2`` and so on up to ``backups``, and the oldest one is dropped. Nothing here ever
    touches a database file, so G6 is safe by construction.
    """

    __slots__ = ("_path", "_max_bytes", "_backups", "_encoding")

    def __init__(
        self, path: str, *, max_bytes: int = 1_048_576, backups: int = 1, encoding: str = "utf-8"
    ) -> None:
        if not isinstance(path, str) or not path:
            raise GrafxConfigurationError(
                f"The metrics writer needs a destination path; got {path!r}.",
                field="path",
                value=repr(path),
            )
        if max_bytes < 1:
            raise GrafxConfigurationError(
                f"The metrics writer needs a positive size bound; got {max_bytes!r}.",
                field="max_bytes",
                value=max_bytes,
            )
        if backups < 0:
            raise GrafxConfigurationError(
                f"The metrics writer cannot keep a negative number of backups; got {backups!r}.",
                field="backups",
                value=backups,
            )
        self._path = path
        self._max_bytes = max_bytes
        self._backups = backups
        self._encoding = encoding

    @property
    def path(self) -> str:
        """Return the destination path of the current document."""
        return self._path

    def __call__(self, document: str) -> None:
        """Append one document and rotate the file when it has grown past the bound."""
        payload = document if document.endswith("\n") else document + "\n"
        try:
            with open(self._path, "a", encoding=self._encoding, newline="\n") as handle:
                handle.write(payload)
            if os.path.getsize(self._path) >= self._max_bytes:
                self._rotate()
        except OSError as failure:
            raise GrafxConfigurationError(
                f"The metrics writer could not write to its destination: {failure.strerror}.",
                field="path",
                value=self._path,
            ) from failure

    def _rotate(self) -> None:
        """Shift the backups by one and move the current file into the first backup slot."""
        if self._backups == 0:
            os.remove(self._path)
            return
        oldest = f"{self._path}.{self._backups}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for index in range(self._backups - 1, 0, -1):
            source = f"{self._path}.{index}"
            if os.path.exists(source):
                os.replace(source, f"{self._path}.{index + 1}")
        os.replace(self._path, f"{self._path}.1")


class JsonMetricsSink:
    """A MetricsSink that publishes the aggregated state as a JSON document.

    Recording is delegated to a ``MetricAggregator``, which is the same core the OpenMetrics
    adapter uses, so the registration contract, the label bounds and the histogram arithmetic are
    literally the same code. ``publish()`` is explicit: nothing is written on a timer, because an
    embedded database does not own a background thread of its host.
    """

    __slots__ = ("_delegate", "_writer", "_indent")

    def __init__(
        self,
        writer: Callable[[str], None],
        *,
        enabled: bool = True,
        aggregator: MetricAggregator | None = None,
        indent: int | None = None,
        events: EventSink | None = None,
    ) -> None:
        if not callable(writer):
            raise GrafxConfigurationError(
                f"The JSON metrics sink needs a callable destination; got {type(writer).__name__}.",
                field="writer",
                value=repr(writer),
            )
        self._delegate = OpenMetricsSink(enabled=enabled, aggregator=aggregator, events=events)
        self._writer = writer
        self._indent = indent

    @property
    def enabled(self) -> bool:
        """Return True when this sink records what it is given."""
        return self._delegate.enabled

    @property
    def aggregator(self) -> MetricAggregator:
        """Return the aggregation core, which a second sink may share."""
        return self._delegate.aggregator

    def register(self, descriptor: MetricDescriptor) -> None:
        """Declare a metric, whether or not this sink is recording."""
        self._delegate.register(descriptor)

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Add to a counter."""
        self._delegate.increment(name, value, labels)

    def set_gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Set the current value of a gauge."""
        self._delegate.set_gauge(name, value, labels)

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Record one observation of a histogram."""
        self._delegate.observe(name, value, labels)

    def time(
        self, name: str, labels: Mapping[str, str] | None = None
    ) -> AbstractContextManager[None]:
        """Return a context manager that observes the duration of the block it wraps."""
        return self._delegate.time(name, labels)

    def snapshot(self) -> Mapping[str, object]:
        """Return the machine-readable current values."""
        return self._delegate.snapshot()

    def sample_value(self, name: str, labels: Mapping[str, str] | None = None) -> float | None:
        """Return one recorded number, or None when the series has not been touched."""
        return self._delegate.sample_value(name, labels)

    def document(self) -> str:
        """Return the current state as one JSON document, without writing it anywhere."""
        return json.dumps(
            build_document(self._delegate.aggregator.collect()),
            indent=self._indent,
            sort_keys=False,
            ensure_ascii=True,
        )

    def publish(self) -> str:
        """Build the document, hand it to the injected destination and return it."""
        document = self.document()
        try:
            self._writer(document)
        except GrafxConfigurationError:
            raise
        except Exception as failure:
            raise GrafxConfigurationError(
                "The JSON metrics destination refused the document.",
                field="writer",
                value=type(failure).__name__,
            ) from failure
        return document
