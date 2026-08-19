"""The buffer pool: one page cache per database, with a budget that belongs to that database.

CONTRACT.md section 8.1 and SPEC-M1 FR-13 and BR-8 say the same thing from two sides: the budget
is per Database instance, and memory pressure inside one database is invisible to every other
one. That is why there is no module-level cache here, no class attribute holding pages and no
shared registry of any kind. Two pools built in the same process share exactly nothing, and the
test that proves it is part of the definition of done.

The pool is also where the torn-read protocol of CONTRACT.md section 6.3 lives. A reader never
blocks a writer, so a page can be read while it is being written: the sequence counter is odd, or
the checksum does not match, and the answer is to read again, up to a bounded number of times and
without ever sleeping inside the engine. When the budget of re-reads is exhausted the bytes are
declared corrupt and the location is named.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

from okto_grafx.domain.errors import (
    GrafxBufferBudgetExceeded,
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import NO_PAGE, PageIndex
from okto_grafx.domain.page import (
    Page,
    PageType,
    chunk_capacity,
    join_chunks,
    split_payload,
    validate_page_size,
)
from okto_grafx.domain.ports.codec import PageCodec
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.engine.metrics_catalog import metric

__all__ = [
    "TORN_READ_RETRY_BUDGET",
    "MAX_DB_LABEL_LENGTH",
    "BUFFER_POOL_METRICS",
    "BUFFER_BUDGET_USED_BYTES",
    "BUFFER_BUDGET_EXCEEDED_TOTAL",
    "CHECKSUM_VERIFICATIONS_TOTAL",
    "CHECKSUM_FAILURES_TOTAL",
    "BufferPool",
    "write_chain",
    "read_chain",
]

TORN_READ_RETRY_BUDGET: int = 8
"""Re-reads a page gets after a torn or unreadable image before it is declared corrupt."""

MAX_DB_LABEL_LENGTH: int = 64
"""Characters of the db metric label, which carries a short name or hash and never a path."""

BUFFER_BUDGET_USED_BYTES: str = "oktografx_buffer_budget_used_bytes"
BUFFER_BUDGET_EXCEEDED_TOTAL: str = "oktografx_buffer_budget_exceeded_total"
CHECKSUM_VERIFICATIONS_TOTAL: str = "oktografx_checksum_verifications_total"
CHECKSUM_FAILURES_TOTAL: str = "oktografx_checksum_failures_total"

BUFFER_POOL_METRICS: tuple[MetricDescriptor, ...] = tuple(
    metric(name)
    for name in (
        BUFFER_BUDGET_USED_BYTES,
        BUFFER_BUDGET_EXCEEDED_TOTAL,
        CHECKSUM_VERIFICATIONS_TOTAL,
        CHECKSUM_FAILURES_TOTAL,
    )
)
"""The descriptors the pool registers and emits, taken from the frozen catalog rather than
declared again. A metric is a contract (G7), so there is exactly one declaration of each name in
the engine; looking them up at import time also means a name that leaves the catalog breaks the
import of this module instead of a scrape in production."""


class _Frame:
    """One resident page together with how many callers currently hold it pinned."""

    __slots__ = ("page", "pins")

    def __init__(self, page: Page) -> None:
        self.page: Page = page
        self.pins: int = 0


class BufferPool:
    """The page cache of one database, bounded by a budget that only that database spends."""

    __slots__ = (
        "_storage",
        "_codec",
        "_metrics",
        "_budget_bytes",
        "_db_label",
        "_page_size",
        "_frames",
        "_labels",
        "_page_labels",
    )

    def __init__(
        self,
        storage: StorageDevice,
        codec: PageCodec,
        metrics: MetricsSink,
        *,
        budget_bytes: int,
        db_label: str,
    ) -> None:
        """Build a pool over one device, with its own budget and its own metric label."""
        self._storage: StorageDevice = storage
        self._codec: PageCodec = codec
        self._metrics: MetricsSink = metrics
        self._page_size: int = validate_page_size(storage.page_size)
        self._budget_bytes: int = _validate_budget(budget_bytes, self._page_size)
        self._db_label: str = _validate_db_label(db_label)
        self._frames: OrderedDict[tuple[str, PageIndex], _Frame] = OrderedDict()
        # Both label mappings belong to this instance. Nothing in this module holds data
        # that two databases could share, which is the structural half of FR-13 and BR-8.
        self._labels: dict[str, str] = {"db": self._db_label}
        self._page_labels: dict[str, str] = {"kind": "page"}
        if metrics.enabled:
            for descriptor in BUFFER_POOL_METRICS:
                metrics.register(descriptor)
            metrics.set_gauge(BUFFER_BUDGET_USED_BYTES, 0.0, self._labels)

    # --- identity --------------------------------------------------------------------------

    @property
    def storage(self) -> StorageDevice:
        """Return the device this pool caches, for callers that must create or size a file."""
        return self._storage

    @property
    def codec(self) -> PageCodec:
        """Return the codec this pool encodes and decodes page images with."""
        return self._codec

    @property
    def page_size(self) -> int:
        """Return the page size of every frame in this pool."""
        return self._page_size

    @property
    def budget_bytes(self) -> int:
        """Return the resident bytes this database is allowed to hold."""
        return self._budget_bytes

    @property
    def db_label(self) -> str:
        """Return the short, bounded label this pool reports its metrics under."""
        return self._db_label

    @property
    def capacity_pages(self) -> int:
        """Return how many frames the budget can hold at once."""
        return self._budget_bytes // self._page_size

    # --- residency -------------------------------------------------------------------------

    def used_bytes(self) -> int:
        """Return the bytes currently resident in this pool."""
        return len(self._frames) * self._page_size

    def is_resident(self, file: str, page_index: PageIndex) -> bool:
        """Return True when the page is currently cached by this pool."""
        return (file, page_index) in self._frames

    def pin_count(self, file: str, page_index: PageIndex) -> int:
        """Return how many callers hold the page pinned; zero when it is not resident."""
        frame = self._frames.get((file, page_index))
        return 0 if frame is None else frame.pins

    # --- the four operations -----------------------------------------------------------------

    def pin(self, file: str, page_index: PageIndex) -> Page:
        """Return the page, reading it from the device when it is not resident, and pin it.

        A pinned page is never evicted, so the caller must unpin it. Everything that can fail
        fails before the pin count moves.
        """
        key = (file, page_index)
        frame = self._frames.get(key)
        if frame is not None:
            self._frames.move_to_end(key)
            frame.pins += 1
            return frame.page
        self._make_room(file, page_index)
        page = self._read_page(file, page_index)
        frame = _Frame(page)
        frame.pins = 1
        self._frames[key] = frame
        self._report_usage()
        return page

    def unpin(self, file: str, page_index: PageIndex, *, dirty: bool = False) -> None:
        """Release one pin on the page, marking it dirty when the caller changed it.

        A page also remembers on its own that it was mutated, so a caller that forgets the flag
        still cannot lose a write; passing it is how a caller says so explicitly.
        """
        key = (file, page_index)
        frame = self._frames.get(key)
        if frame is None:
            raise GrafxUnsupportedOperation(
                f"Page {page_index} of {file!r} is not resident, so it cannot be unpinned.",
                file=file,
                page=page_index,
            )
        if frame.pins <= 0:
            raise GrafxUnsupportedOperation(
                f"Page {page_index} of {file!r} is not pinned, so it cannot be unpinned.",
                file=file,
                page=page_index,
            )
        if dirty:
            frame.page.dirty = True
        frame.pins -= 1

    def pinned(self, file: str, page_index: PageIndex) -> AbstractContextManager[Page]:
        """Return a context manager that pins the page and always unpins it again."""
        return self._pinned(file, page_index)

    @contextmanager
    def _pinned(self, file: str, page_index: PageIndex) -> Iterator[Page]:
        page = self.pin(file, page_index)
        try:
            yield page
        finally:
            # The page carries its own dirty flag, so an exception in the body cannot lose a
            # change that had already been applied to it.
            self.unpin(file, page_index, dirty=page.dirty)

    def allocate(self, file: str, page_type: int) -> Page:
        """Grow the file by one page and return it pinned, empty and of the requested type.

        The page comes back pinned on purpose: an unpinned fresh page could be evicted before
        the caller had written anything into it, and the caller would then be holding a page the
        pool has already forgotten.
        """
        page_index = self._storage.allocate(file, 1)
        key = (file, page_index)
        existing = self._frames.get(key)
        if existing is not None:
            if existing.pins:
                raise GrafxCorruptionDetected(
                    f"The device handed out page {page_index} of {file!r}, which this pool "
                    f"still holds pinned.",
                    file=file,
                    page=page_index,
                )
            del self._frames[key]
        self._make_room(file, page_index)
        page = Page(page_type, page_size=self._page_size, page_index=page_index)
        page.dirty = True
        frame = _Frame(page)
        frame.pins = 1
        self._frames[key] = frame
        self._report_usage()
        return page

    def flush(self, file: str | None = None) -> int:
        """Write every dirty page of the file, or of the whole pool, and return how many."""
        written = 0
        for (name, page_index), frame in list(self._frames.items()):
            if file is not None and name != file:
                continue
            if not frame.page.dirty:
                continue
            self._write_back(name, page_index, frame.page)
            written += 1
        return written

    def invalidate(self, file: str | None = None) -> None:
        """Drop the cached pages of the file, or of the whole pool, forcing a re-read.

        Dirty pages are written before they are dropped: invalidating is about forgetting what
        was read, never about discarding what was written. A pinned page cannot be dropped at
        all, because its holder still has the object in its hands, and the refusal comes before
        anything is dropped, so a pool that refuses to invalidate is left exactly as it was.
        """
        targets = [
            (key, frame)
            for key, frame in self._frames.items()
            if file is None or key[0] == file
        ]
        for (name, page_index), frame in targets:
            if frame.pins:
                raise GrafxUnsupportedOperation(
                    f"Page {page_index} of {name!r} is pinned {frame.pins} times and cannot be "
                    f"invalidated.",
                    file=name,
                    page=page_index,
                    pins=frame.pins,
                )
        for (name, page_index), frame in targets:
            if frame.page.dirty:
                self._write_back(name, page_index, frame.page)
            del self._frames[(name, page_index)]
        self._report_usage()

    # --- internals ---------------------------------------------------------------------------

    def _make_room(self, file: str, page_index: PageIndex) -> None:
        """Evict until one more frame fits in the budget, or refuse the request."""
        while (len(self._frames) + 1) * self._page_size > self._budget_bytes:
            victim = self._find_victim()
            if victim is None:
                if self._metrics.enabled:
                    self._metrics.increment(BUFFER_BUDGET_EXCEEDED_TOTAL, 1.0, self._labels)
                raise GrafxBufferBudgetExceeded(
                    f"Database {self._db_label!r} holds {len(self._frames)} pinned pages of "
                    f"{self._page_size} bytes and cannot admit page {page_index} of {file!r} "
                    f"within its budget of {self._budget_bytes} bytes.",
                    db=self._db_label,
                    file=file,
                    page=page_index,
                    budget_bytes=self._budget_bytes,
                    used_bytes=self.used_bytes(),
                )
            name, victim_index = victim
            frame = self._frames[victim]
            if frame.page.dirty:
                self._write_back(name, victim_index, frame.page)
            del self._frames[victim]

    def _find_victim(self) -> tuple[str, PageIndex] | None:
        """Return the least recently used unpinned frame, or None when everything is pinned."""
        for key, frame in self._frames.items():
            if frame.pins == 0:
                return key
        return None

    def _read_page(self, file: str, page_index: PageIndex) -> Page:
        """Read one page, applying the torn-read protocol of CONTRACT.md section 6.3."""
        failure: GrafxCorruptionDetected | None = None
        for attempt in range(TORN_READ_RETRY_BUDGET + 1):
            raw = self._storage.read_page(file, page_index)
            if self._metrics.enabled:
                self._metrics.increment(CHECKSUM_VERIFICATIONS_TOTAL, 1.0, self._page_labels)
            try:
                page = self._codec.decode_page(raw, verify=True)
            except GrafxCorruptionDetected as detected:
                failure = detected
                if self._metrics.enabled:
                    self._metrics.increment(CHECKSUM_FAILURES_TOTAL, 1.0, self._page_labels)
                continue
            if page.seq % 2 == 1:
                # An odd sequence counter says a writer is in the middle of this page. There is
                # no sleep in the engine: the answer is to look again.
                failure = GrafxCorruptionDetected(
                    f"Page {page_index} of {file!r} was read while it was being written "
                    f"(sequence {page.seq}).",
                    file=file,
                    page=page_index,
                    seq=page.seq,
                    attempt=attempt,
                )
                continue
            page.page_index = page_index
            return page
        raise GrafxCorruptionDetected(
            f"Page {page_index} of {file!r} could not be read after "
            f"{TORN_READ_RETRY_BUDGET + 1} attempts.",
            file=file,
            page=page_index,
            attempts=TORN_READ_RETRY_BUDGET + 1,
            cause=None if failure is None else failure.message,
        )

    def _write_back(self, file: str, page_index: PageIndex, page: Page) -> None:
        """Encode the page and hand it to the device, advancing its sequence counter.

        The counter moves by two, so an image that reaches the device always carries an even
        value: a reader that sees an odd one is looking at a write that did not complete.
        """
        page.page_index = page_index
        page.seq = (page.seq + 2) & 0xFFFFFFFF
        image = self._codec.encode_page(page)
        self._storage.write_page(file, page_index, image)
        page.dirty = False

    def _report_usage(self) -> None:
        """Publish the resident bytes of this database under its own label."""
        if self._metrics.enabled:
            self._metrics.set_gauge(
                BUFFER_BUDGET_USED_BYTES, float(self.used_bytes()), self._labels
            )

    def __repr__(self) -> str:
        return (
            f"BufferPool(db={self._db_label!r}, page_size={self._page_size}, "
            f"budget_bytes={self._budget_bytes}, resident={len(self._frames)})"
        )


def _validate_budget(budget_bytes: int, page_size: int) -> int:
    """Return the budget after checking that it can hold at least one page."""
    if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int):
        raise GrafxConfigurationError(
            f"A buffer budget must be an integer number of bytes; got "
            f"{type(budget_bytes).__name__}.",
            field="budget_bytes",
            value=repr(budget_bytes),
        )
    if budget_bytes < page_size:
        raise GrafxConfigurationError(
            f"A buffer budget must hold at least one page of {page_size} bytes; got "
            f"{budget_bytes}.",
            field="budget_bytes",
            value=budget_bytes,
        )
    return budget_bytes


def _validate_db_label(db_label: str) -> str:
    """Return the metric label after checking it is short and bounded (SPEC-M1 TR-7)."""
    if not isinstance(db_label, str) or not db_label:
        raise GrafxConfigurationError(
            f"A database metric label must be a non-empty string; got {db_label!r}.",
            field="db_label",
            value=repr(db_label),
        )
    if len(db_label) > MAX_DB_LABEL_LENGTH:
        raise GrafxConfigurationError(
            f"A database metric label may be at most {MAX_DB_LABEL_LENGTH} characters; got "
            f"{len(db_label)}.",
            field="db_label",
            value=len(db_label),
        )
    if not all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in db_label
    ):
        raise GrafxConfigurationError(
            f"A database metric label carries a short name or hash, never a path or free text; "
            f"got {db_label!r}.",
            field="db_label",
            value=db_label,
        )
    return db_label


def write_chain(
    pool: BufferPool,
    file: str,
    payload: bytes,
    *,
    page_type: int = int(PageType.OVERFLOW),
    reuse: tuple[PageIndex, ...] = (),
) -> tuple[PageIndex, ...]:
    """Write a payload across a chain of pages and return the pages it occupies, in order.

    Pages listed in reuse are overwritten before any new page is allocated, which is how the
    catalog rewrites itself in place instead of growing its file on every save. The chain always
    has at least one page, so an empty payload is still addressable.
    """
    chunks = split_payload(payload, chunk_capacity(pool.page_size))
    indices: list[PageIndex] = list(reuse[: len(chunks)])
    while len(indices) < len(chunks):
        page = pool.allocate(file, page_type)
        index = page.page_index
        pool.unpin(file, index, dirty=True)
        indices.append(index)
    for position, index in enumerate(indices):
        with pool.pinned(file, index) as page:
            page.clear()
            page.page_type = page_type
            page.next_page = indices[position + 1] if position + 1 < len(indices) else NO_PAGE
            page.insert_slot(chunks[position])
    return tuple(indices)


def read_chain(
    pool: BufferPool,
    file: str,
    first_page: PageIndex,
    *,
    page_type: int | None = None,
) -> bytes:
    """Follow a page chain from its first page and return the payload it carries.

    A chain that points back at a page it already visited is corruption, not an infinite loop:
    the walk keeps what it has seen and names the page that closed the cycle.
    """
    chunks: list[bytes] = []
    visited: set[PageIndex] = set()
    index = first_page
    while index != NO_PAGE:
        if index in visited:
            raise GrafxCorruptionDetected(
                f"The page chain of {file!r} returns to page {index}, so it is a cycle.",
                file=file,
                page=index,
                visited=len(visited),
            )
        visited.add(index)
        with pool.pinned(file, index) as page:
            if page_type is not None and page.page_type != page_type:
                raise GrafxCorruptionDetected(
                    f"Page {index} of {file!r} is of type {page.page_type}, but the chain "
                    f"expects type {page_type}.",
                    file=file,
                    page=index,
                    page_type=page.page_type,
                )
            if page.slot_count < 1:
                raise GrafxCorruptionDetected(
                    f"Page {index} of {file!r} is part of a chain but carries no chunk.",
                    file=file,
                    page=index,
                )
            chunks.append(page.read_slot(0))
            index = page.next_page
    return join_chunks(chunks)
