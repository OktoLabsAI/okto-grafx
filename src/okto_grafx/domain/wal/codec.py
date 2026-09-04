"""Decoding one write-ahead log record out of a buffer (CONTRACT.md section 6.5, SPEC-M1 TR-4).

Decoding is the half of the format that has to survive damage, so it never raises: it returns an
outcome saying either "here is a record and it is this many bytes long" or "this is what is
wrong here, and this is how far you may advance". The caller -- the log manager -- decides
whether to stop, to resynchronise, or to hand the failure to the ledger.

Two rules shape everything below.

* **The checksum is the authority on length.** A header can claim any length it likes; only a
  record whose CRC-32C matches has proved that its declared length describes real bytes. So a
  record is skipped by its own ``total_length`` only after the checksum has passed, and a
  failure before that point advances nothing and lets the caller resynchronise instead.
* **Reading is more tolerant than writing.** An unknown record type decodes, because a
  self-describing format exists so that a later build's record can be stepped over rather than
  declared corrupt. A version this build does not have is refused, because guessing at a layout
  is how a decoder invents data.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum

from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.wal.record import (
    CHECKSUM_LENGTH,
    SUPPORTED_FORMAT_VERSIONS,
    WAL_HEADER_LENGTH,
    WAL_MAGIC,
    HEADER_LENGTHS,
    WalRecord,
    v2_record_semantics_error,
)

__all__ = [
    "FailureReason",
    "DecodeOutcome",
    "decode_record",
    "MAGIC_BYTES",
]

MAGIC_BYTES: bytes = struct.pack("<I", WAL_MAGIC)
"""The magic as it appears on disk, which is what a resynchronising scanner searches for."""

_HEADER = struct.Struct("<IHHHHIQQQII")
_CHECKSUM = struct.Struct("<I")


class FailureReason(str, Enum):
    """Why a stretch of bytes is not a usable record.

    The values are stable strings so a ledger entry, a metric label or a report can carry them
    without translating an enum member into prose.
    """

    TRUNCATED_TAIL = "truncated_tail"
    BAD_MAGIC = "bad_magic"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNSUPPORTED_REQUIRED_RECORD = "unsupported_required_record"
    BAD_HEADER = "bad_header"
    CHECKSUM_FAILURE = "checksum_failure"
    UNREADABLE_DESCRIPTOR = "unreadable_descriptor"
    UNREPRESENTABLE_RECORD = "unrepresentable_record"
    UNREADABLE_SEGMENT = "unreadable_segment"
    LSN_DISCONTINUITY = "lsn_discontinuity"


@dataclass(frozen=True, slots=True)
class DecodeOutcome:
    """What one decode attempt produced, and how far the caller may advance.

    ``consumed`` is zero whenever the bytes at this offset cannot be trusted to describe their
    own length. A caller that meets zero must advance by searching, never by adding a length it
    just failed to verify -- that is the difference between resynchronising and inventing.
    """

    consumed: int
    record: WalRecord | None = None
    reason: FailureReason | None = None
    detail: str = ""
    checked: bool = False

    @property
    def ok(self) -> bool:
        """Return True when a record was decoded and its checksum matched."""
        return self.record is not None


def _truncated(available: int, wanted: int, what: str) -> DecodeOutcome:
    """Return the outcome for bytes that simply are not there."""
    return DecodeOutcome(
        consumed=available,
        reason=FailureReason.TRUNCATED_TAIL,
        detail=f"{what} needs {wanted} bytes and only {available} remain.",
    )


def decode_record(data: bytes, offset: int = 0) -> DecodeOutcome:
    """Decode the record that starts at the offset, without ever raising.

    ``data`` is a window of one segment and ``offset`` indexes into it. Nothing outside the
    window is read, so a record whose declared length passes the end of the window is a
    truncated tail rather than a read past the buffer.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return DecodeOutcome(
            consumed=0,
            reason=FailureReason.BAD_HEADER,
            detail=f"A record is decoded from bytes, not from {type(data).__name__}.",
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return DecodeOutcome(
            consumed=0,
            reason=FailureReason.BAD_HEADER,
            detail=f"An offset into a segment starts at zero; got {offset!r}.",
        )
    view = memoryview(data)
    available = len(view) - offset
    if available <= 0:
        return _truncated(max(available, 0), WAL_HEADER_LENGTH, "A record header")
    if available < WAL_HEADER_LENGTH:
        return _truncated(available, WAL_HEADER_LENGTH, "A record header")
    (
        magic,
        format_version,
        record_type,
        header_length,
        flags,
        total_length,
        lsn,
        epoch,
        txn_id,
        descriptor_length,
        payload_length,
    ) = _HEADER.unpack_from(view, offset)
    if magic != WAL_MAGIC:
        return DecodeOutcome(
            consumed=0,
            reason=FailureReason.BAD_MAGIC,
            detail=f"A record starts with {WAL_MAGIC:#010x} and these bytes start with {magic:#010x}.",
        )
    if format_version not in SUPPORTED_FORMAT_VERSIONS:
        return DecodeOutcome(
            consumed=0,
            reason=FailureReason.UNSUPPORTED_VERSION,
            detail=(
                f"This build reads format versions {SUPPORTED_FORMAT_VERSIONS} and this record "
                f"declares {format_version}."
            ),
        )
    declared_header = HEADER_LENGTHS[format_version]
    if header_length != declared_header:
        return DecodeOutcome(
            consumed=0,
            reason=FailureReason.BAD_HEADER,
            detail=(
                f"Format version {format_version} declares a {declared_header} byte header and "
                f"this record claims {header_length}."
            ),
        )
    expected_total = (
        header_length + descriptor_length + payload_length + CHECKSUM_LENGTH
    )
    if total_length != expected_total:
        return DecodeOutcome(
            consumed=0,
            reason=FailureReason.BAD_HEADER,
            detail=(
                f"A record of {descriptor_length} descriptor bytes and {payload_length} payload "
                f"bytes is {expected_total} bytes long and this one claims {total_length}."
            ),
        )
    if total_length > available:
        return _truncated(available, total_length, "This record")
    body_end = offset + total_length - CHECKSUM_LENGTH
    stored = _CHECKSUM.unpack_from(view, body_end)[0]
    computed = crc32c(bytes(view[offset:body_end]))
    if stored != computed:
        return DecodeOutcome(
            consumed=0,
            reason=FailureReason.CHECKSUM_FAILURE,
            detail=f"The record checksum is {stored:#010x} and its bytes compute {computed:#010x}.",
            checked=True,
        )
    descriptor_start = offset + header_length
    payload_start = descriptor_start + descriptor_length
    try:
        descriptor = bytes(view[descriptor_start:payload_start]).decode("utf-8")
    except UnicodeDecodeError as failure:
        # The checksum has already proved these are the bytes that were written, so the length
        # is trustworthy and the caller may step over the record. What it cannot do is read it.
        return DecodeOutcome(
            consumed=total_length,
            reason=FailureReason.UNREADABLE_DESCRIPTOR,
            detail=f"The descriptor of the record is not valid UTF-8 at byte {failure.start}.",
            checked=True,
        )
    try:
        record = WalRecord(
            record_type=record_type,
            payload=bytes(view[payload_start:body_end]),
            descriptor=descriptor,
            lsn=lsn,
            epoch=epoch,
            txn_id=txn_id,
            flags=flags,
            format_version=format_version,
        )
    except GrafxError as failure:
        # The record model refuses fields a CALLER must not write -- a descriptor past
        # MAX_DESCRIPTOR_BYTES is the reachable one -- and those limits do not bind bytes that
        # are already on disk. The checksum proved this record was written exactly as it stands,
        # so the length is trustworthy and the scan steps over it; what it must not do is raise.
        # A decoder that raises takes the whole log with it: the manager cannot finish opening,
        # and recovery's own repair door is behind that open.
        return DecodeOutcome(
            consumed=total_length,
            reason=FailureReason.UNREPRESENTABLE_RECORD,
            detail=f"The record is outside what this build can hold: {failure.message}",
            checked=True,
        )
    if format_version > 1:
        semantic_error = v2_record_semantics_error(record_type, flags)
        if semantic_error is not None:
            return DecodeOutcome(
                consumed=total_length,
                reason=FailureReason.UNSUPPORTED_REQUIRED_RECORD,
                detail=semantic_error,
                checked=True,
            )
    return DecodeOutcome(consumed=total_length, record=record, checked=True)
