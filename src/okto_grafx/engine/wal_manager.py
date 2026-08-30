"""The segmented write-ahead log (CONTRACT.md section 8.3; SPEC-M1 FR-5, FR-6, BR-4, BR-10, AC-8, AC-9).

The log is a run of append-only segments under one directory, numbered upwards and never reused.
Records carry increasing log sequence numbers with no gaps, so the log on disk is always a
contiguous run: that single property is what lets recovery say where the good work ended, and
what lets recycling drop the oldest segments without ever leaving a hole in the middle.

Five decisions are worth stating up front, because each one is the reason a whole class of defect
cannot happen here.

**A batch is all or nothing.** ``append_many`` encodes every record, writes them with ONE append,
and if that append fails -- a full device, a short write -- it puts the segment back to the byte
it started at and raises. No log sequence number is consumed, so no number is ever written twice.
A partially stored batch is exactly the shape that produces a duplicated record after a retry,
and undoing it is cheaper than reasoning about it.

**A batch never spans two segments.** The roll decision is taken before the first byte is
written, so a torn write damages one file and the repair is one truncation. A batch larger than
``segment_bytes`` makes its segment larger than ``segment_bytes`` rather than splitting; the
caller chose the batch, and honouring it whole is what keeps the repair exact. Such a batch is
still refused before any write when its segment would exceed the reader's hard ceiling: this
writer never creates a file that this same build cannot reopen.

**Damage closes the door.** When the scan at ``open`` finds bytes that are not a record, the
manager remembers it and refuses every append with ``corruption_detected``. Appending after a
damaged tail would put good records behind garbage, where the next scan can never reach them.
The way out is ``truncate_after``, which is recovery's door and not the writer's, so quarantine
and the ledger get their evidence before anything is destroyed (FR-8, FR-10, G8).

**Recycling follows the horizon and only the horizon** (BR-10). A segment goes only when every
record it holds sits below the horizon the caller computed with
:func:`okto_grafx.engine.coordination.recyclable_horizon`, the newest segment never goes at all,
and the walk stops at the first segment that must stay. Absence of readers is not an input.

**A damaged reader record is not this component's to retire (carried finding CF-1).** C3 fails
closed when a ``.reader`` file cannot be read: ``reader_horizon`` raises and no horizon can be
computed, so this manager is asked to recycle nothing and recycles nothing. That is the correct
outcome and this component will not be given a way around it. Concretely, the position C4 takes
and asks C6 to hold the other end of:

* **C4 never retires a reader record, a lease record, or any other control-plane file.** It owns
  ``wal/`` and nothing else. A recycling pass that could delete the evidence of the reader
  blocking it is a fail-open path wearing a cleanup's clothes, and BR-10 forbids reclaiming by
  removing readers rather than by advancing the horizon.
* **An unknown horizon is not a horizon.** If the caller cannot produce one, recycling does not
  happen. The cost is bounded, visible disk growth, reported through
  ``oktografx_wal_truncation_lag_segments`` and ``oktografx_wal_segments`` on every pass, which
  is a diagnosable state rather than a silent one.
* **Retirement belongs to C6, under quarantine and ledger evidence, as a deliberate recovery
  act** -- never as a side effect of a hot path. The record is copied to quarantine with a
  manifest and a forensic ledger entry is written FIRST; only then may the damaged name be
  retired, and the operator can see afterwards exactly what was retired and why. The lease file
  is the same rule: whoever may retire a damaged reader record may retire a damaged lease record
  on the same evidence. The two differ only in the consequence of failing closed -- a damaged
  reader record blocks reclamation, a damaged lease record blocks writing altogether -- and that
  difference argues for making the C6 path exist, never for letting a writer delete the file
  that is refusing it.
"""

from __future__ import annotations

import struct
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDurabilityBarrierFailed,
    GrafxError,
    GrafxPortNotConfigured,
    GrafxSchemaVersionMismatch,
    GrafxStaleEpoch,
    GrafxTransactionStateError,
)
from okto_grafx.domain.ids import NO_LSN, PROVISIONAL_CSN, Epoch, Lsn
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.wal.codec import (
    MAGIC_BYTES,
    DecodeOutcome,
    FailureReason,
    decode_record,
)
from okto_grafx.domain.wal.record import (
    MAX_DESCRIPTOR_BYTES,
    WAL_HEADER_LENGTH,
    WalRecord,
    WalRecordType,
)
from okto_grafx.domain.wal.replay import (
    MAX_FAILURE_SAMPLE_BYTES,
    RecycleReport,
    ScanFailure,
    ScanItem,
    TruncationReport,
)
from okto_grafx.domain.wal.segment import (
    MIN_SEGMENT_NUMBER,
    SegmentInfo,
    parse_segment_number,
    recyclable_prefix,
    segment_name,
)
from okto_grafx.engine.metrics_catalog import metric

__all__ = [
    "DEFAULT_WAL_DIRECTORY",
    "MIN_SEGMENT_BYTES",
    "MAX_SEGMENT_READ_BYTES",
    "MAX_RESYNC_CANDIDATES",
    "RESYNC_CHECKSUM_BUDGET_FACTOR",
    "LSN_INDEX_STRIDE_BYTES",
    "SEGMENT_HEADER_STRUCT_FORMAT",
    "STORAGE_PORT_METHODS",
    "CLOCK_PORT_METHODS",
    "METRICS_PORT_METHODS",
    "WAL_METRICS",
    "FSYNC_DURATION_SECONDS",
    "BARRIER_FAILURES_TOTAL",
    "WAL_SIZE_BYTES",
    "WAL_SEGMENTS",
    "WAL_TRUNCATION_LAG_SEGMENTS",
    "CHECKSUM_VERIFICATIONS_TOTAL",
    "CHECKSUM_FAILURES_TOTAL",
    "SegmentHeader",
    "WalManager",
    "RecycleReport",
    "ScanFailure",
    "ScanItem",
    "SegmentInfo",
    "TruncationReport",
]

DEFAULT_WAL_DIRECTORY: str = "wal"
"""Directory of the log inside the database, as CONTRACT.md section 6.1 names it."""

MIN_SEGMENT_BYTES: int = 256
"""Smallest segment size a caller may configure.

The bound is this low so a test can force a roll every few records without the production
default having to be small. It is not a promise that a batch fits: a batch larger than the
configured size lands whole in a segment of its own, which is the rule that keeps one torn write
confined to one file.
"""

MAX_SEGMENT_READ_BYTES: int = 1024 * 1024 * 1024
"""Largest segment this manager will read into memory in one piece.

A segment is scanned whole, which is one allocation of its size and one call to the device. A
file larger than this is not a segment this log wrote -- ``segment_bytes`` is exceeded only by a
single batch, and a batch that large was already held in memory by its author -- so the refusal
names the file rather than trying to page through it.
"""

MAX_RESYNC_CANDIDATES: int = 4096
"""Places a scan will try after damage before it gives the rest of the segment up.

Resynchronising means searching forward for the next byte pattern that starts a record and
proving it with its checksum. Both the count of attempts and the bytes they may checksum are
bounded, because an adversarial file can hold a great many patterns that look like a record
header and each one costs a checksum over its declared length.
"""

RESYNC_CHECKSUM_BUDGET_FACTOR: int = 2
"""Multiple of the segment size a resynchronisation may spend on checksums.

A real record is never larger than the segment that holds it, so a genuine record always fits
the budget when the search reaches it, while an adversarial file full of plausible-looking
headers cannot demand an unbounded amount of checksumming. The ordinary case -- a run of zeros
with good records after it -- offers one candidate, because zeros hold no magic.
"""

LSN_INDEX_STRIDE_BYTES: int = 4096
"""How far apart the remembered sequence-number-to-offset marks of a segment are.

:meth:`WalManager.read_from` is asked for the records ABOVE a sequence number, and the commit
protocol of CONTRACT.md section 8.5 asks for it twice per commit with a number that is normally
the end of the log. Records are variable length and hold no back pointer, so the only way to
find one is to decode forward from a known boundary -- and with no remembered boundary the only
known one is byte zero of the first segment. That makes every read decode and checksum the WHOLE
log to answer a question about its last few records, so the cost of a commit grows with the
number of commits already made: quadratic in the life of the database.

One mark every stride is what removes that. A read starts at the last mark at or below the
number it was asked for, so the bytes it decodes before reaching its answer are bounded by this
constant instead of by the size of the log. Marks are cheap -- one per stride of a segment, two
integers each -- and they are only ever recorded for records this manager has already decoded or
written itself, so a mark can never point at a boundary that was not verified.

A page is the unit chosen: small enough that the wasted decode is under a page, large enough
that a busy log holds a few hundred marks per segment rather than one per record.
"""

SEGMENT_HEADER_STRUCT_FORMAT: str = "<QQd"
"""Payload of a SEGMENT_HEADER record: segment number, previous last LSN, creation wall time."""

FSYNC_DURATION_SECONDS: str = "oktografx_fsync_duration_seconds"
BARRIER_FAILURES_TOTAL: str = "oktografx_barrier_failures_total"
WAL_SIZE_BYTES: str = "oktografx_wal_size_bytes"
WAL_SEGMENTS: str = "oktografx_wal_segments"
WAL_TRUNCATION_LAG_SEGMENTS: str = "oktografx_wal_truncation_lag_segments"
CHECKSUM_VERIFICATIONS_TOTAL: str = "oktografx_checksum_verifications_total"
CHECKSUM_FAILURES_TOTAL: str = "oktografx_checksum_failures_total"

WAL_METRICS: tuple[MetricDescriptor, ...] = tuple(
    metric(name)
    for name in (
        FSYNC_DURATION_SECONDS,
        BARRIER_FAILURES_TOTAL,
        WAL_SIZE_BYTES,
        WAL_SEGMENTS,
        WAL_TRUNCATION_LAG_SEGMENTS,
        CHECKSUM_VERIFICATIONS_TOTAL,
        CHECKSUM_FAILURES_TOTAL,
    )
)
"""The descriptors this manager registers and emits, looked up in the frozen catalog of
CONTRACT.md section 9 rather than declared again. A metric is a contract (G7), so a name that
leaves the catalog breaks the import of this module instead of a scrape in production."""

_SEGMENT_HEADER = struct.Struct(SEGMENT_HEADER_STRUCT_FORMAT)
_TOTAL_LENGTH_OFFSET: int = 12
_WAL_TARGET_LABELS: dict[str, str] = {"target": "wal"}
_RECORD_KIND_LABELS: dict[str, str] = {"kind": "record"}
_READER_PRESENT_LABELS: dict[str, dict[str, str]] = {
    "true": {"reader_present": "true"},
    "false": {"reader_present": "false"},
}


class SegmentHeader:
    """The payload of the record that opens every segment.

    It is a record like any other -- it carries a log sequence number, an epoch and a checksum --
    which is what keeps the contiguity rule to a single sentence: every record in the log follows
    the one before it. The payload says which segment this is and which sequence number the
    previous segment ended on, so a scan can tell "this segment starts a new run" apart from
    "records are missing here".
    """

    __slots__ = ("number", "previous_last_lsn", "created_at_wall")

    def __init__(
        self, number: int, previous_last_lsn: Lsn, created_at_wall: float
    ) -> None:
        """Hold the three fields a segment header carries."""
        self.number = number
        self.previous_last_lsn = previous_last_lsn
        self.created_at_wall = created_at_wall

    def encode(self) -> bytes:
        """Return the payload bytes of a SEGMENT_HEADER record."""
        return _SEGMENT_HEADER.pack(
            self.number, self.previous_last_lsn, float(self.created_at_wall)
        )

    @classmethod
    def decode(cls, payload: bytes) -> SegmentHeader:
        """Return the header a SEGMENT_HEADER payload carries, refusing a payload that is short."""
        if len(payload) < _SEGMENT_HEADER.size:
            raise GrafxCorruptionDetected(
                f"A segment header payload is {_SEGMENT_HEADER.size} bytes and this one holds "
                f"{len(payload)}.",
                reason="short_segment_header",
                expected=_SEGMENT_HEADER.size,
                actual=len(payload),
            )
        number, previous, created = _SEGMENT_HEADER.unpack_from(payload, 0)
        return cls(number, previous, created)

    def __repr__(self) -> str:
        """Return a short, readable form for a test failure or a log line."""
        return (
            f"SegmentHeader(number={self.number}, previous_last_lsn={self.previous_last_lsn}, "
            f"created_at_wall={self.created_at_wall!r})"
        )


STORAGE_PORT_METHODS: tuple[str, ...] = (
    "append_log",
    "create",
    "durable_barrier",
    "exists",
    "list_files",
    "log_size",
    "read_log",
    "recycle",
    "truncate_log",
)
"""Exactly the doors of the storage port this component opens."""

CLOCK_PORT_METHODS: tuple[str, ...] = ("wall",)
"""The clock is read only for the human-facing stamp in a segment header."""

METRICS_PORT_METHODS: tuple[str, ...] = (
    "enabled",
    "increment",
    "observe",
    "register",
    "set_gauge",
    "time",
)
"""Everything this component asks of a metrics sink."""


def _require_port(slot: str, instance: object, methods: Sequence[str]) -> None:
    """Return the port, refusing an empty or incomplete slot the way G5 asks for.

    The check is by attribute rather than by ``isinstance`` against the Protocol on purpose. A
    runtime-checkable Protocol changed how it inspects an instance between the interpreters this
    project supports -- 3.11 asks ``hasattr`` and 3.12 asks ``getattr_static`` -- so a device
    that forwards through ``__getattr__``, which is how a wrapper is written, would be accepted
    on one supported interpreter and refused on the other. Asking for the doors this component
    actually opens is stable across both and says exactly what is missing.
    """
    missing = [name for name in methods if not hasattr(instance, name)]
    if instance is None or missing:
        raise GrafxPortNotConfigured(
            f"The write-ahead log needs a {slot} port; "
            f"{type(instance).__name__} is missing {sorted(missing)}.",
            slot=slot,
            missing=tuple(sorted(missing)),
        )


def _require_lsn(field: str, value: object) -> int:
    """Return a log sequence number, refusing anything that is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"The {field} must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if value < 0:
        raise GrafxConfigurationError(
            f"The {field} must not be negative; got {value}.", field=field, value=value
        )
    return value


class WalManager:
    """The segmented write-ahead log of one database."""

    __slots__ = (
        "_storage",
        "_clock",
        "_metrics",
        "_directory",
        "_segment_bytes",
        "_descriptor",
        "_opened",
        "_indexed",
        "_segments",
        "_marks",
        "_last_lsn",
        "_max_epoch",
        "_damage",
        "_append_uncertain",
        "_unflushed",
        "_next_number",
        "_total_bytes",
        "_tail_hold_depth",
    )

    def __init__(
        self,
        storage: StorageDevice,
        clock: Clock,
        metrics: MetricsSink,
        *,
        directory: str = DEFAULT_WAL_DIRECTORY,
        segment_bytes: int,
        descriptor: str,
    ) -> None:
        """Build the manager over its three ports. Nothing touches the device until ``open``."""
        _require_port("storage", storage, STORAGE_PORT_METHODS)
        _require_port("clock", clock, CLOCK_PORT_METHODS)
        _require_port("metrics", metrics, METRICS_PORT_METHODS)
        self._storage: StorageDevice = storage
        self._clock: Clock = clock
        self._metrics: MetricsSink = metrics
        self._directory = _validate_directory(directory)
        self._segment_bytes = _validate_segment_bytes(segment_bytes)
        self._descriptor = _validate_descriptor(descriptor)
        self._opened: bool = False
        self._indexed: bool = False
        self._segments: list[SegmentInfo] = []
        self._marks: dict[str, list[tuple[Lsn, int]]] = {}
        self._last_lsn: Lsn = NO_LSN
        self._max_epoch: Epoch = 0
        self._damage: ScanFailure | None = None
        self._append_uncertain: bool = False
        self._unflushed: list[str] = []
        self._next_number: int = MIN_SEGMENT_NUMBER
        self._total_bytes: int = 0
        self._tail_hold_depth: int = 0
        if self._metrics.enabled:
            for declared in WAL_METRICS:
                self._metrics.register(declared)

    # --- state ---------------------------------------------------------------------------

    @property
    def last_lsn(self) -> Lsn:
        """Return the sequence number of the last record the log holds, or zero when empty.

        Every door that answers a question ABOUT THE LOG re-derives the tail first, because the
        log belongs to the database and not to this object: another participant commits,
        reclaims and repairs while this one holds an opinion. Answering from that opinion is how
        a reader is told the log ends three records before it does, and how a repair reports
        success while leaving records above its own cut. The cost is the cheap path of
        :meth:`refresh` -- one directory listing and one size query when nothing has moved.
        """
        self._refresh_if_open()
        return self._last_lsn

    @property
    def directory(self) -> str:
        """Return the directory the segments of this log live in."""
        return self._directory

    @property
    def descriptor(self) -> str:
        """Return the granularity descriptor stamped on records that do not carry their own."""
        return self._descriptor

    @property
    def segment_bytes(self) -> int:
        """Return the size at which the log rolls to a new segment."""
        return self._segment_bytes

    def _refresh_if_open(self) -> None:
        """Re-derive the tail, unless this manager has not been opened yet."""
        if self._opened:
            self._refresh_tail_if_needed()

    @property
    def damage(self) -> ScanFailure | None:
        """Return the first damage the last scan found, or None when the log reads clean.

        While this is set every append is refused. It is the state recovery clears by calling
        :meth:`truncate_after`, after it has quarantined the affected bytes and written the
        ledger entries that make the discard traceable (G8).
        """
        self._refresh_if_open()
        return self._damage

    @property
    def append_uncertain(self) -> bool:
        """Return whether a failed append could not restore the exact pre-append bytes.

        Unlike scan damage, this latch is not inferred from the current tail.  A complete,
        decodable COMMIT can survive a failed rollback and make a subsequent scan look healthy,
        while the caller that received the exception has no right to treat its outcome as known.
        Recovery clears the latch only after it has either cut the uncertain suffix or forced and
        completed the intact WAL range.
        """
        return self._append_uncertain

    def segments(self) -> tuple[SegmentInfo, ...]:
        """Return what the log holds, oldest first, re-derived from the device."""
        self._refresh_if_open()
        return tuple(self._segments)

    def total_bytes(self) -> int:
        """Return the size of the live log in bytes, re-derived from the device."""
        self._refresh_if_open()
        return self._total_bytes

    # --- lifecycle -----------------------------------------------------------------------

    def open(self) -> None:
        """Discover the segments and their sizes; read what is inside them on demand.

        What the log HOLDS -- the last good sequence number, the damage, the record count of
        each segment -- can only be learnt by decoding every record, because records are
        variable length and hold no back pointer. That pass is taken once, at the first door
        that needs an answer from it, and NOT a second time here.

        The reason is that the first door is very often :meth:`scan_all`, which is recovery
        replaying the log. Scanning at ``open`` and scanning again for the caller decodes and
        checksums every record twice for one answer, which is the whole of the open-with-replay
        cost of a database that has just crashed. One pass serves both: :meth:`scan_all` hands
        out the items of the very walk that builds the index.

        Deferred is not lazy about failing. The directory is listed and every segment is sized
        here, so a device that cannot be reached still refuses the open, and the size gauges are
        published before any caller has asked a question. Nothing is repaired either way: a log
        whose tail is damaged holds evidence that quarantine and the ledger are entitled to see
        before it is destroyed, and :attr:`damage` reports it as soon as it is known.
        """
        self._unflushed.clear()
        self._survey()

    def _survey(self) -> None:
        """Take the cheap half of the accounting: which segments exist and how big they are.

        The sequence-number fields of what this leaves in the index are deliberately empty, and
        no door may read them: ``_indexed`` is False, so every door re-derives first.
        """
        discovered = self._discover()
        self._next_number = discovered[-1][0] + 1 if discovered else MIN_SEGMENT_NUMBER
        sizes = {name: self._storage.log_size(name) for _, name in discovered}
        self._segments = [
            SegmentInfo(
                number=number,
                name=name,
                first_lsn=NO_LSN,
                last_lsn=NO_LSN,
                size_bytes=sizes[name],
                record_count=0,
            )
            for number, name in discovered
            if sizes[name] > 0
        ]
        self._marks = {}
        self._total_bytes = sum(segment.size_bytes for segment in self._segments)
        self._last_lsn = NO_LSN
        self._max_epoch = 0
        self._damage = None
        self._opened = True
        self._indexed = False
        # A fresh handle did not append the discovered bytes, so they do not belong in the
        # performance cache used by ordinary commit barriers. Recovery establishes durability
        # of foreign work explicitly through force_barrier_range; marking all history pending
        # here would make the first commit after every clean open fsync the whole retained WAL.
        self._unflushed = []
        self._publish_size_metrics()

    def _rebuild(self) -> None:
        """Re-derive every piece of state from what the device currently holds.

        The items of the pass are discarded here; this caller wants the accounting only. The
        pass absorbs damage on its own account (``stop_at_damage``), so nothing a damaged log
        can hold reaches this frame.
        """
        for _item in self._index_pass(stop_at_damage=True):
            pass

    def _index_pass(self, *, stop_at_damage: bool) -> Iterator[ScanItem]:
        """Walk the whole log once, yielding every item AND taking the accounting from it.

        This is the only place a full pass over the log happens, and it exists in one copy so
        that a caller who wants the items -- recovery, through :meth:`scan_all` -- does not pay
        for a second identical walk to build the index it also needs.

        ``stop_at_damage`` is the difference between the two callers, and it is the difference
        the doors already have. :meth:`_rebuild` wants the state and nothing else, so it stops at
        the first stretch of bytes that is not a record, exactly where the old scan stopped, and
        absorbs a read that cannot continue as damage. :meth:`scan_all` is the tolerant door: it
        reports failures as items and keeps going, so the component that classifies discarded
        work sees what came after the damage as well (SD-4).

        Nothing here writes to the manager until the pass finishes. A caller that abandons the
        iterator half way leaves the index PENDING rather than half built, so the next door
        re-derives instead of trusting a partial answer.
        """
        discovered = self._discover()
        next_number = discovered[-1][0] + 1 if discovered else MIN_SEGMENT_NUMBER
        ordered = [name for _, name in discovered]
        sizes = {name: self._storage.log_size(name) for name in ordered}
        first: dict[str, Lsn] = {name: NO_LSN for name in ordered}
        last: dict[str, Lsn] = {name: NO_LSN for name in ordered}
        counts: dict[str, int] = {name: 0 for name in ordered}
        marks: dict[str, list[tuple[Lsn, int]]] = {}
        damage: ScanFailure | None = None
        last_lsn: Lsn = NO_LSN
        max_epoch: Epoch = 0
        reached = ordered[0] if ordered else ""
        try:
            for item in self._walk(tuple(ordered), sizes):
                yield item
                reached = item.segment
                if damage is not None:
                    # The accounting ended at the first failure; the yielding did not.
                    continue
                if item.failure is not None:
                    damage = item.failure
                    if stop_at_damage:
                        break
                    continue
                record = item.record
                if (
                    record is None
                ):  # pragma: no cover - the walk sets exactly one of the two
                    continue
                counts[item.segment] += 1
                if first[item.segment] == NO_LSN:
                    first[item.segment] = record.lsn
                last[item.segment] = record.lsn
                last_lsn = record.lsn
                _note_mark(marks, item.segment, record.lsn, item.offset)
                if record.epoch > max_epoch:
                    max_epoch = record.epoch
        except (GrafxCorruptionDetected, GrafxSchemaVersionMismatch) as failure:
            if not stop_at_damage:
                # The tolerant door still raises what it cannot read at all, and the index stays
                # pending so the next question re-derives rather than trusting a partial pass.
                raise
            # A damaged log MUST still open. Recovery repairs through truncate_after, and every
            # door of this manager is behind _require_open: a scan that raised its way out of
            # the accounting would leave the log permanently unopenable and its own repair
            # unreachable, which is the one shape a write-ahead log must not have.
            #
            # Only damage is absorbed. A device that cannot be read at all is an access failure
            # (A11-revised), there is nothing to repair, and pretending to be open would be a
            # lie -- so those propagate.
            damage = ScanFailure(
                reason=FailureReason.UNREADABLE_SEGMENT,
                segment=str(failure.details.get("file") or reached),
                offset=0,
                length=sizes.get(str(failure.details.get("file") or reached), 0),
                expected_lsn=last_lsn + 1,
                detail=(
                    f"The scan could not continue "
                    f"({failure.details.get('reason', failure.code)}): {failure.message}"
                ),
            )
        segments: list[SegmentInfo] = []
        for number, name in discovered:
            if sizes[name] == 0:
                # A segment that holds nothing: an interrupted roll left it, or a truncation
                # that kept nothing burned its number into it. Registering it would put an
                # unreadable range in front of the recyclable prefix and stop recycling for good.
                continue
            segments.append(
                SegmentInfo(
                    number=number,
                    name=name,
                    first_lsn=first[name],
                    last_lsn=last[name],
                    size_bytes=sizes[name],
                    record_count=counts[name],
                )
            )
        self._next_number = next_number
        self._segments = segments
        self._marks = marks
        self._total_bytes = sum(segment.size_bytes for segment in segments)
        self._last_lsn = last_lsn
        self._max_epoch = max_epoch
        self._damage = damage
        self._opened = True
        self._indexed = True
        self._unflushed = [name for name in self._unflushed if name in sizes]
        self._publish_size_metrics()

    def refresh(self) -> None:
        """Re-derive the end of the log from the device, reading only what is new (CF-6).

        The segment index is built at :meth:`open` and held in memory, which is correct for one
        participant and wrong for two: a second writer that never looks again keeps assigning
        sequence numbers the first has already used, and the duplication is discovered by a cold
        reader long afterwards, as a log that lost records. D1 asks for real multi-process
        writing, so the tail is re-derived rather than remembered.

        It is cheap by construction. When nothing has changed the cost is one directory listing
        and one size query, and no byte of the log is read. When something HAS changed, only the
        bytes that arrived since this participant last looked are scanned, which is proportional
        to the other participant's work rather than to the size of the log. A segment that
        SHRANK is the one case that cannot be resolved incrementally -- recovery truncated it --
        and that falls back to a full re-derivation.

        It never raises on damage. This runs on the commit path, and a re-derivation that raised
        would put the permanently-unopenable failure of a damaged tail onto every commit, which
        is strictly worse than meeting it at open. Damage is recorded and the next append is
        refused by :meth:`_require_healthy`, leaving the repair door open.
        """
        self._require_open()
        self._refresh_tail()

    @contextmanager
    def hold_tail(self) -> Iterator[None]:
        """Reuse one freshly derived tail while an external exclusive section is held.

        The outermost hold refreshes before it exposes the cached picture. WAL doors called
        inside it then use the in-memory index, including changes appended through this manager.
        ``finally`` always releases the picture, so the next independent section must observe a
        foreign append before assigning another LSN. Nested holds share the same picture.

        This is safe only while the caller prevents another writer from appending, normally by
        owning ``COMMIT_SECTION``. Recovery durability proofs use their forced-refresh door and
        deliberately bypass this optimisation.
        """
        self._require_open()
        outermost = self._tail_hold_depth == 0
        if outermost:
            self._refresh_tail()
        self._tail_hold_depth += 1
        try:
            yield
        finally:
            self._tail_hold_depth -= 1

    def _refresh_tail_if_needed(self) -> None:
        """Refresh unless the caller already holds the section's freshly derived tail."""
        if self._tail_hold_depth == 0:
            self._refresh_tail()

    def _refresh_tail(self) -> None:
        """Bring the index up to date with what the device now holds."""
        if not self._indexed:
            # open() surveyed the segments and left what is INSIDE them unread. There is no
            # remembered tail to bring up to date yet, so this is the pass that reads them.
            self._rebuild()
            return
        discovered = self._discover()
        names = [name for _, name in discovered]
        known = {segment.name: segment for segment in self._segments}
        if names == [segment.name for segment in self._segments]:
            if not names:
                return
            tail = self._segments[-1]
            if self._storage.log_size(tail.name) == tail.size_bytes:
                return
        cached = [segment.name for segment in self._segments]
        if cached and cached[-1] not in set(names):
            # The segment this participant's numbering is anchored in is gone: a repair rolled
            # it, or reclamation reached past it. Nothing it remembers about where the log ends
            # survives that, so the answer comes from a full pass rather than from an anchor
            # that no longer exists. Trusting the anchor here is how a healthy log gets called
            # damaged and how a repair aimed at a stale number destroys a live one.
            self._rebuild()
            return
        sizes = {name: self._storage.log_size(name) for name in names}
        # Bytes appended by another participant are no more provably durable than bytes appended
        # through this object.  Remember every new or grown segment so gap completion's barrier
        # really flushes the foreign COMMIT it is about to publish over.
        changed = [
            name
            for name in names
            if name not in known or sizes[name] != known[name].size_bytes
        ]
        if self._damage is not None and changed:
            # The remembered cut may sit inside the record another participant was still
            # appending. Resuming at the old byte size would interpret only the newly arrived
            # suffix (often one checksum byte) as a fresh record and preserve invented damage.
            # Once a damaged observation changes, no offset beyond its last good record is a
            # trustworthy incremental anchor; re-derive the segment from byte zero.
            self._rebuild()
            return
        pending = [name for name in self._unflushed if name in sizes]
        pending.extend(name for name in changed if name not in pending)
        self._unflushed = pending
        if any(sizes[name] < known[name].size_bytes for name in names if name in known):
            # A segment lost bytes, so what this participant remembers about the records inside
            # it may be wrong anywhere, not only past the end. Only a full pass can say.
            self._rebuild()
            return
        starts = {name: known[name].size_bytes for name in names if name in known}
        # The marks of a segment nobody removed still describe bytes nobody rewrote, so they
        # carry over; a name that has gone takes its marks with it.
        marks = {name: self._marks[name] for name in names if name in self._marks}
        first = {
            name: (known[name].first_lsn if name in known else NO_LSN) for name in names
        }
        last = {
            name: (known[name].last_lsn if name in known else NO_LSN) for name in names
        }
        counts = {
            name: (known[name].record_count if name in known else 0) for name in names
        }
        last_lsn = self._last_lsn
        max_epoch = self._max_epoch
        damage = self._damage
        reached = names[-1] if names else ""
        try:
            for item in self._walk(
                tuple(names),
                sizes,
                starts=starts,
                expected_lsn=self._last_lsn + 1 if self._last_lsn else None,
            ):
                reached = item.segment
                if item.failure is not None:
                    damage = item.failure
                    break
                record = item.record
                if (
                    record is None
                ):  # pragma: no cover - the walk sets exactly one of the two
                    continue
                counts[item.segment] += 1
                if first[item.segment] == NO_LSN:
                    first[item.segment] = record.lsn
                last[item.segment] = record.lsn
                last_lsn = record.lsn
                _note_mark(marks, item.segment, record.lsn, item.offset)
                if record.epoch > max_epoch:
                    max_epoch = record.epoch
        except (GrafxCorruptionDetected, GrafxSchemaVersionMismatch) as failure:
            damage = ScanFailure(
                reason=FailureReason.UNREADABLE_SEGMENT,
                segment=str(failure.details.get("file") or reached),
                offset=0,
                length=sizes.get(str(failure.details.get("file") or reached), 0),
                expected_lsn=last_lsn + 1,
                detail=(
                    f"The scan could not continue "
                    f"({failure.details.get('reason', failure.code)}): {failure.message}"
                ),
            )
        self._marks = marks
        self._segments = [
            SegmentInfo(
                number=number,
                name=name,
                first_lsn=first[name],
                last_lsn=last[name],
                size_bytes=sizes[name],
                record_count=counts[name],
            )
            for number, name in discovered
            if sizes[name] > 0
        ]
        self._total_bytes = sum(segment.size_bytes for segment in self._segments)
        self._last_lsn = last_lsn
        self._max_epoch = max_epoch
        self._damage = damage
        self._next_number = discovered[-1][0] + 1 if discovered else MIN_SEGMENT_NUMBER
        self._unflushed = [name for name in self._unflushed if name in sizes]
        self._publish_size_metrics()

    def _discover(self) -> list[tuple[int, str]]:
        """Return every segment file of the directory, ordered by number."""
        found: list[tuple[int, str]] = []
        for name in self._storage.list_files(f"{self._directory}/"):
            number = parse_segment_number(self._directory, name)
            if number is not None:
                found.append((number, name))
        found.sort()
        return found

    # --- appending -----------------------------------------------------------------------

    def append(self, record: WalRecord) -> Lsn:
        """Append one record and return the sequence number it was given. No barrier is taken."""
        return self.append_many((record,))

    def planned_terminal_lsn(self, records: Sequence[WalRecord]) -> Lsn:
        """Return the terminal LSN this exact batch would receive without writing a byte.

        A segment header consumes a real LSN when the batch rolls.  Callers that stamp heap and
        index effects with the COMMIT LSN therefore need the roll decision, not merely
        ``last_lsn + len(records)``.  The append revalidates this answer under the same tail
        checks immediately before it writes, so a foreign tail change becomes a clean refusal
        instead of a batch whose payload names a different commit number.
        """
        self._require_open()
        self._refresh_tail_if_needed()
        self._require_healthy()
        _batch, _body_length, _rolling, terminal = self._plan_batch(records)
        return terminal

    def append_many(
        self,
        records: Sequence[WalRecord],
        *,
        expected_terminal_lsn: Lsn | None = None,
    ) -> Lsn:
        """Append a batch with one write and return the sequence number of its last record.

        Every check happens before the first byte leaves: an epoch older than the log already
        holds is refused here, so a stale writer never reaches the device (BR-7). If the write
        itself fails the segment is put back to the byte it started at, so the batch consumed no
        sequence number and a retry cannot write one twice.

        Durability is not claimed by this call. BR-4 puts the acknowledgement after
        :meth:`barrier` returns, and this method deliberately does not take one, so a caller can
        group many appends behind a single barrier.
        """
        self._require_open()
        # CF-6: an unheld call re-derives the shared tail before assigning a sequence number. A
        # commit that already owns COMMIT_SECTION may instead reuse the picture established by
        # hold_tail(); no foreign append can move it until that context is released.
        self._refresh_tail_if_needed()
        self._require_healthy()
        batch, _body_length, rolling, terminal = self._plan_batch(records)
        if expected_terminal_lsn is not None:
            expected = _require_lsn("expected_terminal_lsn", expected_terminal_lsn)
            if terminal != expected:
                raise GrafxTransactionStateError(
                    "The WAL tail or segment-roll decision changed after the commit batch was "
                    "materialised; no byte of this batch reached the device.",
                    field="expected_terminal_lsn",
                    expected_terminal_lsn=expected,
                    planned_terminal_lsn=terminal,
                    last_lsn=self._last_lsn,
                )
        lsn = self._last_lsn
        images: list[bytes] = []
        # Where every record of this batch lands. The writer knows each offset exactly, without
        # reading anything back, which is what keeps the sequence-number index of a busy log up
        # to date between the walks that would otherwise be the only source of a mark.
        placed: list[tuple[Lsn, int]] = []
        if rolling:
            number = self._next_number
            name = segment_name(self._directory, number)
            lsn += 1
            header = WalRecord(
                record_type=WalRecordType.SEGMENT_HEADER,
                payload=SegmentHeader(
                    number, self._last_lsn, self._clock.wall()
                ).encode(),
                descriptor=self._descriptor,
                lsn=lsn,
                epoch=max(self._max_epoch, max(record.epoch for record in batch)),
            )
            images.append(header.encode())
            size_before = 0
            placed.append((lsn, 0))
            cursor = len(images[0])
        else:
            number = self._segments[-1].number
            name = self._segments[-1].name
            size_before = self._segments[-1].size_bytes
            cursor = size_before
        stamped: list[WalRecord] = []
        for record in batch:
            lsn += 1
            stamped_record = record.with_lsn(lsn)
            stamped.append(stamped_record)
            image = stamped_record.encode()
            images.append(image)
            placed.append((lsn, cursor))
            cursor += len(image)
        blob = b"".join(images)
        state_before = (
            list(self._segments),
            {segment: list(marks) for segment, marks in self._marks.items()},
            self._next_number,
            self._total_bytes,
            self._last_lsn,
            self._max_epoch,
            list(self._unflushed),
        )
        try:
            if rolling:
                self._storage.create(name)
            self._storage.append_log(name, blob)
        except BaseException as failure:
            repaired = self._undo_append(name, size_before, created=rolling)
            if not repaired:
                try:
                    failure.add_note(
                        f"WAL rollback to byte {size_before} of {name!r} did not complete; "
                        "the manager was marked damaged."
                    )
                except BaseException:  # noqa: BLE001 - never replace the append failure
                    pass
            raise
        try:
            self._register_append(
                number, name, size_before, blob, stamped, rolling, placed
            )
        except BaseException as failure:
            (
                self._segments,
                self._marks,
                self._next_number,
                self._total_bytes,
                self._last_lsn,
                self._max_epoch,
                self._unflushed,
            ) = state_before
            repaired = self._undo_append(name, size_before, created=rolling)
            if not repaired:
                try:
                    failure.add_note(
                        f"WAL registration failed and rollback to byte {size_before} of "
                        f"{name!r} did not complete; the manager was marked damaged."
                    )
                except BaseException:  # noqa: BLE001 - never replace registration failure
                    pass
            raise
        return lsn

    def _plan_batch(
        self, records: Sequence[WalRecord]
    ) -> tuple[tuple[WalRecord, ...], int, bool, Lsn]:
        """Validate a batch and return its encoded length, roll decision and terminal LSN."""
        batch = self._validate_batch(records)
        body_length = sum(record.encoded_length() for record in batch)
        rolling = self._needs_roll(body_length)
        header_length = (
            WalRecord(
                record_type=WalRecordType.SEGMENT_HEADER,
                payload=bytes(_SEGMENT_HEADER.size),
                descriptor=self._descriptor,
            ).encoded_length()
            if rolling
            else 0
        )
        size_before = 0 if rolling else self._segments[-1].size_bytes
        projected_size = size_before + header_length + body_length
        if projected_size > MAX_SEGMENT_READ_BYTES:
            raise GrafxConfigurationError(
                "The WAL batch would create a segment larger than this build can read; no "
                "byte of the batch reached the device.",
                field="records",
                value=len(batch),
                projected_segment_bytes=projected_size,
                maximum_segment_bytes=MAX_SEGMENT_READ_BYTES,
            )
        terminal = self._last_lsn + len(batch) + int(rolling)
        if terminal >= PROVISIONAL_CSN:
            raise GrafxConfigurationError(
                "The write-ahead log has exhausted its usable sequence-number space; the "
                "maximum unsigned value is reserved for provisional heap versions.",
                field="last_lsn",
                value=self._last_lsn,
            )
        return batch, body_length, rolling, terminal

    def _validate_batch(self, records: Sequence[WalRecord]) -> tuple[WalRecord, ...]:
        """Return the batch, stamped with this log's descriptor, or refuse it whole.

        Nothing here touches the device, which is what makes the epoch refusal of BR-7 exact:
        a stale writer is turned away before a single byte can reach the disk.
        """
        if isinstance(records, (str, bytes, bytearray)) or not isinstance(
            records, Sequence
        ):
            raise GrafxConfigurationError(
                f"A batch of log records must be a sequence; got {type(records).__name__}.",
                field="records",
                value=type(records).__name__,
            )
        if not records:
            raise GrafxConfigurationError(
                "A batch of log records must hold at least one record.",
                field="records",
                value=0,
            )
        batch: list[WalRecord] = []
        for position, record in enumerate(records):
            if type(record) is not WalRecord:
                observed = _builtin_type_name(record)
                raise GrafxConfigurationError(
                    f"Entry {position} of the batch is a {observed}, not an exact WalRecord.",
                    field="records",
                    value=position,
                )
            record = _canonical_record(record, position)
            if record.lsn != NO_LSN:
                raise GrafxConfigurationError(
                    "The log assigns sequence numbers; entry "
                    f"{position} arrived carrying {record.lsn}.",
                    field="lsn",
                    value=record.lsn,
                )
            if not record.is_known_type:
                raise GrafxConfigurationError(
                    f"Record type {record.record_type} is not one this build knows how to write.",
                    field="record_type",
                    value=record.record_type,
                )
            if record.epoch < self._max_epoch:
                raise GrafxStaleEpoch(
                    f"Epoch {record.epoch} is older than epoch {self._max_epoch}, which this log "
                    "already holds; no byte of this batch reached the device.",
                    epoch=record.epoch,
                    current_epoch=self._max_epoch,
                    position=position,
                )
            batch.append(
                record
                if record.descriptor
                else record.with_descriptor(self._descriptor)
            )
        return tuple(batch)

    def _needs_roll(self, body_length: int) -> bool:
        """Return True when this batch has to start a new segment."""
        if not self._segments:
            return True
        return self._segments[-1].size_bytes + body_length > self._segment_bytes

    def _register_append(
        self,
        number: int,
        name: str,
        size_before: int,
        blob: bytes,
        stamped: Sequence[WalRecord],
        rolling: bool,
        placed: Sequence[tuple[Lsn, int]],
    ) -> None:
        """Take the append into the in-memory index, now that the device has accepted it."""
        size_after = size_before + len(blob)
        for record_lsn, offset in placed:
            _note_mark(self._marks, name, record_lsn, offset)
        if rolling:
            first_lsn = stamped[0].lsn - 1
            self._segments.append(
                SegmentInfo(
                    number=number,
                    name=name,
                    first_lsn=first_lsn,
                    last_lsn=stamped[-1].lsn,
                    size_bytes=size_after,
                    record_count=len(stamped) + 1,
                )
            )
            self._next_number = number + 1
        else:
            previous = self._segments[-1]
            self._segments[-1] = SegmentInfo(
                number=previous.number,
                name=previous.name,
                first_lsn=previous.first_lsn,
                last_lsn=stamped[-1].lsn,
                size_bytes=size_after,
                record_count=previous.record_count + len(stamped),
            )
        self._total_bytes += len(blob)
        self._last_lsn = stamped[-1].lsn
        for record in stamped:
            if record.epoch > self._max_epoch:
                self._max_epoch = record.epoch
        if name not in self._unflushed:
            self._unflushed.append(name)
        try:
            self._publish_size_metrics()
        except BaseException:  # noqa: BLE001 - observability cannot make an append ambiguous
            pass

    def _undo_append(self, name: str, size_before: int, *, created: bool) -> bool:
        """Put a segment back to the size it had before a failed append.

        A device that stored part of a batch leaves bytes nobody acknowledged. Removing them is
        not a discard the ledger is owed: BR-2 puts synchronous, typed failures outside the
        ledger entirely, and the caller has the exception in its hands. What the ledger would
        never forgive is leaving the fragment in place, because the next append would write good
        records behind bytes no scan can pass.

        When the repair itself fails the log is marked damaged, so the door closes rather than
        writing after a hole.
        """
        try:
            current = self._storage.log_size(name)
            if current > size_before:
                self._storage.truncate_log(name, size_before)
                current = self._storage.log_size(name)
            if current != size_before:
                raise GrafxCorruptionDetected(
                    f"Segment {name!r} holds {current} bytes after a failed append that started "
                    f"at {size_before}.",
                    reason="unrepaired_append",
                    file=name,
                    offset=size_before,
                )
            if created:
                # The segment was born for this batch and holds nothing. Letting it go keeps the
                # numbering dense; if the platform will not take it now, the number is burned so
                # the next roll cannot land on a name that already exists.
                self._storage.recycle(name)
                self._next_number = (
                    max(
                        self._next_number,
                        parse_segment_number(self._directory, name) or 0,
                    )
                    + 1
                )
        except BaseException as failure:
            # This latch is independent of the decoder's damage verdict.  A complete batch can
            # remain after recycle/truncate itself fails and decode cleanly on the next refresh;
            # its caller still observed an exception and cannot know whether that COMMIT is the
            # outcome.  Only recovery may clear this uncertainty.
            self._append_uncertain = True
            self._damage = ScanFailure(
                reason=FailureReason.TRUNCATED_TAIL,
                segment=name,
                offset=size_before,
                length=0,
                expected_lsn=self._last_lsn + 1,
                detail=(
                    f"A failed append left {name!r} in a state this manager could not repair "
                    f"({type(failure).__name__}): {failure}"
                ),
            )
            return False
        return True

    # --- durability ----------------------------------------------------------------------

    def barrier(self) -> None:
        """Put every segment written since the last barrier on stable storage (FR-5, BR-4).

        Segments are flushed oldest first, so an interrupted barrier leaves a durable prefix
        rather than a durable suffix with a hole in front of it. A failure is counted into
        ``oktografx_barrier_failures_total`` before it is re-raised (A25), and the pending set is
        NOT cleared, so a caller that retries flushes the same files again.

        With nothing appended since the last barrier this call touches no device: there is
        nothing whose durability could be in question, and an fsync of an unchanged file would
        cost a commit the very latency FR-5 is measured on.
        """
        self._require_open()
        targets = tuple(self._unflushed)
        if not targets:
            return
        if self._metrics.enabled:
            with self._metrics.time(FSYNC_DURATION_SECONDS, _WAL_TARGET_LABELS):
                self._flush(targets)
        else:
            self._flush(targets)
        self._unflushed.clear()

    def force_barrier_range(self, first_lsn: Lsn, through_lsn: Lsn) -> tuple[str, ...]:
        """Force every segment intersecting an intact LSN range, ignoring the pending cache.

        ``_unflushed`` is an optimisation for bytes appended by this object, not durability
        evidence for bytes discovered during recovery or written by another participant.  Gap
        completion and startup therefore use this door: it re-derives the inventory, proves the
        requested range is retained in the clean prefix, and fsyncs its segments oldest-first
        even if an earlier ordinary barrier emptied the cache.
        """
        self._require_open()
        first = _require_lsn("first_lsn", first_lsn)
        through = _require_lsn("through_lsn", through_lsn)
        if first == NO_LSN:
            raise GrafxConfigurationError(
                "A forced WAL range begins at a record LSN, never at the no-LSN sentinel.",
                field="first_lsn",
                value=first,
            )
        if through < first:
            return ()
        self._refresh_tail()
        if self._damage is not None:
            raise self._damage.as_error()
        if through > self._last_lsn:
            raise GrafxTransactionStateError(
                "The WAL cannot barrier a range beyond its intact tail.",
                field="through_lsn",
                through_lsn=through,
                last_lsn=self._last_lsn,
            )
        intersecting = tuple(
            segment
            for segment in self._segments
            if segment.first_lsn <= through and segment.last_lsn >= first
        )
        if (
            not intersecting
            or first < intersecting[0].first_lsn
            or through > intersecting[-1].last_lsn
        ):
            raise GrafxTransactionStateError(
                "The retained WAL does not cover the complete range requested for a forced "
                "durability barrier.",
                field="wal_range",
                first_lsn=first,
                through_lsn=through,
                retained_first_lsn=(
                    intersecting[0].first_lsn if intersecting else NO_LSN
                ),
                retained_last_lsn=(
                    intersecting[-1].last_lsn if intersecting else NO_LSN
                ),
            )
        targets = tuple(segment.name for segment in intersecting)
        if self._metrics.enabled:
            with self._metrics.time(FSYNC_DURATION_SECONDS, _WAL_TARGET_LABELS):
                self._flush(targets)
        else:
            self._flush(targets)
        selected = set(targets)
        self._unflushed = [name for name in self._unflushed if name not in selected]
        if through == self._last_lsn:
            self._append_uncertain = False
        return targets

    def _flush(self, targets: Sequence[str]) -> None:
        """Ask the device for a barrier on each segment, counting a failure before re-raising."""
        for name in targets:
            try:
                self._storage.durable_barrier(name)
            except GrafxDurabilityBarrierFailed:
                if self._metrics.enabled:
                    self._metrics.increment(BARRIER_FAILURES_TOTAL, 1.0, None)
                raise

    # --- reading -------------------------------------------------------------------------

    def read_from(self, lsn: Lsn) -> Iterator[WalRecord]:
        """Return every record from this sequence number onwards, in order.

        This is the strict door. The first stretch of bytes that is not a record raises, because
        a caller reading the log to decide something -- what conflicts with what, what to redo --
        must never be handed a silently shortened stream. :meth:`scan_all` is the tolerant door
        that reports damage as data instead.

        It begins at the record it was asked for, not at the beginning of the log. The commit
        protocol of CONTRACT.md section 8.5 asks this question twice for every commit, with a
        number that is normally the end of the log, so a door that decoded and checksummed the
        whole log to answer it would make the cost of a commit grow with the number of commits
        already made. What it decodes is bounded by :data:`LSN_INDEX_STRIDE_BYTES` plus the
        answer itself; see :meth:`_read_plan` for what the skip is allowed to rest on.
        """
        self._require_open()
        start = _require_lsn("lsn", lsn)
        self._refresh_tail_if_needed()
        return self._read_from(start)

    def _read_from(self, start: Lsn) -> Iterator[WalRecord]:
        """Yield the records at or above the sequence number, raising at the first damage."""
        names, sizes, begin = self._read_plan(start)
        for item in self._walk(names, sizes, starts=begin):
            if item.failure is not None:
                raise item.failure.as_error()
            record = item.record
            if record is not None and record.lsn >= start:
                yield record

    def _read_plan(
        self, start: Lsn
    ) -> tuple[tuple[str, ...], dict[str, int], dict[str, int]]:
        """Return the segments a strict read must walk and the byte to begin each one at.

        Two facts make skipping safe, and both are checked here rather than assumed.

        **The log must read clean.** While :attr:`damage` is set, every byte from the first
        segment onwards is walked, because a skip could step over the very stretch of bytes the
        strict door exists to refuse -- and a caller asking for a number ABOVE the damage would
        then be handed an empty stream instead of the error that says the log is broken. That is
        precisely the silent shortening this door forbids.

        **A mark is only ever a boundary this manager verified.** Marks come from a walk that
        decoded the record it marked, or from an append this manager itself wrote, and the
        component's one standing invariant is that an observed byte range of a segment never
        changes -- a repair rolls a segment into a new name rather than rewriting it in place.
        So a mark cannot point into the middle of a record, and every record before it carries a
        smaller sequence number than the mark does.

        Whole segments go first: sequence numbers increase along the log, so a segment whose
        last one is below the start holds nothing the caller asked for. What is left is one
        segment entered part way and every segment after it, entered whole.
        """
        names = tuple(segment.name for segment in self._segments)
        sizes = {segment.name: segment.size_bytes for segment in self._segments}
        if self._damage is not None:
            return names, sizes, {}
        selected: list[str] = []
        begin: dict[str, int] = {}
        for segment in self._segments:
            if not selected:
                if segment.last_lsn != NO_LSN and segment.last_lsn < start:
                    continue
                begin[segment.name] = _mark_offset(self._marks.get(segment.name), start)
            selected.append(segment.name)
        return tuple(selected), sizes, begin

    def scan_all(self) -> Iterator[ScanItem]:
        """Walk every segment, reporting damage as items rather than raising.

        A failure is followed by whatever could still be read after it, so the component that
        classifies discarded work sees both where the good run ended and what came after it --
        which is the difference between a forensic entry and a reapplicable one (CONTRACT.md
        section 8.6, SD-4).

        On a log that has just been opened this IS the pass that builds the index (see
        :meth:`open`), so a database recovering from a crash decodes and checksums its log once
        rather than twice. A caller that abandons the iterator leaves the index pending, and the
        next question re-derives.
        """
        self._require_open()
        if not self._indexed:
            return self._index_pass(stop_at_damage=False)
        self._refresh_tail_if_needed()
        return self._walk_registered()

    def _walk_registered(self) -> Iterator[ScanItem]:
        """Walk the segments the index currently holds."""
        names = tuple(segment.name for segment in self._segments)
        sizes = {segment.name: segment.size_bytes for segment in self._segments}
        return self._walk(names, sizes)

    def _walk(
        self,
        names: Sequence[str],
        sizes: Mapping[str, int],
        *,
        starts: Mapping[str, int] | None = None,
        expected_lsn: Lsn | None = None,
    ) -> Iterator[ScanItem]:
        """Walk the named segments in order, tracking sequence number contiguity across them.

        The walk reports; it does not judge. Every caller that cannot tolerate damage stops at
        the first failure item itself -- :meth:`_read_from` raises it, :meth:`_rebuild` and
        :meth:`_find_cut` break -- and because this is a generator, abandoning it stops the work
        as well. A strict MODE here would be a second mechanism answering the same question, and
        a mutation battery showed the pair mutually untestable: no input could distinguish them
        (A67, A93).
        """
        begin = {} if starts is None else starts
        expected: Lsn | None = expected_lsn
        for name in names:
            size = sizes[name]
            base = begin.get(name, 0)
            if size <= base:
                # Nothing here this walk has not already accounted for.
                continue
            data = self._read_segment(name, size, base)
            offset = base
            spent = 0
            budget = len(data) * RESYNC_CHECKSUM_BUDGET_FACTOR
            candidates = 0
            while offset - base < len(data):
                outcome = decode_record(data, offset - base)
                self._count_checksum(outcome)
                record = outcome.record
                if record is not None:
                    if expected is not None and record.lsn != expected:
                        failure = ScanFailure(
                            reason=FailureReason.LSN_DISCONTINUITY,
                            segment=name,
                            offset=offset,
                            length=len(data) - (offset - base),
                            expected_lsn=expected,
                            detail=(
                                f"The log expected sequence number {expected} here and the "
                                f"record carries {record.lsn}."
                            ),
                            sample=data[
                                offset - base : offset - base + MAX_FAILURE_SAMPLE_BYTES
                            ],
                        )
                        yield ScanItem(segment=name, offset=offset, failure=failure)
                    expected = record.lsn + 1
                    yield ScanItem(segment=name, offset=offset, record=record)
                    offset += outcome.consumed
                    continue
                reason = outcome.reason or FailureReason.BAD_HEADER
                if reason is FailureReason.TRUNCATED_TAIL:
                    yield ScanItem(
                        segment=name,
                        offset=offset,
                        failure=ScanFailure(
                            reason=reason,
                            segment=name,
                            offset=offset,
                            length=len(data) - (offset - base),
                            expected_lsn=expected if expected is not None else NO_LSN,
                            detail=outcome.detail,
                            sample=data[
                                offset - base : offset - base + MAX_FAILURE_SAMPLE_BYTES
                            ],
                        ),
                    )
                    break
                if outcome.consumed > 0:
                    # The checksum matched, so the declared length describes real bytes and the
                    # scan may step over the record it cannot use.
                    yield ScanItem(
                        segment=name,
                        offset=offset,
                        failure=ScanFailure(
                            reason=reason,
                            segment=name,
                            offset=offset,
                            length=outcome.consumed,
                            expected_lsn=expected if expected is not None else NO_LSN,
                            detail=outcome.detail,
                            sample=data[
                                offset - base : offset - base + MAX_FAILURE_SAMPLE_BYTES
                            ],
                        ),
                    )
                    offset += outcome.consumed
                    continue
                following, candidates, spent = _next_record_offset(
                    data, offset - base + 1, candidates, spent, budget
                )
                end = len(data) if following is None else following
                yield ScanItem(
                    segment=name,
                    offset=offset,
                    failure=ScanFailure(
                        reason=reason,
                        segment=name,
                        offset=offset,
                        length=end - (offset - base),
                        expected_lsn=expected if expected is not None else NO_LSN,
                        detail=outcome.detail,
                        sample=data[
                            offset - base : offset - base + MAX_FAILURE_SAMPLE_BYTES
                        ],
                    ),
                )
                if following is None:
                    break
                offset = base + following

    def _read_segment(self, name: str, size: int, base: int = 0) -> bytes:
        """Return a segment from ``base`` onwards, refusing a range too large to be one.

        The base is what makes re-deriving the tail cheap: a participant that already knows the
        first N bytes reads only what has arrived since, which is proportional to the work
        another participant did rather than to the size of the log.
        """
        if size - base > MAX_SEGMENT_READ_BYTES:
            raise GrafxCorruptionDetected(
                f"Segment {name!r} holds {size} bytes, past the {MAX_SEGMENT_READ_BYTES} a "
                "segment of this log can reach.",
                reason="oversized_segment",
                file=name,
                size=size,
            )
        return self._storage.read_log(name, base, size - base)

    def _count_checksum(self, outcome: DecodeOutcome) -> None:
        """Count one record checksum, and its failure, when the decoder really computed one.

        Only a decode that reached the checksum is counted. A stretch of bytes refused on its
        magic or its header never had a checksum taken over it, and counting one there would
        make ``oktografx_checksum_verifications_total`` a count of decode attempts under a name
        that promises something else.
        """
        if not self._metrics.enabled or not outcome.checked:
            return
        self._metrics.increment(CHECKSUM_VERIFICATIONS_TOTAL, 1.0, _RECORD_KIND_LABELS)
        if outcome.reason is FailureReason.CHECKSUM_FAILURE:
            self._metrics.increment(CHECKSUM_FAILURES_TOTAL, 1.0, _RECORD_KIND_LABELS)

    # --- repair and reclamation ------------------------------------------------------------

    def truncate_after(self, lsn: Lsn) -> TruncationReport:
        """Remove every record above this sequence number, and make the removal durable.

        This is recovery's door, and the order it works in is the crash-safe one: the newest
        segments go first, and the segment holding the cut is shortened last. An interruption at
        any point therefore leaves a log that is LONGER than asked for, which is a valid log that
        a re-run finishes off, rather than a short one followed by segments that would read as
        records lost from the middle.

        For the same reason, the final shortening is skipped when a segment above it could not be
        released: reporting ``completed`` as False and leaving the log intact is the honest
        outcome, and the caller retries.

        The segment carrying the cut is not shortened in place; its surviving records are rolled
        into a new segment and the old one is released (see :meth:`_roll_cut_segment`).
        ``TruncationReport.truncated_segment`` therefore names the NEW segment that now ends at
        the cut, and the old name appears among the segments this pass removed.

        A cut that keeps NOTHING releases every segment, and it burns a segment number on the way
        out (see :meth:`_reserve_empty_segment`), because a directory with no segments left in it
        would number the next one from the beginning and re-issue a name this log has already
        used.
        """
        self._require_open()
        target = _require_lsn("lsn", lsn)
        self._refresh_tail()
        cut_name, cut_offset, _kept_last = self._find_cut(target)
        before_count = self._count_records()
        removed: list[str] = []
        deferred: list[str] = []
        removed_bytes = 0
        completed = True
        index = self._index_of(cut_name)
        doomed = list(reversed(self._segments[index + 1 :]))
        # A cut below every record takes the whole log with it, so the last segment released is
        # the last one there is. The number is burned before that release rather than after it:
        # a crash in between must never be able to leave a directory that has forgotten how far
        # the numbering got.
        empties_the_log = cut_name is None and bool(doomed)
        for position, segment in enumerate(doomed):
            if empties_the_log and position == len(doomed) - 1:
                self._reserve_empty_segment()
            released = self._storage.recycle(segment.name)
            if released or not self._storage.exists(segment.name):
                removed.append(segment.name)
                removed_bytes += segment.size_bytes
                if not released:
                    deferred.append(segment.name)
            else:
                deferred.append(segment.name)
                completed = False
                break
        truncated: str | None = None
        if completed and cut_name is not None:
            current = self._storage.log_size(cut_name)
            if current > cut_offset:
                truncated, old_gone, old_deferred = self._roll_cut_segment(
                    cut_name, cut_offset
                )
                if old_gone:
                    removed_bytes += current - cut_offset
                    removed.append(cut_name)
                    if old_deferred:
                        deferred.append(cut_name)
                else:
                    # The durable replacement exists, but the original still names the full
                    # tail. Reporting success here would let recovery publish over two copies
                    # of one LSN range and a later scan would meet a discontinuity we created.
                    deferred.append(cut_name)
                    completed = False
        if removed or truncated is not None:
            gone = set(removed)
            self._unflushed = [name for name in self._unflushed if name not in gone]
            self.barrier()
            self._rebuild()
        # What WENT, counted as the drop between what the log held before this pass and what it
        # holds now. Counted from what was intended, a pass that could not release every segment
        # would report a number nobody could act on; counted from a position in the log, it would
        # need a name to measure against, and this pass releases that name -- the segment holding
        # the cut is rolled into a new one -- so afterwards the old name resolves to nothing and
        # every surviving record reads as still-to-go. A difference of two totals cannot be told
        # either story, and it is the number the operator-facing FR-8 finding claims to be.
        after_count = self._count_records()
        if completed and self._damage is None:
            self._append_uncertain = False
        return TruncationReport(
            last_lsn=self._last_lsn,
            removed_records=before_count - after_count,
            removed_bytes=removed_bytes,
            truncated_segment=truncated,
            removed_segments=tuple(reversed(removed)),
            deferred_segments=tuple(deferred),
            completed=completed,
        )

    def _roll_cut_segment(
        self, cut_name: str, cut_offset: int
    ) -> tuple[str, bool, bool]:
        """Move the records at or below the cut into a NEW segment and let the old one go.

        A segment is never rewritten in place. The incremental re-derivation of :meth:`refresh`
        rests on one invariant -- an observed byte range of a segment never changes -- and
        shortening a segment is the only thing in this component that would break it. A
        participant holding the old picture could then resume inside a record that is no longer
        there, or meet a name and a size it already knows and conclude that nothing happened.
        Burning the number makes both unrepresentable rather than checked: after a repair the
        name set has changed, so every other participant re-derives from a fact it cannot miss.

        Crash behaviour, stated plainly because this is recovery's own door: the new segment is
        made durable BEFORE the old one is released, so no record at or below the cut can be
        lost. A crash in that window leaves both, which the next scan reports as a sequence
        number discontinuity -- damage, detected, and cleared by running this same call again.
        What it can never do is destroy a record the cut was meant to keep, or leave two
        participants disagreeing in silence.
        """
        surviving = self._storage.read_log(cut_name, 0, cut_offset)
        number = self._next_number
        name = segment_name(self._directory, number)
        self._storage.create(name)
        self._storage.append_log(name, surviving)
        self._next_number = number + 1
        self._flush((name,))
        released = self._storage.recycle(cut_name)
        old_gone = released or not self._storage.exists(cut_name)
        # ``released=False`` has two storage-contract meanings: a Windows deletion was queued
        # and the logical name is already gone, or no deletion could be scheduled and the name
        # remains. The caller must distinguish them when deciding whether truncation completed.
        return name, old_gone, not released

    def _reserve_empty_segment(self) -> str:
        """Create the empty segment that stops the numbering from restarting at one.

        A truncation that keeps nothing releases every segment of the log, and the next number is
        derived from the names the directory holds. With no names left, that derivation starts
        again at :data:`~okto_grafx.domain.wal.segment.MIN_SEGMENT_NUMBER` and the next roll
        re-creates a name this log has already used and already handed to other participants.
        :mod:`okto_grafx.domain.wal.segment` says why that must never happen: a released name may
        still be held open by another process, so re-claiming it means appending into a file
        whose destruction is queued -- an acknowledged, barriered record written into a grave.

        Burning the number into an empty file is what keeps the rule. The file holds no bytes, so
        it is not part of the log and no scan reads it: :meth:`_index_pass` skips a zero-length
        segment for the same reason it skips the one an interrupted roll leaves behind. What it
        does hold is a NUMBER, and the number is the only thing that had to survive.

        It is made durable before the last live segment is released, so there is no window in
        which a crash could leave a directory that has forgotten how far the numbering got.
        """
        number = self._next_number
        name = segment_name(self._directory, number)
        self._storage.create(name)
        self._next_number = number + 1
        self._flush((name,))
        return name

    def _find_cut(self, target: Lsn) -> tuple[str | None, int, Lsn]:
        """Return the segment and offset where the records at or below the target end."""
        cut_name: str | None = None
        cut_offset = 0
        kept_last: Lsn = NO_LSN
        # A walk that RAISES ends the good run exactly as a walk that reports one does. Treating
        # the two differently would put the repair behind the damage it exists to repair.
        try:
            for item in self._walk_registered():
                if item.failure is not None:
                    break
                record = item.record
                if record is None or record.lsn > target:
                    break
                cut_name = item.segment
                cut_offset = item.offset + record.encoded_length()
                kept_last = record.lsn
        except (GrafxCorruptionDetected, GrafxSchemaVersionMismatch):
            pass
        return cut_name, cut_offset, kept_last

    def _count_records(self) -> int:
        """Return how many records the log decodes to right now, damage and all.

        Everything decodable is counted, including what sits AFTER a damaged stretch, because a
        truncation that throws those bytes away really did remove records and a report that said
        otherwise would understate what it destroyed.
        """
        counted = 0
        try:
            for item in self._walk_registered():
                if item.record is not None:
                    counted += 1
        except (GrafxCorruptionDetected, GrafxSchemaVersionMismatch):
            # The count is for the report; a scan that cannot finish reports what it reached.
            pass
        return counted

    def _index_of(self, name: str | None) -> int:
        """Return the position of a segment in the index, or -1 for "before every segment".

        ``None`` is the cut that keeps nothing, and it is the -1 this is written for. A name the
        index does not hold answers -1 as well, and that is not a distinction a lookup by name
        can make -- which is why nothing here measures records against a name that a pass may
        have released.
        """
        for position, segment in enumerate(self._segments):
            if segment.name == name:
                return position
        return -1

    def recycle(
        self, horizon_lsn: Lsn, *, reader_present: bool = False
    ) -> RecycleReport:
        """Drop every segment whose records all sit below the horizon (FR-6, BR-10, AC-8, AC-9).

        The horizon is what :func:`okto_grafx.engine.coordination.recyclable_horizon` returns:
        the reader registry and the checkpoint combined into one number. Nothing here asks
        whether a reader exists, and no reader is ever evicted; when the horizon does not move,
        nothing is reclaimed and the segments held back are reported instead.

        ``reader_present`` only labels the lag gauge. The caller is the one that knows whether a
        live reader is the reason the horizon stopped where it did -- this manager sees a number
        -- and the metric of CONTRACT.md section 9 carries that as a bounded label. Leaving it
        out would leave the series unpopulated, which A25 records as the way a metric silently
        never fires.

        Windows deferral is invisible to the result: a segment whose deletion the platform queued
        is counted in ``deferred`` and the space returns when the last handle closes. A segment
        the platform would not release AT ALL keeps its name, and the walk stops there, because a
        segment removed from behind a segment that stayed would leave the log with a hole.
        """
        self._require_open()
        self._refresh_tail_if_needed()
        horizon = _require_lsn("horizon_lsn", horizon_lsn)
        if not isinstance(reader_present, bool):
            raise GrafxConfigurationError(
                f"The reader_present label must be a bool; got {type(reader_present).__name__}.",
                field="reader_present",
                value=type(reader_present).__name__,
            )
        count = recyclable_prefix(self._segments, horizon)
        recycled: list[str] = []
        deferred: list[str] = []
        reclaimed = 0
        dropped = 0
        for segment in self._segments[:count]:
            released = self._storage.recycle(segment.name)
            if released:
                recycled.append(segment.name)
                reclaimed += segment.size_bytes
                dropped += 1
                continue
            if self._storage.exists(segment.name):
                # A27: the device queued nothing and the name is still live, so the segment is
                # still readable and still part of the log. Asking again later is the caller's
                # business, and stopping here keeps the surviving log contiguous.
                deferred.append(segment.name)
                break
            deferred.append(segment.name)
            dropped += 1
        surviving = self._segments[dropped:]
        self._segments = surviving
        self._marks = {
            segment.name: self._marks[segment.name]
            for segment in surviving
            if segment.name in self._marks
        }
        self._total_bytes = sum(segment.size_bytes for segment in surviving)
        # A deferral that kept its name keeps its SEGMENT: it is still part of the log, so it is
        # still owed a barrier. Dropping every deferred name would leave that segment volatile
        # for good, and a crash would take records this manager still reports in last_lsn.
        gone = (set(recycled) | set(deferred)) - {segment.name for segment in surviving}
        self._unflushed = [name for name in self._unflushed if name not in gone]
        lag = max(len(surviving) - 1, 0)
        self._publish_size_metrics()
        if self._metrics.enabled:
            self._metrics.set_gauge(
                WAL_TRUNCATION_LAG_SEGMENTS,
                float(lag),
                _READER_PRESENT_LABELS["true" if reader_present else "false"],
            )
        return RecycleReport(
            horizon_lsn=horizon,
            recycled=tuple(recycled),
            deferred=tuple(deferred),
            retained=tuple(segment.name for segment in surviving),
            reclaimed_bytes=reclaimed,
            lag_segments=lag,
            reader_present=reader_present,
        )

    # --- helpers -------------------------------------------------------------------------

    def _publish_size_metrics(self) -> None:
        """Publish the two gauges that say how much log there is."""
        if self._metrics.enabled:
            self._metrics.set_gauge(WAL_SIZE_BYTES, float(self._total_bytes), None)
            self._metrics.set_gauge(WAL_SEGMENTS, float(len(self._segments)), None)

    def _require_open(self) -> None:
        """Refuse any work on a manager whose segments have not been discovered yet."""
        if not self._opened:
            raise GrafxTransactionStateError(
                "The write-ahead log has not been opened; call open() first.",
                state="closed",
                directory=self._directory,
            )

    def _require_healthy(self) -> None:
        """Refuse to append while the log holds bytes a scan cannot pass."""
        if self._damage is not None:
            raise self._damage.as_error()
        if self._append_uncertain:
            raise GrafxCorruptionDetected(
                "A previous WAL append could not restore its exact starting bytes; recovery "
                "must settle the surviving suffix before another append is allowed.",
                reason="append_uncertain",
                directory=self._directory,
                last_lsn=self._last_lsn,
            )

    def __repr__(self) -> str:
        """Return a short, readable form for a test failure or a log line."""
        return (
            f"WalManager(directory={self._directory!r}, segments={len(self._segments)}, "
            f"last_lsn={self._last_lsn}, damaged={self._damage is not None}, "
            f"append_uncertain={self._append_uncertain})"
        )


def _next_record_offset(
    data: bytes, start: int, candidates: int, spent: int, budget: int
) -> tuple[int | None, int, int]:
    """Return where the next real record begins after damage, or None when there is none.

    Resynchronising is a search for the magic pattern followed by a proof: only a candidate whose
    checksum matches is a record, because the pattern alone appears inside ordinary payload bytes
    often enough to matter. Two bounds keep an adversarial file from turning the search into an
    unbounded amount of work, and both are chosen so that a genuine record is never skipped: a
    real record is never larger than the segment holding it, and a hole with valid records after
    it offers one candidate rather than thousands.
    """
    index = data.find(MAGIC_BYTES, start)
    while index != -1:
        if candidates >= MAX_RESYNC_CANDIDATES:
            return None, candidates, spent
        declared = _declared_length(data, index)
        if declared is not None and spent + declared <= budget:
            candidates += 1
            spent += declared
            outcome = decode_record(data, index)
            if outcome.record is not None:
                return index, candidates, spent
        index = data.find(MAGIC_BYTES, index + 1)
    return None, candidates, spent


def _note_mark(
    marks: dict[str, list[tuple[Lsn, int]]], name: str, lsn: Lsn, offset: int
) -> None:
    """Remember that a record with this sequence number starts at this byte of this segment.

    At most one mark per :data:`LSN_INDEX_STRIDE_BYTES` of a segment is kept, so the index costs
    two integers per stride rather than two per record. The first record of a segment is always
    marked, which is what lets a read enter a segment it has never decoded.

    Marks arrive in increasing offset order from every caller -- a walk goes forwards and an
    append only ever adds to the end -- so the list stays sorted by both offset and sequence
    number without being sorted.
    """
    kept = marks.setdefault(name, [])
    if kept and offset - kept[-1][1] < LSN_INDEX_STRIDE_BYTES:
        return
    kept.append((lsn, offset))


def _mark_offset(marks: Sequence[tuple[Lsn, int]] | None, start: Lsn) -> int:
    """Return the byte to begin decoding a segment at to reach a sequence number.

    The answer is the last mark at or below the number asked for, and zero when the segment has
    no such mark. Zero is always safe: it is the beginning of the segment, which is where a
    reader that remembers nothing has to start anyway.
    """
    if not marks:
        return 0
    position = bisect_right(marks, start, key=lambda mark: mark[0])
    if position == 0:
        return 0
    return marks[position - 1][1]


def _declared_length(data: bytes, offset: int) -> int | None:
    """Return the total length a candidate header declares, or None when it cannot be read."""
    if len(data) - offset < WAL_HEADER_LENGTH:
        return None
    return int(struct.unpack_from("<I", data, offset + _TOTAL_LENGTH_OFFSET)[0])


def _validate_directory(directory: object) -> str:
    """Return the log directory, refusing a name the storage port could not use."""
    if not issubclass(type(directory), str):
        observed = _builtin_type_name(directory)
        raise GrafxConfigurationError(
            f"The write-ahead log directory must be a non-empty string; got {observed}.",
            field="directory",
            value=observed,
        )
    directory = str.__str__(directory)
    if not directory:
        raise GrafxConfigurationError(
            "The write-ahead log directory must be a non-empty string.",
            field="directory",
            value="",
        )
    if directory.endswith("/") or "\\" in directory or directory.startswith("/"):
        raise GrafxConfigurationError(
            "The write-ahead log directory is a relative logical name with no trailing slash; "
            f"got {directory!r}.",
            field="directory",
            value=directory,
        )
    return directory


def _builtin_type_name(value: object) -> str:
    """Name a WAL argument without invoking a hostile metaclass descriptor."""
    value_type = type(value)
    declared = type.__dict__["__name__"].__get__(value_type, type(value_type))
    return str.__str__(declared)


def _canonical_record(record: WalRecord, position: int) -> WalRecord:
    """Copy one exact record into exact built-ins before planning or virtual methods run."""

    def value_of(field: str) -> object:
        """Read one slot from the exact record without invoking subclass lookup."""
        try:
            return object.__getattribute__(record, field)
        except Exception as failure:
            cause = _builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"Entry {position} of the WAL batch has no readable {field!r}; got {cause}.",
                field="records",
                value=position,
                record_field=field,
                cause=cause,
            ) from failure

    def integer(field: str) -> int:
        """Copy one integer slot through the built-in implementation."""
        value = value_of(field)
        if type(value) is bool or not issubclass(type(value), int):
            observed = _builtin_type_name(value)
            raise GrafxConfigurationError(
                f"Entry {position} of the WAL batch has a non-integer {field!r}: {observed}.",
                field=field,
                value=observed,
                position=position,
            )
        return int.__int__(value)

    raw_payload = value_of("payload")
    if not isinstance(raw_payload, (bytes, bytearray, memoryview)):
        observed = _builtin_type_name(raw_payload)
        raise GrafxConfigurationError(
            f"Entry {position} of the WAL batch has a non-bytes payload: {observed}.",
            field="payload",
            value=observed,
            position=position,
        )
    try:
        payload = (
            raw_payload
            if type(raw_payload) is bytes
            else memoryview(raw_payload).tobytes()
        )
    except GrafxError:
        raise
    except Exception as failure:
        cause = _builtin_type_name(failure)
        raise GrafxConfigurationError(
            f"Entry {position} of the WAL batch has an unreadable payload; got {cause}.",
            field="payload",
            value=cause,
            position=position,
        ) from failure
    raw_descriptor = value_of("descriptor")
    if not issubclass(type(raw_descriptor), str):
        observed = _builtin_type_name(raw_descriptor)
        raise GrafxConfigurationError(
            f"Entry {position} of the WAL batch has a non-string descriptor: {observed}.",
            field="descriptor",
            value=observed,
            position=position,
        )
    return WalRecord(
        record_type=integer("record_type"),
        payload=payload,
        descriptor=str.__str__(raw_descriptor),
        lsn=integer("lsn"),
        epoch=integer("epoch"),
        txn_id=integer("txn_id"),
        flags=integer("flags"),
        format_version=integer("format_version"),
    )


def _validate_segment_bytes(segment_bytes: object) -> int:
    """Return a roll size that every reader of this build can open."""
    if type(segment_bytes) is bool or not issubclass(type(segment_bytes), int):
        observed = _builtin_type_name(segment_bytes)
        raise GrafxConfigurationError(
            f"The segment size must be an integer; got {observed}.",
            field="segment_bytes",
            value=observed,
        )
    segment_bytes = int.__int__(segment_bytes)
    if segment_bytes < MIN_SEGMENT_BYTES:
        raise GrafxConfigurationError(
            f"A segment holds at least {MIN_SEGMENT_BYTES} bytes; got {segment_bytes}.",
            field="segment_bytes",
            value=segment_bytes,
        )
    if segment_bytes > MAX_SEGMENT_READ_BYTES:
        raise GrafxConfigurationError(
            f"A segment holds at most {MAX_SEGMENT_READ_BYTES} bytes; got {segment_bytes}.",
            field="segment_bytes",
            value=segment_bytes,
        )
    return segment_bytes


def _validate_descriptor(descriptor: object) -> str:
    """Return the granularity descriptor, refusing one the record header could not carry."""
    if not issubclass(type(descriptor), str):
        observed = _builtin_type_name(descriptor)
        raise GrafxConfigurationError(
            "The granularity descriptor must be a non-empty string; it is what makes a record "
            f"readable without the configuration that produced it; got {observed}.",
            field="descriptor",
            value=observed,
        )
    descriptor = str.__str__(descriptor)
    if not descriptor:
        raise GrafxConfigurationError(
            "The granularity descriptor must be a non-empty string; it is what makes a record "
            "readable without the configuration that produced it.",
            field="descriptor",
            value="",
        )
    try:
        encoded = descriptor.encode("utf-8")
    except UnicodeEncodeError as failure:
        raise GrafxConfigurationError(
            "The granularity descriptor must be encodable as UTF-8.",
            field="descriptor",
            value=repr(descriptor),
        ) from failure
    if len(encoded) > MAX_DESCRIPTOR_BYTES:
        raise GrafxConfigurationError(
            f"A granularity descriptor holds at most {MAX_DESCRIPTOR_BYTES} bytes; got "
            f"{len(encoded)}.",
            field="descriptor",
            value=len(encoded),
        )
    return descriptor
