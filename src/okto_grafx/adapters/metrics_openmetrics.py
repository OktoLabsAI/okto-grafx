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

import selectors
import socket
import sys
import threading
from bisect import bisect_left
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BufferedReader, BytesIO
from math import isfinite
from time import monotonic, perf_counter
from types import MappingProxyType

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.events import EventSink
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricKind, MetricsSink

__all__ = [
    "CONTENT_TYPE",
    "DEFAULT_CONNECTION_TIMEOUT_SECONDS",
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "LabelKey",
    "MetricSample",
    "MetricAggregator",
    "OpenMetricsSink",
    "OpenMetricsPublisher",
    "FREE_FORM_LABEL_ALPHABET",
    "FREE_FORM_LABEL_MAX_LENGTH",
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

DEFAULT_CONNECTION_TIMEOUT_SECONDS: float = 10.0
"""Read deadline of one connection.

A scraper sends its request immediately after connecting, so anything slower than this is a
client that stopped talking. Without a deadline such a connection would pin a thread and a
socket for as long as the host process lives.
"""

DEFAULT_SHUTDOWN_TIMEOUT_SECONDS: float = 5.0
"""How long ``stop()`` waits for the serving threads it has already told to finish."""

_POLL_INTERVAL_SECONDS: float = 0.05
"""How often an idle connection looks up to see whether the publisher is stopping."""

_READ_CHUNK_BYTES: int = 65536
"""How much of a request head is taken from the socket per readable event."""

_MAX_HEAD_BYTES: int = 65536 + 16384
"""Ceiling on a request head: the request line limit of the standard library plus its headers.

Past this the head is handed to the parser as it stands, whatever state it is in, so a client
cannot buy unbounded memory by never finishing a request. What the parser then answers depends
on the head it received: an over-long request line becomes 414 and too many headers become 431,
while a head that is merely unterminated is treated the same way the standard library treats a
connection that ended, and is answered on its merits.
"""

_UNAVAILABLE_BODY: bytes = (
    b"Metrics are unavailable: the publisher holds no recording sink.\n"
)
_RENDER_FAILED_BODY: bytes = (
    b"Metrics are unavailable: the recording sink could not be rendered.\n"
)
_NOT_FOUND_BODY: bytes = b"Not found.\n"
_MISSING_HOST_BODY: bytes = b"Bad request: HTTP/1.1 requires a Host header.\n"
_PLAIN_TEXT: str = "text/plain; charset=utf-8"
_METRICS_PATH: str = "/metrics"

_TEARDOWN_ERRORS: tuple[type[BaseException], ...] = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionError,
    ConnectionResetError,
    TimeoutError,
    OSError,
)
"""Failures that mean the connection went away, which on a scrape endpoint is not an error.

``OSError`` is the last entry because a socket torn down under a blocked thread surfaces
differently on each platform: a connection error on POSIX, a bare ``OSError`` with WinError 10038
on Windows. The endpoint touches no file and no device, so a socket is the only thing here that
can raise one.
"""


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
        declared_names = {label.name for label in declared}
        provided_names = set(provided)
        unknown = sorted(provided_names - declared_names)
        missing = sorted(declared_names - provided_names)
        if unknown or missing:
            # Both sides are reported, and the comparison is between the sets rather than their
            # sizes: swapping a declared label for an undeclared one keeps the count identical,
            # and telling that caller only that something is missing sends them looking for the
            # wrong defect.
            complaints: list[str] = []
            if unknown:
                complaints.append(
                    f"does not declare the label(s) {', '.join(repr(name) for name in unknown)}"
                )
            if missing:
                complaints.append(
                    f"requires the label(s) {', '.join(repr(name) for name in missing)}"
                )
            raise GrafxConfigurationError(
                f"Metric {descriptor.name!r} {' and '.join(complaints)}; a label set is part of "
                f"the registration contract.",
                field="labels",
                value={"unknown": unknown, "missing": missing},
            )
        key: list[tuple[str, str]] = []
        for label in declared:
            # Presence is already proven: the set comparison above left nothing missing.
            value = provided[label.name]
            if not isinstance(value, str) or not value:
                raise GrafxConfigurationError(
                    f"Metric {descriptor.name!r} needs a non-empty string for the label "
                    f"{label.name!r}; got {value!r}.",
                    field="labels",
                    value=repr(value),
                )
            if label.allowed_values is None:
                _check_free_form_value(descriptor.name, label.name, value)
            elif value not in label.allowed_values:
                raise GrafxConfigurationError(
                    f"Metric {descriptor.name!r} declares the label {label.name!r} over "
                    f"{sorted(label.allowed_values)}; {value!r} is not one of them.",
                    field="labels",
                    value=value,
                )
            if record:
                seen = state.observed_label_values.get(label.name, frozenset())
                if value not in seen and len(seen) >= label.max_cardinality:
                    raise GrafxConfigurationError(
                        f"Metric {descriptor.name!r} declares at most "
                        f"{label.max_cardinality} distinct values for the label "
                        f"{label.name!r}; {value!r} would be one too many.",
                        field="labels",
                        value=value,
                    )
            key.append((label.name, value))
        if record:
            # Nothing is recorded until every label has passed. A refusal on the second label of
            # a metric must not leave the first one counted against its budget: a rejected
            # emission leaves no residue at all, which is what makes the bound reproducible.
            for name, value in key:
                state.observed_label_values.setdefault(name, set()).add(value)
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


FREE_FORM_LABEL_ALPHABET: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)
"""Every character a label value may contain when its domain is not enumerated.

CONTRACT.md section 9 says these values carry a short hash or a catalog name, never a path and
never free text, and this is that sentence made enforceable. It is stated positively on purpose.
A rule that rejected the shapes that look like paths would be a denylist over a string the
caller controls, and a denylist is a password: the first spelling nobody enumerated walks
straight through. So the set of accepted characters is closed, and everything outside it is
refused without anyone having to predict it.

The alphabet is exactly what the two promised contents need: hexadecimal and base-36 digests
fit, and so do catalog names such as ``minilm-v2`` or ``warehouse.main``. A filesystem path
cannot, in either family, because the separators are absent; a sentence cannot, because the
space is absent; and neither can a label smuggling exposition syntax, because the quote, the
brace, the comma and the newline are all absent.
"""

FREE_FORM_LABEL_MAX_LENGTH: int = 64
"""Longest value an unenumerated label may take: a catalog name and a digest, not a document.

The cardinality bound already stops a label from growing without limit. This is the other axis:
a scrape endpoint is read by third parties, and a value long enough to hold a deployment layout
discloses it to all of them however few distinct values there are.
"""


def _check_free_form_value(metric: str, label: str, value: str) -> None:
    """Refuse a value an unenumerated label may not carry, naming what it may."""
    if len(value) > FREE_FORM_LABEL_MAX_LENGTH:
        raise GrafxConfigurationError(
            f"Metric {metric!r} allows at most {FREE_FORM_LABEL_MAX_LENGTH} characters in the "
            f"label {label!r}, which carries a short hash or a catalog name; got {len(value)}.",
            field="labels",
            value=label,
        )
    for character in value:
        if character not in FREE_FORM_LABEL_ALPHABET:
            raise GrafxConfigurationError(
                f"Metric {metric!r} allows only letters, digits, '-', '_' and '.' in the label "
                f"{label!r}, which carries a short hash or a catalog name, never a path or free "
                f"text; got {character!r}.",
                field="labels",
                value=label,
            )


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


_SHUTDOWN_WAIT_SECONDS: float = 5.0
"""Longest the accept loop is waited for after it has been asked to stop."""


@contextmanager
def _selector_over(server: object) -> Iterator[selectors.BaseSelector]:
    """Yield a selector watching one server socket, closed on the way out."""
    selector = selectors.DefaultSelector()
    try:
        selector.register(server, selectors.EVENT_READ)
        yield selector
    finally:
        with suppress(Exception):
            selector.close()


class _MetricsServer(ThreadingHTTPServer):
    """The HTTP server of the publisher: it knows every connection and every thread it owns.

    The standard library server does not. ``shutdown()`` only breaks the accept loop and
    ``server_close()`` only closes the listening socket, so a connection that was already
    accepted keeps a daemon thread alive and keeps answering, and nothing joins those threads
    because ``_Threads.append`` drops daemon threads on the floor. An embedded database cannot
    leave either of those behind in a host that asked it to stop, so this server keeps the two
    registries the standard library omits and uses them in ``close_live_connections()`` and
    ``await_handlers()``.
    """

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        sink: MetricsSink | None,
        events: EventSink | None = None,
        *,
        connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS,
    ) -> None:
        self.sink = sink
        self.events = events
        self.connection_timeout = connection_timeout
        self._stopping = False
        self._loop_exited = threading.Event()
        self._loop_exited.set()
        self._registry_lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._handlers: set[threading.Thread] = set()
        super().__init__(address, _MetricsRequestHandler)

    def server_bind(self) -> None:
        """Take the address exclusively, in the way each family actually means it (A30).

        The two platforms need opposite options for the same guarantee. POSIX needs
        ``SO_REUSEADDR`` or a listener that has just been closed cannot be bound again while the
        connections it served sit in TIME_WAIT. Windows needs the opposite: it does not have that
        problem, and ``SO_REUSEADDR`` there means something else entirely, namely that a second
        socket may take an address a live socket already owns. A publisher that binds that way
        reports success on a port it does not own and publishes numbers a continuous integration
        gate reads, so on Windows the address is claimed with ``SO_EXCLUSIVEADDRUSE`` instead.

        The result is the same statement on both families: if the address is taken, ``start()``
        raises rather than pretending.
        """
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            # The two options contradict each other, so the reuse flag goes off first.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()

    # --- registries -----------------------------------------------------------------------

    def get_request(self) -> tuple[socket.socket, object]:
        """Accept a connection and remember it, so shutting down can reach it later."""
        connection, address = super().get_request()
        with self._registry_lock:
            self._connections.add(connection)
        return connection, address

    def shutdown_request(self, request: socket.socket) -> None:
        """Forget a connection that finished, then close it the ordinary way."""
        with self._registry_lock:
            self._connections.discard(request)
        super().shutdown_request(request)

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        """Serve one connection, under a named and tracked thread."""
        current = threading.current_thread()
        current.name = "oktografx-metrics-handler"
        with self._registry_lock:
            self._handlers.add(current)
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._registry_lock:
                self._handlers.discard(current)

    @property
    def stopping(self) -> bool:
        """Return True once the publisher has begun shutting this server down."""
        return self._stopping

    def is_serving_thread(self, thread: threading.Thread) -> bool:
        """Return True when a teardown may end up waiting for this thread.

        The accept loop is waited for by ``shutdown()`` and a handler by ``await_handlers()``,
        so neither may ever block waiting for a teardown someone else owns: it would be waiting
        for the thread that is waiting for it.
        """
        with self._registry_lock:
            return thread in self._handlers

    @property
    def live_connections(self) -> int:
        """Return how many accepted connections this server still owns."""
        with self._registry_lock:
            return len(self._connections)

    @property
    def live_handlers(self) -> int:
        """Return how many serving threads this server still owns."""
        with self._registry_lock:
            return len(self._handlers)

    # --- shutdown -------------------------------------------------------------------------

    def begin_stopping(self) -> None:
        """Mark the server as shutting down, so a torn-down connection is never a fault."""
        with self._registry_lock:
            self._stopping = True

    def close_live_connections(self) -> int:
        """Close every accepted connection and return how many there were.

        ``shutdown`` before ``close`` on purpose: closing a socket does not reliably wake a
        thread that is blocked reading it, while a shutdown of both directions ends that read
        with an end of file on every supported platform.
        """
        with self._registry_lock:
            connections = tuple(self._connections)
            self._connections.clear()
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                connection.close()
        return len(connections)

    def await_handlers(self, timeout: float) -> bool:
        """Join the serving threads within one bounded window; True when none is left."""
        deadline = monotonic() + timeout
        with self._registry_lock:
            handlers = tuple(self._handlers)
        current = threading.current_thread()
        for handler in handlers:
            if handler is current:
                # A handler that is stopping the publisher cannot wait for itself; joining the
                # current thread raises, and raising here would leave the publisher half shut.
                continue
            remaining = deadline - monotonic()
            if remaining <= 0.0:
                break
            handler.join(timeout=remaining)
        with self._registry_lock:
            return not any(
                handler.is_alive() and handler is not current for handler in self._handlers
            )

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Accept connections until the stopping flag is set, and end on that flag alone.

        The loop is written out here rather than delegated because the standard library gives no
        way to ask it to stop without also waiting for it, and a stop that originates on this
        very thread cannot wait for itself. An earlier version closed the listening socket and
        relied on the select that followed to fail. That is a Windows behaviour: measured, the
        loop ended there and did not end on POSIX at all, where closing a descriptor another
        thread is selecting on wakes nothing and the thread was still alive half a second later.

        Ending on a flag this class owns is the same statement on both families, needs no
        cooperation from the socket layer, and bounds ``shutdown()`` as a side effect: the wait
        below is on an event this loop sets, not on the standard library's own.
        """
        self._loop_exited.clear()
        try:
            with _selector_over(self) as selector:
                while True:
                    # One place decides, and it decides after the wait rather than before it.
                    # Two checks around the same wait is the masked-sibling shape again: the
                    # loop condition looked load bearing and was not, because this one answered
                    # first and a mutation of the other changed nothing anybody could see. The
                    # cost of the single check is one poll interval of latency on entry.
                    ready = selector.select(poll_interval)
                    if self._stopping:
                        break
                    if ready:
                        self._handle_request_noblock()
                    self.service_actions()
        except Exception:
            if not self._stopping:
                self.handle_error(None, None)
        finally:
            self._loop_exited.set()

    def shutdown(self, timeout: float | None = None) -> None:
        """Ask the accept loop to stop and wait for it, within a bound.

        ``BaseServer.shutdown`` waits forever, which was the one wait in the teardown that the
        publisher's shutdown budget did not cover -- measured at 3.68 s against a 0.5 s setting.
        This one waits on the event the loop above sets, so the bound is real.
        """
        self._stopping = True
        self._loop_exited.wait(timeout=_SHUTDOWN_WAIT_SECONDS if timeout is None else timeout)

    def handle_error(self, request: object, client_address: object) -> None:
        """Absorb a failed request instead of printing a traceback to the host standard error.

        A scraper that gives up on its own timeout tears the connection down mid-response, which
        is normal operation on this endpoint and not an error at all; the default handler of the
        standard library would print a full traceback for each one. So does a connection this
        server closed itself while stopping. A genuine internal fault is still worth telling
        somebody about, so it goes to the event sink, which is the channel the host chose,
        rather than to a stream the host never opted into.
        """
        failure = sys.exc_info()[1]
        if failure is None or self._stopping or isinstance(failure, _TEARDOWN_ERRORS):
            return
        self.report("metrics.publisher_request_failed", type(failure).__name__)

    def report(self, event: str, error: str) -> None:
        """Tell the event sink about a fault, and never let the telling become one."""
        events = self.events
        if events is None:
            return
        try:
            events.emit(event, {"error": error})
        except Exception:
            return


_BLANK_LINE: bytes = b"\r\n\r\n"
"""End of a request head in its canonical form."""

_BARE_BLANK_LINE: bytes = b"\n\n"
"""End of a request head as a lenient client writes it."""

_LINE_FEED: bytes = b"\n"
"""End of one line of a request head."""

_CARRIAGE_RETURN: bytes = b"\r"
"""Trailing byte of a canonical line ending."""


def _head_is_complete(buffer: bytes) -> bool:
    """Return True when the buffer already holds a whole request head.

    A head ends at the first blank line. A request line with fewer than three words is the
    HTTP/0.9 form, which carries no headers at all, so its first line is the whole head.
    """
    if _BLANK_LINE in buffer or _BARE_BLANK_LINE in buffer:
        return True
    end_of_line = buffer.find(_LINE_FEED)
    if end_of_line == -1:
        return False
    return len(buffer[:end_of_line].rstrip(_CARRIAGE_RETURN).split()) < 3


class _MetricsRequestHandler(BaseHTTPRequestHandler):
    """Answers ``GET /metrics`` and refuses everything else, in plain text and under a deadline."""

    protocol_version = "HTTP/1.1"
    default_request_version = "HTTP/1.0"
    server_version = "OktoGrafxMetrics/1.0"
    sys_version = ""
    timeout = DEFAULT_CONNECTION_TIMEOUT_SECONDS

    def setup(self) -> None:
        """Apply the read deadline of this server before the socket is wrapped for reading."""
        self.timeout = getattr(
            self.server, "connection_timeout", DEFAULT_CONNECTION_TIMEOUT_SECONDS
        )
        super().setup()
        self._socket_rfile = self.rfile

    def finish(self) -> None:
        """Close the socket reader as well, even when the request was served from memory."""
        try:
            super().finish()
        finally:
            original = getattr(self, "_socket_rfile", None)
            if original is not None and original is not self.rfile:
                with suppress(Exception):
                    original.close()

    def handle_one_request(self) -> None:
        """Read one whole request head under an interruptible deadline, then answer it once.

        The read happens here, byte by byte, rather than inside the blocking ``readline`` of the
        standard library, because that read cannot be interrupted portably: shutting down or
        closing the socket from another thread wakes the reader on POSIX and does not on Windows.
        A client that sends its request line and then pauses before the blank line would park a
        serving thread that a stopping publisher has no way to reach.

        Polling for readability instead makes one loop serve every deadline on both families:
        the client that says nothing, the client that stops halfway, and the publisher that is
        shutting down. The head is then handed to the standard parser as an in-memory stream, so
        the parsing itself is the code that ships with Python and not a rewrite of it.
        """
        head = self._read_head()
        if head is None or getattr(self.server, "stopping", False):
            # The state can change while the head is being read: a request whose last bytes
            # arrived during shutdown is not served, which is what stopping is supposed to mean.
            self.close_connection = True
            return
        self.rfile = BufferedReader(BytesIO(head))
        super().handle_one_request()

    def _read_head(self) -> bytes | None:
        """Return the bytes of one request head, or None when no request will arrive.

        The deadline is an idle deadline: it is reset whenever the client makes progress, so a
        slow client that keeps sending is served while a silent one is dropped.
        """
        idle_limit = max(float(self.timeout or 0.0), 0.0)
        deadline = monotonic() + idle_limit
        buffer = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.connection, selectors.EVENT_READ)
                while True:
                    if getattr(self.server, "stopping", False):
                        return None
                    if not selector.select(timeout=_POLL_INTERVAL_SECONDS):
                        if monotonic() >= deadline:
                            return None
                        continue
                    try:
                        chunk = self.connection.recv(_READ_CHUNK_BYTES)
                    except TimeoutError:
                        continue
                    if not chunk:
                        return bytes(buffer) if buffer else None
                    buffer += chunk
                    deadline = monotonic() + idle_limit
                    if _head_is_complete(buffer) or len(buffer) >= _MAX_HEAD_BYTES:
                        return bytes(buffer)
        except (OSError, ValueError):
            # The socket was closed under us, which is what stopping the publisher does.
            return None

    def version_string(self) -> str:
        """Name the product without disclosing the build it is running."""
        return "okto-grafx"

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        """Answer a protocol-level refusal in plain text, like every other answer here."""
        self.close_connection = True
        reason = message or "Refused."
        self._send(
            code,
            f"{reason}\n".encode("utf-8"),
            _PLAIN_TEXT,
            with_body=not self._head_requested(),
        )

    def _request_method(self) -> str:
        """Return the method exactly as the standard parser derives it.

        One derivation, one field, one split rule (A66). ``parse_request`` identifies the
        method with ``requestline.split()``, which separates on any run of whitespace and
        compares case sensitively, and it dispatches on that spelling alone. Anything here
        that tokenised differently would disagree with the parser about what the request
        even was: a tab-separated HEAD is served as a HEAD and must be answered as one.

        The raw request line is the field this reads, because it is the only one the parser
        never clears. ``command`` is set to None before the version check and to the empty
        string when the line is too long, which is precisely the set of paths that reach
        ``send_error``, and an earlier version of this method consulted it first and was
        wrong on every one of them.
        """
        raw = getattr(self, "raw_requestline", b"")
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            return ""
        words = bytes(raw).decode("iso-8859-1").split()
        return words[0] if words else ""

    def _head_requested(self) -> bool:
        """Return True when the client asked for headers only.

        RFC 9110 section 9.3.2: a response to HEAD carries no body, whatever the status.
        """
        return self._request_method() == "HEAD"

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
        """Route one read request to the metrics body, a 503, a 404 or a 400."""
        if self.request_version == "HTTP/1.1" and self.headers.get("Host") is None:
            self._send(400, _MISSING_HOST_BODY, _PLAIN_TEXT, with_body=with_body)
            return
        if self.path.split("?", 1)[0] != _METRICS_PATH:
            self._send(404, _NOT_FOUND_BODY, _PLAIN_TEXT, with_body=with_body)
            return
        sink = getattr(self.server, "sink", None)
        renderer = getattr(sink, "render", None)
        try:
            unavailable = sink is None or renderer is None or not sink.enabled
        except Exception as failure:
            self._report_render_failure(failure)
            self._send(503, _RENDER_FAILED_BODY, _PLAIN_TEXT, with_body=with_body)
            return
        if unavailable:
            self._send(503, _UNAVAILABLE_BODY, _PLAIN_TEXT, with_body=with_body)
            return
        try:
            body = renderer().encode("utf-8")
        except Exception as failure:
            # A sink that cannot be rendered is not the same condition as a publisher without
            # one, and an operator has to be able to tell them apart: the scraper gets its own
            # reason and the fault reaches the event sink the host configured.
            self._report_render_failure(failure)
            self._send(503, _RENDER_FAILED_BODY, _PLAIN_TEXT, with_body=with_body)
            return
        self._send(200, body, CONTENT_TYPE, with_body=with_body)

    def _report_render_failure(self, failure: BaseException) -> None:
        """Route a render fault to the event sink of the server, if it has one."""
        reporter = getattr(self.server, "report", None)
        if reporter is None:
            return
        with suppress(Exception):
            reporter("metrics.render_failed", type(failure).__name__)

    def _send(self, status: int, body: bytes, content_type: str, *, with_body: bool) -> None:
        """Write one complete response, always with an explicit length.

        A client that closed the connection before reading is the ordinary outcome of a scrape
        timeout, so the write is allowed to fail: the connection is marked closed and the
        response is abandoned without a word. Nothing here reaches the standard error of the
        host, and nothing that is not a Grafx error escapes towards its caller.
        """
        try:
            if getattr(self, "request_version", "") == "HTTP/0.9":
                # The standard library discards the status line and every header when the
                # request names HTTP/0.9, while still writing the body, so the answer arrives
                # unframed: no status, no content type, no length. ``default_request_version``
                # already keeps a version-less request line framed; this is its sibling, the
                # explicit token, and the API contract of SPEC-M1 freezes a framed answer for
                # both. The response is HTTP/1.1 either way, which is what protocol_version says.
                self.request_version = "HTTP/1.0"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if with_body:
                self.wfile.write(body)
        except _TEARDOWN_ERRORS:
            self.close_connection = True


def _checked_host(host: str) -> str:
    """Return the interface to bind, or refuse one that cannot mean what the caller meant."""
    if not isinstance(host, str) or not host:
        raise GrafxConfigurationError(
            f"The metrics publisher needs a non-empty host; got {host!r}. The empty string "
            f"binds every interface, which this endpoint never does implicitly.",
            field="host",
            value=repr(host),
        )
    return host


def _checked_port(port: int) -> int:
    """Return the port to bind, or refuse a value the socket layer could only fail on."""
    if isinstance(port, bool) or not isinstance(port, int):
        raise GrafxConfigurationError(
            f"The metrics publisher needs an integer port; got {type(port).__name__}.",
            field="port",
            value=repr(port),
        )
    if not 0 <= port <= 65535:
        raise GrafxConfigurationError(
            f"The metrics publisher needs a port between 0 and 65535; got {port}.",
            field="port",
            value=port,
        )
    return port


def _checked_timeout(field: str, value: float) -> float:
    """Return a positive finite deadline, or refuse it while it is still a declaration.

    A deadline that is not a number, or is zero or negative, does not fail at construction by
    accident: it starts cleanly and then blacks out every scrape, which is the worst shape a
    defect can take. It belongs to the build, like every other defective declaration here.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrafxConfigurationError(
            f"The metrics publisher needs a numeric {field}; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    seconds = float(value)
    if not isfinite(seconds) or seconds <= 0.0:
        raise GrafxConfigurationError(
            f"The metrics publisher needs a positive finite {field}; got {value!r}.",
            field=field,
            value=repr(value),
        )
    return seconds


class OpenMetricsPublisher:
    """A loopback HTTP endpoint that exposes one sink at ``GET /metrics``.

    The publisher is started and stopped by the host application; nothing starts a thread on
    import, and no thread, socket or answered request survives ``stop()``. The stopped server
    object itself is kept, so ``live_connections`` and ``live_handlers`` keep reporting what it
    owned rather than snapping to zero the moment a field is cleared. Bind to port 0 to let the
    operating system choose a free port and read it back from ``port`` after ``start()``.

    ``GET /metrics`` answers 200 with the exposition body, any other path answers 404, and a
    publisher holding no sink, or holding a sink that is not recording, answers 503 exactly as
    the frozen API contract of SPEC-M1 states.
    """

    __slots__ = (
        "_connection_timeout",
        "_events",
        "_host",
        "_lifecycle",
        "_requested_port",
        "_running",
        "_shutdown_timeout",
        "_sink",
        "_server",
        "_tearing_down",
        "_teardown_done",
        "_thread",
    )

    def __init__(
        self,
        sink: MetricsSink | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        events: EventSink | None = None,
        connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self._host = _checked_host(host)
        self._requested_port = _checked_port(port)
        self._sink = sink
        self._events = events
        self._connection_timeout = _checked_timeout("connection_timeout", connection_timeout)
        self._shutdown_timeout = _checked_timeout("shutdown_timeout", shutdown_timeout)
        self._lifecycle = threading.Lock()
        self._running = False
        self._tearing_down = False
        self._teardown_done = threading.Event()
        self._server: _MetricsServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def sink(self) -> MetricsSink | None:
        """Return the sink this publisher exposes, which may be None."""
        return self._sink

    @property
    def running(self) -> bool:
        """Return True until the matching ``stop()`` has actually finished.

        Not until it was asked for: a publisher whose socket is still bound has not stopped, and
        saying otherwise gave a host a window in which it was told it could rebind a port that
        was still taken.
        """
        return self._running

    @property
    def host(self) -> str:
        """Return the interface the publisher binds to."""
        return self._host

    @property
    def port(self) -> int:
        """Return the bound port, or the requested one while the publisher is stopped."""
        server = self._server
        if server is None or not self._running:
            return self._requested_port
        return int(server.server_address[1])

    @property
    def url(self) -> str:
        """Return the absolute URL of the metrics endpoint."""
        return f"http://{self._host}:{self.port}{_METRICS_PATH}"

    @property
    def live_connections(self) -> int:
        """Return how many accepted connections the publisher still owns.

        The server reference outlives ``stop()`` on purpose: a count that snapped to zero the
        moment the field was cleared would report success while forty threads were still
        running, which is a number nobody can act on and no test can fail on.
        """
        server = self._server
        return 0 if server is None else server.live_connections

    @property
    def live_handlers(self) -> int:
        """Return how many serving threads the publisher still owns, stopped or not."""
        server = self._server
        return 0 if server is None else server.live_handlers

    def start(self) -> int:
        """Bind the socket, serve in a daemon thread and return the bound port."""
        with self._lifecycle:
            if self._running:
                raise GrafxConfigurationError(
                    "The metrics publisher is already running; stop it before starting it again.",
                    field="state",
                    value="running",
                )
            address = f"{self._host}:{self._requested_port}"
            try:
                server = _MetricsServer(
                    (self._host, self._requested_port),
                    self._sink,
                    self._events,
                    connection_timeout=self._connection_timeout,
                )
            except OSError as failure:
                # The port and the host are already refused at construction, so binding is the
                # only step left that can fail and OSError is the only way it fails. Catching
                # more than that here was dead breadth: it named OverflowError and TypeError,
                # which _checked_port makes unreachable, and it did not cover the one non-OSError
                # that genuinely escapes this method, which is raised below and guarded there.
                raise GrafxConfigurationError(
                    f"The metrics publisher could not bind {address}.",
                    field="address",
                    value=address,
                ) from failure
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.01},
                name="oktografx-metrics",
                daemon=True,
            )
            try:
                thread.start()
            except Exception as failure:
                # A thread that cannot be created leaves a bound listener behind, and the fields
                # below are still unset, so nothing could ever have closed it. Release it here
                # and report the failure as the configuration error it is.
                server.server_close()
                raise GrafxConfigurationError(
                    f"The metrics publisher bound {address} but could not start serving it.",
                    field="state",
                    value="thread_start_failed",
                ) from failure
            # Only now is the publisher running: a half-built one must never be observable.
            self._server = server
            self._thread = thread
            self._running = True
            self._tearing_down = False
            self._teardown_done = threading.Event()
            return int(server.server_address[1])

    def stop(self) -> None:
        """Stop serving, close every live connection and release the socket.

        The lifecycle lock covers the decision and nothing else. Deciding that a stop is
        happening, and which caller owns it, is a state transition that cannot block; the
        teardown that follows can block for a long time and is done with the lock released.

        That distinction is the whole fix, and it took three rounds to find because the rule it
        breaks is one step further out than "do not call host code while holding a lock".
        ``_tear_down`` calls none. It calls ``shutdown()``, which waits for the accept loop, and
        the accept loop runs host code -- ``handle_error`` reports through the event sink there.
        A host that touches the lifecycle from that callback closed the cycle: the stopper held
        the lock and waited for the loop, the loop waited for the lock. What a lock may not span
        is not merely foreign code but anything that can be waiting on foreign code (L2).

        A second caller finds the decision already made. It waits for the owner to finish, unless
        it is a thread the owner may itself be waiting for -- the accept loop or a handler -- in
        which case it returns at once, because waiting there is the deadlock in the other
        direction. Stopping a stopped publisher does nothing.

        That check knows only about *this* publisher's threads, so it cannot see a cycle that
        runs through another one: two publishers whose host sinks stop each other close a ring no
        per-publisher guard can detect. The wait is therefore bounded, and the bound is what
        breaks such a ring -- measured resolving in exactly one ``shutdown_timeout``. It is load
        bearing rather than defensive. The teardown's own wait for the accept loop is bounded
        too: this server overrides ``shutdown()`` so it waits on an event the loop sets rather
        than on the standard library's, which waits without a limit.
        """
        with self._lifecycle:
            server = self._server
            thread = self._thread
            if server is None or not (self._running or self._tearing_down):
                return
            owner = not self._tearing_down
            if owner:
                self._tearing_down = True
                self._teardown_done = threading.Event()
                server.begin_stopping()
            done = self._teardown_done

        if not owner:
            if self._may_be_waited_for(server, thread):
                # The owner may be waiting for this very thread. Returning is the only move
                # that does not close a cycle; the stop it asked for is already under way.
                return
            done.wait(timeout=self._shutdown_timeout)
            return

        pending: tuple[str, str] | None = None
        try:
            pending = self._tear_down(server, thread)
        finally:
            with self._lifecycle:
                # P7: only now. Reporting "stopped" while the socket was still bound made the
                # window a caller could observe, and a host that believed it could rebind found
                # the port taken by a publisher that had already said it was finished.
                self._running = False
                self._tearing_down = False
                self._thread = None
            done.set()

        # A91: the one call that reaches host code happens with nothing held.
        if pending is not None:
            server.report(*pending)

    def _may_be_waited_for(
        self, server: _MetricsServer, thread: threading.Thread | None
    ) -> bool:
        """Return True when the current thread is one a teardown may block waiting for."""
        current = threading.current_thread()
        if thread is not None and current is thread:
            return True
        return server.is_serving_thread(current)

    def _tear_down(
        self, server: _MetricsServer, thread: threading.Thread | None
    ) -> tuple[str, str] | None:
        """Break the accept loop, close every connection, release the socket, join the threads.

        Returns what the caller should tell the event sink once it has let go of the lock, or
        None when there is nothing to report. Nothing in here calls host code.

        Every step is synchronous, including the one that runs on the accept loop itself. That
        thread cannot wait for the loop it is (shutdown() would block on it), so it closes the
        listener instead and lets serve_forever end on the next select. Handing the work to a
        helper thread would have been simpler and was wrong twice over: stop() returned while
        the port was still bound, and the spawn itself could fail for exactly the reason host
        code ends up on the accept loop in the first place.
        """
        on_accept_loop = thread is not None and threading.current_thread() is thread
        if not on_accept_loop:
            server.shutdown()
        server.close_live_connections()
        server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._shutdown_timeout)
        if not server.await_handlers(self._shutdown_timeout):
            # Never raise out of a shutdown path, but never stay quiet about a thread that
            # outlived the window it was given either.
            return ("metrics.publisher_stop_incomplete", "handlers_still_running")
        return None

    def __enter__(self) -> OpenMetricsPublisher:
        """Start the publisher and return it."""
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        """Stop the publisher and let any error propagate."""
        self.stop()
        return False
