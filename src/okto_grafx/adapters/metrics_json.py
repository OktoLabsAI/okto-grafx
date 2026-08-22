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

import contextlib
import json
import os
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager

from math import isfinite

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


def _number(value: float) -> float | str:
    """Return a number JSON can carry, or the text form the exposition already uses.

    Emission refuses a non-finite value, so one can only arrive by accumulation overflowing.
    ``json.dumps`` would then write the bare token ``Infinity``, which is not JSON and which a
    strict parser rejects outright, while the text adapter renders the same number as ``+Inf``
    and stays valid. Both adapters are built from one ``collect()`` and they should not differ
    in whether their output can be read back, so this borrows the exposition spelling.
    """
    if isfinite(value):
        return value
    return format_number(value)


# One mechanism, not two. An earlier version also passed allow_nan=False to json.dumps as a
# second line of defence, but with every number already passing through _number() that argument
# can never fire: reverting it left the suite green, which is the definition of untested. A
# guard that cannot be shown to do anything is removed rather than kept for reassurance.


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
                        "count": _number(sample.count),
                        "sum": _number(sample.total),
                        "buckets": [
                            {"le": boundary, "count": _number(cumulative)}
                            for boundary, cumulative in zip(boundaries, sample.buckets, strict=True)
                        ],
                    }
                )
            else:
                entries.append({"labels": labels, "value": _number(sample.value)})
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


_ADOPTION_READ_LIMIT: int = 4096
"""How much of an existing destination is read to decide whether this writer wrote it."""

_FORMAT_TAG: str = '"format": "' + DOCUMENT_FORMAT + '"'
"""The tag this writer emits first in every document, and the only thing it recognises by."""


def _is_our_document(prefix: str) -> bool:
    """Return True when the start of a file is a document this class wrote.

    A closed-world test: the format tag is one we define and emit first, so recognising it needs
    no guess about what any other file might contain. The inverse test -- deciding whether a
    file belongs to somebody else -- has no finite answer and is not attempted.

    Only the beginning is read, because a document of the full catalogue is far longer than any
    prefix worth reading, so parsing is not an option and the tag is matched where it is written.
    Both the compact and the indented spellings put it there.
    """
    text = prefix.lstrip()
    if not text.startswith("{"):
        return False
    return _FORMAT_TAG in text


class RotatingFileWriter:
    """Append JSON documents to a file, one document per line, rotating on size.

    Rotation is the plain scheme an operator can reason about without a manual: when the file
    would grow past ``max_bytes`` it becomes ``<path>.1``, the previous ``<path>.1`` becomes
    ``<path>.2`` and so on up to ``backups``, and the oldest one is dropped.

    The destination is host-supplied, so "nothing here touches a database file" cannot be
    asserted -- it has to be made true. Pointed at ``heap.dat`` an earlier version of this class
    appended JSON to it and then renamed it to ``heap.dat.1``, and with ``backups=0`` removed it
    outright: three of the operations G6 says no sanctioned path performs.

    So this writer only ever writes, rotates or removes a file it recognises as its own. A path
    that does not exist is created here and is ours from then on. A path that does exist is
    adopted only if its first line is a document this class wrote, which is a closed-world test
    of a format we define rather than a guess about what somebody else's file might be -- there
    is no attempt to detect a database, because the set of things that are not ours is not
    enumerable. Anything else is refused before a single byte is written. The identity of the
    file is captured when it is opened and re-checked before it is renamed or unlinked, so a
    path swapped underneath the writer is not followed.
        The message of any failure here is ours, in en-US. The text the platform supplies is
    localized -- on a non-English system ``strerror`` is neither en-US nor ASCII -- so it travels
    in ``details["platform_message"]`` beside the numeric codes, where a human can still read it
    and no gate has to pretend a runtime string is ASCII (G1, A7). The source gate cannot catch
    this on its own: the literal is ASCII in the file and only becomes localized when the
    operating system fills it in, so the test that holds it forces a non-ASCII ``strerror``.

"""

    __slots__ = ("_path", "_max_bytes", "_backups", "_encoding", "_identity", "_handle")

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
        self._identity: tuple[int, int] | None = None
        self._handle: int | None = None

    @property
    def path(self) -> str:
        """Return the destination path of the current document."""
        return self._path

    def __call__(self, document: str) -> None:
        """Append one document to a file of ours, and rotate it when it outgrows the bound."""
        payload = document if document.endswith("\n") else document + "\n"
        handle = self._claim()
        try:
            self._write_everything(handle, payload.encode(self._encoding))
            if os.fstat(handle).st_size >= self._max_bytes:
                self._rotate()
        except OSError as failure:
            raise GrafxConfigurationError(
                "The metrics writer could not write to its destination.",
                field="path",
                value=self._path,
                errno=failure.errno,
                winerror=getattr(failure, "winerror", None),
                platform_message=failure.strerror,
            ) from failure

    def _write_everything(self, handle: int, payload: bytes) -> None:
        """Store every byte of one document, or refuse. A short write is not a written document.

        ``os.write`` is permitted to store fewer bytes than it was handed, and it reports that by
        its RETURN VALUE rather than by raising -- a device that fills up mid-document is the
        ordinary way it happens, which is the condition ``GrafxDeviceFull`` exists for. Ignoring
        the count appended half a JSON document, returned normally, and let ``publish()`` hand the
        caller the whole document as though it had been stored; the next document was then
        appended straight behind the fragment, so the file gained a line that is two half
        documents. That is corruption of the one file this adapter exists to produce, and it is
        silent, which is worse than the failure it hides. Measured before the fix: 8 of 62 bytes
        stored, no error raised.

        Looping is also what makes the ordinary full-device path visible at all. The first short
        write reports its count and the next one raises ``ENOSPC``, which the caller above turns
        into a Grafx refusal; without the loop that second call never happens.

        The zero-progress refusal is the loop's bound, not decoration: a device that accepts
        nothing and raises nothing would otherwise spin here for ever, and a hang is the one
        outcome worse than a refusal. C2's ``storage_local`` writes its pages under the same rule;
        this is that rule at this adapter's only device write.
        """
        view = memoryview(payload)
        stored = 0
        while stored < len(payload):
            written = os.write(handle, view[stored:])
            if written <= 0:
                raise GrafxConfigurationError(
                    f"The metrics writer stored {stored} of {len(payload)} bytes and the "
                    f"destination accepted no more; a partial document is not a document.",
                    field="path",
                    value=self._path,
                    stored=stored,
                    expected=len(payload),
                )
            stored += written

    def close(self) -> None:
        """Release the destination. The next write claims it again."""
        self._release()

    def _release(self) -> None:
        """Close the held descriptor, if there is one."""
        handle, self._handle, self._identity = self._handle, None, None
        if handle is not None:
            with contextlib.suppress(OSError):
                os.close(handle)

    def _claim(self) -> int:
        """Make the destination ours, hold it open and return its descriptor, or refuse it.

        The descriptor is RETURNED rather than left for the write path to read back off the
        instance. That path used to narrow the field with ``assert self._handle is not None``,
        which is not a guard in a shipped adapter: ``python -O`` deletes it, and what followed
        would have been a ``TypeError`` out of ``os.write`` -- not a ``Grafx*`` error, out of a
        public door. Returning the descriptor removes the question instead of answering it.

        The descriptor is kept, not reopened per write, because it is the only identity the two
        families agree on. A stat comparison is not one: unlinking a file and creating another at
        the same path reuses the inode on POSIX, and every timestamp with it -- measured, a
        swapped file matched on st_dev, st_ino, st_ctime_ns and st_mtime_ns together, while an
        ordinary append changed two of them. Holding the descriptor pins the inode so the reuse
        cannot happen, which turns the comparison back into an identity; on Windows it goes
        further and stops the swap outright, because the file cannot be unlinked while it is open.

        Holding it also closes the window between deciding the destination is ours and writing to
        it: the write goes to the descriptor that was checked, not to whatever the path resolves
        to afterwards.
        """
        if self._handle is not None and self._still_ours():
            return self._handle
        self._release()
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
        try:
            handle = os.open(self._path, flags)
        except FileExistsError:
            return self._adopt()
        except OSError as failure:
            raise GrafxConfigurationError(
                "The metrics writer could not create its destination.",
                field="path",
                value=self._path,
                errno=failure.errno,
                winerror=getattr(failure, "winerror", None),
                platform_message=failure.strerror,
            ) from failure
        self._handle = handle
        self._identity = self._identity_of_handle()
        return handle

    def _adopt(self) -> int:
        """Adopt an existing destination only if this class wrote its first line."""
        try:
            with open(self._path, "r", encoding=self._encoding, errors="replace") as handle:
                first = handle.read(_ADOPTION_READ_LIMIT)
        except OSError as failure:
            raise GrafxConfigurationError(
                "The metrics writer could not read its destination.",
                field="path",
                value=self._path,
                errno=failure.errno,
                winerror=getattr(failure, "winerror", None),
                platform_message=failure.strerror,
            ) from failure
        if first.strip() and not _is_our_document(first):
            raise GrafxConfigurationError(
                f"The metrics writer refuses {self._path!r}: the file exists and was not "
                f"written by this writer. It only ever writes, rotates or removes files of its "
                f"own, so a destination that already holds something else is never adopted.",
                field="path",
                value=self._path,
            )
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
        try:
            handle = os.open(self._path, flags)
        except OSError as failure:
            raise GrafxConfigurationError(
                "The metrics writer could not open its destination.",
                field="path",
                value=self._path,
                errno=failure.errno,
                winerror=getattr(failure, "winerror", None),
                platform_message=failure.strerror,
            ) from failure
        self._handle = handle
        self._identity = self._identity_of_handle()
        return handle

    def _identity_of_handle(self) -> tuple[int, int] | None:
        """Return the (device, inode) of the file the held descriptor refers to."""
        if self._handle is None:
            return None
        try:
            status = os.fstat(self._handle)
        except OSError:
            return None
        return (status.st_dev, status.st_ino)

    def _current_identity(self) -> tuple[int, int] | None:
        """Return the (device, inode) the destination path resolves to right now."""
        try:
            status = os.stat(self._path)
        except OSError:
            return None
        return (status.st_dev, status.st_ino)

    def _still_ours(self) -> bool:
        """Return True when the path still resolves to the file this writer is holding."""
        if self._handle is None or self._identity is None:
            return False
        return self._current_identity() == self._identity_of_handle() == self._identity

    def _rotate(self) -> None:
        """Shift the backups by one and move the current file into the first backup slot."""
        if not self._still_ours():
            raise GrafxConfigurationError(
                f"The metrics writer will not rotate {self._path!r}: it is not the file this "
                f"writer created. A file it did not make is not its to rename or unlink.",
                field="path",
                value=self._path,
            )
        self._release()
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
