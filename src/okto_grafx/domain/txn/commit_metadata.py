"""Bounded, immutable commit metadata admission (GX-CAP-1A).

Canonical bytes also define the nested v1 body in COMMIT_CATALOG_V1.md. The
development public begin door captures these bytes again before IO; native
publication/recovery persists them atomically with the corresponding commit.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Mapping, TypeAlias, cast

from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch,
    GrafxTransactionBudgetExceeded,
)

MetadataValue: TypeAlias = (
    bool | int | float | str | None | tuple["MetadataValue", ...] | Mapping[str, "MetadataValue"]
)
_U32 = struct.Struct("<I")
_INT = struct.Struct("<q")
_DOUBLE = struct.Struct("<d")
MAX_METADATA_BYTES = 65_536
_LIMIT_RANGES = (
    ("max_bytes", 14, MAX_METADATA_BYTES), ("max_attributes", 0, 256),
    ("max_key_bytes", 1, 1_024), ("max_string_bytes", 1, 16_384),
    ("max_depth", 0, 16), ("max_values", 1, 4_096),
)


def _invalid(field: str, reason: str) -> GrafxConfigurationError:
    # No supplied key, value, repr or exception text belongs in diagnostics.
    return GrafxConfigurationError("Invalid commit metadata.", field=field, reason=reason)


def _budget(field: str, limit: int, observed: int) -> GrafxTransactionBudgetExceeded:
    return GrafxTransactionBudgetExceeded(
        "Commit metadata exceeds its admission budget.",
        budget="commit_metadata_" + field, limit=limit, observed_at_least=observed,
    )


@dataclass(frozen=True, slots=True)
class MetadataLimits:
    """Configurable admission limits inside fixed hard ceilings, independent of I/O."""

    max_bytes: int = 16_384
    max_attributes: int = 64
    max_key_bytes: int = 128
    max_string_bytes: int = 4_096
    max_depth: int = 4
    max_values: int = 256

    def __post_init__(self) -> None:
        for name, low, high in _LIMIT_RANGES:
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise _invalid(name, "invalid_limit")


class _Admission:
    """One bounded normalization walk; state and mutable children never escape."""

    __slots__ = ("limits", "encoded", "values", "active")

    def __init__(self, limits: MetadataLimits) -> None:
        self.limits = limits
        self.encoded = bytearray(b"GXCM\x01")
        self.values = 0
        self.active: set[int] = set()

    def append(self, data: bytes) -> None:
        """Append canonical bytes only after checking the complete metadata byte budget."""
        size = len(self.encoded) + len(data)
        if size > self.limits.max_bytes:
            raise _budget("bytes", self.limits.max_bytes, size)
        self.encoded.extend(data)

    def text(self, value: str, *, key: bool = False, field: str = "attributes") -> str:
        """Validate or decode one bounded UTF-8 metadata string."""
        if type(value) is not str:
            raise _invalid(field, "expected_string")
        ceiling = self.limits.max_key_bytes if key else self.limits.max_string_bytes
        label = "key_bytes" if key else "string_bytes"
        # UTF-8 needs at least one byte per code point. Bound the allocation even
        # for very large caller strings; a multibyte value gets the exact check.
        if len(value) > ceiling:
            raise _budget(label, ceiling, len(value))
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise _invalid(field, "invalid_utf8") from None
        if len(encoded) > ceiling:
            raise _budget(label, ceiling, len(encoded))
        self.append(b"s" + _U32.pack(len(encoded)) + encoded)
        return value

    def freeze(self, value: object, depth: int = 0) -> MetadataValue:
        """Copy a bounded primitive metadata tree into immutable canonical values."""
        kind = type(value)
        container = kind in (dict, list, tuple)
        if container and id(value) in self.active:
            raise _invalid("attributes", "cycle")
        self.values += 1
        if self.values > self.limits.max_values:
            raise _budget("values", self.limits.max_values, self.values)
        if container:
            if depth > self.limits.max_depth:
                raise _budget("depth", self.limits.max_depth, depth)
            source = cast(dict[object, object] | list[object] | tuple[object, ...], value)
            minimum = self.values + len(source)
            if minimum > self.limits.max_values:
                raise _budget("values", self.limits.max_values, minimum)
            if kind is dict and len(source) > self.limits.max_attributes:
                raise _budget("attributes", self.limits.max_attributes, len(source))
            # Snapshot exact built-ins before writing counts. A caller changing a
            # list later must not make the canonical count disagree with its body.
            snapshot = cast(dict[object, object], value).copy() if kind is dict else tuple(source)
            minimum = self.values + len(snapshot)
            if minimum > self.limits.max_values:
                raise _budget("values", self.limits.max_values, minimum)
            if kind is dict and len(snapshot) > self.limits.max_attributes:
                raise _budget("attributes", self.limits.max_attributes, len(snapshot))
            self.active.add(id(value))
            try:
                if kind is dict:
                    if any(type(key) is not str for key in snapshot):
                        raise _invalid("attributes", "expected_string_key")
                    mapping = cast(dict[str, object], snapshot)
                    # Reject giant keys before comparisons in sorted(), not after
                    # spending unbounded work on a common over-budget prefix.
                    for key in mapping:
                        if len(key) > self.limits.max_key_bytes:
                            raise _budget("key_bytes", self.limits.max_key_bytes, len(key))
                    self.append(b"m" + _U32.pack(len(snapshot)))
                    output: dict[str, MetadataValue] = {}
                    # Unicode scalar order matches UTF-8 byte order. Invalid
                    # surrogates refuse in text(), never become canonical data.
                    for key in sorted(mapping):
                        self.text(key, key=True)
                        output[key] = self.freeze(mapping[key], depth + 1)
                    return MappingProxyType(output)
                self.append(b"a" + _U32.pack(len(snapshot)))
                return tuple(self.freeze(item, depth + 1) for item in snapshot)
            finally:
                self.active.remove(id(value))
        if value is None:
            self.append(b"n")
            return None
        if kind is bool:
            self.append(b"t" if value else b"f")
            return cast(bool, value)
        elif kind is int:
            integer = cast(int, value)
            if not -(1 << 63) <= integer < (1 << 63):
                raise _invalid("attributes", "integer_range")
            self.append(b"i" + _INT.pack(integer))
            return integer
        elif kind is float:
            real = cast(float, value)
            if not isfinite(real):
                raise _invalid("attributes", "nonfinite_float")
            self.append(b"d" + _DOUBLE.pack(real))
            return real
        elif kind is str:
            return self.text(cast(str, value))
        else:
            raise _invalid("attributes", "unsupported_value_type")


@dataclass(frozen=True, slots=True, init=False, eq=False, repr=False)
class CommitMetadata:
    """A fully detached canonical value; repr deliberately contains no supplied data."""

    actor: str | None
    origin: str | None
    correlation_id: str | None
    reason: str | None
    attributes: Mapping[str, MetadataValue] = field(repr=False)
    _canonical: bytes = field(repr=False)

    def __init__(
        self, *, actor: str | None = None, origin: str | None = None,
        correlation_id: str | None = None, reason: str | None = None,
        attributes: dict[str, object] | None = None, limits: MetadataLimits = MetadataLimits(),
    ) -> None:
        if type(limits) is not MetadataLimits:
            raise _invalid("limits", "expected_metadata_limits")
        # Freeze and revalidate the current values; the Python frozen decorator
        # is not evidence that a host has never changed an object's slots.
        limits = MetadataLimits(
            max_bytes=limits.max_bytes, max_attributes=limits.max_attributes,
            max_key_bytes=limits.max_key_bytes, max_string_bytes=limits.max_string_bytes,
            max_depth=limits.max_depth, max_values=limits.max_values,
        )
        if attributes is not None and type(attributes) is not dict:
            raise _invalid("attributes", "expected_dict")
        admission = _Admission(limits)
        for name, value in (("actor", actor), ("origin", origin),
                            ("correlation_id", correlation_id), ("reason", reason)):
            if value is None:
                admission.append(b"n")
            else:
                admission.text(value, field=name)
            object.__setattr__(self, name, value)
        frozen = admission.freeze({} if attributes is None else attributes)
        object.__setattr__(self, "attributes", frozen)
        object.__setattr__(self, "_canonical", bytes(admission.encoded))

    @property
    def canonical_bytes(self) -> bytes:
        """Canonical admission bytes and commit-catalog nested metadata body v1."""
        return self._canonical

    def __eq__(self, other: object) -> bool:
        if type(other) is not CommitMetadata:
            return NotImplemented
        return self._canonical == other._canonical

    def __hash__(self) -> int:
        return hash(self._canonical)

    def __repr__(self) -> str:
        return f"CommitMetadata(encoded_bytes={len(self._canonical)}, attributes={len(self.attributes)})"


def capture_commit_metadata(value: CommitMetadata | None) -> bytes | None:
    """Admit the canonical value again at begin, before any storage/coordinator IO.

    Canonical bytes, not display fields, define this value's equality and payload.
    Never retain the caller object or trust a host-replaced private byte field.
    """
    if value is None:
        return None
    if type(value) is not CommitMetadata:
        raise _invalid("metadata", "expected_commit_metadata")
    raw = value._canonical
    if type(raw) is not bytes:
        raise _invalid("metadata", "invalid_encoding")
    try:
        return decode_commit_metadata(raw).canonical_bytes
    except (GrafxCorruptionDetected, GrafxSchemaVersionMismatch):
        raise _invalid("metadata", "invalid_encoding") from None


def _corrupt(field: str, offset: int, reason: str) -> GrafxCorruptionDetected:
    return GrafxCorruptionDetected(
        "Invalid encoded commit metadata.", component="commit_metadata",
        field=field, offset=offset, reason=reason,
    )


class _MetadataReader:
    """Closed bounded decoder; no pickle, object hooks or implicit scalar coercion."""

    __slots__ = ("raw", "offset", "limits", "values")

    def __init__(self, raw: bytes, limits: MetadataLimits) -> None:
        self.raw = raw
        self.offset = 5
        self.limits = limits
        self.values = 0

    def take(self, count: int, field: str) -> bytes:
        """Consume exactly the requested bytes or report truncated metadata."""
        if count > len(self.raw) - self.offset:
            raise _corrupt(field, self.offset, "truncated")
        start = self.offset
        self.offset += count
        return self.raw[start:self.offset]

    def count(self, field: str) -> int:
        """Decode one little-endian metadata count from the bounded input."""
        return int.from_bytes(self.take(4, field), "little")

    def text(self, *, key: bool = False) -> str:
        """Validate or decode one bounded UTF-8 metadata string."""
        size = self.count("string_length")
        ceiling = self.limits.max_key_bytes if key else self.limits.max_string_bytes
        if size > ceiling:
            raise _corrupt("key" if key else "string", self.offset, "format_limit")
        offset = self.offset
        raw = self.take(size, "string")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _corrupt("string", offset, "invalid_utf8") from None

    def optional_field(self) -> str | None:
        """Decode a nullable metadata text field with strict tag admission."""
        tag = self.take(1, "field_tag")
        if tag == b"n":
            return None
        if tag == b"s":
            return self.text()
        raise _corrupt("field_tag", self.offset - 1, "expected_optional_text")

    def value(self, depth: int = 0) -> object:
        """Decode one bounded metadata value while enforcing depth and value quotas."""
        self.values += 1
        if self.values > self.limits.max_values:
            raise _corrupt("values", self.offset, "format_limit")
        tag = self.take(1, "value_tag")
        if tag == b"n":
            return None
        if tag == b"f":
            return False
        if tag == b"t":
            return True
        if tag == b"i":
            return int.from_bytes(self.take(8, "integer"), "little", signed=True)
        if tag == b"d":
            real = float(_DOUBLE.unpack(self.take(8, "float"))[0])
            if not isfinite(real):
                raise _corrupt("float", self.offset - 8, "nonfinite_float")
            return real
        if tag == b"s":
            return self.text()
        if tag not in (b"a", b"m"):
            raise _corrupt("value_tag", self.offset - 1, "unknown_tag")
        if depth > self.limits.max_depth:
            raise _corrupt("depth", self.offset - 1, "format_limit")
        count = self.count("container_count")
        if count > self.limits.max_values - self.values:
            raise _corrupt("values", self.offset, "format_limit")
        # Each child takes at least one byte (maps need more). Refuse a forged
        # count before allocating a list or entering a potentially long loop.
        if count > len(self.raw) - self.offset:
            raise _corrupt("container_count", self.offset, "truncated")
        if tag == b"a":
            return [self.value(depth + 1) for _ in range(count)]
        if count > self.limits.max_attributes:
            raise _corrupt("attributes", self.offset, "format_limit")
        result: dict[str, object] = {}
        previous: str | None = None
        for _ in range(count):
            if self.take(1, "key_tag") != b"s":
                raise _corrupt("key_tag", self.offset - 1, "expected_string")
            key = self.text(key=True)
            if previous is not None and key <= previous:
                raise _corrupt("key_order", self.offset, "not_strictly_increasing")
            previous = key
            result[key] = self.value(depth + 1)
        return result


def decode_commit_metadata(raw: bytes) -> CommitMetadata:
    """Decode the complete v1 body using format ceilings, never local write defaults.

    This validates values, not physical authority, a checksum or publication.
    The enclosing journal/page/recovery layer must supply those separate proofs.
    """
    if type(raw) is not bytes:
        raise _invalid("encoded_metadata", "expected_bytes")
    if len(raw) < 5 or len(raw) > MAX_METADATA_BYTES:
        raise _corrupt("size", 0, "format_limit")
    if raw[:4] != b"GXCM":
        raise _corrupt("magic", 0, "invalid_magic")
    if raw[4] != 1:
        raise GrafxSchemaVersionMismatch(
            "Unsupported commit metadata encoding version.",
            component="commit_metadata", field="version", version=raw[4],
        )
    limits = MetadataLimits(**{name: high for name, _low, high in _LIMIT_RANGES})
    reader = _MetadataReader(raw, limits)
    actor, origin, correlation, reason = (reader.optional_field() for _ in range(4))
    attributes = reader.value()
    if type(attributes) is not dict:
        raise _corrupt("attributes", reader.offset, "expected_map")
    if reader.offset != len(raw):
        raise _corrupt("trailing", reader.offset, "trailing_bytes")
    try:
        metadata = CommitMetadata(
            actor=actor, origin=origin, correlation_id=correlation, reason=reason,
            attributes=cast(dict[str, object], attributes), limits=limits,
        )
    except (GrafxConfigurationError, GrafxTransactionBudgetExceeded):
        raise _corrupt("metadata", 0, "invalid_value") from None
    if metadata.canonical_bytes != raw:
        raise _corrupt("metadata", 0, "noncanonical_encoding")
    return metadata

__all__ = ["MetadataLimits","CommitMetadata","capture_commit_metadata","decode_commit_metadata","MetadataValue","MAX_METADATA_BYTES"]
