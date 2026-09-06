"""The rule that decides what a discarded piece of the log is worth (SD-4, FR-9, BR-2, BR-3).

CONTRACT.md section 8.6 step 4 states it in two lines:

* the checksum matched but the record cannot be used -- it sat above the cut, or its epoch is
  stale -- so the OPERATION is known and the entry is **reapplicable**;
* the checksum did not match, or the bytes never decoded at all, so nothing but the range itself
  survives and the entry is **forensic**.

Everything else here is the mapping onto section 6.6's six frozen reason codes, and the one
judgement it needs. Those codes name the REASON FOR THE DISCARD rather than the shape of the
damage: there is a code for a truncated tail and one for a checksum failure, and none for a bad
magic, a short header or an unreadable descriptor. A run of zeros in the middle of a segment --
the NTFS signature AC-5 is written about -- decodes as a bad magic, and it is discarded because
the tail was cut there. So every damaged range that is not a checksum failure is recorded as
``TRUNCATED_TAIL``, and the decoder's own verdict travels in the payload envelope, where it is
exact and constrains nothing.

Two reasons are deliberately NOT reachable from a discard: ``UNSUPPORTED_VERSION`` and a
checksum-valid required record whose semantics this build lacks. Those bytes are not damage, and
truncating them would destroy work another build committed. They stop recovery with a typed schema
refusal, and :func:`refuses_recovery` is the predicate that says so.
"""

from __future__ import annotations

from okto_grafx.domain.ledger.entry import LedgerOriginClass, LedgerReason
from okto_grafx.domain.wal.codec import FailureReason

__all__ = [
    "FORENSIC_REASONS",
    "classify_failure",
    "classify_record",
    "refuses_recovery",
]

FORENSIC_REASONS: frozenset[FailureReason] = frozenset(
    {
        FailureReason.TRUNCATED_TAIL,
        FailureReason.BAD_MAGIC,
        FailureReason.BAD_HEADER,
        FailureReason.CHECKSUM_FAILURE,
        FailureReason.UNREADABLE_DESCRIPTOR,
    }
)
"""Every scan verdict that means the bytes did not decode, so only the range survives.

``LSN_DISCONTINUITY`` is absent because the record it reports DID decode -- the walk yields it
immediately afterwards and it is classified on its own -- and ``UNSUPPORTED_VERSION`` plus
``UNSUPPORTED_REQUIRED_RECORD`` are absent because they stop recovery instead of producing an
entry.
"""


def refuses_recovery(reason: FailureReason) -> bool:
    """Return True when this verdict must stop recovery rather than produce a ledger entry.

    A record this build cannot read because it is NEWER than this build is the one thing recovery
    must never discard: the bytes are intact, some other build wrote them deliberately, and
    truncating them would turn an upgrade problem into data loss. Failing closed here is what
    keeps FR-8's replay from becoming a downgrade.
    """
    return reason in {
        FailureReason.UNSUPPORTED_VERSION,
        FailureReason.UNSUPPORTED_REQUIRED_RECORD,
    }


def classify_failure(reason: FailureReason) -> tuple[LedgerOriginClass, LedgerReason]:
    """Return the class and the reason code for a stretch of bytes that did not decode."""
    if reason is FailureReason.CHECKSUM_FAILURE:
        return LedgerOriginClass.FORENSIC, LedgerReason.CHECKSUM_FAILURE
    return LedgerOriginClass.FORENSIC, LedgerReason.TRUNCATED_TAIL


def classify_record(
    *, stale_epoch: bool = False
) -> tuple[LedgerOriginClass, LedgerReason]:
    """Return the class and the reason code for a record that decoded but cannot be used.

    Both cases section 8.6 names are reapplicable, and they differ only in why the record is
    unusable: a stale epoch means another writer took over and this one's work never became
    visible, while everything else means the record sat above the cut.
    """
    if stale_epoch:
        return LedgerOriginClass.REAPPLICABLE, LedgerReason.STALE_EPOCH
    return LedgerOriginClass.REAPPLICABLE, LedgerReason.TRUNCATED_TAIL
