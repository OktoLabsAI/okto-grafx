"""A deterministic, dependency-free text form for the flat records C6 keeps beside its bytes.

The quarantine manifest of CONTRACT.md section 6.1 is named ``manifest.json``, and the ledger
payload envelope of section 6.6 carries the provenance of the bytes it wraps. Both are JSON, and
neither may use the standard library's JSON module: G2 keeps mechanism and undeclared imports out
of ``domain/`` and ``engine/``, and the import-boundary gate lists exactly which standard-library
modules the pure core may reach for.

So the writer here emits a strict subset of JSON -- one object, scalar values only, keys sorted,
no insignificant whitespace -- and the reader accepts exactly that subset and refuses the rest.
Two properties are what the rest of the component rests on:

* **The output is a function of the mapping alone.** Sorted keys and a fixed number format mean
  the same fields produce the same bytes on every platform and in every process, so a digest over
  a manifest identifies the manifest rather than the run that wrote it.
* **A parse failure is damage, never a caller's mistake.** These bytes are only ever read back off
  a device, so a manifest that will not parse is a damaged manifest: it answers
  ``corruption_detected`` (A11-revised). Refusing a value the writer cannot encode is the mirror
  case -- that is an argument, and it answers ``configuration_error``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected

__all__ = [
    "MAX_TEXT_FORM_BYTES",
    "decode_fields",
    "encode_fields",
]

MAX_TEXT_FORM_BYTES: int = 1 << 20
"""Longest text record this reader will parse, so a damaged length cannot buy unbounded work."""

_ESCAPES: dict[str, str] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

_UNESCAPES: dict[str, str] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

_LITERALS: tuple[tuple[str, object], ...] = (
    ("true", True),
    ("false", False),
    ("null", None),
)

_NUMBER_CHARACTERS: str = "+-0123456789.eE"
_HEX_DIGITS: str = "0123456789abcdefABCDEF"
_WHITESPACE: str = " \t\r\n"

_HIGH_SURROGATE_FIRST: int = 0xD800
_HIGH_SURROGATE_LAST: int = 0xDBFF
_LOW_SURROGATE_FIRST: int = 0xDC00
_LOW_SURROGATE_LAST: int = 0xDFFF


def _encode_string(value: str) -> str:
    """Return the value as a quoted, ASCII-only string literal.

    ``\\uXXXX`` carries exactly four hexadecimal digits, so a character above the basic plane
    does not fit in one escape and is written as the SURROGATE PAIR the format defines for it.
    Formatting its code point in one escape produced five digits -- ``\\u1f600`` for an emoji --
    and the reader, correctly consuming four, read that back as two different characters. That is
    LESSONS L5's second lesson at a serialisation boundary: a units question that ``format`` will
    answer wrongly without complaining. It is not cosmetic here, because a quarantine manifest
    carries the ORIGIN NAME of the bytes it preserves: a lossy round trip means a manifest that
    names a file nobody can find, and two distinct origins that come back as one.
    """
    parts: list[str] = ['"']
    for character in value:
        escape = _ESCAPES.get(character)
        if escape is not None:
            parts.append(escape)
        elif " " <= character <= "~":
            parts.append(character)
        else:
            parts.append(_escaped_code_point(ord(character)))
    parts.append('"')
    return "".join(parts)


def _escaped_code_point(code_point: int) -> str:
    """Return one code point as the ``\\uXXXX`` escapes the format can carry it in.

    A LONE surrogate is refused here rather than written. The reader refuses one -- correctly,
    because a surrogate that is not part of a pair cannot be encoded back to UTF-8 and is damage
    when it comes off a device -- and an encoder that can emit what the reader refuses makes the
    component report its own freshly written bytes as corruption through a FROZEN section 8.6
    door. A guard on the decode side is the one that matters for damage arriving from outside
    (LESSONS L14); this is its other half, and without it the pair is a false integrity incident
    waiting for a caller who holds an unpaired code point.
    """
    if _HIGH_SURROGATE_FIRST <= code_point <= _LOW_SURROGATE_LAST:
        raise GrafxConfigurationError(
            f"A lone surrogate (U+{code_point:04X}) is not a character and the text form "
            f"cannot carry one; the reader would refuse the bytes this would write.",
            field="text",
            value=f"U+{code_point:04X}",
        )
    if code_point <= 0xFFFF:
        return "\\u" + format(code_point, "04x")
    offset = code_point - 0x10000
    high = _HIGH_SURROGATE_FIRST + (offset >> 10)
    low = _LOW_SURROGATE_FIRST + (offset & 0x3FF)
    return "\\u" + format(high, "04x") + "\\u" + format(low, "04x")


def _encode_scalar(field: str, value: object) -> str:
    """Return one value in its text form, refusing anything this subset cannot carry."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GrafxConfigurationError(
                f"Field {field!r} carries {value!r}, which no text form can represent.",
                field=field,
                value=repr(value),
            )
        return repr(value)
    if isinstance(value, str):
        return _encode_string(value)
    raise GrafxConfigurationError(
        f"Field {field!r} is a {type(value).__name__}, which this text form does not carry.",
        field=field,
        value=type(value).__name__,
    )


def encode_fields(fields: Mapping[str, object]) -> bytes:
    """Return the flat mapping as one deterministic ASCII object.

    Keys are sorted, so two mappings that are equal produce identical bytes and a digest over the
    result identifies the content rather than the insertion order it was built in.
    """
    if not isinstance(fields, Mapping):
        raise GrafxConfigurationError(
            f"A text record is built from a mapping; got {type(fields).__name__}.",
            field="fields",
            value=type(fields).__name__,
        )
    parts: list[str] = []
    for key in sorted(fields):
        if not isinstance(key, str):
            raise GrafxConfigurationError(
                f"A text record field name must be a string; got {type(key).__name__}.",
                field="key",
                value=repr(key),
            )
        parts.append(_encode_string(key) + ":" + _encode_scalar(key, fields[key]))
    return ("{" + ",".join(parts) + "}").encode("ascii")


def _damaged(detail: str, offset: int) -> GrafxCorruptionDetected:
    """Return the refusal for text that is not a record of this subset."""
    return GrafxCorruptionDetected(
        f"{detail} at byte {offset} of the text record.",
        field="text_form",
        offset=offset,
    )


class _Reader:
    """A cursor over one text record, which refuses everything outside the written subset."""

    __slots__ = ("_text", "_at")

    def __init__(self, text: str) -> None:
        """Start at the first character of the record."""
        self._text = text
        self._at = 0

    @property
    def offset(self) -> int:
        """Return how far into the record the cursor has reached."""
        return self._at

    def skip_spaces(self) -> None:
        """Step over the whitespace this subset tolerates between tokens."""
        while self._at < len(self._text) and self._text[self._at] in _WHITESPACE:
            self._at += 1

    def peek(self) -> str:
        """Return the character under the cursor, or the empty string at the end."""
        return self._text[self._at] if self._at < len(self._text) else ""

    def take(self, expected: str) -> None:
        """Consume one expected character, refusing anything else."""
        if self.peek() != expected:
            raise _damaged(f"Expected {expected!r}", self._at)
        self._at += 1

    def read_string(self) -> str:
        """Read one quoted string, resolving the escapes the writer emits."""
        self.take('"')
        out: list[str] = []
        while True:
            if self._at >= len(self._text):
                raise _damaged("A string never ends", self._at)
            character = self._text[self._at]
            self._at += 1
            if character == '"':
                return "".join(out)
            if character != "\\":
                out.append(character)
                continue
            if self._at >= len(self._text):
                raise _damaged("An escape never ends", self._at)
            marker = self._text[self._at]
            self._at += 1
            simple = _UNESCAPES.get(marker)
            if simple is not None:
                out.append(simple)
                continue
            if marker != "u":
                raise _damaged(f"Unknown escape {marker!r}", self._at)
            out.append(chr(self._read_code_point()))

    def _read_hex_escape(self) -> int:
        """Consume the four hexadecimal digits of one ``\\uXXXX`` escape and return its value."""
        digits = self._text[self._at : self._at + 4]
        if len(digits) < 4 or any(digit not in _HEX_DIGITS for digit in digits):
            raise _damaged("A unicode escape needs four hexadecimal digits", self._at)
        self._at += 4
        return int(digits, 16)

    def _read_code_point(self) -> int:
        """Return one code point, joining a surrogate PAIR and refusing a lone surrogate.

        LESSONS L14: the writer is not the only way these bytes are produced -- they are read
        back off a device, so the format is a public door and the guard belongs on the DECODE
        side. A lone surrogate is not a character: it cannot be encoded back to UTF-8, so a
        manifest carrying one would parse here and then raise a ``UnicodeEncodeError`` -- a
        non-``Grafx*`` escape -- at whatever later point re-serialised or logged it. These bytes
        only ever come off a device, so a surrogate that is not part of a pair is damage
        (A11-revised), and it is refused here where the offset is still known.
        """
        first = self._read_hex_escape()
        if not _HIGH_SURROGATE_FIRST <= first <= _HIGH_SURROGATE_LAST:
            if _LOW_SURROGATE_FIRST <= first <= _LOW_SURROGATE_LAST:
                raise _damaged(
                    f"The escape \\u{first:04x} is a trailing surrogate with no leading one",
                    self._at,
                )
            return first
        if self._text[self._at : self._at + 2] != "\\u":
            raise _damaged(
                f"The escape \\u{first:04x} is a leading surrogate with no trailing one",
                self._at,
            )
        self._at += 2
        second = self._read_hex_escape()
        if not _LOW_SURROGATE_FIRST <= second <= _LOW_SURROGATE_LAST:
            raise _damaged(
                f"The escape \\u{first:04x} is followed by \\u{second:04x}, which is not a "
                "trailing surrogate",
                self._at,
            )
        return (
            0x10000
            + ((first - _HIGH_SURROGATE_FIRST) << 10)
            + (second - _LOW_SURROGATE_FIRST)
        )

    def read_scalar(self) -> object:
        """Read one value: a string, a number, a boolean, or null."""
        if self.peek() == '"':
            return self.read_string()
        for name, value in _LITERALS:
            if self._text.startswith(name, self._at):
                self._at += len(name)
                return value
        start = self._at
        while self._at < len(self._text) and self._text[self._at] in _NUMBER_CHARACTERS:
            self._at += 1
        token = self._text[start : self._at]
        if not token:
            raise _damaged("A value is missing", start)
        try:
            if any(mark in token for mark in ".eE"):
                return float(token)
            return int(token)
        except ValueError as failure:
            raise _damaged(f"{token!r} is not a number", start) from failure


def decode_fields(raw: bytes) -> dict[str, object]:
    """Parse one text record back into the flat mapping it was written from."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise GrafxCorruptionDetected(
            f"A text record is bytes; got {type(raw).__name__}.",
            field="text_form",
            value=type(raw).__name__,
        )
    data = bytes(raw)
    if len(data) > MAX_TEXT_FORM_BYTES:
        raise GrafxCorruptionDetected(
            f"A text record of {len(data)} bytes is past the {MAX_TEXT_FORM_BYTES} this reader "
            "will parse.",
            field="text_form",
            value=len(data),
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as failure:
        raise GrafxCorruptionDetected(
            "A text record is not valid UTF-8.", field="text_form", offset=failure.start
        ) from failure
    reader = _Reader(text)
    reader.skip_spaces()
    reader.take("{")
    fields: dict[str, object] = {}
    reader.skip_spaces()
    if reader.peek() == "}":
        reader.take("}")
    else:
        while True:
            reader.skip_spaces()
            key = reader.read_string()
            if key in fields:
                raise _damaged(f"Field {key!r} appears twice", reader.offset)
            reader.skip_spaces()
            reader.take(":")
            reader.skip_spaces()
            fields[key] = reader.read_scalar()
            reader.skip_spaces()
            if reader.peek() == ",":
                reader.take(",")
                continue
            reader.take("}")
            break
    reader.skip_spaces()
    if reader.peek():
        raise _damaged("A text record carries trailing bytes", reader.offset)
    return fields
