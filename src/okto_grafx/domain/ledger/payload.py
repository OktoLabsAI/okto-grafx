"""The envelope that gives a ledger payload its provenance (SPEC-M1 FR-9, TR-5).

CONTRACT.md section 6.6 freezes the entry header, and the header has room for the sequence
numbers, the epoch and the reason -- but not for the file the bytes came from, the offset they sat
at, or which quarantine entry now holds a copy. FR-9 requires a forensic entry to carry the raw
bytes, the offset, the expected sequence number, the reason and a digest, so the three the frozen
header has no room for travel inside the payload, in front of the bytes themselves.

    u16 header_len | header (the deterministic text form) | body

The split is what makes ``LedgerStore.export`` honest. The digest in the frozen header covers the
WHOLE payload, envelope included, so the entry proves that the provenance and the bytes were
written together; ``export`` verifies that digest and then returns the BODY, which is the range as
it was found on the device -- byte for byte, with nothing of this component's added to it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.ledger.textform import decode_fields, encode_fields

__all__ = [
    "MAX_ENVELOPE_HEADER_BYTES",
    "LedgerPayload",
    "decode_envelope",
    "decode_payload",
    "encode_payload",
]

_HEADER_LENGTH: struct.Struct = struct.Struct("<H")

MAX_ENVELOPE_HEADER_BYTES: int = 0xFFFF
"""Longest provenance header an envelope carries: the length prefix is sixteen bits."""

_ORIGIN = "origin"
_OFFSET = "offset"
_LENGTH = "length"
_EXPECTED_LSN = "expected_lsn"
_RECORD_TYPE = "record_type"
_DETAIL = "detail"
_FAILURE = "failure"
_QUARANTINE = "quarantine"


@dataclass(frozen=True, slots=True)
class LedgerPayload:
    """Where a discarded range came from, and the range itself."""

    origin: str
    offset: int = 0
    length: int = 0
    expected_lsn: Lsn = NO_LSN
    record_type: int = 0
    failure: str = ""
    detail: str = ""
    quarantine: str = ""
    body: bytes = b""

    def __post_init__(self) -> None:
        """Refuse an envelope that could not describe a real range."""
        if not isinstance(self.origin, str) or not self.origin:
            raise GrafxConfigurationError(
                "A ledger payload must name the file its bytes came from.",
                field="origin",
                value=repr(self.origin),
            )
        for field, value in (
            (_OFFSET, self.offset),
            (_LENGTH, self.length),
            (_EXPECTED_LSN, self.expected_lsn),
            (_RECORD_TYPE, self.record_type),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GrafxConfigurationError(
                    f"The {field} of a ledger payload must be a non-negative integer; "
                    f"got {value!r}.",
                    field=field,
                    value=repr(value),
                )
        for field, value in (
            (_FAILURE, self.failure),
            (_DETAIL, self.detail),
            (_QUARANTINE, self.quarantine),
        ):
            if not isinstance(value, str):
                raise GrafxConfigurationError(
                    f"The {field} of a ledger payload must be a string; "
                    f"got {type(value).__name__}.",
                    field=field,
                    value=type(value).__name__,
                )
        if not isinstance(self.body, (bytes, bytearray, memoryview)):
            raise GrafxConfigurationError(
                f"A ledger payload body must be bytes; got {type(self.body).__name__}.",
                field="body",
                value=type(self.body).__name__,
            )
        if not isinstance(self.body, bytes):
            object.__setattr__(self, "body", bytes(self.body))

    def fields(self) -> dict[str, object]:
        """Return the provenance as the flat mapping the text form writes."""
        return {
            _ORIGIN: self.origin,
            _OFFSET: self.offset,
            _LENGTH: self.length,
            _EXPECTED_LSN: self.expected_lsn,
            _RECORD_TYPE: self.record_type,
            _FAILURE: self.failure,
            _DETAIL: self.detail,
            _QUARANTINE: self.quarantine,
        }


def encode_payload(payload: LedgerPayload) -> bytes:
    """Return the envelope followed by the bytes it describes."""
    header = encode_fields(payload.fields())
    if len(header) > MAX_ENVELOPE_HEADER_BYTES:
        raise GrafxConfigurationError(
            f"A ledger payload envelope of {len(header)} bytes is past the "
            f"{MAX_ENVELOPE_HEADER_BYTES} its length prefix can describe.",
            field="header_len",
            value=len(header),
        )
    return _HEADER_LENGTH.pack(len(header)) + header + payload.body


def decode_envelope(raw: bytes) -> LedgerPayload:
    """Parse the provenance of an envelope WITHOUT copying the bytes it wraps.

    The ledger indexes every entry it holds by the range that entry describes, so it reads this
    for each entry at load. Slicing a body of up to a megabyte per entry to answer a question
    about its offset would make opening the ledger cost the size of the ledger.
    """
    return _decode(raw, with_body=False)


def decode_payload(raw: bytes) -> LedgerPayload:
    """Parse an envelope back into its provenance and its body."""
    return _decode(raw, with_body=True)


def _decode(raw: bytes, *, with_body: bool) -> LedgerPayload:
    """Parse an envelope, taking the body it wraps only when the caller wants it."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise GrafxCorruptionDetected(
            f"A ledger payload is decoded from bytes; got {type(raw).__name__}.",
            field="payload",
            value=type(raw).__name__,
        )
    data = bytes(raw)
    if len(data) < _HEADER_LENGTH.size:
        raise GrafxCorruptionDetected(
            f"A ledger payload needs {_HEADER_LENGTH.size} bytes for its envelope length and "
            f"has {len(data)}.",
            field="header_len",
            value=len(data),
        )
    header_length = _HEADER_LENGTH.unpack_from(data, 0)[0]
    start = _HEADER_LENGTH.size
    end = start + header_length
    if end > len(data):
        raise GrafxCorruptionDetected(
            f"A ledger payload declares a {header_length}-byte envelope and carries "
            f"{len(data) - start}.",
            field="header_len",
            value=header_length,
        )
    fields = decode_fields(data[start:end])
    return LedgerPayload(
        origin=_text(fields, _ORIGIN),
        offset=_number(fields, _OFFSET),
        length=_number(fields, _LENGTH),
        expected_lsn=_number(fields, _EXPECTED_LSN),
        record_type=_number(fields, _RECORD_TYPE),
        failure=_text(fields, _FAILURE),
        detail=_text(fields, _DETAIL),
        quarantine=_text(fields, _QUARANTINE),
        body=data[end:] if with_body else b"",
    )


def _text(fields: dict[str, object], name: str) -> str:
    """Return one string field of a parsed envelope, refusing a value of the wrong shape."""
    value = fields.get(name, "")
    if not isinstance(value, str):
        raise GrafxCorruptionDetected(
            f"The {name} of a ledger payload envelope is a {type(value).__name__}.",
            field=name,
            value=type(value).__name__,
        )
    return value


def _number(fields: dict[str, object], name: str) -> int:
    """Return one integer field of a parsed envelope, refusing a value of the wrong shape."""
    value = fields.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GrafxCorruptionDetected(
            f"The {name} of a ledger payload envelope is not a non-negative integer.",
            field=name,
            value=repr(value),
        )
    return value
