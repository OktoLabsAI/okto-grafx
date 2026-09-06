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
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, ContextDecorator, contextmanager, nullcontext
from dataclasses import dataclass
from functools import wraps
from itertools import islice
from struct import calcsize
from typing import Protocol, cast

from okto_grafx.domain.errors import (
    GrafxBufferBudgetExceeded,
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDurabilityBarrierFailed,
    GrafxError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import MAX_PAGE_INDEX, NO_PAGE, PageIndex
from okto_grafx.domain.page import (
    HEADER_PAGE_INDEX,
    Page,
    PageType,
    chunk_capacity,
    is_unwritten_image,
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
    "MAX_REDO_GAP_PAGES",
    "MAX_SEQ",
    "BUFFER_POOL_METRICS",
    "BUFFER_BUDGET_USED_BYTES",
    "BUFFER_RETAINED_ESTIMATE_BYTES",
    "BUFFER_RETAINED_ESTIMATOR_VERSION",
    "BUFFER_BUDGET_EXCEEDED_TOTAL",
    "CHECKSUM_VERIFICATIONS_TOTAL",
    "CHECKSUM_FAILURES_TOTAL",
    "FSYNC_DURATION_SECONDS",
    "BARRIER_FAILURES_TOTAL",
    "READ_VIEW_DROPS_TOTAL",
    "BufferPool",
    "next_seq",
    "write_chain",
    "build_chain_images",
    "read_chain",
    "refuse_endless_chain",
    "visited_pages",
    "grow_to",
    "apply_page_image",
]

TORN_READ_RETRY_BUDGET: int = 8
"""Re-reads a page gets after a torn or unreadable image before it is declared corrupt."""

MAX_DB_LABEL_LENGTH: int = 64
"""Characters of the db metric label, which carries a short name or hash and never a path."""

MAX_REDO_GAP_PAGES: int = 4096
"""Pages a single redo may bridge between what a file holds and what an image names.

A crash between allocating a page and writing it leaves a gap of the pages that one interrupted
operation had allocated, which is small. A larger gap says the index does not describe this file,
and honouring it would allocate zero-filled pages that G6 then forbids reclaiming.
"""

MAX_SEQ: int = 0xFFFFFFFF
"""The write sequence counter is a 32-bit header field and wraps inside it."""

_READ_VIEW_BASELINE_UNSET: object = object()
_READ_VIEW_MAX_TARGETS: int = 1024
_CLEAN_CANDIDATE_LINGER_PER_FILE: int = 32
"""Clean hot pages retained in the D-10 candidate index to avoid read-path set churn."""

_FRESH_PAGE_WITNESS_SEAL: object = object()


@dataclass(frozen=True, slots=True)
class _FreshPageWitness:
    """Pool-owned proof that one immutable raw image passed the full read protocol."""

    seal: object
    owner: object
    file: str
    page_index: PageIndex
    image: bytes

BUFFER_BUDGET_USED_BYTES: str = "oktografx_buffer_budget_used_bytes"
BUFFER_RETAINED_ESTIMATE_BYTES: str = "oktografx_buffer_retained_estimate_bytes"
BUFFER_RETAINED_ESTIMATOR_VERSION: str = "python-v2"
"""Version of the callback-free retained-memory estimator exposed by this pool.

The estimate covers the resident and retired-pinned frame graph plus the pool-owned residency,
dirty-page, abandoned-page, epoch, label and in-flight admission containers. It is not process RSS:
allocator arenas, storage/codec/metrics collaborators and arbitrary objects held by the read-view
token are outside its authority, as are temporary raw/decode values owned only by an executing
call stack. Versioning prevents a more accurate future formula from silently changing the meaning
of a time series. ``python-v2`` adds the bounded single-flight reservations and detached
dirty-eviction frames introduced after the original ``python-v1`` estimator.
"""
BUFFER_BUDGET_EXCEEDED_TOTAL: str = "oktografx_buffer_budget_exceeded_total"
CHECKSUM_VERIFICATIONS_TOTAL: str = "oktografx_checksum_verifications_total"
CHECKSUM_FAILURES_TOTAL: str = "oktografx_checksum_failures_total"
FSYNC_DURATION_SECONDS: str = "oktografx_fsync_duration_seconds"
BARRIER_FAILURES_TOTAL: str = "oktografx_barrier_failures_total"
READ_VIEW_DROPS_TOTAL: str = "oktografx_read_view_drops_total"

BUFFER_POOL_METRICS: tuple[MetricDescriptor, ...] = tuple(
    metric(name)
    for name in (
        BUFFER_BUDGET_USED_BYTES,
        BUFFER_RETAINED_ESTIMATE_BYTES,
        BUFFER_BUDGET_EXCEEDED_TOTAL,
        CHECKSUM_VERIFICATIONS_TOTAL,
        CHECKSUM_FAILURES_TOTAL,
        FSYNC_DURATION_SECONDS,
        BARRIER_FAILURES_TOTAL,
        READ_VIEW_DROPS_TOTAL,
    )
)
"""The descriptors the pool registers and emits, taken from the frozen catalog rather than
declared again. A metric is a contract (G7), so there is exactly one declaration of each name in
the engine; looking them up at import time also means a name that leaves the catalog breaks the
import of this module instead of a scrape in production."""


def _require_page_index(field: str, page_index: PageIndex) -> PageIndex:
    """Return the page index after checking it is an unsigned integer the format can address.

    A bool is refused with everything else: True would silently mean page 1, and a caller that
    passed a flag where an index belongs would grow a file and write a page it never meant to.
    """
    if isinstance(page_index, bool) or not isinstance(page_index, int):
        raise GrafxCorruptionDetected(
            f"A page index must be an integer; {field} is a {type(page_index).__name__}.",
            field=field,
            value=repr(page_index),
        )
    if not 0 <= page_index <= MAX_PAGE_INDEX:
        raise GrafxCorruptionDetected(
            f"A page index must be between 0 and {MAX_PAGE_INDEX}; {field} is {page_index}.",
            field=field,
            value=page_index,
        )
    return page_index


def _require_read_view_file(field: str, file: object) -> str:
    """Return one internal read-view file name, refusing an ambiguous proof target."""

    if not isinstance(file, str) or not file or "\x00" in file:
        raise GrafxConfigurationError(
            f"The {field} read-view target must be a non-empty file name; got {file!r}.",
            field=field,
            value=repr(file),
        )
    return file


def _read_view_targets(
    changed_pages: Iterable[tuple[str, PageIndex]],
    changed_files: Iterable[str],
) -> tuple[frozenset[tuple[str, PageIndex]], frozenset[str]]:
    """Materialise and validate a CE-3 proof before any resident state can move."""

    if isinstance(changed_pages, (str, bytes, bytearray, memoryview)):
        raise GrafxConfigurationError(
            "changed_pages must be an iterable of (file, page_index) pairs.",
            field="changed_pages",
            value=type(changed_pages).__name__,
        )
    if isinstance(changed_files, (str, bytes, bytearray, memoryview)):
        raise GrafxConfigurationError(
            "changed_files must be an iterable of complete file names, not one string.",
            field="changed_files",
            value=type(changed_files).__name__,
        )
    try:
        offered_pages = tuple(islice(iter(changed_pages), _READ_VIEW_MAX_TARGETS + 1))
        offered_files = tuple(islice(iter(changed_files), _READ_VIEW_MAX_TARGETS + 1))
    except TypeError as failure:
        raise GrafxConfigurationError(
            "Read-view change targets must be finite iterables.",
            field="read_view_changes",
        ) from failure
    if (
        len(offered_pages) > _READ_VIEW_MAX_TARGETS
        or len(offered_files) > _READ_VIEW_MAX_TARGETS
    ):
        raise GrafxConfigurationError(
            "A read-view proof exceeds the bounded target budget.",
            field="read_view_changes",
            limit=_READ_VIEW_MAX_TARGETS,
        )

    pages: set[tuple[str, PageIndex]] = set()
    for position, target in enumerate(offered_pages):
        if not isinstance(target, tuple) or len(target) != 2:
            raise GrafxConfigurationError(
                "Each changed page must be exactly a (file, page_index) tuple.",
                field="changed_pages",
                position=position,
                value=repr(target),
            )
        file = _require_read_view_file(f"changed_pages[{position}].file", target[0])
        page_index = _require_page_index(
            f"changed_pages[{position}].page_index", target[1]
        )
        pages.add((file, page_index))
    files = frozenset(
        _require_read_view_file(f"changed_files[{position}]", file)
        for position, file in enumerate(offered_files)
    )
    if len(pages) + len(files) > _READ_VIEW_MAX_TARGETS:
        raise GrafxConfigurationError(
            "A read-view proof exceeds the bounded target budget.",
            field="read_view_changes",
            limit=_READ_VIEW_MAX_TARGETS,
        )
    return frozenset(pages), files


def _require_image(file: str, page_index: PageIndex, image: object) -> bytes:
    """Return the page image as bytes, refusing anything that is not a byte buffer."""
    if isinstance(image, (bytes, bytearray, memoryview)):
        return bytes(image)
    raise GrafxCorruptionDetected(
        f"A page image must be a byte buffer; got {type(image).__name__}.",
        file=file,
        page=page_index,
        value=type(image).__name__,
    )


def next_seq(seq: int) -> int:
    """Return the next write sequence counter: always even, always ahead of the one given.

    An even counter gains two and an odd one gains one, so the result is even whatever it
    started from. A durable page image therefore always carries an even counter, and a reader
    that finds an odd one is looking at a write that did not complete (CONTRACT.md section 6.3,
    amendment A21).
    """
    return ((seq | 1) + 1) & MAX_SEQ


class _Frame:
    """One resident page together with how many callers currently hold it pinned."""

    __slots__ = (
        "page",
        "pins",
        "doomed",
        "discard_unwritten",
        "device_base_seq",
    )

    def __init__(self, page: Page, *, device_base_seq: int | None = None) -> None:
        self.page: Page = page
        self.pins: int = 0
        self.doomed: bool = False
        # A foreign refresh can retire a clean frame while its reader still holds the Page
        # object. That holder must be able to release its exact object, but it must never be
        # able to publish the now-stale bytes on the way out -- even if it mutates the object
        # after the refresh observed it as clean.
        self.discard_unwritten: bool = False
        # The sequence observed on the device when this frame was admitted (or after its last
        # successful write-back). It is separate from ``page.seq`` because redo may replace the
        # mutable page with a newer logged image before publishing it.
        self.device_base_seq: int = (
            page.seq if device_base_seq is None else device_base_seq
        )


class BufferPoolGuard(Protocol):
    """The injected mechanism that makes pool transitions atomic and cold misses waitable.

    The protocol deliberately belongs to the engine while its implementation belongs to an
    adapter.  C1 therefore imports no thread, task or operating-system mechanism.  One object is
    both the pool mutex and its condition: testing a flight, releasing the mutex to wait and
    re-testing after notification are one atomic protocol, with no second-lock ordering window.
    """

    def __enter__(self) -> object:
        """Take the pool guard."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool | None:
        """Release the pool guard."""
        ...

    def wait_for(
        self, predicate: Callable[[], bool], timeout: float | None = None
    ) -> bool:
        """Atomically release the guard, wait, reacquire it and evaluate ``predicate``."""
        ...

    def notify_all(self) -> None:
        """Wake every waiter; the caller holds this same guard."""
        ...

    def thread_token(self) -> int:
        """Return the stable token of the calling execution thread."""
        ...


class _UnguardedPoolGuard:
    """Single-thread composition of :class:`BufferPoolGuard`, containing no mechanism."""

    __slots__ = ()

    def __enter__(self) -> _UnguardedPoolGuard:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        return None

    def wait_for(
        self, predicate: Callable[[], bool], timeout: float | None = None
    ) -> bool:
        """Evaluate immediately: a single-thread composition has nobody to wake it."""
        return bool(predicate())

    def notify_all(self) -> None:
        """Wake nobody."""
        return None

    def thread_token(self) -> int:
        """Return the sole token in a single-thread composition."""
        return 0


class _PageLoad:
    """One reserved frame slot and one authority to load a cold page."""

    __slots__ = ("owner", "epoch", "valid")

    def __init__(self, owner: int, epoch: tuple[int, int]) -> None:
        self.owner = owner
        self.epoch = epoch
        self.valid = True


class _PageEviction:
    """One dirty frame detached while its bytes are written outside the pool guard."""

    __slots__ = ("owner", "frame")

    def __init__(self, owner: int, frame: _Frame) -> None:
        self.owner = owner
        self.frame = frame


def _condition_capability(candidate: object) -> BufferPoolGuard | None:
    """Return the explicitly injected wait/wake capability, when it is complete."""

    if all(
        callable(getattr(candidate, name, None))
        for name in ("wait_for", "notify_all", "thread_token")
    ):
        return cast(BufferPoolGuard, candidate)
    return None


class _BufferWorkProbe:
    """Data-only accounting for buffer scans during one instrumented commit.

    The probe contains no sink and invokes no callback. A pool only holds it while an enabled
    commit trace is active, so the default no-op path allocates nothing.
    """

    __slots__ = ("flushes", "frames_examined")

    def __init__(self) -> None:
        self.flushes: int = 0
        self.frames_examined: int = 0

    def record_scan(self, frames: int, *, flush: bool = False) -> None:
        """Account for one complete resident-frame scan."""
        self.frames_examined += frames
        if flush:
            self.flushes += 1


class _PinnedPage(AbstractContextManager[Page], ContextDecorator):
    """One lazy, single-use page pin without generator context-manager machinery."""

    __slots__ = ("_pool", "_file", "_page_index", "_page", "_used")

    def __init__(self, pool: BufferPool, file: str, page_index: PageIndex) -> None:
        self._pool: BufferPool | None = pool
        self._file: str | None = file
        self._page_index: PageIndex | None = page_index
        self._page: Page | None = None
        self._used = False

    def __enter__(self) -> Page:
        pool = self._pool
        file = self._file
        page_index = self._page_index
        if self._used or pool is None or file is None or page_index is None:
            raise RuntimeError("a pinned-page context manager is single-use")
        # A generator context is consumed even when its pre-yield pin raises. Set the marker
        # first so this class preserves that one-shot boundary as well.
        self._used = True
        try:
            page = pool.pin(file, page_index)
        except BaseException:
            self._pool = None
            self._file = None
            self._page_index = None
            raise
        self._page = page
        return page

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        del exc_type, exc, tb
        pool = self._pool
        file = self._file
        page_index = self._page_index
        page = self._page
        if pool is None or file is None or page_index is None or page is None:
            raise RuntimeError("the pinned-page context was not entered")
        # Retire every retained authority before unpin so even a failing unpin cannot keep the
        # pool alive or make this context reusable. Python preserves a body exception as
        # __context__ if unpin now raises another one.
        self._pool = None
        self._file = None
        self._page_index = None
        self._page = None
        pool.unpin(
            file,
            page_index,
            dirty=page.dirty,
            page=page,
        )
        return False

    def _recreate_cm(self) -> _PinnedPage:
        """Preserve ContextDecorator's fresh-manager-per-call behaviour."""
        pool = self._pool
        file = self._file
        page_index = self._page_index
        if self._used or pool is None or file is None or page_index is None:
            raise RuntimeError("a pinned-page context manager is single-use")
        return type(self)(pool, file, page_index)


def _guarded(method: Callable[..., object]) -> Callable[..., object]:
    """Run one non-cold-load pool door under the pool's guard.

    The pool is process-wide state that several threads of one participant reach at once -- a
    commit applying pages in the participant section, and searches and scans pinning pages
    outside any section (SPEC-M1 FR-3 names N threads). Its doors were sequences of dictionary
    steps that were individually atomic and jointly not: a reader's eviction could take a frame
    between a writer's lookup and its pin, hand the same page out twice as two objects, and the
    writer's entry landed in the orphan -- an index entry lost with nobody refused (found by the
    thread-concurrency test under suite load: ``index_entry_missing``, no error anywhere).

    The guard is INJECTED, not created here: the pure core imports no mechanism, so the
    composition root hands in the re-entrant condition and the default contains no mechanism.
    ``pin`` owns a more precise phase protocol: cache-table decisions run under this guard while
    cold storage/codec work runs outside it. The body of ``pinned()`` also runs outside the guard
    -- only the pin and the unpin are guarded -- so a caller working with a page never holds the
    pool's lock (A91).
    """

    @wraps(method)
    def wrapper(self: object, *args: object, **kwargs: object) -> object:
        """Enter the pool's guard, run the door, leave the guard."""
        defer_metrics = self._metrics_defer  # type: ignore[attr-defined]
        if defer_metrics is None:
            with self._guard:  # type: ignore[attr-defined]
                return method(self, *args, **kwargs)
        # The production containment adapter queues both pool telemetry and descriptor-cache
        # telemetry emitted by nested storage calls. Its outer exit runs only after the pool
        # guard has been released, so no host sink callback can re-enter a guarded pool door.
        with defer_metrics():
            with self._guard:  # type: ignore[attr-defined]
                return method(self, *args, **kwargs)

    return wrapper


class BufferPool:
    """The page cache of one database, bounded by a budget that only that database spends."""

    __slots__ = (
        "_storage",
        "_codec",
        "_metrics",
        "_metrics_enabled",
        "_metrics_defer",
        "_budget_bytes",
        "_db_label",
        "_guard",
        "_condition",
        "_loads",
        "_evictions",
        "_flight_state_epoch",
        "_page_write_section",
        "_page_sequence_fence",
        "_doomed",
        "_page_size",
        "_frames",
        "_labels",
        "_retained_labels",
        "_page_labels",
        "_data_labels",
        "_structure_epochs",
        "_structure_signatures",
        "_drop_epochs",
        "_every_file_drop",
        "_read_view_token",
        "_grown",
        "_abandoned",
        "_dirty_candidates",
        "_clean_candidate_linger",
        "_modified",
        "_work_probe",
    )

    def __init__(
        self,
        storage: StorageDevice,
        codec: PageCodec,
        metrics: MetricsSink,
        *,
        budget_bytes: int,
        db_label: str,
        guard: AbstractContextManager[object] | None = None,
        metrics_defer: Callable[[], AbstractContextManager[object]] | None = None,
        page_write_section: Callable[[str, PageIndex], AbstractContextManager[object]]
        | None = None,
        page_sequence_fence: Callable[[str, PageIndex], bool] | None = None,
    ) -> None:
        """Build a pool over one device, with its own budget and its own metric label."""
        self._storage: StorageDevice = storage
        self._codec: PageCodec = codec
        self._metrics: MetricsSink = metrics
        metrics_enabled = metrics.enabled
        self._metrics_enabled: bool = metrics_enabled
        self._metrics_defer: Callable[[], AbstractContextManager[object]] | None = (
            metrics_defer if metrics_enabled else None
        )
        self._page_size: int = validate_page_size(storage.page_size)
        selected_guard: AbstractContextManager[object] = (
            _UnguardedPoolGuard() if guard is None else guard
        )
        self._guard = selected_guard
        # A bare context-manager guard remains supported for legacy/direct single-thread tests.
        # Production injects one complete condition, and only that explicit capability enables
        # releasing the mutex across a miss.  Never fabricate a second lock here: reservation and
        # waiting must use the exact mutex that protects ``_frames``.
        self._condition: BufferPoolGuard | None = _condition_capability(selected_guard)
        self._loads: dict[tuple[str, PageIndex], _PageLoad] = {}
        self._evictions: dict[tuple[str, PageIndex], _PageEviction] = {}
        self._flight_state_epoch: int = 0
        self._page_write_section: Callable[
            [str, PageIndex], AbstractContextManager[object]
        ] = (
            (lambda _file, _page_index: nullcontext())
            if page_write_section is None
            else page_write_section
        )
        self._page_sequence_fence: Callable[[str, PageIndex], bool] = (
            (lambda _file, _page_index: False)
            if page_sequence_fence is None
            else page_sequence_fence
        )
        self._doomed: dict[tuple[str, PageIndex], list[_Frame]] = {}
        # Pages this pool has grown a file for and not yet written back, and the subset of them
        # an attempt abandoned and this pool may hand out again. See allocate() and _reclaim().
        self._grown: set[tuple[str, PageIndex]] = set()
        self._abandoned: dict[str, list[PageIndex]] = {}
        # A candidate is either already dirty or is pinned and can become dirty while its
        # caller works outside the guard.  The page remains the authority: every consumer
        # revalidates ``page.dirty`` before writing or reporting it.  Grouping the conservative
        # superset by file makes the commit path proportional to pages that may have changed,
        # instead of to every clean resident frame in a saturated cache (D-10).
        self._dirty_candidates: dict[str, set[PageIndex]] = {}
        # Repeatedly pinning one clean hot page would otherwise add/remove it from a set on every
        # read.  Keep a small, per-file insertion-ordered cohort as conservative candidates.
        # Consumers still revalidate page.dirty, and their normal refresh drains clean entries.
        self._clean_candidate_linger: dict[str, dict[PageIndex, None]] = {}
        # Pages written back since the last forget_modified(), so an eviction cannot take a page
        # out of the answer modified_pages() gives. See that method.
        self._modified: set[tuple[str, PageIndex]] = set()
        self._work_probe: _BufferWorkProbe | None = None
        self._budget_bytes: int = _validate_budget(budget_bytes, self._page_size)
        self._db_label: str = _validate_db_label(db_label)
        self._frames: OrderedDict[tuple[str, PageIndex], _Frame] = OrderedDict()
        # Both label mappings belong to this instance. Nothing in this module holds data
        # that two databases could share, which is the structural half of FR-13 and BR-8.
        self._labels: dict[str, str] = {"db": self._db_label}
        self._retained_labels: dict[str, str] = {
            "db": self._db_label,
            "estimator": BUFFER_RETAINED_ESTIMATOR_VERSION,
        }
        self._page_labels: dict[str, str] = {"kind": "page"}
        self._data_labels: dict[str, str] = {"target": "data"}
        # Bumped whenever a page replacement changes format-owned structure. Files without a
        # registered classifier retain the conservative wholesale-replacement rule. Anything a
        # component derived by walking links -- a chain length, a tail -- predates the bump and
        # cannot be trusted after it.
        self._structure_epochs: dict[str, int] = {}
        # Optional format-owned classifiers used by the ordinary transaction/recovery apply
        # door.  They are immutable code, not cached page authority: a store registers the
        # signature for its file once, and every apply still compares the resident page with
        # the checksum-verified incoming page under the existing redo rule.
        self._structure_signatures: dict[str, Callable[[Page], object]] = {}
        self._drop_epochs: dict[str, int] = {}
        self._every_file_drop: int = 0
        self._read_view_token: object = None
        if metrics_enabled:
            for descriptor in BUFFER_POOL_METRICS:
                metrics.register(descriptor)
            metrics.set_gauge(BUFFER_BUDGET_USED_BYTES, 0.0, self._labels)
            metrics.set_gauge(
                BUFFER_RETAINED_ESTIMATE_BYTES,
                float(self._retained_bytes_estimate()),
                self._retained_labels,
            )

    # --- identity --------------------------------------------------------------------------

    @property
    def storage(self) -> StorageDevice:
        """Return the device this pool caches, for callers that must create or size a file."""
        return self._storage

    def structure_epoch(self, file: str) -> int:
        """Return how many times the pages of this file have been RELINKED underneath.

        A component that walks a chain and remembers where it ended has derived that answer from
        the links between pages. Any door that can rewrite those links -- redo installing a page
        image above all, which can shorten a chain without touching the page the walk stopped at
        -- makes the derived answer wrong in a way no property of that page can reveal. Reading
        this counter before deriving, and again before trusting, is how the holder of a derived
        answer finds out (A40).

        Dropping the cache is NOT one of those doors, and this counter deliberately no longer
        counts it. invalidate() writes every dirty frame back before it forgets it, so the links
        on the device afterwards are exactly the links a walk found before it -- and folding the
        two together made CatalogStore.save() refuse after a plain invalidate(), with a remedy
        that would have thrown the caller's tables away (D7, round 8). What a caller does about a
        dropped cache is nothing; what it does about a relink is re-derive. Two statements, two
        readings.
        """
        return self._structure_epochs.get(file, 0)

    def cache_drop_epoch(self, file: str) -> int:
        """Return how many times cached frames covering this file have been dropped.

        Nothing on the device changed. A holder whose derived answer is only about LINKS may
        ignore this reading entirely; a holder that also wants to be re-checked against freshly
        read bytes adds it in, which is what derived_epoch does.
        """
        return self._every_file_drop + self._drop_epochs.get(file, 0)

    def derived_epoch(self, file: str) -> int:
        """Return the conservative reading: relinks plus cache drops.

        This is what a holder reads when a stale answer would be a wrong answer rather than a
        slow one. The heap's tail cache reads it, because re-walking after a drop costs one walk
        and trusting wrongly costs an append that lands outside the chain.
        """
        return self.structure_epoch(file) + self.cache_drop_epoch(file)

    def _bump_structure_epoch(self, file: str) -> None:
        """Record that the page structure of this file may no longer be what a walk found."""
        self._structure_epochs[file] = self._structure_epochs.get(file, 0) + 1

    @_guarded
    def _register_structure_signature(
        self, file: str, signature: Callable[[Page], object]
    ) -> None:
        """Attach one deterministic format classifier to a file in this pool.

        This is deliberately an engine-internal registration rather than a storage/WAL port.
        The classifier neither persists nor crosses a process boundary, and absence retains the
        historical conservative rule that every applied image may be structural.
        """
        previous = self._structure_signatures.get(file)
        if previous is not None and previous is not signature:
            raise GrafxConfigurationError(
                f"File {file!r} already has a different structural page classifier.",
                file=file,
                field="structure_signature",
            )
        self._structure_signatures[file] = signature

    @_guarded
    def _registered_structure_signature(
        self, file: str
    ) -> Callable[[Page], object] | None:
        """Return the immutable format classifier registered for one file, if any."""
        return self._structure_signatures.get(file)

    def _bump_drop_epoch(self, file: str) -> None:
        """Record that cached frames covering this one file were dropped."""
        self._drop_epochs[file] = self._drop_epochs.get(file, 0) + 1

    def _bump_every_file_drop_epoch(self) -> None:
        """Record that cached frames covering EVERY file were dropped.

        Counting per file cannot express this. A file this pool has never touched still has a
        holder somewhere -- another store, another pool, a component that read it and put the
        answer away -- and there is no list of those files to iterate. One counter added to every
        reading covers them all, including the ones that do not exist yet.

        Proven by test_dropping_every_cache_moves_the_epoch_of_a_pool_that_has_never_been_read,
        test_dropping_every_cache_speaks_for_a_file_this_pool_has_never_touched and
        test_the_every_file_branch_and_the_one_file_branch_move_a_reading_by_the_same_step, the
        last of which is what pins the two spellings to one strength (A66.1, A85).
        """
        self._every_file_drop += 1

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

    @property
    def retained_bytes_estimator(self) -> str:
        """Return the semantic version of :meth:`retained_bytes_estimate`."""
        return BUFFER_RETAINED_ESTIMATOR_VERSION

    # --- residency -------------------------------------------------------------------------

    def used_bytes(self) -> int:
        """Return the bytes currently resident in this pool."""
        return len(self._frames) * self._page_size

    @_guarded
    def retained_bytes_estimate(self) -> int:
        """Return the versioned estimate of Python memory this pool currently retains.

        Unlike :meth:`used_bytes`, this diagnostic includes Page objects, slot directories,
        frame wrappers and the pool's auxiliary bookkeeping.  It deliberately does not control
        eviction in this release, so the long-standing nominal page budget remains compatible.
        The walk follows only engine-owned objects and exact built-in containers; it never calls
        a storage, codec, metrics or host callback.
        """
        return self._retained_bytes_estimate()

    def _retained_bytes_estimate(self) -> int:
        """Build the callback-free ``python-v2`` retained-object estimate under the guard."""
        roots: tuple[object, ...] = (
            self._frames,
            self._doomed,
            self._loads,
            self._evictions,
            self._grown,
            self._abandoned,
            self._dirty_candidates,
            self._clean_candidate_linger,
            self._modified,
            self._structure_epochs,
            self._structure_signatures,
            self._drop_epochs,
            self._labels,
            self._retained_labels,
            self._page_labels,
            self._data_labels,
            self._work_probe,
            self._flight_state_epoch,
        )
        stack = list(roots)
        seen: set[int] = set()
        pointer = calcsize("P")
        retained = (2 + len(BufferPool.__slots__)) * pointer
        while stack:
            item = stack.pop()
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
            kind = type(item)
            if item is None or kind is bool:
                retained += pointer
                continue
            if kind in {int, float}:
                retained += 4 * pointer
                continue
            if kind is str:
                retained += 6 * pointer + len(item) * 4
                continue
            if kind in {bytes, bytearray}:
                retained += 6 * pointer + len(item)
                continue
            if kind is tuple:
                retained += (3 + len(item)) * pointer
                stack.extend(item)
                continue
            if kind is list:
                length = len(item)
                capacity = length + length // 4 + (8 if length else 0)
                retained += (5 + capacity) * pointer
                stack.extend(item)
                continue
            if kind in {set, frozenset}:
                retained += (8 + max(8, 2 * len(item))) * pointer
                stack.extend(item)
                continue
            if kind in {dict, OrderedDict}:
                links = 4 * len(item) if kind is OrderedDict else 0
                retained += (8 + max(8, 3 * len(item)) + links) * pointer
                for key, value in item.items():
                    stack.append(key)
                    stack.append(value)
                continue
            if kind is _Frame:
                retained += (2 + len(_Frame.__slots__)) * pointer
                stack.extend(
                    (
                        item.page,
                        item.pins,
                        item.doomed,
                        item.discard_unwritten,
                        item.device_base_seq,
                    )
                )
                continue
            if kind is _PageLoad:
                retained += (2 + len(_PageLoad.__slots__)) * pointer
                stack.extend((item.owner, item.epoch, item.valid))
                continue
            if kind is _PageEviction:
                retained += (2 + len(_PageEviction.__slots__)) * pointer
                stack.extend((item.owner, item.frame))
                continue
            if isinstance(item, Page):
                retained += Page._retained_bytes_estimate_v1(item)
                continue
            if kind is _BufferWorkProbe:
                retained += (2 + len(_BufferWorkProbe.__slots__)) * pointer
                stack.extend((item.flushes, item.frames_examined))
                continue
            # No arbitrary object's traversal or __sizeof__ method is trusted while the pool
            # guard is held. The current root set reaches this branch only if a future internal
            # field acquires a new value type, where zero is safer than a host callback.
        return retained

    @_guarded
    def _attach_work_probe(self, probe: _BufferWorkProbe) -> bool:
        """Attach one trusted commit-work probe, or decline a nested measurement."""
        if self._work_probe is not None:
            return False
        self._work_probe = probe
        return True

    @_guarded
    def _detach_work_probe(self, probe: _BufferWorkProbe) -> None:
        """Detach ``probe`` if it is still the pool's current measurement."""
        if self._work_probe is probe:
            self._work_probe = None

    @_guarded
    def is_resident(self, file: str, page_index: PageIndex) -> bool:
        """Return True when the page is currently cached by this pool."""
        return (file, page_index) in self._frames

    @_guarded
    def pin_count(self, file: str, page_index: PageIndex) -> int:
        """Return how many callers hold the page pinned; zero when it is not resident."""
        frame = self._frames.get((file, page_index))
        return 0 if frame is None else frame.pins

    def _add_dirty_candidate(self, key: tuple[str, PageIndex]) -> None:
        """Conservatively admit one location before its page can change outside the guard."""

        file, page_index = key
        candidates = self._dirty_candidates.get(file)
        if candidates is not None and page_index in candidates:
            return
        self._dirty_candidates.setdefault(file, set()).add(page_index)

    def _linger_clean_candidate(self, key: tuple[str, PageIndex]) -> None:
        """Retain one clean hot page without allowing the conservative index to grow with N."""

        file, page_index = key
        lingering = self._clean_candidate_linger.setdefault(file, {})
        if page_index in lingering:
            return
        lingering[page_index] = None
        self._add_dirty_candidate(key)
        if len(lingering) <= _CLEAN_CANDIDATE_LINGER_PER_FILE:
            return
        oldest = next(iter(lingering))
        del lingering[oldest]
        if not lingering:
            del self._clean_candidate_linger[file]
        self._refresh_dirty_candidate((file, oldest))

    def _remove_dirty_candidate(self, key: tuple[str, PageIndex]) -> None:
        """Remove a location whose every locally owned frame is clean and unpinned."""

        file, page_index = key
        candidates = self._dirty_candidates.get(file)
        if candidates is not None:
            candidates.discard(page_index)
            if not candidates:
                del self._dirty_candidates[file]
        lingering = self._clean_candidate_linger.get(file)
        if lingering is not None:
            lingering.pop(page_index, None)
            if not lingering:
                del self._clean_candidate_linger[file]

    def _forget_clean_linger(self, key: tuple[str, PageIndex]) -> None:
        """Stop treating an authoritative dirty/pinned frame as read-only linger."""

        file, page_index = key
        lingering = self._clean_candidate_linger.get(file)
        if lingering is None:
            return
        lingering.pop(page_index, None)
        if not lingering:
            del self._clean_candidate_linger[file]

    @staticmethod
    def _frame_needs_dirty_candidate(frame: _Frame) -> bool:
        """Say whether a frame is dirty or can still become dirty through a live pin."""

        return frame.page.dirty or frame.pins > 0

    def _refresh_dirty_candidate(self, key: tuple[str, PageIndex]) -> None:
        """Re-derive one candidate from resident, doomed and in-flight frame authority."""

        resident = self._frames.get(key)
        if resident is not None and self._frame_needs_dirty_candidate(resident):
            self._forget_clean_linger(key)
            self._add_dirty_candidate(key)
            return
        if any(
            self._frame_needs_dirty_candidate(frame)
            for frame in self._doomed.get(key, ())
        ):
            self._forget_clean_linger(key)
            self._add_dirty_candidate(key)
            return
        eviction = self._evictions.get(key)
        if eviction is not None and self._frame_needs_dirty_candidate(eviction.frame):
            self._forget_clean_linger(key)
            self._add_dirty_candidate(key)
            return
        self._remove_dirty_candidate(key)

    def _dirty_candidate_keys(
        self, file: str | None = None
    ) -> tuple[tuple[str, PageIndex], ...]:
        """Snapshot candidate locations so revalidation may shrink the live sets safely."""

        if file is not None:
            return tuple(
                (file, page_index)
                for page_index in self._dirty_candidates.get(file, ())
            )
        return tuple(
            (name, page_index)
            for name, page_indexes in self._dirty_candidates.items()
            for page_index in page_indexes
        )

    @_guarded
    def _assert_dirty_candidate_coverage(self) -> None:
        """Test oracle: prove the index covers every dirty or still-mutable owned frame.

        Production never calls this full scan.  Property and mutation tests use it after each
        transition to compare D-10's bounded index with the pre-optimization authority.
        """

        covered = set(self._dirty_candidate_keys())
        authoritative: set[tuple[str, PageIndex]] = set()
        authoritative.update(
            key
            for key, frame in self._frames.items()
            if self._frame_needs_dirty_candidate(frame)
        )
        authoritative.update(
            key
            for key, frames in self._doomed.items()
            if any(self._frame_needs_dirty_candidate(frame) for frame in frames)
        )
        authoritative.update(
            key
            for key, eviction in self._evictions.items()
            if self._frame_needs_dirty_candidate(eviction.frame)
        )
        lingering = {
            (file, page_index)
            for file, page_indexes in self._clean_candidate_linger.items()
            for page_index in page_indexes
        }
        expected = authoritative | lingering
        if covered != expected:
            raise AssertionError(
                "dirty candidate index diverged from the full frame scan: "
                f"missing={sorted(expected - covered)!r}, "
                f"extra={sorted(covered - expected)!r}"
            )

    # --- the four operations -----------------------------------------------------------------

    def pin(self, file: str, page_index: PageIndex) -> Page:
        """Return the page, reading it from the device when it is not resident, and pin it.

        A pinned page is never evicted, so the caller must unpin it. Everything that can fail
        fails before the pin count moves.  In a production composition, a cold miss reserves its
        frame slot under the injected condition and performs storage read plus codec decode after
        releasing that guard.  Exactly one loader exists for a ``(file, page)`` key; waiters wake
        to the one published Page object.  A cache/drop or structure epoch that moves meanwhile
        makes the detached result ineligible for publication and the load retries from the new
        view.
        """
        _require_page_index("page_index", page_index)
        deferred = (
            nullcontext() if self._metrics_defer is None else self._metrics_defer()
        )
        with deferred:
            if self._condition is None:
                # Compatibility for a direct composition that supplied only a context manager.
                # Such a composition has no injected way to release-and-wait atomically, so it
                # retains the pre-single-flight behaviour. Public assembly always supplies the
                # complete condition capability.
                with self._guard:
                    return self._pin_guarded(file, page_index)
            return self._pin_single_flight(file, page_index)

    def _pin_guarded(self, file: str, page_index: PageIndex) -> Page:
        """Run the compatible cold path for a guard without wait/wake capability."""

        key = (file, page_index)
        frame = self._frames.get(key)
        if frame is not None:
            if frame.pins == 0:
                self._add_dirty_candidate(key)
            self._frames.move_to_end(key)
            frame.pins += 1
            return frame.page
        self._make_room(file, page_index)
        page = self._read_page(file, page_index)
        frame = _Frame(page)
        self._add_dirty_candidate(key)
        try:
            frame.pins = 1
            self._frames[key] = frame
        except BaseException:
            self._refresh_dirty_candidate(key)
            raise
        self._report_usage()
        return page

    def _pin_single_flight(self, file: str, page_index: PageIndex) -> Page:
        """Load and publish one cold frame through the injected condition protocol."""

        condition = self._condition
        assert condition is not None
        key = (file, page_index)
        owner = condition.thread_token()
        while True:
            eviction: _PageEviction | None = None
            eviction_key: tuple[str, PageIndex] | None = None
            load: _PageLoad | None = None
            budget_failure: GrafxBufferBudgetExceeded | None = None
            with self._guard:
                frame = self._frames.get(key)
                if frame is not None:
                    if frame.pins == 0:
                        self._add_dirty_candidate(key)
                    self._frames.move_to_end(key)
                    frame.pins += 1
                    return frame.page

                existing_load = self._loads.get(key)
                if existing_load is not None:
                    self._refuse_own_flight(
                        owner, existing_load.owner, file, page_index, operation="load"
                    )
                    condition.wait_for(
                        lambda: self._loads.get(key) is not existing_load
                    )
                    continue
                existing_eviction = self._evictions.get(key)
                if existing_eviction is not None:
                    self._refuse_own_flight(
                        owner,
                        existing_eviction.owner,
                        file,
                        page_index,
                        operation="eviction",
                    )
                    condition.wait_for(
                        lambda: self._evictions.get(key) is not existing_eviction
                    )
                    continue

                if self._occupied_slots() < self.capacity_pages:
                    load = _PageLoad(owner, self._load_epoch(file))
                    self._loads[key] = load
                else:
                    victim = self._find_victim()
                    if victim is None:
                        if self._loads or self._evictions:
                            self._refuse_capacity_wait_on_self(owner, file, page_index)
                            observed = self._flight_state_epoch
                            condition.wait_for(
                                lambda: self._flight_state_epoch != observed
                            )
                            continue
                        budget_failure = self._budget_failure(file, page_index)
                    else:
                        victim_frame = self._frames[victim]
                        if victim_frame.page.dirty:
                            # Reserve the durable-change evidence before the frame leaves the
                            # authoritative table.  ``set.add`` may allocate; failing here keeps
                            # the dirty frame resident.  Once publication succeeds, settlement
                            # can be exception-safe without risking that a successfully written
                            # page disappears from ``modified_pages()``.
                            self._modified.add(victim)
                        prepared_load = _PageLoad(owner, self._load_epoch(file))
                        prepared_eviction = (
                            _PageEviction(owner, victim_frame)
                            if victim_frame.page.dirty
                            else None
                        )
                        # Construct both tickets while the victim is still authoritative. Publish
                        # the reservations before removing it, all under the same guard: an
                        # allocation failure can then only leave the original resident frame, and
                        # no waiter can observe the short-lived over-counted transition.
                        try:
                            self._loads[key] = prepared_load
                            if prepared_eviction is not None:
                                self._evictions[victim] = prepared_eviction
                            self._frames.pop(victim)
                            if prepared_eviction is None:
                                self._remove_dirty_candidate(victim)
                        except BaseException:
                            self._loads.pop(key, None)
                            self._evictions.pop(victim, None)
                            raise
                        if victim_frame.page.dirty:
                            assert prepared_eviction is not None
                            eviction = prepared_eviction
                            eviction_key = victim
                            # Claim this logical admission now so a second caller of the same key
                            # cannot evict another frame while the first victim is written. It
                            # does not count as a second capacity slot until that victim leaves.
                            load = prepared_load
                        else:
                            # Clean eviction and target reservation are one atomic replacement;
                            # no competitor can steal the slot this caller just made.
                            load = prepared_load

            if budget_failure is not None:
                if self._metrics_active():
                    self._metrics.increment(
                        BUFFER_BUDGET_EXCEEDED_TOTAL, 1.0, self._labels
                    )
                raise budget_failure
            if eviction is not None:
                assert eviction_key is not None
                assert load is not None
                self._evict_dirty_frame(eviction_key, eviction, key, load)
            assert load is not None

            try:
                page = self._read_page(file, page_index)
            except BaseException:
                usage: tuple[float, float] | None = None
                with self._guard:
                    if self._loads.get(key) is load:
                        del self._loads[key]
                        self._signal_flight_state()
                        try:
                            usage = self._usage_reading()
                        except BaseException:
                            # Diagnostics never replace the load/publication failure that owns
                            # this cleanup path.
                            usage = None
                if usage is not None:
                    try:
                        self._emit_usage(usage)
                    except BaseException:
                        # Preserve the exact primary failure for direct, uncontained test/host
                        # compositions as well as the contained production composition.
                        pass
                raise

            usage: tuple[float, float] | None = None
            retry = False
            with self._guard:
                current = self._loads.get(key)
                if current is not load:
                    # No sanctioned path removes a live ticket without waking its owner. Keep a
                    # fail-closed guard here: the detached object has no publication authority.
                    retry = True
                else:
                    resident = self._frames.get(key)
                    if resident is not None:
                        if resident.pins == 0:
                            self._add_dirty_candidate(key)
                        self._frames.move_to_end(key)
                        resident.pins += 1
                        page = resident.page
                    elif load.valid and load.epoch == self._load_epoch(file):
                        admitted: _Frame | None = None
                        try:
                            admitted = _Frame(page)
                            self._add_dirty_candidate(key)
                            admitted.pins = 1
                            self._frames[key] = admitted
                        except BaseException:
                            # The load ticket remains authoritative until both construction and
                            # insertion succeed. Remove it and wake every waiter on either failure,
                            # without leaving a phantom pinned frame behind.
                            if (
                                admitted is not None
                                and self._frames.get(key) is admitted
                            ):
                                del self._frames[key]
                            self._refresh_dirty_candidate(key)
                            del self._loads[key]
                            self._signal_flight_state()
                            raise
                    else:
                        retry = True
                    del self._loads[key]
                    self._signal_flight_state()
                    if not retry and resident is None:
                        try:
                            usage = self._usage_reading()
                        except BaseException:
                            # Publication already succeeded and waiters were notified. An
                            # allocation failure in diagnostic sampling cannot turn that pin into
                            # a reported load failure whose caller would be unable to release it.
                            usage = None
            if retry:
                continue
            if usage is not None:
                self._emit_usage(usage)
            return page

    def read_fresh_page(self, file: str, page_index: PageIndex) -> Page:
        """Read one detached page from the device, bypassing every resident frame.

        This is the observation door for optimistic cross-process certificates.  Returning a
        detached page is load-bearing: replacing a resident object behind a holder would make
        that holder release or mutate a page the frame table no longer owns.  The ordinary
        torn-read/checksum protocol still applies through :meth:`_read_page`, and this door never
        writes, evicts, or changes a cache epoch.
        """
        _require_page_index("page_index", page_index)
        # A fresh cross-process certificate must also be fresh with respect to the directory
        # entry.  In generation mode this targeted invalidation keeps page-0 OCC and speculative
        # index artifact validation from certifying an inode another participant moved aside.
        deferred = (
            nullcontext() if self._metrics_defer is None else self._metrics_defer()
        )
        with deferred:
            self._invalidate_descriptor_identity(file)
            return self._read_page(file, page_index)

    def _observe_fresh_page(
        self,
        file: str,
        page_index: PageIndex,
        previous: object | None = None,
    ) -> tuple[Page | None, object]:
        """Read the device and return a page only when its proved raw image changed.

        ``None`` means byte-for-byte equality with the exact witness returned by this pool for
        this location. The physical read and descriptor-identity invalidation still happen.
        Unknown, foreign or wrong-location witnesses simply miss and take the complete torn-read,
        checksum and structural decode path.
        """
        _require_page_index("page_index", page_index)
        deferred = (
            nullcontext() if self._metrics_defer is None else self._metrics_defer()
        )
        with deferred:
            self._invalidate_descriptor_identity(file)
            page, witness = self._read_page_observation(
                file,
                page_index,
                previous=previous,
                produce_witness=True,
            )
        assert witness is not None
        return page, witness

    @_guarded
    def unpin(
        self,
        file: str,
        page_index: PageIndex,
        *,
        dirty: bool = False,
        page: Page | None = None,
    ) -> None:
        """Release one pin on the page, marking it dirty when the caller changed it.

        A page also remembers on its own that it was mutated, so a caller that forgets the flag
        still cannot lose a write; passing it is how a caller says so explicitly.

        ``page`` names the object the caller pinned. While it was held, a read view may have
        DOOMED its frame -- another participant committed and this frame is no longer what the
        name holds on the device -- and a fresh frame may stand under the same key. The holder
        releases the doomed one; the last release drops it, unwritten (a doomed frame is a
        reader's, and clean: a writer's pins happen inside the participant section that read
        views are taken in, so the two never overlap).
        """
        _require_page_index("page_index", page_index)
        key = (file, page_index)
        frame = self._frames.get(key)
        if page is not None and (frame is None or frame.page is not page):
            doomed = self._doomed.get(key, [])
            for position, candidate in enumerate(doomed):
                if candidate.page is page:
                    if dirty:
                        if page_index not in self._dirty_candidates.get(file, ()):
                            self._add_dirty_candidate(key)
                        candidate.page.dirty = True
                    candidate.pins -= 1
                    if candidate.pins <= 0:
                        if candidate.page.dirty and not candidate.discard_unwritten:
                            self._write_back(file, page_index, candidate.page)
                        del doomed[position]
                        if not doomed:
                            del self._doomed[key]
                        self._bump_drop_epoch(file)
                        self._signal_flight_state()
                    if candidate.pins <= 0:
                        self._refresh_dirty_candidate(key)
                    return
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
            if page_index not in self._dirty_candidates.get(file, ()):
                self._add_dirty_candidate(key)
            frame.page.dirty = True
        frame.pins -= 1
        if frame.pins == 0 and not frame.page.dirty:
            if key not in self._doomed and key not in self._evictions:
                self._linger_clean_candidate(key)
            else:
                self._refresh_dirty_candidate(key)
        if frame.pins == 0:
            self._signal_flight_state()

    def pinned(self, file: str, page_index: PageIndex) -> AbstractContextManager[Page]:
        """Return a context manager that pins the page and always unpins it again."""
        return _PinnedPage(self, file, page_index)

    @contextmanager
    def page_write_fence(self, file: str, page_index: PageIndex) -> Iterator[None]:
        """Hold the local pool guard and the injected page section in canonical order.

        A multi-page operation whose page-0 certificate covers the whole operation (an index
        rebuild, for example) uses this door instead of taking the injected section directly.
        Ordinary guarded writes enter the local guard before their page section. A dirty
        cold-miss eviction is the one exception: it leaves the guard, takes and releases its page
        section, and only then reacquires the guard to settle. This door waits for all such flights
        before taking its section, so neither route can form section->guard against
        guard->section. Production mechanisms remain re-entrant for nested guarded writes.

        The default mechanisms remain no-ops, preserving the pure-core construction used by
        adapters that do not need cross-process page fencing.
        """
        _require_page_index("page_index", page_index)
        deferred = (
            nullcontext() if self._metrics_defer is None else self._metrics_defer()
        )
        with deferred:
            with self._guard:
                # Take no external page section while a flight may need to settle under this
                # guard. Acquiring the section first and then waiting would invert the dirty
                # eviction order (section -> guard against eviction's section -> guard).
                self._wait_for_evictions()
                self._wait_for_loads()
                with self._page_write_section(file, page_index):
                    yield

    def _reusable_index(self, file: str) -> PageIndex | None:
        """Return an index this pool may hand out again, or None to ask the device for one.

        A candidate that is RESIDENT AND PINNED is left where it is rather than taken. Nothing
        should be able to pin a page that was discarded and never written, so this is a guard on
        an invariant rather than a case with a caller -- but it is a guard and not a proof,
        because the alternative is the branch below deleting a frame whose holder still has the
        object, and that holder's next unpin would then decrement a stranger's pin count.
        """
        waiting = self._abandoned.get(file)
        if not waiting:
            return None
        for position in range(len(waiting) - 1, -1, -1):
            candidate = waiting[position]
            frame = self._frames.get((file, candidate))
            if frame is not None and frame.pins:
                continue
            del waiting[position]
            if not self._claim_is_unwritten(file, candidate):
                # A foreign refresh may have occurred since this process abandoned the index.
                # A real device image retires the local claim; it is never blanked or reused.
                self._grown.discard((file, candidate))
                continue
            if not waiting:
                del self._abandoned[file]
            return candidate
        if not waiting:
            del self._abandoned[file]
        return None

    def _claim_is_unwritten(self, file: str, page_index: PageIndex) -> bool:
        """Say whether an allocation claim still names the device's untouched zero image."""
        raw = self._storage.read_page(file, page_index)
        return is_unwritten_image(raw, self._page_size)

    @_guarded
    def allocate(self, file: str, page_type: int, *, reuse: bool = True) -> Page:
        """Return a fresh page, pinned, empty and of the requested type, growing the file if need be.

        The page comes back pinned on purpose: an unpinned fresh page could be evicted before
        the caller had written anything into it, and the caller would then be holding a page the
        pool has already forgotten.

        The budget is settled before the device is asked to grow. Doing it the other way round
        would leave the file one page longer every time the refusal is raised, and the refusal
        is retryable, so a retry loop would grow the data file once per attempt with no
        sanctioned way to shrink it back (G6).

        **The file does not always grow, and ``reuse=False`` is for the callers that need it to.**
        A page this pool grew the file for during an attempt that was then refused is handed out
        again: G6 forbids shrinking, so without this every refused append leaked one page for the
        life of the database -- three participants appending to one table left ten to twenty-four
        all-zero pages behind in a minute, each of them a `verify()` finding on a database in
        which nothing had gone wrong. Reuse is safe because such a page has never been readable
        by anybody and no other participant can hold it: `StorageDevice.allocate` derives its
        index from the file length, the file never shrinks, and every allocation on the commit
        path runs inside the coordinator's commit section.

        A caller whose loop is "allocate until the file is long enough" must pass ``reuse=False``.
        For those, a hand-out that does not lengthen the file is not a saving: it makes the return
        value describe something other than the growth, and the loop go round again. Three do:
        ``grow_to`` and ``IndexStore._grow_buckets`` wait on ``page_count``, and ``write_chain``
        builds a list of DISTINCT pages that the pool must not inject a duplicate into.

        The other seven callers take the default, and each was checked rather than assumed. They
        are ``CatalogStore.bootstrap``, ``Database`` identity, ``HeapStore.bootstrap`` and
        ``IndexStore.create`` -- all four guarded by ``page_count(file) == 0``, where no page can
        be waiting, and all four refusing outright if the index they get is not page 0 -- plus
        ``HeapStore._append`` and ``_extent_for`` and ``IndexStore._append_bucket_page``, which ask
        for a fresh page and link whatever they are given. For every one of those, reuse is the
        point.
        """
        self._wait_for_evictions(file)
        try:
            prospective = self._storage.page_count(file)
        except GrafxError as failure:
            # page_count is the cheaper presence question and, unlike exists(), does not walk
            # every sibling name merely to calculate the budget position of the append.  Only
            # absence has the old zero answer.  A case collision, an unaligned paged file and
            # every other typed refusal remain corruption/incompatibility rather than being
            # mistaken for an empty file.  storage.allocate below still re-proves the exact
            # descriptor identity before it grows anything.
            if failure.details.get("reason") != "missing_file":
                raise
            prospective = 0
        self._make_room(file, prospective)
        page_index = self._reusable_index(file) if reuse else None
        if page_index is None:
            page_index = self._storage.allocate(file, 1)
        self._grown.add((file, page_index))
        key = (file, page_index)
        loading = self._loads.get(key)
        if loading is not None:
            # Allocation installed the current meaning of this physical page while a detached
            # read still carries its previous bytes. The reservation continues to count until
            # the loader returns, but those bytes can no longer be published.
            loading.valid = False
        existing = self._frames.get(key)
        if existing is not None:
            # Two ways to get here and neither leaves a holder behind. The file shrank, which no
            # sanctioned operation does (G6); or this is a reused index that something read after
            # it was discarded, which _reusable_index has already established is unpinned. The
            # original A34 argument -- that no input could reach this branch at all -- stopped
            # holding when reuse was added, so the pin question moved to where a candidate is
            # chosen rather than being answered by declaring the branch dead.
            del self._frames[key]
            self._remove_dirty_candidate(key)
        self._add_dirty_candidate(key)
        try:
            page = Page(page_type, page_size=self._page_size, page_index=page_index)
            page.dirty = True
            # The device image for a page allocated here is all-zero. Its mutable Page may later
            # be replaced by redo, but the CAS base remains the image this frame took ownership
            # of.
            frame = _Frame(page, device_base_seq=0)
            frame.pins = 1
            self._frames[key] = frame
        except BaseException:
            self._refresh_dirty_candidate(key)
            raise
        self._report_usage()
        return page

    @_guarded
    def flush(self, file: str | None = None) -> int:
        """Write every dirty page of the file, or of the whole pool, and return how many.

        Writing is not making durable. A commit does not come through here to be made safe: the
        log is the authority on durability and the redo is idempotent, so the commit protocol
        deliberately does not barrier the data files (CONTRACT.md section 8.5 step 6). The path
        that does want them on the platter is checkpoint().
        """
        self._wait_for_evictions(file)
        candidates = self._dirty_candidate_keys(file)
        if len(candidates) > 1:
            headers = tuple(key for key in candidates if key[1] == HEADER_PAGE_INDEX)
            if headers:
                # The former full-frame walk inherited LRU order, and callers deliberately
                # touched publication headers last so readers could never observe a certificate
                # before the pages it describes. D-10's candidate set removed that incidental
                # ordering. Restore the explicit invariant in O(dirty candidates), without
                # scanning the clean resident population the index exists to avoid. This also
                # covers a database-wide flush: every file's page 0 follows all data pages.
                candidates = (
                    tuple(key for key in candidates if key[1] != HEADER_PAGE_INDEX)
                    + headers
                )
        probe = self._work_probe
        if probe is not None:
            try:
                probe.record_scan(len(candidates), flush=True)
            except BaseException:  # noqa: BLE001 - diagnostics never own buffer progress
                self._work_probe = None
        written = 0
        for name, page_index in candidates:
            frame = self._frames.get((name, page_index))
            if frame is None or not frame.page.dirty:
                self._refresh_dirty_candidate((name, page_index))
                continue
            self._write_back(name, page_index, frame.page)
            written += 1
        return written

    @_guarded
    def modified_pages(
        self, file: str | None = None
    ) -> frozenset[tuple[str, PageIndex]]:
        """Return every page this pool has CHANGED since the last :meth:`forget_modified`.

        This exists so a caller can learn what its own work actually changed, instead of
        re-deriving it from what the work was supposed to change. A caller that enumerates the
        pages it believes it touched has to name every site that touches one, and the day a new
        site appears -- relinking a chain, moving a hint -- the enumeration is silently short by
        one page, with no test able to see it because nothing declares the omission.

        **A still-dirty frame is not the whole answer, and reading only those was a defect.**
        The first version of this returned the resident frames whose page was dirty. A page the
        work changed and that the pool then EVICTED under budget pressure was written back and
        marked clean, so it left the set -- and the caller, which was using two readings of it to
        decide what its commit had to log, logged nothing for that page. Measured on the shape
        that reaches it: one commit of 400 rows against a 256-frame budget replayed to 2 of 402
        rows, with the rest in the log and unreachable, because the chain links that reached them
        had been evicted out of the answer. A set that a write-back can shrink is not a record of
        what happened; it is a record of what has not been dealt with yet.

        So a write-back REMEMBERS the page instead of forgetting it, and this returns the union:
        frames still dirty, plus every page written back since the window opened. The eviction
        can no longer take a page out of the answer, which is the property the whole measurement
        rests on.

        The set is a snapshot, not a view: the frames go on changing after it is returned.
        """
        self._wait_for_evictions(file)
        candidates = self._dirty_candidate_keys(file)
        probe = self._work_probe
        if probe is not None:
            try:
                probe.record_scan(len(candidates))
            except BaseException:  # noqa: BLE001 - diagnostics never own buffer progress
                self._work_probe = None
        live: set[tuple[str, PageIndex]] = set()
        for key in candidates:
            frame = self._frames.get(key)
            if frame is not None and frame.page.dirty:
                live.add(key)
            else:
                self._refresh_dirty_candidate(key)
        return frozenset(
            key for key in (live | self._modified) if file is None or key[0] == file
        )

    @_guarded
    def has_dirty_pages(self, file: str | None = None) -> bool:
        """Say whether resident or doomed frames still hold unpublished local changes."""
        self._wait_for_evictions(file)
        candidates = self._dirty_candidate_keys(file)
        probe = self._work_probe
        examined = 0
        dirty_found = False
        for key in candidates:
            examined += 1
            resident = self._frames.get(key)
            dirty_found = resident is not None and resident.page.dirty
            if not dirty_found:
                dirty_found = any(
                    frame.page.dirty for frame in self._doomed.get(key, ())
                )
            if dirty_found:
                break
            self._refresh_dirty_candidate(key)
        if probe is not None:
            try:
                probe.record_scan(examined)
            except BaseException:  # noqa: BLE001 - diagnostics never own buffer progress
                self._work_probe = None
        return dirty_found

    @_guarded
    def pages_written_back(
        self, file: str | None = None
    ) -> frozenset[tuple[str, PageIndex]]:
        """Return the pages this pool has WRITTEN OUT since the last :meth:`forget_modified`.

        The other half of :meth:`modified_pages`, and the caller that needs it apart is the one
        undoing a unit of work. Forgetting a frame restores the device only for a page the device
        has never seen; for a page this pool has already written, the device carries the work and
        forgetting the frame throws away the only corrected copy of it. A caller cannot tell those
        two apart from the dirty flag -- a written-back page is CLEAN -- so it asks here.
        """
        self._wait_for_evictions(file)
        return frozenset(
            key for key in self._modified if file is None or key[0] == file
        )

    @_guarded
    def write_back(self, file: str, page_index: PageIndex) -> bool:
        """Put one resident page on the device now, and say whether there was one to put.

        A flush of the whole file would also write pages that have nothing to do with the caller.
        This writes exactly the page named, which is what an all-or-nothing undo needs: it decides
        page by page and must not carry anything else along with its decision.
        """
        self._wait_for_evictions(file, page_index)
        frame = self._frames.get((file, page_index))
        if frame is None:
            return False
        self._write_back(file, page_index, frame.page)
        return True

    @_guarded
    def forget_modified(self) -> None:
        """Open a fresh window for :meth:`modified_pages`, forgetting what was written back.

        The written-back half of that answer accumulates, so something has to say when a new
        window starts. Its caller is whoever is about to measure a unit of work, and it says so
        immediately before taking its first reading -- never after work has begun, which would
        drop pages that unit had already changed.
        """
        self._wait_for_evictions()
        self._modified.clear()

    @_guarded
    def settle_abandoned(self, file: str | None = None) -> int:
        """Write out every page an attempt abandoned as the FREE page it is; return how many.

        A page this pool grew a file for and then had discarded is unreferenced, and the file
        cannot shrink to give it back (G6). While this pool lives it is reusable and costs
        nothing. When the pool stops -- the process closes -- the chance to reuse it is gone, and
        left all zeros it stays that way for the life of the database. ``is_unwritten_image``
        cannot tell that page from the one a crash leaves between allocating and writing, so
        ``verify()`` reports ``page_unwritten`` and a database in which nothing went wrong stops
        reporting clean. Under the three writer processes of `tests/smoke` that happened about once a
        run, which made a test that asserts a clean database fail one run in six.

        Writing the page settles the ambiguity in the honest direction rather than teaching
        verify to look away. The page IS free; a FREE page with a valid checksum and an even
        sequence counter says so, carries no table descriptor so no walk can claim it, and is a
        page type the layout defines. It stops being a finding because it stops being a question.

        Settling ENDS the reuse of that page, and that is deliberate: the reclaim rests on the
        page never having been readable, and this makes it readable.
        """
        self._wait_for_evictions(file)
        names = sorted(self._abandoned) if file is None else [file]
        written = 0
        for name in names:
            for page_index in list(self._abandoned.pop(name, ())):
                if not self._claim_is_unwritten(name, page_index):
                    # Another participant acquired meaning for this physical page after the
                    # local attempt abandoned it. Retire the claim without touching that image.
                    self._grown.discard((name, page_index))
                    continue
                blank = Page(
                    int(PageType.FREE), page_size=self._page_size, page_index=page_index
                )
                self._write_back(name, page_index, blank)
                written += 1
        return written

    @_guarded
    def checkpoint(self, file: str | None = None) -> int:
        """Flush the dirty pages and put them on the platter, returning how many were written.

        This is the data-file half of amendment A25. The device has no metrics slot and cannot
        tell a log barrier from a data barrier without reading file names, so the caller of
        durable_barrier is the one that can classify it: the write-ahead log times its own
        barrier as target wal, and this one times it as target data. A barrier that fails is
        counted before it is re-raised, because a failed barrier means nothing may be
        acknowledged as durable.
        """
        written = self.flush(file)
        self.durability_barrier(file)
        return written

    @_guarded
    def durability_barrier(self, file: str | None = None) -> None:
        """Put prior data writes on the platter without flushing a second time.

        The ordinary checkpoint door remains ``flush + barrier``.  TransactionManager's
        concurrent checkpoint uses the two halves separately so another process may commit
        while the device performs the slow durability barrier; this method deliberately owns
        the exact same timer and failure counter as :meth:`checkpoint`.
        """
        self._wait_for_evictions(file)
        if self._metrics_active():
            with self._metrics.time(FSYNC_DURATION_SECONDS, self._data_labels):
                self._barrier(file)
        else:
            self._barrier(file)

    def _barrier(self, file: str | None) -> None:
        """Ask the device for a durability barrier, counting a failure before re-raising it."""
        try:
            self._storage.durable_barrier(file)
        except GrafxDurabilityBarrierFailed:
            if self._metrics_active():
                self._metrics.increment(BARRIER_FAILURES_TOTAL, 1.0, None)
            raise

    @_guarded
    def begin_read_view(
        self,
        token: object = None,
        *,
        own: bool = False,
        allow_writeback: bool = True,
        unfenced_file: str | None = None,
        changed_pages: Iterable[tuple[str, PageIndex]] | None = None,
        changed_files: Iterable[str] = (),
        expected_previous: object = _READ_VIEW_BASELINE_UNSET,
    ) -> bool:
        """Start a fresh read view over this database, and say whether anything was dropped.

        THE PROBLEM THIS EXISTS FOR. Every epoch this pool keeps is a PROCESS-LOCAL counter: it
        moves when this pool relinks pages or drops frames, and nothing moves it when another
        participant commits. So a participant that had already read a table went on answering
        from frames it cached before that commit -- no error, no missing file, just fewer rows
        than exist, which is the worst shape a wrong answer can take. A participant that opened
        AFTER the commit saw everything, which is why it looked like it worked.

        This is not the snapshot guarantee doing its job. A snapshot is entitled to a stable view
        for its own lifetime; a NEW read view in the same participant must see what has been
        committed since and is at or below its snapshot. That is what this door establishes.

        The token is how a caller avoids paying for a drop that nothing needs. C1 cannot see a
        foreign commit on its own -- the only shared thing it has is the device, and asking the
        device whether a page changed means reading the page, which is the cost the cache exists
        to avoid. So the caller passes whatever it already watches that moves when another
        participant commits: the durable WAL tail, the lease epoch, a control-record counter.
        Same token as last time means nothing has happened and nothing is dropped. A DIFFERENT
        token, or no token at all, means assume it has, which is the safe default for a caller
        that has nothing to watch.

        A conservative full refresh retains invalidate's established behaviour and writes dirty
        unpinned frames before dropping them; direct engine composition relies on that door to
        publish legitimate local work. ``allow_writeback=False`` is the observational variant:
        an unproved full refresh preflights every resident/doomed frame and refuses dirty state
        before moving a frame, descriptor generation or token. A proved bounded foreign delta is
        already zero-write and refuses any targeted dirty frame before moving state.

        ``own`` is the caller SAYING the token moved only because this participant itself
        published a commit (CQ-2/QW-4): the resident frames are the very committed state this
        pool produced, so dropping them re-reads every page for no new information. The claim
        is honoured only while it is provable from here -- a previous view existed and no frame
        outside ``unfenced_file`` is dirty; anything else takes the full drop exactly as a
        foreign token does. ``unfenced_file`` names the one file whose device state can move
        without moving the token (the catalog has no page-0 sequence fence), so its frames are
        dropped and its epoch bumped even in an own view; a foreign mutation that moves no LSN
        anywhere else remains the business of the page-0 certificates, as it is today.

        ``changed_pages`` is CE-3's optional proof of the exact committed WAL delta between the
        previous token and this one. ``None`` retains the conservative full-pool drop. A concrete
        iterable permits a zero-write discard of only those pages plus every file named by
        ``changed_files``; the caller must fall back to ``None`` whenever the WAL interval is not
        complete. ``unfenced_file`` is always added as a whole-file target because its device
        image can move without a page-zero certificate. All targets are preflighted before the
        first frame moves. A dirty target therefore refuses without advancing the token, while
        unrelated dirty work remains resident. ``expected_previous`` is required for a partial
        proof and atomically binds it to the token from which the caller derived it; omission or
        mismatch takes the conservative full foreign refresh rather than applying an unbound or
        stale partial interval.
        """
        if type(allow_writeback) is not bool:
            raise GrafxConfigurationError(
                "A read-view write-back choice must be exactly True or False.",
                field="allow_writeback",
                value=repr(allow_writeback),
            )
        self._wait_for_evictions()
        previous = self._read_view_token
        pages: frozenset[tuple[str, PageIndex]] | None = None
        files: frozenset[str] = frozenset()
        if changed_pages is not None:
            pages, files = _read_view_targets(changed_pages, changed_files)
        if unfenced_file is not None:
            unfenced_file = _require_read_view_file("unfenced_file", unfenced_file)

        # A token says nothing about this file: a vector-space/catalog save can move it without
        # appending WAL.  Same-token views remain free for every fenced file, but not for this
        # explicitly unfenced one.
        if token is not None and token == previous:
            if unfenced_file is None:
                return False
            return bool(
                self._discard_clean_changes(
                    pages=frozenset(),
                    files=frozenset({unfenced_file}),
                )
            )

        # An own commit may retain every resident frame: those objects are the state this pool
        # just published. A detached cold read is different -- it may have captured device bytes
        # immediately before that commit. Revoke only those in-flight results so their loaders
        # re-read the new view; do not move an epoch or throw away the proved resident cache.
        for loading in self._loads.values():
            loading.valid = False

        own_proved = (
            own
            and previous is not None
            and not any(
                resident is not None and resident.page.dirty
                for key in self._dirty_candidate_keys()
                if key[0] != unfenced_file
                for resident in (self._frames.get(key),)
            )
        )
        if own_proved:
            if self._metrics_active():
                self._metrics.increment(
                    READ_VIEW_DROPS_TOTAL, 1.0, {"view_origin": "own"}
                )
            if unfenced_file is None:
                self._read_view_token = token
                return False
            dropped = self._discard_clean_changes(
                pages=frozenset(),
                files=frozenset({unfenced_file}),
            )
            self._read_view_token = token
            return bool(dropped)

        partial_proved = (
            pages is not None
            and token is not None
            and previous is not None
            and expected_previous is not _READ_VIEW_BASELINE_UNSET
            and expected_previous == previous
        )
        if not partial_proved:
            baseline_mismatch = (
                pages is not None
                and previous is not None
                and expected_previous is not _READ_VIEW_BASELINE_UNSET
                and expected_previous != previous
            )
            if baseline_mismatch:
                # Applying a late partial proof to a newer local view could omit the intervening
                # change. Refuse dirty state and take a zero-write full refresh instead.
                self._discard_clean_changes(
                    pages=frozenset(),
                    files=frozenset(),
                    every_file=True,
                )
            elif allow_writeback:
                # No bounded proof is available. Preserve the established full-refresh
                # semantics, including publication of legitimate local dirty work. Clean pinned
                # readers are made discard-only by _invalidate before they can mutate late.
                self._invalidate(None, doom_pinned=True)
            else:
                # Observational maintenance must not turn freshness into an implicit flush.
                # The all-target preflight refuses before descriptor identity, cache state or
                # the read-view token can move.
                self._discard_clean_changes(
                    pages=frozenset(),
                    files=frozenset(),
                    every_file=True,
                )
        else:
            if unfenced_file is not None:
                files = files | {unfenced_file}
            self._discard_clean_changes(
                pages=frozenset(key for key in pages if key[0] not in files),
                files=files,
            )
        self._read_view_token = token
        if self._metrics_active():
            self._metrics.increment(
                READ_VIEW_DROPS_TOTAL, 1.0, {"view_origin": "foreign"}
            )
        return True

    @_guarded
    def read_view_token(self) -> object:
        """Return the exact token currently attached to resident frames.

        TransactionManager uses this only while its participant section is held, immediately
        before deriving a bounded WAL delta. Exposing the value through the pool keeps one owner
        of the token and avoids a second process-local cache whose drift would turn a partial
        interval into a false proof.
        """

        return self._read_view_token

    def _discard_clean_changes(
        self,
        *,
        pages: frozenset[tuple[str, PageIndex]],
        files: frozenset[str],
        every_file: bool = False,
    ) -> int:
        """Discard one proved foreign change-set atomically and without write-back.

        The caller already owns the pool guard. File targets dominate page targets. Every
        resident and previously doomed target is proved clean before anything moves, so a late
        dirty target cannot leave a partially advanced read view. Clean pinned frames are doomed
        and marked discard-only; their eventual release can never overwrite the foreign commit.
        """

        if every_file:
            self._wait_for_evictions()
        else:
            for changed_file in files | {name for name, _page_index in pages}:
                self._wait_for_evictions(changed_file)

        target_keys: set[tuple[str, PageIndex]] = set()
        if every_file:
            target_keys.update(self._frames)
            target_keys.update(self._doomed)
        else:
            for key in pages:
                if key in self._frames or key in self._doomed:
                    target_keys.add(key)
            if files:
                target_keys.update(key for key in self._frames if key[0] in files)
                target_keys.update(key for key in self._doomed if key[0] in files)

        for file, page_index in target_keys:
            resident = self._frames.get((file, page_index))
            candidates = (
                *((resident,) if resident is not None else ()),
                *self._doomed.get((file, page_index), ()),
            )
            for frame in candidates:
                if not frame.page.dirty or frame.discard_unwritten:
                    continue
                raise GrafxUnsupportedOperation(
                    f"Page {page_index} of {file!r} is dirty and cannot be discarded for a "
                    "foreign refresh without losing or writing local work.",
                    field="dirty",
                    file=file,
                    page=page_index,
                )

        changed_names = files | {file for file, _page_index in pages}
        if every_file:
            # A baseline mismatch is a full refresh even though its clean-only path does not
            # call _invalidate(None).  Advance the optional local descriptor proof only after
            # every dirty refusal has passed and before any frame can leave the old view.
            self._invalidate_descriptor_identity(None)
        else:
            # A bounded WAL delta does not advance the global generation.  It does, however,
            # prove exactly which logical names changed.  Reprove only those names before their
            # frames move so a canonical index displaced by another speculative participant
            # cannot keep serving its now-orphaned descriptor into pre-WAL validation.
            for changed_name in sorted(changed_names):
                self._invalidate_descriptor_identity(changed_name)

        dropped = 0
        for key in target_keys:
            resident = self._frames.get(key)
            if resident is not None:
                dropped += 1
                if resident.pins:
                    resident.doomed = True
                    resident.discard_unwritten = True
                    self._doomed.setdefault(key, []).append(resident)
                del self._frames[key]
            retained = self._doomed.get(key)
            if retained is not None:
                for frame in tuple(retained):
                    if frame.pins:
                        frame.discard_unwritten = True
                    else:
                        retained.remove(frame)
                if not retained:
                    del self._doomed[key]
            self._refresh_dirty_candidate(key)

        if every_file:
            self._bump_every_file_drop_epoch()
        else:
            for file in sorted(changed_names):
                self._bump_drop_epoch(file)
        if dropped:
            self._signal_flight_state()
            self._report_usage()
        return dropped

    @_guarded
    def discard(self, file: str, page_index: PageIndex) -> bool:
        """Forget one resident page WITHOUT writing it back, and say whether a frame was dropped.

        This is the opposite of :meth:`invalidate` on purpose. Invalidating forgets what was READ
        and keeps what was written; discarding forgets what an attempt WROTE that never became a
        commit. A row written into a page by a commit that was then refused sits in this pool as
        a dirty frame holding a picture of the page at the moment of the refused attempt. Written
        back -- by the next flush, or by a read view dropping frames -- it would land over a page
        another participant has since committed and put on the device, and their rows would be
        gone with ``verify()`` agreeing. So the frame is dropped unwritten; the device, which
        never saw the attempt, is the truth the next pin re-reads.

        A pinned frame cannot be dropped: its holder still has the object. It is marked CLEAN
        instead, which is the half of the guarantee that matters -- nothing will write it back --
        and the holder keeps a page whose abandoned versions the restamp has already made
        invisible. Returns True when the frame was actually dropped.
        """
        _require_page_index("page_index", page_index)
        key = (file, page_index)
        self._wait_for_evictions(file, page_index)
        loading = self._loads.get(key)
        if loading is not None:
            # The detached read is not a resident frame, so it does not change the return value;
            # it nevertheless lost publication authority at this discard boundary.
            loading.valid = False
        for doomed in self._doomed.pop(key, ()):
            doomed.page.dirty = (
                False  # an abandoned attempt's bytes, never written back
            )
        frame = self._frames.get(key)
        if frame is None:
            self._refresh_dirty_candidate(key)
            return False
        if frame.pins:
            frame.page.dirty = False
            self._refresh_dirty_candidate(key)
            return False
        del self._frames[key]
        self._refresh_dirty_candidate(key)
        self._reclaim(file, page_index)
        self._bump_drop_epoch(file)
        self._signal_flight_state()
        self._report_usage()
        return True

    @_guarded
    def discard_clean_file(self, file: str) -> int:
        """Forget every clean frame of ``file`` without writing a byte.

        A foreign freshness certificate says the device moved underneath this cache.  Calling
        :meth:`invalidate` in response would be unsafe: invalidate writes dirty frames first,
        and a stale local frame can therefore overwrite the very foreign state that caused the
        refresh. This narrower door first proves that *all* resident and doomed frames of the
        file are clean, then drops unpinned frames and dooms pinned ones atomically. A holder
        keeps its exact Page object and releases it through ``unpin(page=...)``; the last release
        discards those stale bytes without write-back. Any dirty frame refuses before a frame,
        allocation claim, reuse claim, or epoch moves.
        """
        self._wait_for_evictions(file)
        targets = [
            (key, frame) for key, frame in self._frames.items() if key[0] == file
        ]
        doomed = [
            (key, frame)
            for key, frames in self._doomed.items()
            if key[0] == file
            for frame in frames
        ]
        for (name, page_index), frame in (*targets, *doomed):
            if frame.page.dirty and not frame.discard_unwritten:
                raise GrafxUnsupportedOperation(
                    f"Page {page_index} of {name!r} is dirty and cannot be discarded for a "
                    "foreign refresh without losing or writing local work.",
                    field="dirty",
                    file=name,
                    page=page_index,
                )
        for key, frame in targets:
            if frame.pins:
                frame.doomed = True
                frame.discard_unwritten = True
                self._doomed.setdefault(key, []).append(frame)
            del self._frames[key]
        for key, frame in doomed:
            frames = self._doomed.get(key)
            if frames is not None:
                if frame.pins:
                    frame.discard_unwritten = True
                else:
                    frames.remove(frame)
                    if not frames:
                        del self._doomed[key]
        for key, _frame in (*targets, *doomed):
            self._refresh_dirty_candidate(key)
        # Allocation claims survive the cache drop. Clearing them here strands their all-zero
        # device pages so close can no longer settle them. Reuse and settlement each re-read the
        # device first, retiring a claim if a foreign participant gave that page meaning.
        self._bump_drop_epoch(file)
        self._signal_flight_state()
        self._report_usage()
        return len(targets)

    @_guarded
    def discard_clean_page(self, file: str, page_index: PageIndex) -> bool:
        """Forget clean frames for one page without write-back, atomically or not at all.

        Header transitions use this after a fresh device observation so a cached page 0 cannot
        overwrite a foreign flag. The whole-file variant rebases readers; this narrow variant
        permits a writer to retain unrelated dirty bucket pages. A clean pinned frame is doomed
        rather than refused: its holder keeps the Page object until ``unpin(page=...)``, while
        the stale bytes can no longer be found by a new pin or written on release.
        """
        _require_page_index("page_index", page_index)
        key = (file, page_index)
        self._wait_for_evictions(file, page_index)
        frames: list[_Frame] = []
        resident = self._frames.get(key)
        if resident is not None:
            frames.append(resident)
        doomed = list(self._doomed.get(key, ()))
        frames.extend(doomed)
        for frame in frames:
            if frame.page.dirty and not frame.discard_unwritten:
                raise GrafxUnsupportedOperation(
                    f"Page {page_index} of {file!r} is dirty and cannot be discarded for a "
                    "foreign refresh without losing or writing local work.",
                    field="dirty",
                    file=file,
                    page=page_index,
                )
        dropped = resident is not None
        if resident is not None:
            if resident.pins:
                resident.doomed = True
                resident.discard_unwritten = True
                self._doomed.setdefault(key, []).append(resident)
            del self._frames[key]
        retained = self._doomed.get(key)
        if retained is not None:
            for frame in doomed:
                if frame.pins:
                    frame.discard_unwritten = True
                else:
                    retained.remove(frame)
            if not retained:
                del self._doomed[key]
        self._refresh_dirty_candidate(key)
        # Keep any allocation claim until reuse or close revalidates the device image. Dropping
        # it here would leave an abandoned all-zero page permanently unverifiable.
        self._bump_drop_epoch(file)
        self._signal_flight_state()
        self._report_usage()
        return dropped

    @_guarded
    def invalidate(self, file: str | None = None) -> None:
        """Drop the cached pages of the file, or of the whole pool, forcing a re-read.

        Dirty pages are written before they are dropped: invalidating is about forgetting what
        was read, never about discarding what was written. A pinned page cannot be dropped at
        all, because its holder still has the object in its hands, and the refusal comes before
        anything is dropped, so a pool that refuses to invalidate is left exactly as it was.
        """
        self._invalidate(file, doom_pinned=False)

    def _invalidate(self, file: str | None, *, doom_pinned: bool) -> None:
        """Forget cached pages; refuse a pinned one, or DOOM it, as the caller decided.

        A read view is the caller that dooms. It is taken at ``begin()``, inside the participant
        section, and a searching or scanning thread of the same participant may be holding a
        page pinned outside any section at that moment. Refusing would fail that ``begin`` --
        and every thread's next ``begin`` -- for as long as anyone is reading, which turned a
        concurrent reader into a writer's refusal. Dooming keeps the holder's object for the
        holder alone: the frame leaves the table so the next pin reads the device, and the last
        release drops it. Explicit ``invalidate`` keeps refusing, because its callers mean it.
        """
        self._wait_for_evictions(file)
        targets = [
            (key, frame)
            for key, frame in self._frames.items()
            if file is None or key[0] == file
        ]
        if not doom_pinned:
            for (name, page_index), frame in targets:
                if frame.pins:
                    raise GrafxUnsupportedOperation(
                        f"Page {page_index} of {name!r} is pinned {frame.pins} times and cannot "
                        f"be invalidated.",
                        file=name,
                        page=page_index,
                        pins=frame.pins,
                    )
        # A generation-mode local adapter must prove a replaced inode before any dirty frame is
        # written back through its cached descriptor.  The preflight above remains atomic: a
        # pinned refusal does not move either frames or descriptor identity state.
        self._invalidate_descriptor_identity(file)
        for (name, page_index), frame in targets:
            if frame.pins:
                frame.doomed = True
                # A frame that was clean at the foreign read-view boundary contains only an old
                # observation. Its holder may mutate the Page object after this call and release
                # it as dirty; marking it now prevents that late stale write from overwriting the
                # commit which caused the refresh. A frame already dirty is legitimate local
                # work under the established full-refresh contract and remains publishable.
                if doom_pinned and not frame.page.dirty:
                    frame.discard_unwritten = True
                self._doomed.setdefault((name, page_index), []).append(frame)
                del self._frames[(name, page_index)]
                self._refresh_dirty_candidate((name, page_index))
                continue
            if frame.page.dirty:
                self._write_back(name, page_index, frame.page)
            del self._frames[(name, page_index)]
            self._refresh_dirty_candidate((name, page_index))
        # Announced before anything else could read the epoch, and never derived from what
        # happened to be resident: a file with no cached page is exactly the case where the next
        # read comes from the device, so it is the case that needs saying most.
        #
        # The two spellings of this argument are two different statements, not one statement with
        # a parameter (A66.1). Naming a file says that file changed. Naming none says every file
        # did -- including files this pool has never touched, which a per-file counter cannot
        # reach and which iterating the frames or the counters silently skipped. Asking for
        # everything must never be weaker than asking for one thing.
        if file is None:
            self._bump_every_file_drop_epoch()
        else:
            self._bump_drop_epoch(file)
        self._signal_flight_state()
        self._report_usage()

    def _invalidate_descriptor_identity(self, file: str | None) -> None:
        """Invoke the adapter-only descriptor cache capability when one is present."""
        invalidate = getattr(self._storage, "invalidate_descriptor_identity", None)
        if callable(invalidate):
            invalidate(file)

    # --- internals ---------------------------------------------------------------------------

    def _load_epoch(self, file: str) -> tuple[int, int]:
        """Return the two monotonic authorities a detached load must still match."""

        return (self.structure_epoch(file), self.cache_drop_epoch(file))

    def _occupied_slots(self) -> int:
        """Return resident plus atomically reserved frame slots under the pool guard."""

        # Every dirty eviction is paired atomically with exactly one load reservation: the
        # reservation owns the victim's still-occupied slot until write-back completes, then owns
        # the same slot for its read. Counting both maps would double-charge that one page.
        return len(self._frames) + len(self._loads)

    def _signal_flight_state(self) -> None:
        """Move the bounded wait generation and wake waiters while holding the pool guard."""

        self._flight_state_epoch += 1
        condition = self._condition
        if condition is not None:
            condition.notify_all()

    @staticmethod
    def _refuse_own_flight(
        owner: int,
        flight_owner: int,
        file: str,
        page_index: PageIndex,
        *,
        operation: str,
    ) -> None:
        """Refuse a callback that would wait for the load/eviction it is executing."""

        if owner != flight_owner:
            return
        raise GrafxUnsupportedOperation(
            f"A re-entrant buffer callback tried to wait for its own {operation} of page "
            f"{page_index} in {file!r}.",
            field="buffer_flight_reentrant",
            operation=operation,
            file=file,
            page=page_index,
            retryable=False,
        )

    def _refuse_capacity_wait_on_self(
        self, owner: int, file: str, page_index: PageIndex
    ) -> None:
        """Prevent a nested callback from waiting for a slot reserved by its own stack."""

        if not any(load.owner == owner for load in self._loads.values()) and not any(
            eviction.owner == owner for eviction in self._evictions.values()
        ):
            return
        raise GrafxUnsupportedOperation(
            f"A re-entrant buffer callback cannot reserve page {page_index} of {file!r}: "
            "its own outer load or eviction currently owns the only available capacity.",
            field="buffer_flight_reentrant",
            operation="capacity",
            file=file,
            page=page_index,
            retryable=False,
        )

    def _wait_for_evictions(
        self,
        file: str | None = None,
        page_index: PageIndex | None = None,
    ) -> None:
        """Wait until relevant detached dirty writes settle, without waiting on a load.

        A cache invalidation is allowed to overtake a read-only load -- its epoch then rejects the
        stale publication.  It must not overtake a dirty eviction, because that detached frame is
        still publishing local work.  Waiting only for evictions preserves that distinction.
        """

        condition = self._condition
        if condition is None:
            return
        owner = condition.thread_token()

        def _relevant() -> tuple[tuple[str, PageIndex], _PageEviction] | None:
            for key, eviction in self._evictions.items():
                if file is not None and key[0] != file:
                    continue
                if page_index is not None and key[1] != page_index:
                    continue
                return key, eviction
            return None

        while (found := _relevant()) is not None:
            key, eviction = found
            self._refuse_own_flight(
                owner,
                eviction.owner,
                key[0],
                key[1],
                operation="eviction",
            )
            condition.wait_for(lambda: self._evictions.get(key) is not eviction)

    def _wait_for_loads(self) -> None:
        """Wait for every detached read before entering an external page-write section."""

        condition = self._condition
        if condition is None:
            return
        owner = condition.thread_token()
        while self._loads:
            key, load = next(iter(self._loads.items()))
            self._refuse_own_flight(
                owner,
                load.owner,
                key[0],
                key[1],
                operation="load",
            )
            condition.wait_for(lambda: self._loads.get(key) is not load)

    def _budget_failure(
        self, file: str, page_index: PageIndex
    ) -> GrafxBufferBudgetExceeded:
        """Build the compatible typed admission refusal without invoking any collaborator."""

        return GrafxBufferBudgetExceeded(
            f"Database {self._db_label!r} holds {len(self._frames)} pinned pages of "
            f"{self._page_size} bytes and cannot admit page {page_index} of {file!r} "
            f"within its budget of {self._budget_bytes} bytes.",
            db=self._db_label,
            file=file,
            page=page_index,
            budget_bytes=self._budget_bytes,
            used_bytes=self.used_bytes(),
        )

    def _evict_dirty_frame(
        self,
        key: tuple[str, PageIndex],
        eviction: _PageEviction,
        target_key: tuple[str, PageIndex],
        load: _PageLoad,
    ) -> None:
        """Write one detached victim outside the guard, then atomically settle its slot."""

        file, page_index = key
        frame = eviction.frame
        try:
            self._publish_page(file, page_index, frame.page, frame=frame)
        except BaseException:
            with self._guard:
                if self._evictions.get(key) is eviction:
                    del self._evictions[key]
                    if self._loads.get(target_key) is load:
                        del self._loads[target_key]
                    # The ticket kept every sanctioned mutation of this key away. Restore the
                    # victim at the LRU end it occupied before the failed write-back.
                    self._frames[key] = frame
                    self._frames.move_to_end(key, last=False)
                    self._signal_flight_state()
            raise

        with self._guard:
            if self._evictions.get(key) is eviction:
                try:
                    self._remember_write_back(file, page_index, key=key)
                    target_epoch = (
                        self._load_epoch(target_key[0])
                        if self._loads.get(target_key) is load
                        else None
                    )
                except BaseException as failure:
                    # The page publication already succeeded.  Its modified marker was reserved
                    # before detachment, so abandon only the target admission and release every
                    # waiter.  A retry may read the target afresh; no dirty work or flight remains
                    # stranded behind this bookkeeping failure.
                    del self._evictions[key]
                    if self._loads.get(target_key) is load:
                        del self._loads[target_key]
                    try:
                        self._signal_flight_state()
                    except BaseException as signal_failure:
                        failure.add_note(
                            "Buffer-flight notification also failed after publication with "
                            f"{type(signal_failure).__name__}: {signal_failure}"
                        )
                    raise
                del self._evictions[key]
                if self._loads.get(target_key) is load:
                    # No target bytes have been read yet. Rebase the certificate after the
                    # potentially slow victim write, while preserving an explicit discard's
                    # ``valid=False`` revocation.
                    assert target_epoch is not None
                    load.epoch = target_epoch
                self._signal_flight_state()

    def _make_room(self, file: str, page_index: PageIndex) -> None:
        """Evict until one more frame fits in the budget, or refuse the request."""
        while (self._occupied_slots() + 1) * self._page_size > self._budget_bytes:
            victim = self._find_victim()
            if victim is None:
                condition = self._condition
                if condition is not None and (self._loads or self._evictions):
                    owner = condition.thread_token()
                    self._refuse_capacity_wait_on_self(owner, file, page_index)
                    observed = self._flight_state_epoch
                    condition.wait_for(lambda: self._flight_state_epoch != observed)
                    continue
                if self._metrics_active():
                    self._metrics.increment(
                        BUFFER_BUDGET_EXCEEDED_TOTAL, 1.0, self._labels
                    )
                raise self._budget_failure(file, page_index)
            name, victim_index = victim
            frame = self._frames[victim]
            if frame.page.dirty:
                self._write_back(name, victim_index, frame.page)
            del self._frames[victim]
            self._refresh_dirty_candidate(victim)

    def _find_victim(self) -> tuple[str, PageIndex] | None:
        """Return the least recently used unpinned frame, or None when everything is pinned."""
        for key, frame in self._frames.items():
            if frame.pins == 0:
                return key
        return None

    def _read_page(self, file: str, page_index: PageIndex) -> Page:
        """Read one page, applying the torn-read protocol of CONTRACT.md section 6.3."""
        page, _witness = self._read_page_observation(
            file,
            page_index,
            previous=None,
            produce_witness=False,
        )
        assert page is not None
        return page

    def _read_page_observation(
        self,
        file: str,
        page_index: PageIndex,
        *,
        previous: object | None,
        produce_witness: bool,
    ) -> tuple[Page | None, _FreshPageWitness | None]:
        """Run the canonical read protocol, optionally reusing an exact raw-image proof."""
        known = (
            previous
            if isinstance(previous, _FreshPageWitness)
            and previous.seal is _FRESH_PAGE_WITNESS_SEAL
            and previous.owner is self
            and previous.file == file
            and previous.page_index == page_index
            else None
        )
        failure: GrafxCorruptionDetected | None = None
        for attempt in range(TORN_READ_RETRY_BUDGET + 1):
            raw = self._storage.read_page(file, page_index)
            if known is not None and raw == known.image:
                return None, known
            if is_unwritten_image(raw, self._page_size):
                # The device zero-fills what it allocates, so an image of nothing but zeros is a
                # page nobody has written yet: free, not damaged. Spending the retry budget on it
                # and then declaring corruption would turn the ordinary gap between a page
                # allocation and the write that follows it into a false integrity incident.
                page = Page(
                    int(PageType.FREE),
                    page_size=self._page_size,
                    page_index=page_index,
                )
                witness = (
                    _FreshPageWitness(
                        seal=_FRESH_PAGE_WITNESS_SEAL,
                        owner=self,
                        file=file,
                        page_index=page_index,
                        image=bytes(raw),
                    )
                    if produce_witness
                    else None
                )
                return page, witness
            if self._metrics_active():
                self._metrics.increment(
                    CHECKSUM_VERIFICATIONS_TOTAL, 1.0, self._page_labels
                )
            try:
                page = self._codec.decode_page(raw, verify=True)
            except GrafxCorruptionDetected as detected:
                failure = detected
                if self._metrics_active():
                    self._metrics.increment(
                        CHECKSUM_FAILURES_TOTAL, 1.0, self._page_labels
                    )
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
            witness = (
                _FreshPageWitness(
                    seal=_FRESH_PAGE_WITNESS_SEAL,
                    owner=self,
                    file=file,
                    page_index=page_index,
                    image=bytes(raw),
                )
                if produce_witness
                else None
            )
            return page, witness
        raise GrafxCorruptionDetected(
            f"Page {page_index} of {file!r} could not be read after "
            f"{TORN_READ_RETRY_BUDGET + 1} attempts.",
            file=file,
            page=page_index,
            attempts=TORN_READ_RETRY_BUDGET + 1,
            cause=None if failure is None else failure.message,
        )

    def _reclaim(self, file: str, page_index: PageIndex) -> None:
        """Take a page an attempt allocated and abandoned back for reuse, if that is what it is.

        The narrow condition is the whole safety argument. Only a page THIS pool grew the file
        for, and which no write-back has since put on the device, is reclaimed: such a page has
        never been readable by anybody, so nothing can hold a reference to it. A page that was
        already on the device is discarded here like any other and is NOT reclaimed, because a
        discard forgets what an attempt wrote and says nothing about what the page was before.

        Handing the index out again cannot collide with another participant. The file never
        shrinks (G6) and ``StorageDevice.allocate`` derives its index from the file length, so
        every index any participant is ever given is at or past the length at that moment and no
        other participant can be handed one this pool already holds. (``BufferPool.allocate``
        itself no longer has that property, which is the whole point of it -- the claim is about
        the device door underneath.)
        """
        key = (file, page_index)
        if key not in self._grown:
            return
        self._grown.discard(key)
        if page_index == HEADER_PAGE_INDEX:
            # The reserved header page of every paged file (A2), which _require_reserved_header_page
            # and CatalogStore._release both refuse by name. It cannot arrive here today -- the
            # first begin() invalidates the whole pool and writes page 0 back, which takes it out
            # of _grown for good, and a probe over the whole suite saw 73 reclaims of the heap and none of
            # them page 0. That is soundness by scheduling; this is soundness by construction, and the
            # two cost the same.
            return
        self._abandoned.setdefault(file, []).append(page_index)

    def _owning_frame(
        self, file: str, page_index: PageIndex, page: Page
    ) -> _Frame | None:
        """Return the live/doomed frame that owns ``page``, if this is a framed write."""
        key = (file, page_index)
        resident = self._frames.get(key)
        if resident is not None and resident.page is page:
            return resident
        for frame in self._doomed.get(key, ()):
            if frame.page is page:
                return frame
        eviction = self._evictions.get(key)
        if eviction is not None and eviction.frame.page is page:
            return eviction.frame
        return None

    def _write_back(self, file: str, page_index: PageIndex, page: Page) -> None:
        """Encode the page and hand it to the device, advancing its sequence counter.

        The counter always lands on an even value and always moves forward: from an even one it
        gains two, from an odd one it gains one. Adding two would have preserved the parity it
        found, so a page that ever acquired an odd counter would stay unreadable for good, and
        amendment A21 says a durable image carries an even counter without exception.
        """
        frame = self._owning_frame(file, page_index, page)
        self._publish_page(file, page_index, page, frame=frame)
        self._remember_write_back(file, page_index)

    def _publish_page(
        self,
        file: str,
        page_index: PageIndex,
        page: Page,
        *,
        frame: _Frame | None,
    ) -> None:
        """Perform only the page/codec/device publication half of write-back.

        Normal write doors invoke this while already guarded. Dirty cold-miss eviction invokes it
        for an unpinned, detached frame after releasing the pool guard; its ticket gives this
        exact object exclusive ownership until the caller settles bookkeeping under the guard.
        """

        page.page_index = page_index
        fenced = page_index == HEADER_PAGE_INDEX and self._page_sequence_fence(
            file, page_index
        )
        section = (
            self._page_write_section(file, page_index) if fenced else nullcontext()
        )
        with section:
            publish_base = page.seq
            if fenced:
                # Page 0 is the cross-process freshness clock.  The section makes this a real
                # compare-and-swap rather than two racing reads, and the frame's admission base
                # distinguishes a legitimate newer redo image from a stale cached writer.
                if frame is None:
                    raise GrafxUnsupportedOperation(
                        f"Page 0 of {file!r} has no owning frame, so its device base is unknown.",
                        field="page_sequence_base",
                        file=file,
                        page=page_index,
                        retryable=False,
                    )
                # The page-zero CAS is a correctness fence, not a bulk traversal.  Reprove the
                # exact logical name before comparing its device clock so a detached old inode
                # cannot validate a write whose namespace now points somewhere else.
                self._invalidate_descriptor_identity(file)
                fresh = self._read_page(file, page_index)
                if fresh.seq != frame.device_base_seq:
                    raise GrafxUnsupportedOperation(
                        f"Page 0 of {file!r} changed from sequence "
                        f"{frame.device_base_seq} to {fresh.seq} in another participant.",
                        field="page_sequence_conflict",
                        file=file,
                        page=page_index,
                        cached_seq=frame.device_base_seq,
                        device_seq=fresh.seq,
                        retryable=True,
                    )
                # Redo may install a logged image whose seq is ahead of the current device.  It
                # is legitimate and supplies the publishing base; a lower image must still move
                # strictly beyond the device clock, hence max().
                publish_base = max(page.seq, fresh.seq)
                if publish_base >= MAX_SEQ - 1:
                    raise GrafxUnsupportedOperation(
                        f"Page 0 of {file!r} exhausted its non-wrapping sequence clock at "
                        f"{publish_base}.",
                        field="page_sequence_exhausted",
                        file=file,
                        page=page_index,
                        seq=publish_base,
                        retryable=False,
                    )
            previous_seq = page.seq
            page.seq = next_seq(publish_base)
            try:
                image = self._codec.encode_page(page)
                self._storage.write_page(file, page_index, image)
            except BaseException:
                # If the device completed and then raised, the next CAS observes the advanced
                # sequence and fails closed.  Restoring the object prevents an unconfirmed write
                # from becoming the base of another publication.
                page.seq = previous_seq
                raise
            if frame is not None:
                frame.device_base_seq = page.seq
        page.dirty = False

    def _remember_write_back(
        self,
        file: str,
        page_index: PageIndex,
        *,
        key: tuple[str, PageIndex] | None = None,
    ) -> None:
        """Settle pool-owned bookkeeping after a page publication has succeeded."""

        # Remembered, not forgotten. Clearing the dirty flag is what used to take an evicted
        # page out of modified_pages() and out of the log with it; see that method.
        self._modified.add((file, page_index))
        # Written is real. A page whose image is on the device may be referenced by anything
        # that has read it since, so it stops being a page this pool may hand out again -- and
        # that has to withdraw it from BOTH lists. Withdrawing it only from _grown left a page
        # that had already been reclaimed still standing on the reuse list, where the safety
        # check that put it there is never re-taken, so allocate() would hand out an index whose
        # image was by then real and write a blank page over it.
        # A detached dirty eviction already owns this tuple. Reusing it avoids making the
        # successful-publication settlement depend on one more allocation after the device write.
        if key is None:
            key = (file, page_index)
        self._grown.discard(key)
        waiting = self._abandoned.get(file)
        if waiting is not None and page_index in waiting:
            waiting.remove(page_index)
            if not waiting:
                del self._abandoned[file]
        self._refresh_dirty_candidate(key)

    def _usage_reading(self) -> tuple[float, float] | None:
        """Capture callback-free usage under the guard, or None when telemetry is disabled."""

        if not self._metrics_active():
            return None
        return (float(self.used_bytes()), float(self._retained_bytes_estimate()))

    def _metrics_active(self) -> bool:
        """Honor a sink switched off after assembly without enabling an unsafe late opt-in."""

        return self._metrics_enabled and self._metrics.enabled

    def _emit_usage(self, reading: tuple[float, float]) -> None:
        """Emit a previously captured usage pair; callers hold no pool guard."""

        used, retained = reading
        self._metrics.set_gauge(BUFFER_BUDGET_USED_BYTES, used, self._labels)
        self._metrics.set_gauge(
            BUFFER_RETAINED_ESTIMATE_BYTES,
            retained,
            self._retained_labels,
        )

    def _report_usage(self) -> None:
        """Publish the resident bytes of this database under its own label."""
        reading = self._usage_reading()
        if reading is not None:
            self._emit_usage(reading)

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


def _require_reserved_header_page(pool: BufferPool, file: str) -> None:
    """Refuse a chain in a file whose very first allocation would hand out page 0.

    Page 0 is the reserved header page of every paged file (A2), and a chunk written over it
    takes the file header with it. A file that exists and holds nothing at all is the one case
    where an allocation reaches it, so it is closed before anything is touched.
    """
    if pool.storage.exists(file) and pool.storage.page_count(file) == 0:
        raise GrafxCorruptionDetected(
            f"The file {file!r} has no reserved header page, so the first page a chain "
            f"allocated would be page {HEADER_PAGE_INDEX}.",
            file=file,
            page=HEADER_PAGE_INDEX,
        )


def _require_distinct_reusable_pages(file: str, reuse: tuple[PageIndex, ...]) -> None:
    """Refuse a reuse list that names page 0, or names any page more than once.

    Written once and reached from both chain doors -- the one that writes through the pool and
    the one that only builds images -- because the harm is a property of the LIST and not of what
    the caller then does with it. Each door reaches it on its own path, so a mutation that drops
    the call from either is a failure of that door's own tests (A67).
    """
    seen_reuse: set[PageIndex] = set()
    for candidate in reuse:
        _require_page_index("reuse", candidate)
        if candidate in seen_reuse:
            # Each chunk clears its page before writing, so a page named twice keeps only the
            # last chunk written to it and the call returns carrying a fraction of the payload
            # with no error at all. A caller that means to write one chain has to name distinct
            # pages; anything else is a bug in the caller, not a chain this can write.
            raise GrafxCorruptionDetected(
                f"The reuse list for a chain in {file!r} names page {candidate} more than once, "
                f"so the chain would carry less than it was given.",
                file=file,
                page=candidate,
                field="reuse",
            )
        seen_reuse.add(candidate)
        if candidate == HEADER_PAGE_INDEX:
            raise GrafxCorruptionDetected(
                f"Page {HEADER_PAGE_INDEX} of {file!r} is the reserved header page and cannot "
                f"be reused as a chain page.",
                file=file,
                page=candidate,
            )


def _require_chain_file(pool: BufferPool, file: str) -> None:
    """Refuse a chain in a file that does not exist, whose first page would be page 0."""
    if not pool.storage.exists(file):
        raise GrafxCorruptionDetected(
            f"The file {file!r} does not exist, so a chain written into it would start at page "
            f"{HEADER_PAGE_INDEX}, which is reserved for the file header.",
            file=file,
            page=HEADER_PAGE_INDEX,
        )


def build_chain_images(
    pool: BufferPool,
    file: str,
    payload: bytes,
    *,
    page_type: int = int(PageType.OVERFLOW),
    reuse: tuple[PageIndex, ...] = (),
) -> tuple[tuple[PageIndex, bytes], ...]:
    """Return the images a chain of this payload needs, WITHOUT writing or allocating anything.

    The offline twin of :func:`write_chain`. It answers the same question -- which pages does
    this payload occupy and what does each of them hold -- and answers it as a value the caller
    can hand to a transaction, discard, or apply later, instead of as a mutation of the file.

    That difference is the whole point. ``write_chain`` makes the change REACHABLE the moment it
    runs: the pages are in the buffer pool, and the next flush of that file carries them to the
    device whether or not the caller that asked for them was ever allowed to commit. A change
    that can still be refused must not be reachable, which is the rule the row path already obeys
    and the rule a catalog change had no way to obey.

    Pages beyond the reuse list are given the page numbers the next allocations WOULD hand out
    (``page_count``, ``page_count + 1``, ...) and the file is deliberately NOT grown. Growing it
    is what makes a refused change visible on the device -- a longer file is a changed file --
    and it is not needed: the redo rule of :func:`apply_page_image` grows a file to reach a page
    an image names, so the commit that applies these images allocates exactly the pages the
    change turned out to need, and a commit that never happens allocates none.

    A page offered for reuse must already exist, because a reuse index at or past the end of the
    file would collide with one of those prospective numbers: two images for one page, of which
    the caller keeps whichever it staged last, and the chain would then carry less than it was
    given. ``write_chain`` cannot reach that shape -- it takes new page numbers from the device --
    so the guard belongs here rather than in the list check both doors share.
    """
    _require_reserved_header_page(pool, file)
    _require_distinct_reusable_pages(file, reuse)
    _require_chain_file(pool, file)
    present = pool.storage.page_count(file)
    for candidate in reuse:
        if candidate >= present:
            raise GrafxCorruptionDetected(
                f"The reuse list for a chain in {file!r} names page {candidate}, which the file "
                f"does not have; it holds {present} pages.",
                file=file,
                page=candidate,
                field="reuse",
                page_count=present,
            )
    chunks = split_payload(payload, chunk_capacity(pool.page_size))
    indices: list[PageIndex] = list(reuse[: len(chunks)])
    reused = len(indices)
    while len(indices) < len(chunks):
        indices.append(present + len(indices) - reused)
    images: list[tuple[PageIndex, bytes]] = []
    for position, index in enumerate(indices):
        page = Page(page_type, page_size=pool.page_size, page_index=index)
        page.next_page = (
            indices[position + 1] if position + 1 < len(indices) else NO_PAGE
        )
        page.insert_slot(chunks[position])
        images.append((index, pool.codec.encode_page(page)))
    return tuple(images)


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

    Page 0 can never be part of a chain: it is the reserved header page of every paged file
    (A2), and a chunk written over it takes the file header with it. Both routes to page 0 are
    closed before anything is touched -- a page 0 offered for reuse, and a file with no pages at
    all, where the first allocation would hand out page 0 and leave a stored chain image on it
    even though the write was going to be refused. A caller that asks for the impossible changes
    nothing (G6).
    """
    _require_reserved_header_page(pool, file)
    _require_distinct_reusable_pages(file, reuse)
    _require_chain_file(pool, file)
    if reuse:
        # Freshly allocated pages belong to no chain, but a page offered for reuse was part of
        # one until this call took it, and its next_page is rewritten here. Anything derived by
        # walking this file was derived before that.
        pool._bump_structure_epoch(file)
    chunks = split_payload(payload, chunk_capacity(pool.page_size))
    indices: list[PageIndex] = list(reuse[: len(chunks)])
    while len(indices) < len(chunks):
        # reuse=False, and the reason is an invariant rather than a preference.
        # _require_distinct_reusable_pages checks the CALLER's list, which cannot see an index the
        # pool injects: a reclaimed page that also stood in that list would be written twice and
        # the chain would read back short, with no refusal anywhere. No production caller can
        # reach that today -- the only non-empty reuse list comes from chain_pages(), whose pages
        # are device-backed -- but the guard's own docstring names that harm, and a guard that
        # holds by an accident of who calls it is not holding.
        page = pool.allocate(file, page_type, reuse=False)
        index = page.page_index
        pool.unpin(file, index, dirty=True)
        indices.append(index)
    for position, index in enumerate(indices):
        with pool.pinned(file, index) as page:
            page.clear()
            page.page_type = page_type
            page.next_page = (
                indices[position + 1] if position + 1 < len(indices) else NO_PAGE
            )
            page.insert_slot(chunks[position])
    return tuple(indices)


def grow_to(pool: BufferPool, file: str, page_index: PageIndex) -> int:
    """Allocate pages until the file has the requested index, and return how many it took.

    Recovery needs this because a crash can happen between the record that allocated a page and
    the write that was to fill it: the log names a page the file no longer has. That gap is small
    by construction -- it is the pages one interrupted operation had allocated.

    The index arrives from a log record, so it is disk-sourced, and a damaged one is a number
    this function would otherwise spend hours honouring: a u32 reaches two tebibytes of
    zero-filled pages at the smallest page size, and G6 forbids ever shrinking them away. An
    operation that neither fails nor completes is the shape A42 exists to prevent, so the gap is
    bounded here and a larger one is refused with both numbers named.

    The bound belongs to C1, not to C6. This is C1's door and C1 is what would do the allocating;
    C6 decides what a refused redo record MEANS -- truncation, quarantine, a forensic entry --
    and it can only decide that if this door hands it a typed refusal instead of a full disk.

    Proven by test_a_page_index_far_past_the_end_is_refused_instead_of_allocated and
    test_the_gap_bound_names_both_numbers_it_compared, with
    test_a_redo_may_bridge_a_gap_left_by_an_interrupted_allocation holding the other side so the
    bound cannot be satisfied by refusing the case it exists for (A85).
    """
    _require_page_index("page_index", page_index)
    storage = pool.storage
    try:
        present = storage.page_count(file)
    except GrafxError as failure:
        # This is the one sanctioned creation route.  Asking page_count first removes an
        # O(namespace) exact-name walk from every already-present redo page while preserving
        # the old exact spelling proof at the loop boundary.  Never turn malformed or
        # case-colliding storage into a new file.
        if failure.details.get("reason") != "missing_file":
            raise
        storage.create(file)
        present = 0
    if page_index >= present + MAX_REDO_GAP_PAGES:
        raise GrafxCorruptionDetected(
            f"A page image names page {page_index} of {file!r}, which holds {present} pages; "
            f"a single redo may bridge at most {MAX_REDO_GAP_PAGES}.",
            file=file,
            page=page_index,
            field="page_index",
            page_count=present,
            limit=MAX_REDO_GAP_PAGES,
        )
    grown = 0
    while storage.page_count(file) <= page_index:
        # reuse=False: this loop's exit condition is the FILE's length, and its return value
        # is how much the file grew. A hand-out that lengthens nothing satisfies neither.
        page = pool.allocate(file, int(PageType.FREE), reuse=False)
        pool.unpin(file, page.page_index, dirty=True)
        grown += 1
    return grown


def apply_page_image(
    pool: BufferPool,
    file: str,
    page_index: PageIndex,
    image: bytes,
    *,
    structure_signature: Callable[[Page], object] | None = None,
) -> bool:
    """Install a page image if it is newer than the page, and say whether it was applied.

    This is the redo rule of CONTRACT.md section 8.5 step 6 expressed for one page, and it is
    written once here because both the heap and the catalog owe C6 exactly the same behaviour
    (amendment A22):

    * a page the file does not have yet is grown into existence first;
    * a page that was allocated and never written is free, so the image always applies;
    * a page whose own page_lsn already covers the image is left alone, which is what makes
      replaying the same log twice produce the same file;
    * the installed image always carries an even sequence counter (A21), because an odd one
      would make the page unreadable for good.

    A store that can distinguish its structural page authority from ordinary payload may pass
    ``structure_signature`` or register the same immutable classifier with its pool for shared
    transaction/recovery callers.  The epoch then moves only when that signature changes.  A
    file with neither remains deliberately conservative: every applied image moves its epoch.
    """
    # The page index is checked by grow_to below, which every path through here reaches.
    # Checking it twice with the same predicate and the same message made neither check
    # demonstrable: break either and the other answers identically (A67).
    try:
        decoded = pool.codec.decode_page(
            _require_image(file, page_index, image), verify=True
        )
    except GrafxCorruptionDetected as damaged:
        # The codec port carries no page index, so a decode failure names no location; the
        # caller of this door knows both and C6 cannot build a finding without them.
        details = {
            key: value
            for key, value in damaged.details.items()
            if key not in {"file", "page"}
        }
        raise GrafxCorruptionDetected(
            f"Page {page_index} of {file!r} could not be applied: {damaged.message}",
            file=file,
            page=page_index,
            **details,
        ) from damaged
    if not isinstance(decoded, Page):
        raise GrafxCorruptionDetected(
            f"The codec returned a {type(decoded).__name__} instead of a page image.",
            file=file,
            page=page_index,
        )
    # The codec PORT carries no page index (section 4 is frozen and has no room for one), so a
    # page that comes back from decode_page reports index 0 whatever it was read from. The pool's
    # read path stamps what it knows; this door did not, and an object that claims to be page 0
    # while being applied to page 7 has a false field on it.
    #
    # Honestly: this stamp is NOT observable from outside C1 today. Nothing surfaces this object
    # -- replace_with keeps the TARGET page's identity by design, and the decode-failure path
    # above already re-raises with the right location -- so a mutation removing this line
    # survives the suite, and it is recorded as a survivor rather than covered by a test that
    # would only appear to prove it (CONTRACT section 13). It is set because the field is either
    # true or it is not, and the cost of keeping it true is one assignment.
    decoded.page_index = page_index
    if decoded.seq % 2:
        decoded.seq = next_seq(decoded.seq)
    effective_signature = (
        structure_signature
        if structure_signature is not None
        else pool._registered_structure_signature(file)
    )
    grow_to(pool, file, page_index)
    with pool.pinned(file, page_index) as page:
        if page.page_type != int(PageType.FREE) and page.page_lsn >= decoded.page_lsn:
            return False
        structure_changed = (
            effective_signature is None
            or effective_signature(page) != effective_signature(decoded)
        )
        page.replace_with(decoded)
        # A structural image can lengthen, shorten or re-route a chain without touching the
        # remembered tail.  The format classifier is the proof that this image did not; without
        # one the historical conservative bump remains the only safe answer.
        if structure_changed:
            pool._bump_structure_epoch(file)
        return True


def visited_pages() -> set[PageIndex]:
    """Return the set a chain walk remembers the pages it has already seen in.

    It is a named factory rather than a set literal so that a test can defeat it on purpose. The
    termination bound that sits beside every one of these sets exists precisely for the case
    where the set stops working, and while the set works the bound can never fire -- which is the
    masked-guard shape A34 is about. Defeating the set is the only way to give the bound a test
    that nothing else can satisfy.
    """
    return set()


def refuse_endless_chain(file: str, steps: int, limit: int) -> None:
    """Refuse a chain walk that has taken more steps than the file could possibly justify.

    A chain visits distinct pages, so it can never be longer than the file that holds them. This
    bound therefore cannot refuse a walk that is merely unusual, and it is not the hint-derived
    bound A40 removed: it comes from the device, which cannot be stale.

    It exists so that termination does not rest on the visited set alone. A guard that is the only
    thing ending a walk turns its own removal into a hang, and a hang is worse than a failure: it
    blocks the machine rather than the build. Because the two guards can refuse the same input,
    this one is tested at its own level, where nothing else can answer for it (A34).
    """
    if steps > limit:
        raise GrafxCorruptionDetected(
            f"The page chain of {file!r} passed {steps} pages in a file that holds "
            f"{max(limit - 1, 0)}, so it does not end.",
            file=file,
            field="chain_length",
            steps=steps,
        )


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
    visited: set[PageIndex] = visited_pages()
    # A chain visits distinct pages, so it can never be longer than the file. The bound is what
    # keeps the walk terminating even if the visited set stops working, so a broken guard is a
    # test failure rather than a hung process.
    limit = pool.storage.page_count(file) + 1
    index = first_page
    while index != NO_PAGE:
        refuse_endless_chain(file, len(chunks) + 1, limit)
        if index in visited:
            raise GrafxCorruptionDetected(
                f"The page chain of {file!r} returns to page {index}, so it is a cycle.",
                file=file,
                page=index,
                visited=len(visited),
                field="cycle",
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
