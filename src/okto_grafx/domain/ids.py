"""Identifier vocabulary shared by every component (CONTRACT.md section 3).

These aliases exist so a signature says what a number means. ``Lsn`` and ``Csn`` are the same
Python type, but a log sequence number and a commit sequence number are read differently by a
human and by a reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from okto_grafx.domain.errors import GrafxCorruptionDetected

__all__ = [
    "Lsn",
    "Csn",
    "Epoch",
    "TxnId",
    "PageIndex",
    "SlotId",
    "RecordId",
    "NO_LSN",
    "NO_CSN",
    "NO_PAGE",
    "MAX_PAGE_INDEX",
    "MAX_SLOT_ID",
    "RecordRef",
    "NULL_REF",
]

Lsn: TypeAlias = int
"""Monotonic log sequence number. Starts at 1; 0 means 'none'."""

Csn: TypeAlias = int
"""Commit sequence number: the LSN of the COMMIT record that made a version visible."""

Epoch: TypeAlias = int
"""Writer epoch. Starts at 1 and is incremented by every takeover."""

TxnId: TypeAlias = int
"""Process-local monotonic transaction number. It is not a durable identity."""

PageIndex: TypeAlias = int
"""Zero-based page position inside one paged file."""

SlotId: TypeAlias = int
"""Zero-based slot position inside one page directory."""

RecordId: TypeAlias = int
"""Stable logical identity of a record across all of its versions."""

NO_LSN: Lsn = 0
NO_CSN: Csn = 0
NO_PAGE: PageIndex = 0xFFFFFFFF

MAX_PAGE_INDEX: PageIndex = 0xFFFFFFFF
"""Largest encodable page index. It is the same value as :data:`NO_PAGE` by construction."""

MAX_SLOT_ID: SlotId = 0xFFFF
"""Largest encodable slot id: the slot directory stores 16-bit entries."""

_SLOT_BITS: int = 16
_MAX_ENCODED: int = (MAX_PAGE_INDEX << _SLOT_BITS) | MAX_SLOT_ID


@dataclass(frozen=True, slots=True)
class RecordRef:
    """Physical location of one record version: the page that holds it and the slot inside that page."""

    page: PageIndex
    slot: SlotId

    def encode(self) -> int:
        """Pack this reference into a single integer as ``(page << 16) | slot``."""
        page = self.page
        slot = self.slot
        if (
            isinstance(page, bool)
            or isinstance(slot, bool)
            or not isinstance(page, int)
            or not isinstance(slot, int)
        ):
            raise GrafxCorruptionDetected(
                f"Record reference must hold integers: page={page!r}, slot={slot!r}.",
                page=page,
                slot=slot,
            )
        if not (0 <= page <= MAX_PAGE_INDEX) or not (0 <= slot <= MAX_SLOT_ID):
            raise GrafxCorruptionDetected(
                f"Record reference is outside the encodable range: page={page}, slot={slot}.",
                page=page,
                slot=slot,
            )
        return (page << _SLOT_BITS) | slot

    @classmethod
    def decode(cls, raw: int) -> RecordRef:
        """Unpack an integer produced by :meth:`encode` back into a reference."""
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise GrafxCorruptionDetected(
                f"Encoded record reference must be an integer: {raw!r}.",
                raw=raw,
            )
        if not (0 <= raw <= _MAX_ENCODED):
            raise GrafxCorruptionDetected(
                f"Encoded record reference is outside the decodable range: {raw}.",
                raw=raw,
            )
        return cls(page=raw >> _SLOT_BITS, slot=raw & MAX_SLOT_ID)


NULL_REF: RecordRef = RecordRef(NO_PAGE, 0)
"""The reference that points nowhere. It is never a valid location for a live version."""
