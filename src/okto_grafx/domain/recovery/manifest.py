"""The manifest that makes a quarantined range readable a year later (SPEC-M1 FR-10, BR-1).

FR-10 asks for "what, when, why, digests" beside every quarantined copy, and CONTRACT.md section
6.1 puts it at ``quarantine/<stamp>-<name>/manifest.json``. This module is the manifest itself:
a flat record in the component's deterministic text form, plus the two derived values that make
a quarantine entry identifiable without reading it.

**The entry name is derived, and only its stamp is not.** ``entry_name`` is
``<stamp>-<origin>-<offset>-<length>``, where the stamp orders entries chronologically and the
rest identifies the range. The suffix is a pure function of the origin, so a second recovery pass
over the same damage recognises the copy it already made instead of making another -- which is
what lets recovery be interrupted and re-run without the quarantine growing a duplicate every
time (AC-4). The stamp is deliberately not part of that identity: the same damage found at two
different moments is the same damage.

**A manifest is never rewritten.** A restore adds a receipt beside it; nothing edits what was
captured. That is the whole of "quarantine must never destroy the evidence it exists to
preserve" expressed at the level of one entry.
"""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.ledger.textform import decode_fields, encode_fields

__all__ = [
    "MANIFEST_FILE_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_NAME_CHARACTERS",
    "RESTORE_RECEIPT_PREFIX",
    "STAMP_DIGITS",
    "QuarantineManifest",
    "RestoreReceipt",
    "entry_suffix",
    "sanitize_name",
    "stamp_of",
]

MANIFEST_FILE_NAME: str = "manifest.json"
"""The name CONTRACT.md section 6.1 gives the manifest inside a quarantine entry."""

RESTORE_RECEIPT_PREFIX: str = "restore-"
"""Restores add ``restore-<n>.json`` beside the manifest; nothing ever replaces the manifest."""

MANIFEST_SCHEMA_VERSION: int = 1
"""Version of the manifest fields, so a later reader knows what it is looking at."""

STAMP_DIGITS: int = 20
"""Digits of the wall-clock stamp, zero padded so a plain name sort is a time sort."""

MAX_NAME_CHARACTERS: int = 64
"""Longest sanitized origin fragment inside an entry name, so no path limit is at risk."""

_SAFE_CHARACTERS: str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"

_SCHEMA = "schema"
_ORIGIN = "origin"
_OFFSET = "offset"
_LENGTH = "length"
_REASON = "reason"
_DETAIL = "detail"
_CAPTURED_AT_WALL = "captured_at_wall"
_DIGEST = "digest"
_PAYLOAD_FILE = "payload_file"
_EXPECTED_LSN = "expected_lsn"
_ENTRY_NAME = "entry_name"
_RESTORED_TO = "restored_to"
_RESTORED_AT_WALL = "restored_at_wall"
_RESTORED_BYTES = "restored_bytes"


def sanitize_name(value: str) -> str:
    """Return a fragment safe to use in a file name on both families.

    Path separators, drive letters and anything outside a small ASCII set become an underscore,
    so an origin like ``wal/000000000001.wal`` becomes ``wal_000000000001.wal``. The result is
    a NAME, never a path: a quarantine entry that could be steered outside its own directory by
    the name of the file it is preserving would be a door, not a copy.
    """
    if not isinstance(value, str) or not value:
        raise GrafxConfigurationError(
            "A quarantine name is built from a non-empty string.",
            field="name",
            value=repr(value),
        )
    cleaned = "".join(character if character in _SAFE_CHARACTERS else "_" for character in value)
    cleaned = cleaned.strip("._") or "unnamed"
    if len(cleaned) > MAX_NAME_CHARACTERS:
        cleaned = cleaned[-MAX_NAME_CHARACTERS:].strip("._") or "unnamed"
    return cleaned


def stamp_of(captured_at_wall: float) -> str:
    """Return the zero-padded chronological stamp an entry name begins with."""
    if isinstance(captured_at_wall, bool) or not isinstance(captured_at_wall, (int, float)):
        raise GrafxConfigurationError(
            f"A quarantine stamp is built from a number; got {type(captured_at_wall).__name__}.",
            field="captured_at_wall",
            value=repr(captured_at_wall),
        )
    whole = int(captured_at_wall)
    if whole < 0:
        whole = 0
    return format(whole, "0" + str(STAMP_DIGITS) + "d")[-STAMP_DIGITS:]


def entry_suffix(origin: str, offset: int, length: int) -> str:
    """Return the part of an entry name that identifies the range, independent of the moment.

    Two captures of the same range share this suffix, which is how a re-run of recovery finds the
    copy it already made rather than making a second one.
    """
    for field, value in (("offset", offset), ("length", length)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GrafxConfigurationError(
                f"A quarantine {field} must be a non-negative integer; got {value!r}.",
                field=field,
                value=repr(value),
            )
    return f"{sanitize_name(origin)}-{offset}-{length}"


@dataclass(frozen=True, slots=True)
class QuarantineManifest:
    """What was copied into quarantine, when, why, and the digest that proves it."""

    origin: str
    offset: int
    length: int
    reason: str
    detail: str
    captured_at_wall: float
    digest: str
    payload_file: str
    entry_name: str
    expected_lsn: Lsn = NO_LSN
    schema: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Refuse a manifest that could not describe a real capture."""
        for name, value in (
            (_ORIGIN, self.origin),
            (_REASON, self.reason),
            (_DIGEST, self.digest),
            (_PAYLOAD_FILE, self.payload_file),
            (_ENTRY_NAME, self.entry_name),
        ):
            if not isinstance(value, str) or not value:
                raise GrafxConfigurationError(
                    f"A quarantine manifest needs a non-empty {name}.",
                    field=name,
                    value=repr(value),
                )
        if not isinstance(self.detail, str):
            raise GrafxConfigurationError(
                f"A quarantine manifest detail must be a string; got "
                f"{type(self.detail).__name__}.",
                field=_DETAIL,
                value=type(self.detail).__name__,
            )
        for name, value in (
            (_OFFSET, self.offset),
            (_LENGTH, self.length),
            (_EXPECTED_LSN, self.expected_lsn),
            (_SCHEMA, self.schema),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GrafxConfigurationError(
                    f"The {name} of a quarantine manifest must be a non-negative integer; "
                    f"got {value!r}.",
                    field=name,
                    value=repr(value),
                )

    @property
    def suffix(self) -> str:
        """Return the origin-derived part of the entry name."""
        return entry_suffix(self.origin, self.offset, self.length)

    def fields(self) -> dict[str, object]:
        """Return the manifest as the flat mapping the text form writes."""
        return {
            _SCHEMA: self.schema,
            _ORIGIN: self.origin,
            _OFFSET: self.offset,
            _LENGTH: self.length,
            _REASON: self.reason,
            _DETAIL: self.detail,
            _CAPTURED_AT_WALL: float(self.captured_at_wall),
            _DIGEST: self.digest,
            _PAYLOAD_FILE: self.payload_file,
            _ENTRY_NAME: self.entry_name,
            _EXPECTED_LSN: self.expected_lsn,
        }

    def serialize(self) -> bytes:
        """Return the manifest bytes exactly as they are written into the entry."""
        return encode_fields(self.fields())

    @classmethod
    def parse(cls, raw: bytes) -> QuarantineManifest:
        """Read a manifest back, refusing bytes that are not one.

        A manifest that will not parse is damaged evidence, so the refusal is
        ``corruption_detected`` and it names the entry rather than the parser.
        """
        fields = decode_fields(raw)
        missing = [
            name
            for name in (_ORIGIN, _OFFSET, _LENGTH, _REASON, _DIGEST, _PAYLOAD_FILE, _ENTRY_NAME)
            if name not in fields
        ]
        if missing:
            raise GrafxCorruptionDetected(
                f"A quarantine manifest is missing the field {missing[0]!r}.",
                field=missing[0],
            )
        return cls(
            origin=_text(fields, _ORIGIN),
            offset=_number(fields, _OFFSET),
            length=_number(fields, _LENGTH),
            reason=_text(fields, _REASON),
            detail=_text(fields, _DETAIL),
            captured_at_wall=_real(fields, _CAPTURED_AT_WALL),
            digest=_text(fields, _DIGEST),
            payload_file=_text(fields, _PAYLOAD_FILE),
            entry_name=_text(fields, _ENTRY_NAME),
            expected_lsn=_number(fields, _EXPECTED_LSN),
            schema=_number(fields, _SCHEMA) or MANIFEST_SCHEMA_VERSION,
        )


@dataclass(frozen=True, slots=True)
class RestoreReceipt:
    """The audit record one restore leaves inside the quarantine entry it read (FR-10)."""

    entry_name: str
    restored_to: str
    restored_bytes: int
    restored_at_wall: float
    digest: str
    schema: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Refuse a receipt that does not say what was restored and to where."""
        for name, value in (
            (_ENTRY_NAME, self.entry_name),
            (_RESTORED_TO, self.restored_to),
            (_DIGEST, self.digest),
        ):
            if not isinstance(value, str) or not value:
                raise GrafxConfigurationError(
                    f"A restore receipt needs a non-empty {name}.", field=name, value=repr(value)
                )
        if isinstance(self.restored_bytes, bool) or not isinstance(self.restored_bytes, int):
            raise GrafxConfigurationError(
                "A restore receipt counts bytes as an integer.",
                field=_RESTORED_BYTES,
                value=repr(self.restored_bytes),
            )

    def serialize(self) -> bytes:
        """Return the receipt bytes exactly as they are written beside the manifest."""
        return encode_fields(
            {
                _SCHEMA: self.schema,
                _ENTRY_NAME: self.entry_name,
                _RESTORED_TO: self.restored_to,
                _RESTORED_BYTES: self.restored_bytes,
                _RESTORED_AT_WALL: float(self.restored_at_wall),
                _DIGEST: self.digest,
            }
        )


def _text(fields: dict[str, object], name: str) -> str:
    """Return one string field of a parsed manifest, refusing a value of the wrong shape."""
    value = fields.get(name, "")
    if not isinstance(value, str):
        raise GrafxCorruptionDetected(
            f"The {name} of a quarantine manifest is a {type(value).__name__}.",
            field=name,
            value=type(value).__name__,
        )
    return value


def _number(fields: dict[str, object], name: str) -> int:
    """Return one integer field of a parsed manifest, refusing a value of the wrong shape."""
    value = fields.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GrafxCorruptionDetected(
            f"The {name} of a quarantine manifest is not a non-negative integer.",
            field=name,
            value=repr(value),
        )
    return value


def _real(fields: dict[str, object], name: str) -> float:
    """Return one floating field of a parsed manifest, refusing a value of the wrong shape."""
    value = fields.get(name, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrafxCorruptionDetected(
            f"The {name} of a quarantine manifest is not a number.",
            field=name,
            value=repr(value),
        )
    return float(value)
