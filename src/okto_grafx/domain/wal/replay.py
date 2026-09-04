"""What a scan of the log found, and what a repair of it did (CONTRACT.md sections 8.3 and 8.6).

Three shapes live here, and all three are plain frozen data so that recovery, the ledger and a
test can pass them around without holding a device open.

* :class:`ScanFailure` is one stretch of bytes that is not a usable record, with enough
  provenance for a forensic ledger entry: which segment, at which offset, how many bytes are
  affected, which log sequence number was expected there, and why. It does not carry the bytes
  themselves -- a damaged range can be as large as a segment, and the component that quarantines
  it already holds the device it would read them from.
* :class:`ScanItem` is one step of a walk over the whole log: a record, or a failure, at a
  place.
* :class:`TruncationReport` and :class:`RecycleReport` say what a repair or a reclamation
  actually did, including the part the platform deferred.

:meth:`ScanFailure.as_error` is the one place that turns a scan failure into an exception, and it
draws the line CONTRACT.md section 2 and amendment A11-revised draw: damaged bytes are
``corruption_detected``, while a record written by a build newer than this one is a version
question and gets ``schema_version_mismatch``. Neither is retryable, because reading the same
bytes again produces the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.wal.codec import FailureReason
from okto_grafx.domain.wal.record import WalRecord

__all__ = [
    "MAX_FAILURE_SAMPLE_BYTES",
    "ScanFailure",
    "ScanItem",
    "TruncationReport",
    "RecycleReport",
]

MAX_FAILURE_SAMPLE_BYTES: int = 64
"""Bytes of the damaged range kept on the failure, purely so a message can show them.

The sample is a diagnostic, never the evidence: quarantine copies the real range off the device
by offset and length, which is why those two fields are the ones that must be exact.
"""


@dataclass(frozen=True, slots=True)
class ScanFailure:
    """One stretch of bytes in the log that is not a usable record."""

    reason: FailureReason
    segment: str
    offset: int
    length: int
    expected_lsn: Lsn = NO_LSN
    detail: str = ""
    sample: bytes = b""

    def as_error(self) -> GrafxError:
        """Return the typed error a caller that cannot tolerate this failure should raise.

        A build that meets a newer format version has not found damage; it has found a record it
        was never taught to read, and telling the operator that the bytes are corrupt would send
        them to quarantine instead of to an upgrade.
        """
        details: dict[str, object] = {
            "reason": str(self.reason.value),
            "file": self.segment,
            "offset": self.offset,
            "length": self.length,
            "expected_lsn": self.expected_lsn,
        }
        message = f"{self.detail} Segment {self.segment!r} at byte {self.offset}."
        if self.reason in {
            FailureReason.UNSUPPORTED_VERSION,
            FailureReason.UNSUPPORTED_REQUIRED_RECORD,
        }:
            return GrafxSchemaVersionMismatch(message, **details)
        return GrafxCorruptionDetected(message, **details)


@dataclass(frozen=True, slots=True)
class ScanItem:
    """One step of a walk over the whole log: a record, or a failure, at a place.

    Exactly one of ``record`` and ``failure`` is set. A failure is followed by whatever the scan
    could still read after it, in order, so the component that classifies discarded work can see
    both where the good run ended and what came after it.
    """

    segment: str
    offset: int
    record: WalRecord | None = None
    failure: ScanFailure | None = None


@dataclass(frozen=True, slots=True)
class TruncationReport:
    """What removing every record above a log sequence number actually did.

    ``completed`` is False when the platform would not let go of a segment that had to be
    removed. The log is then left LONGER than asked rather than shorter: a log that still holds
    records above the cut is a valid log, while a log truncated below a segment that survived
    would have a hole in the middle of it.
    """

    last_lsn: Lsn
    removed_records: int
    removed_bytes: int
    truncated_segment: str | None = None
    removed_segments: tuple[str, ...] = ()
    deferred_segments: tuple[str, ...] = ()
    completed: bool = True


@dataclass(frozen=True, slots=True)
class RecycleReport:
    """What one pass of horizon based recycling reclaimed, kept, and had to leave for later.

    ``deferred`` holds the segments the platform did not release now: on Windows a handle held
    by another process arms a pending delete, and the space returns when the last handle closes.
    ``lag_segments`` is the number of segments still on disk that the newest one does not need,
    which is the starvation signal AC-8 asks to be observable rather than silent.
    """

    horizon_lsn: Lsn
    recycled: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    retained: tuple[str, ...] = ()
    reclaimed_bytes: int = 0
    lag_segments: int = 0
    reader_present: bool = False
