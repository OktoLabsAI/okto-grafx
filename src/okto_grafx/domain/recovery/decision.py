"""The recovery state machine: what the log says, decided before anything is touched.

This module is pure and takes no ports. It consumes the walk C4's ``scan_all`` produces and
answers the three questions CONTRACT.md section 8.6 asks, in the order it asks them:

1. **Where did the good work end?** The scan stops being trustworthy at the first stretch of
   bytes that is not a record, or at the first record whose sequence number breaks contiguity.
   Everything at or after that point is discarded, whether or not it decodes.
2. **What is discarded, and how?** A range that did not decode is forensic; a record that decoded
   but sits above the cut is reapplicable (SD-4). A sequence-number discontinuity is a MARKER and
   not a discard: the record it reports decoded, the walk yields it immediately afterwards, and
   counting the marker as well would give one discarded record two ledger entries and break the
   exactly-one rule of BR-3.
3. **What must be redone?** Only the page writes of transactions whose COMMIT is in the log
   (step 5). Uncommitted transactions need no undo, because pages are written only at commit
   (step 6), so a transaction with no COMMIT simply contributes nothing.

Transactions are keyed on ``(epoch, txn_id)`` and never on the transaction identifier alone.
CONTRACT.md section 3 says a ``TxnId`` is process-local and not a durable identity, so two runs of
a database reuse the same numbers; matching a COMMIT from one run to the page writes of another
would redo pages nobody committed. The epoch is what makes the pair unique, and it is in every
record header of section 6.5 precisely because a writer takeover increments it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from okto_grafx.domain.ids import NO_LSN, Epoch, Lsn, TxnId
from okto_grafx.domain.ledger.classification import refuses_recovery
from okto_grafx.domain.wal.codec import FailureReason
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.domain.wal.replay import ScanFailure, ScanItem

__all__ = [
    "DiscardedRange",
    "DiscardedRecord",
    "RecoveryPlan",
    "plan_recovery",
    "redo_order",
]


@dataclass(frozen=True, slots=True)
class DiscardedRange:
    """A stretch of bytes the decoder could not turn into a record."""

    segment: str
    offset: int
    length: int
    reason: FailureReason
    expected_lsn: Lsn = NO_LSN
    detail: str = ""

    @classmethod
    def of(cls, failure: ScanFailure) -> DiscardedRange:
        """Return the range one scan failure describes."""
        return cls(
            segment=failure.segment,
            offset=failure.offset,
            length=failure.length,
            reason=failure.reason,
            expected_lsn=failure.expected_lsn,
            detail=failure.detail,
        )


@dataclass(frozen=True, slots=True)
class DiscardedRecord:
    """A record that decoded cleanly and still cannot be used, because it sits above the cut."""

    segment: str
    offset: int
    record: WalRecord


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """Everything the scan established, before any byte is quarantined, cut or replayed."""

    last_good_lsn: Lsn = NO_LSN
    damaged: bool = False
    ranges: tuple[DiscardedRange, ...] = field(default_factory=tuple)
    records: tuple[DiscardedRecord, ...] = field(default_factory=tuple)
    discontinuities: tuple[DiscardedRange, ...] = field(default_factory=tuple)
    replayable: tuple[WalRecord, ...] = field(default_factory=tuple)
    unsupported: DiscardedRange | None = None
    records_scanned: int = 0

    @property
    def discards(self) -> int:
        """Return how many discarded items owe a ledger entry (G8, BR-3)."""
        return len(self.ranges) + len(self.records)


def plan_recovery(items: Iterable[ScanItem], *, floor_lsn: Lsn = NO_LSN) -> RecoveryPlan:
    """Decide what the log holds, what must go, and what must be replayed.

    ``floor_lsn`` is the published checkpoint: records at or below it are already on the pages, so
    they are not offered for redo. It never moves the CUT -- where the good work ends is a
    property of the bytes, not of how much of it is still interesting -- and it never suppresses a
    ledger entry, because a record above the cut was lost whether or not it was going to be
    replayed.
    """
    last_good: Lsn = NO_LSN
    damaged = False
    ranges: list[DiscardedRange] = []
    records: list[DiscardedRecord] = []
    discontinuities: list[DiscardedRange] = []
    replayable: list[WalRecord] = []
    unsupported: DiscardedRange | None = None
    scanned = 0
    for item in items:
        failure = item.failure
        if failure is not None:
            damaged_range = DiscardedRange.of(failure)
            if refuses_recovery(failure.reason):
                # Intact bytes from a newer build. Recovery stops rather than discards; the
                # caller turns this into schema_version_mismatch and touches nothing.
                unsupported = damaged_range
                break
            damaged = True
            if failure.reason is FailureReason.LSN_DISCONTINUITY:
                discontinuities.append(damaged_range)
                continue
            ranges.append(damaged_range)
            continue
        record = item.record
        if record is None:
            continue
        scanned += 1
        if damaged:
            records.append(
                DiscardedRecord(segment=item.segment, offset=item.offset, record=record)
            )
            continue
        last_good = record.lsn
        if record.lsn > floor_lsn:
            replayable.append(record)
    return RecoveryPlan(
        last_good_lsn=last_good,
        damaged=damaged,
        ranges=tuple(ranges),
        records=tuple(records),
        discontinuities=tuple(discontinuities),
        replayable=tuple(replayable),
        unsupported=unsupported,
        records_scanned=scanned,
    )


def redo_order(records: Iterable[WalRecord]) -> tuple[WalRecord, ...]:
    """Return the page writes of committed transactions, in the order the log holds them.

    The walk is one pass in sequence-number order. Page writes accumulate against their
    ``(epoch, txn_id)``; a COMMIT releases that transaction's writes into the result, an ABORT
    drops them, and whatever is still pending at the end belonged to a transaction that never
    committed. Section 8.6 step 6 is why that last case needs nothing further: pages are written
    only at commit, so an uncommitted transaction left nothing on a page to undo.
    """
    pending: dict[tuple[Epoch, TxnId], list[WalRecord]] = {}
    committed: list[WalRecord] = []
    for record in records:
        key = (record.epoch, record.txn_id)
        if record.record_type == int(WalRecordType.WRITE_PAGE):
            pending.setdefault(key, []).append(record)
            continue
        if record.record_type == int(WalRecordType.COMMIT):
            committed.extend(pending.pop(key, ()))
            continue
        if record.record_type == int(WalRecordType.ABORT):
            pending.pop(key, None)
    committed.sort(key=lambda item: item.lsn)
    return tuple(committed)
