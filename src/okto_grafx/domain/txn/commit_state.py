"""The published commit state (CONTRACT.md sections 6.1 and 8.5 step 3.7).

``control/commit.state`` is the one place a process that is not committing can learn what the
database has committed. It is published by ``atomic_replace`` as the LAST act of a commit, after
the log barrier has returned and after the pages of that commit have been applied. That order is
the whole of snapshot isolation across processes: a reader picks its snapshot from this file, so
a number it can read is a number whose pages are already in place, and a partially applied commit
can never be chosen as a snapshot. That order is proved by
``test_no_instant_of_a_commit_offers_a_snapshot_of_half_of_it`` in the C5 suite.

The record is small, self-describing and checksummed. It is a control-plane record, never
replayed and never part of the durable data path, so amendment A14 leaves the checksum choice
open; CRC-32C is used anyway because it is already here and one checksum is easier to reason
about than two.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_CSN, NO_LSN, Csn, Lsn
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.page.layout import MAX_U64

__all__ = [
    "COMMIT_STATE_FILE",
    "COMMIT_STATE_FORMAT_VERSION",
    "COMMIT_STATE_LEGACY_FORMAT_VERSION",
    "COMMIT_STATE_MAGIC",
    "COMMIT_STATE_SIZE",
    "CommitState",
]

COMMIT_STATE_FILE: str = "control/commit.state"
"""Where the published state lives inside a database directory (CONTRACT.md section 6.1)."""

COMMIT_STATE_MAGIC: int = 0x5343474F
"""Four ASCII bytes, 'OGCS', so a file that is not this record is recognised as such."""

COMMIT_STATE_LEGACY_FORMAT_VERSION: int = 1
"""Format written until a feature fence explicitly upgrades the database."""

COMMIT_STATE_FORMAT_VERSION: int = 2
"""Version of this record. A reader accepts every version at or below its own."""

_BODY: struct.Struct = struct.Struct("<IHHQQQ")
_CHECKSUM: struct.Struct = struct.Struct("<I")
COMMIT_STATE_SIZE: int = _BODY.size + _CHECKSUM.size
"""Exact size of one published record: the body plus its checksum."""


def _require_lsn(label: str, value: int) -> int:
    """Return the value when it is a storable log sequence number, else refuse it."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"{label} must be an integer; got {type(value).__name__}.",
            field=label,
            value=repr(value),
        )
    if not 0 <= value <= MAX_U64:
        raise GrafxConfigurationError(
            f"{label} must fit in 64 bits; got {value}.", field=label, value=value
        )
    return value


@dataclass(frozen=True, slots=True)
class CommitState:
    """What the database has committed: the last committed LSN, the last CSN and the checkpoint."""

    last_committed_lsn: Lsn = NO_LSN
    last_csn: Csn = NO_CSN
    checkpoint_lsn: Lsn = NO_LSN
    format_version: int = COMMIT_STATE_LEGACY_FORMAT_VERSION

    def __post_init__(self) -> None:
        """Refuse a state whose numbers could not be stored in the record."""
        _require_lsn("last_committed_lsn", self.last_committed_lsn)
        _require_lsn("last_csn", self.last_csn)
        _require_lsn("checkpoint_lsn", self.checkpoint_lsn)
        if (
            isinstance(self.format_version, bool)
            or not isinstance(self.format_version, int)
            or not COMMIT_STATE_LEGACY_FORMAT_VERSION
            <= self.format_version
            <= COMMIT_STATE_FORMAT_VERSION
        ):
            raise GrafxConfigurationError(
                f"format_version must be between {COMMIT_STATE_LEGACY_FORMAT_VERSION} and "
                f"{COMMIT_STATE_FORMAT_VERSION}; got {self.format_version!r}.",
                field="format_version",
                value=repr(self.format_version),
            )

    def encode(self) -> bytes:
        """Return the bytes of the published record, checksum included."""
        body = _BODY.pack(
            COMMIT_STATE_MAGIC,
            self.format_version,
            0,
            self.last_committed_lsn,
            self.last_csn,
            self.checkpoint_lsn,
        )
        return body + _CHECKSUM.pack(crc32c(body))

    @classmethod
    def decode(cls, raw: bytes) -> CommitState:
        """Parse a published record, refusing bytes that are not one.

        Damage here is damage: the file is read by every process that opens a transaction, and a
        reader that accepted a garbled record would hand out a snapshot number from nowhere.
        """
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise GrafxCorruptionDetected(
                f"A commit state must be bytes; got {type(raw).__name__}.",
                file=COMMIT_STATE_FILE,
                field="payload",
                value=type(raw).__name__,
            )
        data = bytes(raw)
        if len(data) != COMMIT_STATE_SIZE:
            raise GrafxCorruptionDetected(
                f"A commit state is {COMMIT_STATE_SIZE} bytes; this one is {len(data)}.",
                file=COMMIT_STATE_FILE,
                field="length",
                value=len(data),
            )
        magic, version, _reserved, committed, csn, checkpoint = _BODY.unpack_from(data, 0)
        if magic != COMMIT_STATE_MAGIC:
            raise GrafxCorruptionDetected(
                "The commit state does not start with its magic number.",
                file=COMMIT_STATE_FILE,
                field="magic",
                value=magic,
            )
        (stored,) = _CHECKSUM.unpack_from(data, _BODY.size)
        computed = crc32c(data[: _BODY.size])
        if stored != computed:
            raise GrafxCorruptionDetected(
                "The commit state failed its own checksum.",
                file=COMMIT_STATE_FILE,
                field="checksum",
                value=stored,
                computed=computed,
            )
        if version > COMMIT_STATE_FORMAT_VERSION:
            # Intact bytes from a newer build are not damage, and calling them damage would
            # start a truncation and a forensic ledger entry over a version number
            # (A11-revised). The honest answer is that this build cannot read them.
            raise GrafxSchemaVersionMismatch(
                f"The commit state is format version {version}; this build reads up to "
                f"{COMMIT_STATE_FORMAT_VERSION}.",
                file=COMMIT_STATE_FILE,
                field="format_version",
                value=version,
                supported=COMMIT_STATE_FORMAT_VERSION,
            )
        if version < COMMIT_STATE_LEGACY_FORMAT_VERSION:
            raise GrafxCorruptionDetected(
                f"The commit state declares invalid format version {version}.",
                file=COMMIT_STATE_FILE,
                field="format_version",
                value=version,
            )
        return cls(
            last_committed_lsn=committed,
            last_csn=csn,
            checkpoint_lsn=checkpoint,
            format_version=version,
        )
