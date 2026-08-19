"""In-process aggregation and OpenMetrics exposition (SPEC-M1 FR-14, TR-7, OR-6, IR-3, AC-14).

Three things live here, in dependency order.

``MetricAggregator`` holds the numbers. It is the shared collection core of every recording sink
in Okto Grafx: ``OpenMetricsSink`` renders it as text and ``JsonMetricsSink`` serialises the same
``collect()`` output as JSON, so the two adapters can never disagree about what was measured.
Mechanism is legal in this layer, which is why the lock lives here and not in the engine.

``OpenMetricsSink`` is the MetricsSink of the default install. ``OpenMetricsPublisher`` is the
smallest possible HTTP surface over it: standard library only, bound to the loopback interface,
started and stopped explicitly by the host application, answering ``GET /metrics`` and nothing
else. It is the single HTTP endpoint of M1, because the engine is an embedded library.

Registration is the authority
-----------------------------
TR-7 says an unbounded label is rejected at registration and never in production. Taken to its
end, that means the descriptor is the whole truth about a metric and the recording path only
checks that reality matches the declaration:

* a name that was never registered is refused, so a typo cannot create a silent shadow series;
* a kind mismatch is refused, so a counter cannot be set like a gauge;
* a label key that the descriptor does not declare is refused, and so is a missing one, because
  a metric family whose series disagree about their label set is not scrapeable;
* a label value outside a declared ``allowed_values`` is refused;
* a new value of an unenumerated label is refused once ``max_cardinality`` distinct values have
  been seen, which is the only place the bound of a label such as ``db`` or ``space`` can be
  enforced at all.

Every one of those raises ``GrafxConfigurationError``: they are all defects in a declaration, and
the declaration is code, so the failure belongs to the build and not to the operator.

Exposition format
-----------------
The body is the Prometheus text exposition format, version 0.0.4, which is what the frozen API
contract of SPEC-M1 pins as the content type. That version has no ``# EOF`` trailer and no
``_created`` series; both belong to OpenMetrics 1.0 and would be rejected by a 0.0.4 parser.

Ordering is total and independent of call order: metric families sort by name, series sort by
their rendered label tuple. A scrape of the same state therefore produces the same bytes on every
process and every platform, which is what makes a golden test meaningful.
"""

from __future__ import annotations

import sys
import threading
from bisect import bisect_left
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from time import perf_counter
from types import MappingProxyType

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.events import EventSink
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricKind, MetricsSink

__all__ = [
    "CONTENT_TYPE",
    "LabelKey",
    "MetricSample",
    "MetricAggregator",
    "OpenMetricsSink",
    "OpenMetricsPublisher",
    "escape_help",
    "escape_label_value",
    "format_number",
    "render_exposition",
]

CONTENT_TYPE: str = "text/plain; version=0.0.4"
"""The content type frozen by the API contract of SPEC-M1 for ``GET /metrics``."""

LabelKey = tuple[tuple[str, str], ...]
"""A series identity: the label values in the order the descriptor declares them."""

_EMPTY_LABELS: Mapping[str, str] = MappingProxyType({})
"""Shared stand-in for a call that passed no labels, so the common path allocates nothing."""

_UNAVAILABLE_BODY: bytes = (
    b"Metrics are unavailable: the publisher holds no recording sink.\n"
)
_NOT_FOUND_BODY: bytes = b"Not found.\n"
_PLAIN_TEXT: str = "text/plain; charset=utf-8"
_METRICS_PATH: str = "/metrics"

_TEARDOWN_ERRORS: tuple[type[BaseException], ...] = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionError,
    ConnectionResetError,
    TimeoutError,
)
"""Failures that mean the client went away, which on a scrape endpoint is not an error."""


def escape_help(text: str) -> str:
    """Escape a description for a ``# HELP`` line: backslash and newline, nothing else."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def escape_label_value(value: str) -> str:
    """Escape a label value: backslash, double quote and newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_number(value: float) -> str:
    """Render a number the way a 0.0.4 parser expects, with an integral value kept short."""
    if value != value:
        return "NaN"
    if value == float("inf"):
        return "+Inf"
    if value == float("-inf"):
        return "-Inf"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


@dataclass(frozen=True, slots=True)
class MetricSample:
    """One series of one metric, copied out of the aggregator so it can be read without a lock.

    A counter or a gauge uses ``value`` alone. A histogram uses ``count``, ``total`` and
    ``buckets``, where ``buckets`` holds cumulative counts aligned with the bucket boundaries of
    the descriptor plus one final entry for the implicit ``+Inf`` bucket.
    """

    labels: LabelKey
    value: float = 0.0
    count: float = 0.0
    total: float = 0.0
    buckets: tuple[float, ...] = ()


@dataclass(slots=True)
class _HistogramState:
    """Mutable per-series histogram accumulator: one counter per bucket, plus sum and count."""

    counts: list[float]
    total: float = 0.0
    count: float = 0.0


@dataclass(slots=True)
class _MetricState:
    """Everything the aggregator keeps for one registered metric."""

    descriptor: MetricDescriptor
    values: dict[LabelKey, float] = field(default_factory=dict)
    histograms: dict[LabelKey, _HistogramState] = field(default_factory=dict)
    observed_label_values: dict[str, set[str]] = field(default_factory=dict)


class MetricAggregator:
    """Thread-safe in-process aggregation of the registered metrics.

    The lock is held for the shortest possible time: recording takes it around a few dictionary
    operations, and ``collect()`` takes it only to copy the numbers out, so the scraping thread
    never formats text while the engine waits.
    """

    __slots__ = ("_lock", "_metrics")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, _MetricState] = {}

    # --- declaration --------------------------------------------------------------------------

    def register(self, descriptor: MetricDescriptor) -> None:
        """Declare a metric. Repeating an identical declaration is a no-op; changing one is not."""
        if not isinstance(descriptor, MetricDescriptor):
            raise GrafxConfigurationError(
                f"A metric registration needs a MetricDescriptor; got {type(descriptor).__name__}.",
                field="descriptor",
                value=repr(descriptor),
            )
        with self._lock:
            existing = self._metrics.get(descriptor.name)
            if existing is None:
                self._metrics[descriptor.name] = _MetricState(descriptor=descriptor)
                return
            if existing.descriptor != descriptor:
                raise GrafxConfigurationError(
                    f"Metric {descriptor.name!r} is already registered with a different "
                    f"declaration; a metric name is a contract, not a variable.",
                    field="name",
                    value=descriptor.name,
                )

    def descriptors(self) -> tuple[MetricDescriptor, ...]:
        """Return every registered descriptor, sorted by name."""
        with self._lock:
            return tuple(sorted((state.descriptor for state in self._metrics.values()), key=_by_name))

    def is_registered(self, name: str) -> bool:
        """Return True when a metric of this name has been declared on this aggregator."""
        with self._lock:
            return name in self._metrics

    # --- recording ----------------------------------------------------------------------------

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Add a non-negative amount to a counter."""
        amount = _checked_value(name, value)
        if amount < 0.0:
            raise GrafxConfigurationError(
                f"Counter {name!r} cannot be incremented by a negative amount; got {amount!r}.",
                field="value",
                value=amount,
            )
        with self._lock:
            state = self._state(name, MetricKind.COUNTER)
            key = self._label_key(state, labels)
            state.values[key] = state.values.get(key, 0.0) + amount

    def set_gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Set the current value of a gauge."""
        amount = _checked_value(name, value)
        with self._lock:
            state = self._state(name, MetricKind.GAUGE)
            state.values[self._label_key(state, labels)] = amount

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Record one observation of a histogram."""
        amount = _checked_value(name, value)
        with self._lock:
            state = self._state(name, MetricKind.HISTOGRAM)
            key = self._label_key(state, labels)
            boundaries = state.descriptor.buckets
            histogram = state.histograms.get(key)
            if histogram is None:
                histogram = _HistogramState(counts=[0.0] * len(boundaries))
                state.histograms[key] = histogram
            index = bisect_left(boundaries, amount)
            if index < len(boundaries):
                histogram.counts[index] += 1.0
            histogram.total += amount
            histogram.count += 1.0

    def prepare_observation(self, name: str, labels: Mapping[str, str] | None = None) -> None:
        """Validate everything an observation needs, before the measured block runs.

        The label values are recorded here, so the declared cardinality bound is enforced at the
        point where raising is correct. The matching ``observe()`` at the end of the block then
        finds a series that already exists and has nothing left to refuse.
        """
        with self._lock:
            state = self._state(name, MetricKind.HISTOGRAM)
            self._label_key(state, labels)

    def reset(self) -> None:
        """Drop every recorded number while keeping every declaration."""
        with self._lock:
            for state in self._metrics.values():
                state.values.clear()
                state.histograms.clear()
                state.observed_label_values.clear()

    # --- reading ------------------------------------------------------------------------------

    def collect(self) -> tuple[tuple[MetricDescriptor, tuple[MetricSample, ...]], ...]:
        """Copy the current state out, sorted by metric name and then by series labels."""
        collected: list[tuple[MetricDescriptor, tuple[MetricSample, ...]]] = []
        with self._lock:
            for state in sorted(self._metrics.values(), key=_by_descriptor_name):
                descriptor = state.descriptor
                samples: list[MetricSample] = []
                if descriptor.kind is MetricKind.HISTOGRAM:
                    for key, histogram in state.histograms.items():
                        cumulative: list[float] = []
                        running = 0.0
                        for bucket_count in histogram.counts:
                            running += bucket_count
                            cumulative.append(running)
                        cumulative.append(histogram.count)
                        samples.append(
                            MetricSample(
                                labels=key,
                                count=histogram.count,
                                total=histogram.total,
                                buckets=tuple(cumulative),
                            )
                        )
                else:
                    for key, value in state.values.items():
                        samples.append(MetricSample(labels=key, value=value))
                samples.sort(key=_by_labels)
                collected.append((descriptor, tuple(samples)))
        return tuple(collected)

    def snapshot(self) -> Mapping[str, object]:
        """Return the machine-readable current state; the CI gate reads this, never a log."""
        report: dict[str, object] = {}
        for descriptor, samples in self.collect():
            entries: list[Mapping[str, object]] = []
            for sample in samples:
                labels = MappingProxyType(dict(sample.labels))
                if descriptor.kind is MetricKind.HISTOGRAM:
                    boundaries = [format_number(edge) for edge in descriptor.buckets]
                    boundaries.append("+Inf")
                    entries.append(
                        MappingProxyType(
                            {
                                "labels": labels,
                                "count": sample.count,
                                "sum": sample.total,
                                "buckets": MappingProxyType(
                                    dict(zip(boundaries, sample.buckets, strict=True))
                                ),
                            }
                        )
                    )
                else:
                    entries.append(MappingProxyType({"labels": labels, "value": sample.value}))
            report[descriptor.name] = MappingProxyType(
                {
                    "kind": descriptor.kind.value,
                    "unit": descriptor.unit,
                    "description": descriptor.description,
                    "samples": tuple(entries),
                }
            )
        return MappingProxyType(report)

    def sample_value(self, name: str, labels: Mapping[str, str] | None = None) -> float | None:
        """Return one recorded number, or None when that series has not been touched yet.

        A counter or a gauge answers with its value; a histogram answers with its observation
        count. This is the shortest path a continuous integration gate can take to a measured
        number, which AC-14 and SPEC-VEC AC-10 both require to exist.
        """
        with self._lock:
            state = self._state(name, None)
            key = self._label_key(state, labels, record=False)
            if state.descriptor.kind is MetricKind.HISTOGRAM:
                histogram = state.histograms.get(key)
                return None if histogram is None else histogram.count
            return state.values.get(key)

    # --- internals ----------------------------------------------------------------------------

    def _state(self, name: str, kind: MetricKind | None) -> _MetricState:
        """Return the state of a registered metric, refusing an unknown name or a wrong kind."""
        state = self._metrics.get(name)
        if state is None:
            raise GrafxConfigurationError(
                f"Metric {name!r} was never registered; register the descriptor before emitting.",
                field="name",
                value=name,
            )
        if kind is not None and state.descriptor.kind is not kind:
            raise GrafxConfigurationError(
                f"Metric {name!r} is a {state.descriptor.kind.value} and cannot be used as a "
                f"{kind.value}.",
                field="kind",
                value=state.descriptor.kind.value,
            )
        return state

    def _label_key(
        self,
        state: _MetricState,
        labels: Mapping[str, str] | None,
        *,
        record: bool = True,
    ) -> LabelKey:
        """Validate the labels of one emission and return the canonical series identity."""
        descriptor = state.descriptor
        if labels is None:
            provided: Mapping[str, str] = _EMPTY_LABELS
        elif isinstance(labels, Mapping):
            provided = labels
        else:
            raise GrafxConfigurationError(
                f"Metric {descriptor.name!r} needs its labels as a mapping; got "
                f"{type(labels).__name__}.",
                field="labels",
                value=type(labels).__name__,
            )
        declared = descriptor.labels
        if len(provided) != len(declared):
            unknown = sorted(set(provided) - {label.name for label in declared})
            if unknown:
                raise GrafxConfigurationError(
                    f"Metric {descriptor.name!r} does not declare the label(s) "
                    f"{', '.join(repr(name) for name in unknown)}; a label is part of the "
                    f"registration contract.",
                    field="labels",
                    value=unknown,
                )
            missing = sorted({label.name for label in declared} - set(provided))
            raise GrafxConfigurationError(
                f"Metric {descriptor.name!r} requires the label(s) "
                f"{', '.join(repr(name) for name in missing)}.",
                field="labels",
                value=missing,
            )
        key: list[tuple[str, str]] = []
        for label in declared:
            if label.name not in provided:
                raise GrafxConfigurationError(
                    f"Metric {descriptor.name!r} requires the label {label.name!r}.",
                    field="labels",
                    value=label.name,
                )
            value = provided[label.name]
            if not isinstance(value, str) or not value:
                raise GrafxConfigurationError(
                    f"Metric {descriptor.name!r} needs a non-empty string for the label "
                    f"{label.name!r}; got {value!r}.",
                    field="labels",
                    value=repr(value),
                )
            if label.allowed_values is not None and value not in label.allowed_values:
                raise GrafxConfigurationError(
                    f"Metric {descriptor.name!r} declares the label {label.name!r} over "
                    f"{sorted(label.allowed_values)}; {value!r} is not one of them.",
                    field="labels",
                    value=value,
                )
            if record:
                seen = state.observed_label_values.setdefault(label.name, set())
                if value not in seen:
                    if len(seen) >= label.max_cardinality:
                        raise GrafxConfigurationError(
                            f"Metric {descriptor.name!r} declares at most "
                            f"{label.max_cardinality} distinct values for the label "
                            f"{label.name!r}; {value!r} would be one too many.",
                            field="labels",
                            value=value,
                        )
                    seen.add(value)
            key.append((label.name, value))
        return tuple(key)


def _by_name(descriptor: MetricDescriptor) -> str:
    """Sort key over descriptors."""
    return descriptor.name


def _by_descriptor_name(state: _MetricState) -> str:
    """Sort key over metric states."""
    return state.descriptor.name


def _by_labels(sample: MetricSample) -> LabelKey:
    """Sort key over the series of one metric."""
    return sample.labels


def _checked_value(name: str, value: float) -> float:
    """Return the value as a finite float, or refuse it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrafxConfigurationError(
            f"Metric {name!r} needs a numeric value; got {value!r}.",
            field="value",
            value=repr(value),
        )
    amount = float(value)
    if not isfinite(amount):
        raise GrafxConfigurationError(
            f"Metric {name!r} needs a finite value; got {value!r}.",
            field="value",
            value=repr(value),
        )
    return amount


def render_exposition(
    collected: tuple[tuple[MetricDescriptor, tuple[MetricSample, ...]], ...],
) -> str:
    """Render collected metrics as a Prometheus 0.0.4 text exposition body."""
    lines: list[str] = []
    for descriptor, samples in collected:
        name = descriptor.name
        lines.append(f"# HELP {name} {escape_help(descriptor.description)}\n")
        lines.append(f"# TYPE {name} {descriptor.kind.value}\n")
        if descriptor.kind is MetricKind.HISTOGRAM:
            boundaries = [format_number(edge) for edge in descriptor.buckets]
            boundaries.append("+Inf")
            for sample in samples:
                for boundary, cumulative in zip(boundaries, sample.buckets, strict=True):
                    bucket_labels = (*sample.labels, ("le", boundary))
                    lines.append(
                        f"{name}_bucket{_render_labels(bucket_labels)} "
                        f"{format_number(cumulative)}\n"
                    )
                lines.append(
                    f"{name}_sum{_render_labels(sample.labels)} {format_number(sample.total)}\n"
                )
                lines.append(
                    f"{name}_count{_render_labels(sample.labels)} {format_number(sample.count)}\n"
                )
        else:
            for sample in samples:
                lines.append(
                    f"{name}{_render_labels(sample.labels)} {format_number(sample.value)}\n"
                )
    return "".join(lines)


def _render_labels(labels: LabelKey) -> str:
    """Render a label set as the brace-delimited suffix of a series name."""
    if not labels:
        return ""
    body = ",".join(f'{name}="{escape_label_value(value)}"' for name, value in labels)
    return "{" + body + "}"


class OpenMetricsSink:
    """The recording MetricsSink of the default install, exposed as Prometheus 0.0.4 text.

    A sink built with ``enabled=False`` keeps its declarations and drops every measurement
    without taking the lock, which makes it exactly as cheap as the no-op adapter on the hot
    path. Validation lives with recording, so a disabled sink checks nothing: an installation
    that wants the registration contract enforced keeps the sink enabled.
    """

    __slots__ = ("_aggregator", "_enabled", "_events")

    def __init__(
        self,
        *,
        enabled: bool = True,
        aggregator: MetricAggregator | None = None,
        events: EventSink | None = None,
    ) -> None:
        self._aggregator = MetricAggregator() if aggregator is None else aggregator
        self._enabled = bool(enabled)
        self._events = events

    @property
    def enabled(self) -> bool:
        """Return True when this sink records what it is given."""
        return self._enabled

    @property
    def aggregator(self) -> MetricAggregator:
        """Return the aggregation core, which a second sink may share."""
        return self._aggregator

    @property
    def content_type(self) -> str:
        """Return the content type a scraper must be given for ``render()``."""
        return CONTENT_TYPE

    def register(self, descriptor: MetricDescriptor) -> None:
        """Declare a metric, whether or not this sink is recording."""
        self._aggregator.register(descriptor)

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Add to a counter."""
        if not self._enabled:
            return
        self._aggregator.increment(name, value, labels)

    def set_gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Set the current value of a gauge."""
        if not self._enabled:
            return
        self._aggregator.set_gauge(name, value, labels)

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Record one observation of a histogram."""
        if not self._enabled:
            return
        self._aggregator.observe(name, value, labels)

    def time(
        self, name: str, labels: Mapping[str, str] | None = None
    ) -> AbstractContextManager[None]:
        """Return a context manager that observes the duration of the block it wraps.

        The declaration is checked when the block is entered, which is where a wrong name, a
        wrong kind or a bad label is a programming error worth raising. What happens at exit is
        only a measurement: it can never replace the outcome of the block it measured.
        """
        return self._timed(name, labels)

    @contextmanager
    def _timed(self, name: str, labels: Mapping[str, str] | None) -> Iterator[None]:
        """Measure a block with the monotonic performance counter and observe the result."""
        if not self._enabled:
            yield
            return
        self._aggregator.prepare_observation(name, labels)
        started = perf_counter()
        try:
            yield
        finally:
            elapsed = perf_counter() - started
            try:
                self._aggregator.observe(name, elapsed, labels)
            except Exception as failure:
                # A metric is a side channel. An operation that already succeeded must not be
                # turned into a failure, and an error raised inside the block must not be
                # replaced by one raised on the way out of it.
                self._report_failed_observation(name, failure)

    def _report_failed_observation(self, name: str, failure: BaseException) -> None:
        """Route a measurement that could not be recorded to the event sink, then let it go."""
        events = self._events
        if events is None:
            return
        try:
            events.emit(
                "metrics.observation_failed",
                {"metric": name, "error": type(failure).__name__},
            )
        except Exception:
            return

    def snapshot(self) -> Mapping[str, object]:
        """Return the machine-readable current values."""
        return self._aggregator.snapshot()

    def sample_value(self, name: str, labels: Mapping[str, str] | None = None) -> float | None:
        """Return one recorded number, or None when the series has not been touched."""
        return self._aggregator.sample_value(name, labels)

    def render(self) -> str:
        """Render the current state as a Prometheus 0.0.4 text exposition body."""
        return render_exposition(self._aggregator.collect())


class _MetricsServer(ThreadingHTTPServer):
    """The HTTP server of the publisher, carrying the sink its handler answers from."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        sink: MetricsSink | None,
        events: EventSink | None = None,
    ) -> None:
        self.sink = sink
        self.events = events
        super().__init__(address, _MetricsRequestHandler)

    def handle_error(self, request: object, client_address: object) -> None:
        """Absorb a failed request instead of printing a traceback to the host standard error.

        A scraper that gives up on its own timeout tears the connection down mid-response, which
        is normal operation on this endpoint and not an error at all; the default handler of the
        standard library would print a full traceback for each one. A genuine internal fault is
        still worth telling somebody about, so it goes to the event sink, which is the channel
        the host chose, rather than to a stream the host never opted into.
        """
        failure = sys.exc_info()[1]
        if failure is None or isinstance(failure, _TEARDOWN_ERRORS):
            return
        events = self.events
        if events is None:
            return
        try:
            events.emit(
                "metrics.publisher_request_failed", {"error": type(failure).__name__}
            )
        except Exception:
            return


class _MetricsRequestHandler(BaseHTTPRequestHandler):
    """Answers ``GET /metrics`` and refuses everything else."""

    protocol_version = "HTTP/1.1"
    server_version = "OktoGrafxMetrics/1.0"
    sys_version = ""

    def do_GET(self) -> None:
        """Answer a GET request."""
        self._answer(with_body=True)

    def do_HEAD(self) -> None:
        """Answer a HEAD request with the headers of the matching GET."""
        self._answer(with_body=False)

    def do_POST(self) -> None:
        """Refuse a POST: the metrics surface is read-only."""
        self._send(404, _NOT_FOUND_BODY, _PLAIN_TEXT, with_body=True)

    def do_PUT(self) -> None:
        """Refuse a PUT: the metrics surface is read-only."""
        self._send(404, _NOT_FOUND_BODY, _PLAIN_TEXT, with_body=True)

    def do_DELETE(self) -> None:
        """Refuse a DELETE: the metrics surface is read-only."""
        self._send(404, _NOT_FOUND_BODY, _PLAIN_TEXT, with_body=True)

    def log_message(self, format: str, *args: object) -> None:
        """Stay silent: an embedded database does not write to the standard error of its host."""

    def _answer(self, *, with_body: bool) -> None:
        """Route one read request to the metrics body, a 503 or a 404."""
        if self.path.split("?", 1)[0] != _METRICS_PATH:
            self._send(404, _NOT_FOUND_BODY, _PLAIN_TEXT, with_body=with_body)
            return
        try:
            sink = getattr(self.server, "sink", None)
            renderer = getattr(sink, "render", None)
            if sink is None or renderer is None or not sink.enabled:
                self._send(503, _UNAVAILABLE_BODY, _PLAIN_TEXT, with_body=with_body)
                return
            body = renderer().encode("utf-8")
        except Exception:  # pragma: no cover - a broken sink must not kill the serving thread
            self._send(503, _UNAVAILABLE_BODY, _PLAIN_TEXT, with_body=with_body)
            return
        self._send(200, body, CONTENT_TYPE, with_body=with_body)

    def _send(self, status: int, body: bytes, content_type: str, *, with_body: bool) -> None:
        """Write one complete response, always with an explicit length.

        A client that closed the connection before reading is the ordinary outcome of a scrape
        timeout, so the write is allowed to fail: the connection is marked closed and the
        response is abandoned without a word. Nothing here reaches the standard error of the
        host, and nothing that is not a Grafx error escapes towards its caller.
        """
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if with_body:
                self.wfile.write(body)
        except _TEARDOWN_ERRORS:
            self.close_connection = True


class OpenMetricsPublisher:
    """A loopback HTTP endpoint that exposes one sink at ``GET /metrics``.

    The publisher is started and stopped by the host application; nothing starts a thread on
    import and nothing survives ``stop()``. Bind to port 0 to let the operating system choose a
    free port and read it back from ``port`` after ``start()``.

    ``GET /metrics`` answers 200 with the exposition body, any other path answers 404, and a
    publisher holding no sink, or holding a sink that is not recording, answers 503 exactly as
    the frozen API contract of SPEC-M1 states.
    """

    __slots__ = ("_events", "_host", "_requested_port", "_sink", "_server", "_thread")

    def __init__(
        self,
        sink: MetricsSink | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        events: EventSink | None = None,
    ) -> None:
        self._host = host
        self._requested_port = port
        self._sink = sink
        self._events = events
        self._server: _MetricsServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def sink(self) -> MetricsSink | None:
        """Return the sink this publisher exposes, which may be None."""
        return self._sink

    @property
    def running(self) -> bool:
        """Return True between a successful ``start()`` and the matching ``stop()``."""
        return self._server is not None

    @property
    def host(self) -> str:
        """Return the interface the publisher binds to."""
        return self._host

    @property
    def port(self) -> int:
        """Return the bound port, or the requested one while the publisher is stopped."""
        server = self._server
        return self._requested_port if server is None else int(server.server_address[1])

    @property
    def url(self) -> str:
        """Return the absolute URL of the metrics endpoint."""
        return f"http://{self._host}:{self.port}{_METRICS_PATH}"

    def start(self) -> int:
        """Bind the socket, serve in a daemon thread and return the bound port."""
        if self._server is not None:
            raise GrafxConfigurationError(
                "The metrics publisher is already running; stop it before starting it again.",
                field="state",
                value="running",
            )
        try:
            server = _MetricsServer(
                (self._host, self._requested_port), self._sink, self._events
            )
        except OSError as failure:
            raise GrafxConfigurationError(
                f"The metrics publisher could not bind {self._host}:{self._requested_port}.",
                field="address",
                value=f"{self._host}:{self._requested_port}",
            ) from failure
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            name="oktografx-metrics",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()
        return self.port

    def stop(self) -> None:
        """Stop serving and release the socket. Stopping a stopped publisher does nothing."""
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)

    def __enter__(self) -> OpenMetricsPublisher:
        """Start the publisher and return it."""
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        """Stop the publisher and let any error propagate."""
        self.stop()
        return False
