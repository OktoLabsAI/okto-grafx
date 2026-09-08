"""Bounded, immutable commit metadata admission (GX-CAP-1A).

Canonical bytes are a private admission/equality representation, NOT a durable
page or WAL format. No public begin option is exposed until publication/recovery
and lookup can persist these values atomically with a logical commit.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Mapping, TypeAlias, cast

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxTransactionBudgetExceeded

MetadataValue: TypeAlias = (
    bool | int | float | str | None | tuple["MetadataValue", ...] | Mapping[str, "MetadataValue"]
)
_U32 = struct.Struct("<I")
_INT = struct.Struct("<q")
_DOUBLE = struct.Struct("<d")


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
        for name, low, high in (
            ("max_bytes", 14, 65_536), ("max_attributes", 0, 256),
            ("max_key_bytes", 1, 1_024), ("max_string_bytes", 1, 16_384),
            ("max_depth", 0, 16), ("max_values", 1, 4_096),
        ):
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
        size = len(self.encoded) + len(data)
        if size > self.limits.max_bytes:
            raise _budget("bytes", self.limits.max_bytes, size)
        self.encoded.extend(data)

    def text(self, value: str, *, key: bool = False, field: str = "attributes") -> str:
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
        """Private admission bytes, not an on-disk encoding compatibility promise."""
        return self._canonical

    def __eq__(self, other: object) -> bool:
        if type(other) is not CommitMetadata:
            return NotImplemented
        return self._canonical == other._canonical

    def __hash__(self) -> int:
        return hash(self._canonical)

    def __repr__(self) -> str:
        return f"CommitMetadata(encoded_bytes={len(self._canonical)}, attributes={len(self.attributes)})"
