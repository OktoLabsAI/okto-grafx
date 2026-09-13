"""Bounded recursive schema descriptors over the existing native value families.

Native columns persist these descriptors under typed_collections_v1 in catalog v2.
Assignment normalization owns input values; stored reads validate without repair.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from .decimal_values import DecimalValue
from .errors import SchemaMismatchError
from . import value as _native
from .value import MAX_VALUE_DEPTH, VECTOR_VALUE_TYPES, ValueType, decode_value, encode_value, value_type_of

__all__ = ["StoredType", "MAX_STORED_TYPE_BYTES", "MAX_STORED_TYPE_NODES", "MAX_STRUCT_FIELDS",
           "encode_stored_type", "decode_stored_type", "normalize_typed_value", "validate_typed_value",
           "stored_type_to_json", "stored_type_from_json", "TYPED_COLLECTIONS_CAPABILITY",
           "TYPED_COLLECTIONS_CAPABILITY_BIT", "TYPED_COLUMN_TAG"]

TYPED_COLLECTIONS_CAPABILITY = "typed_collections_v1"
TYPED_COLLECTIONS_CAPABILITY_BIT = 1 << 27
TYPED_COLUMN_TAG = 252

MAX_STORED_TYPE_BYTES = 65536
MAX_STORED_TYPE_NODES = 4096
MAX_STRUCT_FIELDS = 256
_KINDS = {kind.name: int(kind) for kind in ValueType if kind not in VECTOR_VALUE_TYPES and kind is not ValueType.NULL}
_KINDS.update({"ANY": 255, "ARRAY": 254, "STRUCT": 253})
_NAMES = {tag: name for name, tag in _KINDS.items()}
_MAGIC = b"GXT1"

_StoredValue: TypeAlias = (
    bool | int | float | str | bytes | None | _native.Timestamp | _native.Uuid
    | _native.DateValue | _native.LocalTimeValue | _native.TimeValue
    | _native.LocalDateTimeValue | _native.DateTimeValue | _native.DurationValue | DecimalValue
    | tuple["_StoredValue", ...] | dict["_StoredValue", "_StoredValue"]
)


@dataclass(frozen=True, slots=True)
class StoredType:
    """One exact schema type, including nested nullability and parameter metadata.

    LIST/MAP/ARRAY require element; ARRAY also requires length (0..2**32-1).
    STRUCT fields are ordered unique ASCII identifiers and their own descriptors.
    DECIMAL requires precision/scale. ANY uses native heterogeneous admission;
    embeddings still require an independently declared vector-space authority.
    """

    kind: str
    nullable: bool = True
    element: StoredType | None = None
    length: int | None = None
    fields: tuple[tuple[str, StoredType], ...] = ()
    precision: int | None = None
    scale: int | None = None

    def __post_init__(self) -> None:
        _validate(self)

    @property
    def value_type(self) -> ValueType | None:
        """Return the query/value family; ANY is dynamic, ARRAY/LIST and STRUCT/MAP share tags."""
        _validate(self)
        if self.kind == "ANY":
            return None
        return ValueType.LIST if self.kind == "ARRAY" else ValueType.MAP if self.kind == "STRUCT" else ValueType(_KINDS[self.kind])

    def describe(self) -> str:
        """Return canonical native type syntax with explicit nested nullability."""
        _validate(self)
        if self.kind in ("LIST", "MAP"):
            text = f"{self.kind}<{self.element.describe()}>"
        elif self.kind == "ARRAY":
            text = f"ARRAY<{self.element.describe()},{self.length}>"
        elif self.kind == "STRUCT":
            text = "STRUCT<" + ",".join(f"{name}:{child.describe()}" for name, child in self.fields) + ">"
        elif self.kind == "DECIMAL":
            text = f"DECIMAL({self.precision},{self.scale})"
        else:
            text = "BLOB" if self.kind == "BYTES" else self.kind
        return text if self.nullable else text + " NOT NULL"

    def contains(self, kind: str) -> bool:
        """Check a descriptor family, not the values that may later be stored in ANY."""
        _validate(self)
        return (self.kind == kind or self.element is not None and self.element.contains(kind)
                or any(child.contains(kind) for _, child in self.fields))


def _invalid(reason):
    return GrafxConfigurationError("Invalid stored type declaration.", field="stored_type", reason=reason)


def _validate(root):
    count = 0
    active = set()

    def visit(node: StoredType, depth: int) -> None:
        """Validate descriptor shape and charge the bounded recursive tree."""
        nonlocal count
        count += 1
        if depth > MAX_VALUE_DEPTH or count > MAX_STORED_TYPE_NODES:
            raise _invalid("descriptor_bound")
        if type(node) is not StoredType or id(node) in active:
            raise _invalid("descriptor_shape_or_cycle")
        if type(node.kind) is not str or node.kind not in _KINDS or type(node.nullable) is not bool:
            raise _invalid("kind_or_nullability")
        if type(node.fields) is not tuple or len(node.fields) > MAX_STRUCT_FIELDS:
            raise _invalid("struct_fields")
        if node.kind != "STRUCT" and node.fields:
            raise _invalid("unexpected_fields")
        if node.kind == "DECIMAL":
            try:
                DecimalValue(0, node.precision, node.scale)
            except SchemaMismatchError as failure:
                raise _invalid("decimal_coordinates") from failure
        elif node.precision is not None or node.scale is not None:
            raise _invalid("unexpected_decimal_coordinates")
        if node.kind == "ARRAY":
            if type(node.length) is not int or not 0 <= node.length <= 0xFFFFFFFF:
                raise _invalid("array_length")
        elif node.length is not None:
            raise _invalid("unexpected_array_length")
        active.add(id(node))
        if node.kind in ("LIST", "MAP", "ARRAY"):
            visit(node.element, depth + 1)
        elif node.element is not None:
            raise _invalid("unexpected_element")
        names = set()
        for field in node.fields:
            if type(field) is not tuple or len(field) != 2:
                raise _invalid("struct_field_shape")
            name, child = field
            if (type(name) is not str or not 1 <= len(name) <= 128 or not name.isascii()
                    or not name.isidentifier() or name in names):
                raise _invalid("struct_field_name")
            names.add(name)
            visit(child, depth + 1)
        active.remove(id(node))

    visit(root, 0)


def encode_stored_type(descriptor: StoredType) -> bytes:
    """Encode a complete canonical versioned schema tree, bounded before each append."""
    _validate(descriptor)
    output = bytearray(_MAGIC)

    def append(raw: bytes) -> None:
        """Append an encoded fragment only within the descriptor byte ceiling."""
        if len(output) + len(raw) > MAX_STORED_TYPE_BYTES:
            raise _invalid("descriptor_bytes")
        output.extend(raw)

    def visit(node: StoredType) -> None:
        """Encode one validated descriptor and its ordered children."""
        append(bytes((_KINDS[node.kind], int(node.nullable))))
        if node.kind == "DECIMAL":
            append(bytes((node.precision, node.scale)))
        if node.kind == "ARRAY":
            append(node.length.to_bytes(4, "little"))
        if node.kind in ("LIST", "MAP", "ARRAY"):
            visit(node.element)
        if node.kind == "STRUCT":
            append(len(node.fields).to_bytes(2, "little"))
            for name, child in node.fields:
                append(bytes((len(name),)) + name.encode("ascii"))
                visit(child)

    visit(descriptor)
    return bytes(output)


def decode_stored_type(raw: bytes) -> StoredType:
    """Decode one complete descriptor; malformed/trailing/cyclic-equivalent payloads refuse."""
    if type(raw) is not bytes or not 6 <= len(raw) <= MAX_STORED_TYPE_BYTES or raw[:4] != _MAGIC:
        raise GrafxCorruptionDetected("Invalid stored type envelope.", field="stored_type")
    offset, count = 4, 0

    def take(size: int) -> bytes:
        """Consume a bounded fragment or refuse a truncated descriptor."""
        nonlocal offset
        if size > len(raw) - offset:
            raise GrafxCorruptionDetected("Truncated stored type.", field="stored_type")
        result = raw[offset:offset + size]
        offset += size
        return result

    def visit(depth: int) -> StoredType:
        """Decode one descriptor while charging recursive structural bounds."""
        nonlocal count
        count += 1
        if depth > MAX_VALUE_DEPTH or count > MAX_STORED_TYPE_NODES:
            raise GrafxCorruptionDetected("Stored type exceeds structural bounds.", field="stored_type")
        tag, nullable = take(2)
        if tag not in _NAMES or nullable not in (0, 1):
            raise GrafxCorruptionDetected("Unknown stored type or nullability flag.", field="stored_type")
        kind = _NAMES[tag]
        precision, scale = take(2) if kind == "DECIMAL" else (None, None)
        length = int.from_bytes(take(4), "little") if kind == "ARRAY" else None
        element = visit(depth + 1) if kind in ("LIST", "MAP", "ARRAY") else None
        fields = []
        if kind == "STRUCT":
            size = int.from_bytes(take(2), "little")
            if size > MAX_STRUCT_FIELDS:
                raise GrafxCorruptionDetected("Stored struct exceeds field bound.", field="stored_type")
            for _ in range(size):
                name = take(take(1)[0]).decode("ascii")
                fields.append((name, visit(depth + 1)))
        return StoredType(kind, bool(nullable), element, length, tuple(fields), precision, scale)

    try:
        descriptor = visit(0)
        if offset != len(raw):
            raise GrafxCorruptionDetected("Trailing stored type data.", field="stored_type")
        return descriptor
    except (GrafxConfigurationError, UnicodeDecodeError) as failure:
        raise GrafxCorruptionDetected("Malformed stored type declaration.", field="stored_type") from failure


def stored_type_to_json(descriptor: StoredType) -> dict[str, object]:
    """Return owned explicit JSON schema metadata, without lossy DDL-string parsing."""
    encode_stored_type(descriptor)  # Shared shape and encoded-size bounds.

    def visit(node: StoredType) -> dict[str, object]:
        """Produce owned JSON metadata for one already-validated descriptor."""
        result = {"kind": node.kind, "nullable": node.nullable}
        if node.element is not None:
            result["element"] = visit(node.element)
        if node.kind == "ARRAY":
            result["length"] = node.length
        if node.kind == "STRUCT":
            result["fields"] = [{"name": name, "type": visit(child)} for name, child in node.fields]
        if node.kind == "DECIMAL":
            result.update(precision=node.precision, scale=node.scale)
        return result

    return visit(descriptor)


def stored_type_from_json(value: object) -> StoredType:
    """Decode bounded exact JSON schema shape; unknown/missing metadata refuses."""
    count = 0

    def visit(node: object, depth: int) -> StoredType:
        """Admit exact JSON descriptor fields without unbounded recursion."""
        nonlocal count
        count += 1
        if count > MAX_STORED_TYPE_NODES or depth > MAX_VALUE_DEPTH or type(node) is not dict:
            raise _invalid("descriptor_json_bound_or_shape")
        kind = node.get("kind")
        if type(kind) is not str or kind not in _KINDS:
            raise _invalid("descriptor_json_kind")
        required = {"kind", "nullable"}
        if kind in ("LIST", "MAP", "ARRAY"):
            required.add("element")
        if kind == "ARRAY":
            required.add("length")
        if kind == "STRUCT":
            required.add("fields")
        if kind == "DECIMAL":
            required |= {"precision", "scale"}
        if set(node) != required:
            raise _invalid("descriptor_json_fields")
        fields = []
        if kind == "STRUCT":
            if type(node["fields"]) is not list or len(node["fields"]) > MAX_STRUCT_FIELDS:
                raise _invalid("descriptor_json_struct")
            for field in node["fields"]:
                if type(field) is not dict or set(field) != {"name", "type"}:
                    raise _invalid("descriptor_json_struct_field")
                fields.append((field["name"], visit(field["type"], depth + 1)))
        return StoredType(kind, node["nullable"], visit(node["element"], depth + 1) if "element" in node else None,
                          node.get("length"), tuple(fields), node.get("precision"), node.get("scale"))

    descriptor = visit(value, 0)
    encode_stored_type(descriptor)
    return descriptor


def _mismatch(reason, path):
    return SchemaMismatchError("Value does not match its stored type declaration.",
                               field="stored_type", reason=reason, path=path)


def _leaf(value, path, *, copy):
    kind = value_type_of(value)
    if kind in (ValueType.LIST, ValueType.MAP):
        raise _mismatch("container_shape", path)
    if kind in VECTOR_VALUE_TYPES or kind is ValueType.DOUBLE and not isfinite(value):
        raise _mismatch("nonfinite_or_embedding", path)
    raw = encode_value(value)  # Validate forged native values and finite/range/UTF-8 constraints.
    return decode_value(raw)[0] if copy else value


def _any(value, path, *, copy):
    if len(path) > MAX_VALUE_DEPTH:
        raise _mismatch("value_depth", path)
    if type(value) in (tuple, list):
        result = [] if copy else None
        for index, item in enumerate(value):
            owned = _any(item, (*path, index), copy=copy)
            if copy:
                result.append(owned)
        return tuple(result) if copy else value
    if type(value) in (dict, MappingProxyType):
        result = {} if copy else None
        for key, item in value.items():
            owned_key = _any(key, (*path, "key"), copy=copy)
            owned = _any(item, (*path, "value"), copy=copy)
            if copy:
                result[owned_key] = owned
        return result if copy else value
    return _leaf(value, path, copy=copy)


def _walk(node, value, path, *, copy):
    if len(path) > MAX_VALUE_DEPTH:
        raise _mismatch("value_depth", path)
    if value is None:
        if not node.nullable:
            raise _mismatch("nullability", path)
        return None
    if node.kind == "ANY":
        return _any(value, path, copy=copy)
    if node.kind in ("LIST", "ARRAY"):
        if type(value) not in (tuple, list):
            raise _mismatch("list_shape", path)
        if node.kind == "ARRAY" and len(value) != node.length:
            raise _mismatch("array_length", path)
        result = [] if copy else None
        for index, item in enumerate(value):
            owned = _walk(node.element, item, (*path, index), copy=copy)
            if copy:
                result.append(owned)
        return tuple(result) if copy else value
    if node.kind in ("MAP", "STRUCT"):
        if type(value) not in (dict, MappingProxyType) or any(type(key) is not str for key in value):
            raise _mismatch("string_keyed_map", path)
        if node.kind == "STRUCT":
            names = {name for name, _ in node.fields}
            if any(key not in names for key in value):
                raise _mismatch("unknown_struct_field", path)
            if not copy and len(value) != len(names):
                raise _mismatch("missing_stored_struct_field", path)
            entries = ((name, child, value.get(name)) for name, child in node.fields)
        else:
            entries = ((name, node.element, item) for name, item in value.items())
        result = {} if copy else None
        for name, child, item in entries:
            _leaf(name, (*path, "key"), copy=False)
            owned = _walk(child, item, (*path, name), copy=copy)
            if copy:
                result[name] = owned
        return result if copy else value
    if value_type_of(value) != ValueType(_KINDS[node.kind]):
        raise _mismatch("scalar_type", path)
    if node.kind == "DECIMAL":
        _leaf(value, path, copy=False)
        if not copy and (value.precision != node.precision or value.scale != node.scale):
            raise _mismatch("stored_decimal_coordinates", path)
        if copy:
            return value.rescale(node.precision, node.scale)
    return _leaf(value, path, copy=copy)


def normalize_typed_value(descriptor: StoredType, value: object) -> _StoredValue:
    """Own a typed assignment; fill nullable struct fields and rescale decimals exactly.

    No parameter/index/transaction admission is performed here. Callers retain
    their native input/work/transaction byte budgets and capability prerequisites.
    """
    _validate(descriptor)
    return _walk(descriptor, value, (), copy=True)


def validate_typed_value(descriptor: StoredType, value: object) -> None:
    """Check a stored value strictly, without rescaling or supplying missing fields."""
    _validate(descriptor)
    _walk(descriptor, value, (), copy=False)
