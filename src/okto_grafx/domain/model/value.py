"""The value system of Okto Grafx and its wire encoding (CONTRACT.md section 7.1).

A value is a tag byte followed by a body. Variable length bodies carry a 32-bit length first:
bytes for STRING and BYTES, element counts for LIST and MAP. A vector carries its dimension and
the numeric id of the embedding space it belongs to before its components, so a stored vector
can always answer "which space am I in" without a catalog lookup.

    tag u8 | body
    NULL       -
    BOOL       u8 0 or 1
    INT64      i64
    DOUBLE     f64
    STRING     u32 byte length | UTF-8 bytes
    BYTES      u32 length | raw bytes
    LIST       u32 element count | values
    MAP        u32 pair count | (key value, value value) pairs
    VECTOR_F32 u32 dimension | u32 space_ref | f32 components
    VECTOR_F64 u32 dimension | u32 space_ref | f64 components
    TIMESTAMP  i64 microseconds since the Unix epoch
    UUID       16 raw bytes

Whatever this module checks about a vector, it checks on the way in and never on the way out
(SPEC-VEC BR-5), so a read never has to defend itself against a malformed vector. What it can
check is exactly what a value knows about itself, with no catalog in reach:

* the dimension is at least one and at most MAX_VECTOR_DIMENSION;
* the space reference is a real one, so neither zero nor beyond 32 bits;
* every component is finite, and is representable in the storage dtype of the value.

What it CANNOT check, and does not: that the dimension matches the dimension declared by the
embedding space the column points at, that the space exists, or that it still accepts writes.
Those need the catalog, a ColumnDef carries no dimension, and VEC FR-1 assigns them to the
vector engine of C9. A caller that has only passed through here has not yet satisfied the vector
contract, and this module is not the place that will tell it so.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from math import isfinite
from typing import TypeAlias

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxVectorValidationError
from okto_grafx.domain.model.errors import SchemaMismatchError

__all__ = [
    "MAX_VALUE_DEPTH",
    "MAX_VECTOR_DIMENSION",
    "MAX_STRING_LENGTH",
    "VECTOR_DTYPES",
    "VECTOR_VALUE_TYPES",
    "INT64_MIN",
    "INT64_MAX",
    "MAX_FLOAT32",
    "FLOAT32_OVERFLOW_THRESHOLD",
    "ValueType",
    "Timestamp",
    "Uuid",
    "VectorValue",
    "Value",
    "encode_value",
    "decode_value",
    "value_type_of",
    "encode_values",
    "decode_values",
]

MAX_VALUE_DEPTH: int = 64
"""How deeply a list or a map may nest before the encoding refuses to go further.

Both directions need the bound. A payload of nothing but list tags is a few kilobytes that
unwinds into thousands of nested calls, and the interpreter answers that with a RecursionError,
which is not a Grafx error and carries no location: a corrupt payload would leave the read door
as something C6 could not classify as a corruption incident at all. Real data nests a handful of
levels, so the limit is far above anything a caller means and far below what the interpreter can
take.
"""

MAX_VECTOR_DIMENSION: int = 16384
"""Components one vector may carry. It is far above every published embedding model."""

MAX_STRING_LENGTH: int = 0xFFFFFFFF
"""Bytes a STRING or BYTES body may carry, bounded by its own 32-bit length prefix."""

INT64_MIN: int = -(1 << 63)
INT64_MAX: int = (1 << 63) - 1

MAX_FLOAT32: float = (2.0 - 2.0**-23) * 2.0**127
"""The largest finite value a 32-bit float can hold."""

FLOAT32_OVERFLOW_THRESHOLD: float = (2.0 - 2.0**-24) * 2.0**127
"""Where a double stops fitting in a 32-bit float.

Below this magnitude a double rounds to a finite 32-bit float; at it and above it rounds to
infinity, which is a value the vector contract forbids. The threshold is the midpoint between
MAX_FLOAT32 and the next binade, and the tie rounds away from MAX_FLOAT32 because infinity is
the even alternative, so the boundary itself already overflows.
"""

_TAG = struct.Struct("<B")
_U32 = struct.Struct("<I")
_I64 = struct.Struct("<q")
_F64 = struct.Struct("<d")
_F32 = struct.Struct("<f")
_UUID_SIZE = 16


class ValueType(IntEnum):
    """The twelve types a stored value can have, as written in the tag byte."""

    NULL = 0
    BOOL = 1
    INT64 = 2
    DOUBLE = 3
    STRING = 4
    BYTES = 5
    LIST = 6
    MAP = 7
    VECTOR_F32 = 8
    VECTOR_F64 = 9
    TIMESTAMP = 10
    UUID = 11


VECTOR_VALUE_TYPES: tuple[ValueType, ...] = (ValueType.VECTOR_F32, ValueType.VECTOR_F64)
"""The value types whose body is an embedding."""

VECTOR_DTYPES: dict[str, ValueType] = {
    "float32": ValueType.VECTOR_F32,
    "float64": ValueType.VECTOR_F64,
}
"""The storage dtype of an embedding space mapped to the value type it produces."""


@dataclass(frozen=True, slots=True)
class Timestamp:
    """A point in time as whole microseconds since the Unix epoch.

    A timestamp is its own type rather than an integer so that encoding never has to guess
    whether a number means a count or an instant.
    """

    micros: int

    def __post_init__(self) -> None:
        """Refuse a timestamp that is not a 64-bit signed count of microseconds."""
        if isinstance(self.micros, bool) or not isinstance(self.micros, int):
            raise SchemaMismatchError(
                f"A timestamp needs whole microseconds; got {type(self.micros).__name__}.",
                field="micros",
                value=repr(self.micros),
            )
        if not INT64_MIN <= self.micros <= INT64_MAX:
            raise SchemaMismatchError(
                f"A timestamp must fit in 64 signed bits; got {self.micros}.",
                field="micros",
                value=self.micros,
            )

    @classmethod
    def from_wall(cls, seconds: float) -> Timestamp:
        """Build a timestamp from a Unix epoch reading in seconds, rounded to microseconds."""
        if not isfinite(float(seconds)):
            raise SchemaMismatchError(
                f"A timestamp needs a finite wall reading; got {seconds!r}.",
                field="seconds",
                value=repr(seconds),
            )
        return cls(micros=round(float(seconds) * 1_000_000))

    def to_wall(self) -> float:
        """Return this instant as Unix epoch seconds."""
        return self.micros / 1_000_000


@dataclass(frozen=True, slots=True)
class Uuid:
    """A 128-bit identifier carried as its sixteen raw bytes.

    The domain never imports the standard library uuid module: uuid4 is unseeded randomness and
    uuid1 reads the wall clock, and both are mechanisms that G2b keeps out of the pure core. An
    adapter that already holds a uuid.UUID builds one of these from its bytes.
    """

    raw: bytes

    def __post_init__(self) -> None:
        """Refuse anything that is not exactly sixteen bytes."""
        if not isinstance(self.raw, (bytes, bytearray)):
            raise SchemaMismatchError(
                f"An identifier needs raw bytes; got {type(self.raw).__name__}.",
                field="raw",
                value=type(self.raw).__name__,
            )
        if len(self.raw) != _UUID_SIZE:
            raise SchemaMismatchError(
                f"An identifier needs exactly {_UUID_SIZE} bytes; got {len(self.raw)}.",
                field="raw",
                value=len(self.raw),
            )
        object.__setattr__(self, "raw", bytes(self.raw))

    @property
    def hex(self) -> str:
        """Return the thirty-two hexadecimal digits of this identifier, without separators."""
        return self.raw.hex()

    def canonical(self) -> str:
        """Return the canonical 8-4-4-4-12 hyphenated form of this identifier."""
        digits = self.raw.hex()
        return (
            f"{digits[0:8]}-{digits[8:12]}-{digits[12:16]}-{digits[16:20]}-{digits[20:32]}"
        )

    @classmethod
    def from_hex(cls, text: str) -> Uuid:
        """Build an identifier from its hexadecimal form, with or without hyphens."""
        digits = text.replace("-", "")
        if len(digits) != _UUID_SIZE * 2:
            raise SchemaMismatchError(
                f"An identifier needs {_UUID_SIZE * 2} hexadecimal digits; got {len(digits)}.",
                field="text",
                value=text,
            )
        try:
            raw = bytes.fromhex(digits)
        except ValueError as failure:
            raise SchemaMismatchError(
                f"An identifier needs hexadecimal digits; got {text!r}.",
                field="text",
                value=text,
            ) from failure
        return cls(raw=raw)


@dataclass(frozen=True, slots=True)
class VectorValue:
    """An embedding together with the space it belongs to and the precision it is stored in.

    Construction normalises the components to a tuple of floats and checks the dtype, because a
    decoded vector must be buildable without any further judgement. Everything the vector
    contract judges, which is dimension, space reference and finiteness, is checked by
    encode_value, so a read never re-validates what a write already refused to store.
    """

    values: tuple[float, ...]
    space_ref: int
    dtype: str = "float32"

    def __post_init__(self) -> None:
        """Normalise the components and refuse an unknown storage dtype."""
        if self.dtype not in VECTOR_DTYPES:
            raise GrafxVectorValidationError(
                f"A vector dtype must be one of {sorted(VECTOR_DTYPES)}; got {self.dtype!r}.",
                field="dtype",
                value=repr(self.dtype),
            )
        if isinstance(self.values, (str, bytes, bytearray)):
            raise GrafxVectorValidationError(
                f"A vector needs a sequence of numbers; got {type(self.values).__name__}.",
                field="values",
                value=type(self.values).__name__,
            )
        try:
            components = tuple(float(component) for component in self.values)
        except (TypeError, ValueError) as failure:
            raise GrafxVectorValidationError(
                "A vector needs a sequence of numbers.",
                field="values",
                value=type(self.values).__name__,
            ) from failure
        if isinstance(self.space_ref, bool) or not isinstance(self.space_ref, int):
            raise GrafxVectorValidationError(
                f"A vector space reference must be an integer; got {self.space_ref!r}.",
                field="space_ref",
                value=repr(self.space_ref),
            )
        object.__setattr__(self, "values", components)

    @property
    def dimension(self) -> int:
        """Return the number of components this vector carries."""
        return len(self.values)

    @property
    def value_type(self) -> ValueType:
        """Return the value type this vector encodes to, which follows its dtype."""
        return VECTOR_DTYPES[self.dtype]


Value: TypeAlias = (
    "None | bool | int | float | str | bytes | Timestamp | Uuid | VectorValue"
    " | tuple[Value, ...] | dict[Value, Value]"
)
"""Every Python shape the value system can store. Lists decode to tuples and maps to dicts."""


def _utf8(text: str) -> bytes:
    """Return the UTF-8 bytes of a string, refusing one that has no encoding at all.

    A Python string may hold a lone surrogate, which no UTF-8 encoder can represent. Such a
    string arrives from a lenient decoder or from parsed input, never from a keyboard, and it
    must not leave the write path as a raw UnicodeEncodeError: only a Grafx error is allowed to
    escape the engine (CONTRACT.md section 11 item 5).
    """
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as failure:
        raise SchemaMismatchError(
            f"A stored string must be encodable as UTF-8; the character at position "
            f"{failure.start} cannot be: {failure.reason}.",
            field="value",
            position=failure.start,
            reason=failure.reason,
        ) from failure


def value_type_of(value: Value) -> ValueType:
    """Return the value type a Python object encodes to, refusing anything with no encoding."""
    if value is None:
        return ValueType.NULL
    if isinstance(value, bool):
        return ValueType.BOOL
    if isinstance(value, int):
        return ValueType.INT64
    if isinstance(value, float):
        return ValueType.DOUBLE
    if isinstance(value, str):
        return ValueType.STRING
    if isinstance(value, (bytes, bytearray)):
        return ValueType.BYTES
    if isinstance(value, VectorValue):
        return value.value_type
    if isinstance(value, Timestamp):
        return ValueType.TIMESTAMP
    if isinstance(value, Uuid):
        return ValueType.UUID
    if isinstance(value, (tuple, list)):
        return ValueType.LIST
    if isinstance(value, dict):
        return ValueType.MAP
    raise SchemaMismatchError(
        f"There is no stored value type for a {type(value).__name__}.",
        field="value",
        value=type(value).__name__,
    )


def encode_value(value: Value, *, depth: int = 0) -> bytes:
    """Return the tagged encoding of one value.

    A vector is checked before a single byte is produced: its dimension is within the bounds of
    the format, its space reference is a real one, and every component is finite and fits the
    storage dtype (SPEC-VEC BR-5). The check against the DECLARED dimension of the embedding
    space is not here and cannot be: this function is given a value, not a catalog. C9 owns it.
    """
    if depth > MAX_VALUE_DEPTH:
        raise SchemaMismatchError(
            f"A value may nest at most {MAX_VALUE_DEPTH} levels deep; this one goes further.",
            field="depth",
            value=depth,
            limit=MAX_VALUE_DEPTH,
        )
    kind = value_type_of(value)
    if kind is ValueType.NULL:
        return _TAG.pack(int(kind))
    if kind is ValueType.BOOL:
        return _TAG.pack(int(kind)) + _TAG.pack(1 if value else 0)
    if kind is ValueType.INT64:
        number = int(value)  # type: ignore[arg-type]
        if not INT64_MIN <= number <= INT64_MAX:
            raise SchemaMismatchError(
                f"An INT64 value must fit in 64 signed bits; got {number}.",
                field="value",
                value=number,
            )
        return _TAG.pack(int(kind)) + _I64.pack(number)
    if kind is ValueType.DOUBLE:
        return _TAG.pack(int(kind)) + _F64.pack(float(value))  # type: ignore[arg-type]
    if kind is ValueType.STRING:
        body = _utf8(str(value))
        return _TAG.pack(int(kind)) + _U32.pack(len(body)) + body
    if kind is ValueType.BYTES:
        body = bytes(value)  # type: ignore[arg-type]
        return _TAG.pack(int(kind)) + _U32.pack(len(body)) + body
    if kind is ValueType.TIMESTAMP:
        return _TAG.pack(int(kind)) + _I64.pack(value.micros)  # type: ignore[union-attr]
    if kind is ValueType.UUID:
        return _TAG.pack(int(kind)) + value.raw  # type: ignore[union-attr]
    if kind is ValueType.LIST:
        elements = tuple(value)  # type: ignore[arg-type]
        parts = [_TAG.pack(int(kind)), _U32.pack(len(elements))]
        parts.extend(encode_value(element, depth=depth + 1) for element in elements)
        return b"".join(parts)
    if kind is ValueType.MAP:
        pairs = tuple(value.items())  # type: ignore[union-attr]
        parts = [_TAG.pack(int(kind)), _U32.pack(len(pairs))]
        for key, item in pairs:
            parts.append(encode_value(key, depth=depth + 1))
            parts.append(encode_value(item, depth=depth + 1))
        return b"".join(parts)
    return _encode_vector(value, kind)  # type: ignore[arg-type]


def _encode_vector(vector: VectorValue, kind: ValueType) -> bytes:
    """Return the encoding of a vector after enforcing the vector contract."""
    dimension = vector.dimension
    if dimension < 1:
        raise GrafxVectorValidationError(
            "A vector must have at least one component.",
            field="dimension",
            value=dimension,
        )
    if dimension > MAX_VECTOR_DIMENSION:
        raise GrafxVectorValidationError(
            f"A vector may have at most {MAX_VECTOR_DIMENSION} components; got {dimension}.",
            field="dimension",
            value=dimension,
        )
    if vector.space_ref < 1:
        raise GrafxVectorValidationError(
            "A vector must name the embedding space it belongs to; space reference 0 is "
            "reserved for an unassigned space.",
            field="space_ref",
            value=vector.space_ref,
        )
    if vector.space_ref > 0xFFFFFFFF:
        raise GrafxVectorValidationError(
            f"A vector space reference must fit in 32 bits; got {vector.space_ref}.",
            field="space_ref",
            value=vector.space_ref,
        )
    single = kind is ValueType.VECTOR_F32
    for position, component in enumerate(vector.values):
        if not isfinite(component):
            raise GrafxVectorValidationError(
                f"A vector component must be finite; component {position} is {component!r}.",
                field="values",
                position=position,
                value=repr(component),
            )
        if single and not -FLOAT32_OVERFLOW_THRESHOLD < component < FLOAT32_OVERFLOW_THRESHOLD:
            # A double the target dtype cannot hold would become an infinity on the way to the
            # page, and an infinity is exactly what the vector contract refuses to store. The
            # range is a property of the space, not of the number, so the check belongs here and
            # not next to the finiteness test above (SPEC-VEC BR-5).
            raise GrafxVectorValidationError(
                f"A component of a float32 vector must be within "
                f"{MAX_FLOAT32!r} in magnitude; component {position} is {component!r}.",
                field="values",
                position=position,
                value=repr(component),
                dtype=vector.dtype,
                limit=MAX_FLOAT32,
            )
    body = struct.pack(f"<{dimension}{'f' if single else 'd'}", *vector.values)
    return _TAG.pack(int(kind)) + _U32.pack(dimension) + _U32.pack(vector.space_ref) + body


def decode_value(buf: bytes, offset: int = 0, *, depth: int = 0) -> tuple[Value, int]:
    """Decode one value starting at the offset and return it with the offset that follows it.

    The depth budget is part of the format, not a safety net: stored bytes that nest deeper than
    an encoder was ever allowed to write are corrupt, and they are reported as corrupt with the
    offset that carried them rather than as an interpreter failure with no location at all.
    """
    if depth > MAX_VALUE_DEPTH:
        raise GrafxCorruptionDetected(
            f"A stored value nests deeper than the {MAX_VALUE_DEPTH} levels the format allows.",
            field="depth",
            offset=offset,
            limit=MAX_VALUE_DEPTH,
        )
    _require(buf, offset, 1, "tag")
    tag = buf[offset]
    offset += 1
    try:
        kind = ValueType(tag)
    except ValueError as failure:
        raise GrafxCorruptionDetected(
            f"A stored value carries the unknown type tag {tag}.",
            field="tag",
            value=tag,
            offset=offset - 1,
        ) from failure
    if kind is ValueType.NULL:
        return None, offset
    if kind is ValueType.BOOL:
        _require(buf, offset, 1, "bool")
        raw = buf[offset]
        if raw > 1:
            raise GrafxCorruptionDetected(
                f"A stored BOOL must be 0 or 1; got {raw}.",
                field="bool",
                value=raw,
                offset=offset,
            )
        return raw == 1, offset + 1
    if kind is ValueType.INT64:
        _require(buf, offset, _I64.size, "int64")
        return _I64.unpack_from(buf, offset)[0], offset + _I64.size
    if kind is ValueType.DOUBLE:
        _require(buf, offset, _F64.size, "double")
        return _F64.unpack_from(buf, offset)[0], offset + _F64.size
    if kind is ValueType.TIMESTAMP:
        _require(buf, offset, _I64.size, "timestamp")
        return Timestamp(_I64.unpack_from(buf, offset)[0]), offset + _I64.size
    if kind is ValueType.UUID:
        _require(buf, offset, _UUID_SIZE, "uuid")
        return Uuid(bytes(buf[offset : offset + _UUID_SIZE])), offset + _UUID_SIZE
    if kind in (ValueType.STRING, ValueType.BYTES):
        _require(buf, offset, _U32.size, "length")
        length = _U32.unpack_from(buf, offset)[0]
        offset += _U32.size
        _require(buf, offset, length, kind.name.lower())
        body = bytes(buf[offset : offset + length])
        offset += length
        if kind is ValueType.BYTES:
            return body, offset
        try:
            return body.decode("utf-8"), offset
        except UnicodeDecodeError as failure:
            raise GrafxCorruptionDetected(
                "A stored STRING is not valid UTF-8.",
                field="string",
                offset=offset - length,
                length=length,
            ) from failure
    if kind is ValueType.LIST:
        _require(buf, offset, _U32.size, "length")
        count = _U32.unpack_from(buf, offset)[0]
        offset += _U32.size
        elements: list[Value] = []
        for _ in range(count):
            element, offset = decode_value(buf, offset, depth=depth + 1)
            elements.append(element)
        return tuple(elements), offset
    if kind is ValueType.MAP:
        _require(buf, offset, _U32.size, "length")
        count = _U32.unpack_from(buf, offset)[0]
        offset += _U32.size
        mapping: dict[Value, Value] = {}
        for _ in range(count):
            key, offset = decode_value(buf, offset, depth=depth + 1)
            item, offset = decode_value(buf, offset, depth=depth + 1)
            try:
                mapping[key] = item
            except TypeError as failure:
                raise GrafxCorruptionDetected(
                    f"A stored MAP has a key of type {type(key).__name__}, which cannot be one.",
                    field="map_key",
                    value=type(key).__name__,
                    offset=offset,
                ) from failure
        return mapping, offset
    return _decode_vector(buf, offset, kind)


def _decode_vector(buf: bytes, offset: int, kind: ValueType) -> tuple[Value, int]:
    """Decode a vector body without re-judging its components (SPEC-VEC BR-5)."""
    _require(buf, offset, _U32.size * 2, "vector header")
    dimension = _U32.unpack_from(buf, offset)[0]
    space_ref = _U32.unpack_from(buf, offset + _U32.size)[0]
    offset += _U32.size * 2
    single = kind is ValueType.VECTOR_F32
    width = _F32.size if single else _F64.size
    _require(buf, offset, dimension * width, "vector body")
    components = struct.unpack_from(f"<{dimension}{'f' if single else 'd'}", buf, offset)
    offset += dimension * width
    vector = VectorValue(
        values=components, space_ref=space_ref, dtype="float32" if single else "float64"
    )
    return vector, offset


def encode_values(values: tuple[Value, ...]) -> bytes:
    """Return the concatenated encoding of a positional sequence of values."""
    return b"".join(encode_value(value) for value in values)


def decode_values(buf: bytes, count: int, offset: int = 0) -> tuple[tuple[Value, ...], int]:
    """Decode a fixed number of values and return them with the offset that follows them."""
    decoded: list[Value] = []
    for _ in range(count):
        value, offset = decode_value(buf, offset)
        decoded.append(value)
    return tuple(decoded), offset


def _require(buf: bytes, offset: int, size: int, what: str) -> None:
    """Refuse to read past the end of a buffer, naming what was being read."""
    if offset < 0 or offset + size > len(buf):
        raise GrafxCorruptionDetected(
            f"A stored value needs {size} bytes for its {what} at offset {offset}, but the "
            f"buffer holds {len(buf)}.",
            field=what,
            offset=offset,
            needed=size,
            available=max(len(buf) - max(offset, 0), 0),
        )
