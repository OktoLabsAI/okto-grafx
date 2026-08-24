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
3. **What must be redone?** Every durable page or index effect of transactions whose COMMIT is
   in the log (step 5). Uncommitted transactions need no undo, because effects are applied only
   at commit (step 6), so a transaction with no COMMIT simply contributes nothing.

Transactions are keyed on ``(epoch, txn_id)`` and never on the transaction identifier alone.
CONTRACT.md section 3 says a ``TxnId`` is process-local and not a durable identity, so two runs of
a database reuse the same numbers; matching a COMMIT from one run to the page writes of another
would redo pages nobody committed. The epoch is what makes the pair unique, and it is in every
record header of section 6.5 precisely because a writer takeover increments it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from okto_grafx.domain.errors import GrafxRecoveryRefused
from okto_grafx.domain.ids import NO_LSN, Epoch, Lsn, TxnId
from okto_grafx.domain.ledger.classification import refuses_recovery
from okto_grafx.domain.wal.codec import FailureReason
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.domain.wal.replay import ScanFailure, ScanItem

__all__ = [
    "CommittedReplay",
    "DiscardedRange",
    "DiscardedRecord",
    "RecoveryPlan",
    "committed_replay",
    "plan_recovery",
    "redo_order",
]


_REPLAY_EFFECT_TYPES: frozenset[int] = frozenset(
    {
        int(WalRecordType.WRITE_PAGE),
        int(WalRecordType.INDEX_WRITE),
        int(WalRecordType.INDEX_RECONCILE),
    }
)


@dataclass(frozen=True, slots=True)
class CommittedReplay:
    """Committed durable effects and the commit-state watermark they establish.

    ``effects`` contains only records whose transaction has a complete COMMIT record, ordered by
    their WAL sequence number across transactions. ``incomplete_effects`` retains required
    effects whose transaction has neither a COMMIT nor an ABORT in the offered range. Recovery
    needs that distinction when damage may have destroyed the outcome record: ignoring a page
    image that was already applied would let a later watermark make an uncommitted row visible.
    ``last_committed_lsn`` is independent of either tuple: an empty committed transaction still
    advances the durable commit-state watermark.
    """

    effects: tuple[WalRecord, ...] = field(default_factory=tuple)
    last_committed_lsn: Lsn = NO_LSN
    incomplete_effects: tuple[WalRecord, ...] = field(default_factory=tuple)


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


def committed_replay(records: Iterable[WalRecord]) -> CommittedReplay:
    """Return every committed page/index effect and the resulting commit-state watermark.

    The walk is one pass over WAL sequence order. Required effects accumulate against their
    ``(epoch, txn_id)``; a COMMIT releases that transaction's effects, an ABORT drops them, and
    whatever remains at the end belongs to a transaction that never committed. A transaction may
    have exactly one terminal outcome, and no effect may follow it. Those are semantic WAL
    invariants rather than staging conveniences: accepting ``WRITE_PAGE, ABORT, COMMIT`` would
    discard a page that the old live path may already have applied and then advance the published
    watermark over it. Such an ambiguous lineage is refused before the caller mutates anything.
    Released effects are sorted once at the end because transactions may commit in a different
    order from the one in which their effects entered the WAL.

    A complete COMMIT advances ``last_committed_lsn`` even when its transaction has no effects.
    That distinction is necessary when recovery publishes ``commit.state`` after replay.
    """
    pending: dict[tuple[Epoch, TxnId], list[WalRecord]] = {}
    terminals: dict[tuple[Epoch, TxnId], WalRecord] = {}
    committed: list[WalRecord] = []
    last_committed_lsn: Lsn = NO_LSN
    for record in records:
        key = (record.epoch, record.txn_id)
        if record.record_type in _REPLAY_EFFECT_TYPES:
            terminal = terminals.get(key)
            if terminal is not None:
                _refuse_record_after_terminal(terminal, record)
            pending.setdefault(key, []).append(record)
            continue
        if record.record_type not in (
            int(WalRecordType.COMMIT),
            int(WalRecordType.ABORT),
        ):
            continue
        terminal = terminals.get(key)
        if terminal is not None:
            _refuse_record_after_terminal(terminal, record)
        terminals[key] = record
        if record.record_type == int(WalRecordType.COMMIT):
            committed.extend(pending.pop(key, ()))
            last_committed_lsn = max(last_committed_lsn, record.lsn)
            continue
        pending.pop(key, None)
    committed.sort(key=lambda item: item.lsn)
    incomplete = sorted(
        (effect for effects in pending.values() for effect in effects),
        key=lambda item: item.lsn,
    )
    return CommittedReplay(
        effects=tuple(committed),
        last_committed_lsn=last_committed_lsn,
        incomplete_effects=tuple(incomplete),
    )


def _refuse_record_after_terminal(terminal: WalRecord, record: WalRecord) -> None:
    """Refuse a second outcome or an effect after one transaction already ended."""
    terminal_name = WalRecordType(terminal.record_type).name
    record_name = WalRecordType(record.record_type).name
    raise GrafxRecoveryRefused(
        f"WAL transaction ({record.epoch}, {record.txn_id}) ended with {terminal_name} at "
        f"record {terminal.lsn}, but record {record.lsn} is {record_name}. A transaction has "
        "exactly one terminal outcome and no durable effect may follow it; no replay was "
        "started.",
        field="wal_transaction",
        epoch=record.epoch,
        txn_id=record.txn_id,
        terminal_lsn=terminal.lsn,
        terminal_type=terminal_name,
        offending_lsn=record.lsn,
        offending_type=record_name,
    )


def redo_order(records: Iterable[WalRecord]) -> tuple[WalRecord, ...]:
    """Return committed page writes in WAL order (the legacy page-only replay view)."""
    replay = committed_replay(records)
    return tuple(
        record
        for record in replay.effects
        if record.record_type == int(WalRecordType.WRITE_PAGE)
    )
