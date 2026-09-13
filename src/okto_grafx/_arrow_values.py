"""Exact bounded columnar value conversion; no native storage or transaction authority."""

from __future__ import annotations

from decimal import Decimal as _HostDecimal
from types import MappingProxyType

from okto_grafx.domain.model.decimal_values import DecimalValue
from okto_grafx.domain.model.stored_types import (
    StoredType, encode_stored_type, decode_stored_type, validate_typed_value,
)
from okto_grafx.domain.model.temporal_interchange import (
    TEMPORAL_CLASSES_BY_NAME, temporal_components, temporal_from_components,
)
from okto_grafx.domain.model.value import MAX_VALUE_DEPTH, Timestamp, Uuid, encode_value, decode_value
from okto_grafx.errors import GrafxUnsupportedOperation, GrafxConfigurationError, GrafxQueryBudgetExceeded

__all__: list[str] = []


def _decimal_to_arrow(value):
    # Tuple construction is exact and does not apply the host's decimal context.
    return _HostDecimal((int(value.coefficient < 0),
                         tuple(int(digit) for digit in str(abs(value.coefficient))), -value.scale))


def _decimal_from_arrow(value, kind):
    if type(value) is not _HostDecimal or not value.is_finite():
        raise GrafxUnsupportedOperation("Arrow decimal must be finite and exact.", field="types")
    sign, digits, exponent = value.as_tuple()
    if exponent != -kind.scale or not 1 <= len(digits) <= kind.precision:
        raise GrafxUnsupportedOperation("Arrow decimal coordinates exceed its declaration.", field="types")
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    return DecimalValue(-coefficient if sign else coefficient, kind.precision, kind.scale)


def _own_collections(types):
    """Detach full descriptors once at admission, including during lazy consumption."""
    if type(types) is not tuple or len(types) > 256:
        raise GrafxConfigurationError("Columnar types must be a tuple of at most 256 declarations.", field="types")
    owned = []
    for kind in types:
        if type(kind) is StoredType:
            kind = decode_stored_type(encode_stored_type(kind))
            if kind.kind not in ("LIST", "MAP", "ARRAY", "STRUCT"):
                raise GrafxConfigurationError("Columnar StoredType needs a collection root.", field="types")
        owned.append(kind)
    return tuple(owned)


def _collection_metadata(kind):
    return {b"grafx.type": kind.kind.encode("ascii"), b"grafx.collection": b"nested-v1",
            # Arrow permits binary metadata, but Polars requires UTF-8. Canonical
            # ASCII hex preserves GXT1 exactly and avoids a Rust FFI panic.
            b"grafx.stored_type": encode_stored_type(kind).hex().encode("ascii"),
            b"grafx.any": b"native-value-v1"}


def _collection_schema_charge(types):
    return sum(32 * len(encode_stored_type(kind)) for kind in types if type(kind) is StoredType)


def _collection_type(node, pa, scalars):
    """Portable physical children; all logical nullability/length is descriptor-bound.

    ARRAY deliberately uses a variable list: fixed lists with NULL/zero length
    are not portable through Parquet/Polars. Empty STRUCT has one true sentinel.
    ANY alone is opaque tagged native bytes; declared structure stays columnar.
    """
    if node.kind in ("LIST", "ARRAY"):
        return pa.list_(_collection_type(node.element, pa, scalars))
    if node.kind == "MAP":
        return pa.map_(pa.string(), _collection_type(node.element, pa, scalars))
    if node.kind == "STRUCT":
        return pa.struct([(name, _collection_type(child, pa, scalars)) for name, child in node.fields]
                         if node.fields else [("$empty", pa.bool_())])
    if node.kind == "ANY":
        return pa.binary()
    if node.kind == "DECIMAL":
        return pa.decimal128(node.precision, node.scale)
    return scalars[node.kind]


class _CollectionBudget:
    """Charge occurrences and owned leaf allocations before constructing Python values."""

    def __init__(self, maximum: int) -> None:
        """Create one operation-local conversion allowance."""
        self.maximum = maximum
        self.used = 0

    def add(self, amount: int) -> None:
        """Admit logical work before the corresponding allocation."""
        self.used += amount
        if self.used > self.maximum:
            raise GrafxQueryBudgetExceeded("Arrow nested-value logical budget exceeded.", resource="arrow_collection")

    def native(self, value: object, depth: int = 0) -> None:
        """Bound native nested occurrences before encoding or copying them."""
        if depth > MAX_VALUE_DEPTH:
            raise GrafxUnsupportedOperation("Arrow nested value exceeds native depth.", field="types")
        self.add(128)
        if type(value) in (tuple, list):
            for child in value:
                self.native(child, depth + 1)
        elif type(value) in (dict, MappingProxyType):
            for key, child in value.items():
                self.native(key, depth + 1)
                self.native(child, depth + 1)
        elif type(value) is str:
            self.add(16 * 4 * len(value))
        elif type(value) in (bytes, bytearray):
            self.add(16 * len(value))
        elif type(value) is DecimalValue or type(value) in TEMPORAL_CLASSES_BY_NAME.values():
            self.add(1024)
            if type(value) is TEMPORAL_CLASSES_BY_NAME["DATETIME"] and value.zone:
                self.add(64 * len(value.zone))


def _collection_to_arrow(node, value):
    """Convert an already bounded/strictly validated native value without inference."""
    if value is None:
        return None
    if node.kind in ("LIST", "ARRAY"):
        return [_collection_to_arrow(node.element, item) for item in value]
    if node.kind == "MAP":
        return [(key, _collection_to_arrow(node.element, item)) for key, item in value.items()]
    if node.kind == "STRUCT":
        return ({name: _collection_to_arrow(child, value[name]) for name, child in node.fields}
                if node.fields else {"$empty": True})
    if node.kind == "ANY":
        return encode_value(value)
    if node.kind == "DECIMAL":
        return _decimal_to_arrow(value)
    if node.kind in TEMPORAL_CLASSES_BY_NAME:
        return temporal_components(value)
    if node.kind == "TIMESTAMP":
        return value.micros
    if node.kind == "UUID":
        return value.raw
    if node.kind == "BYTES":
        return bytes(value)
    return value


def _collection_from_arrow(node, scalar, pa, budget):
    """Visit only visible children; preserve exact integers/timestamps without host dates."""
    budget.add(128)
    if not scalar.is_valid:
        return None
    if node.kind in ("LIST", "ARRAY"):
        if node.kind == "ARRAY" and len(scalar.values) != node.length:
            raise GrafxUnsupportedOperation("Arrow ARRAY length differs from descriptor.", field="types")
        return tuple(_collection_from_arrow(node.element, item, pa, budget) for item in scalar.values)
    if node.kind == "MAP":
        values = {}
        for entry in scalar.values:
            if not entry.is_valid or not entry["key"].is_valid:
                raise GrafxUnsupportedOperation("Arrow MAP entries/keys must not be NULL.", field="types")
            budget.add(128 + 16 * entry["key"].as_buffer().size)
            key = entry["key"].as_py()
            if key in values:
                raise GrafxUnsupportedOperation("Arrow MAP has duplicate keys.", field="types")
            values[key] = _collection_from_arrow(node.element, entry["value"], pa, budget)
        return values
    if node.kind == "STRUCT":
        if not node.fields:
            if scalar["$empty"].as_py() is not True:
                raise GrafxUnsupportedOperation("Arrow empty STRUCT sentinel is invalid.", field="types")
            return {}
        return {name: _collection_from_arrow(child, scalar[name], pa, budget) for name, child in node.fields}
    if node.kind in ("ANY", "STRING", "BYTES", "UUID"):
        budget.add(16 * scalar.as_buffer().size)
    if node.kind == "ANY":
        # One-byte NULL/BOOL tags can expand to many Python objects. Admit worst
        # case occurrence cost before decode_value allocates any native tree.
        budget.add(128 * scalar.as_buffer().size)
        raw = scalar.as_py()
        value, following = decode_value(raw)
        # Full consumption + canonical re-encoding also reject duplicate map keys.
        budget.native(value)
        validate_typed_value(node, value)
        if value is None or following != len(raw) or encode_value(value) != raw:
            raise GrafxUnsupportedOperation("Arrow ANY payload is not canonical native-value-v1.", field="types")
        return value
    if node.kind == "DECIMAL":
        budget.add(1024)
        return _decimal_from_arrow(scalar.as_py(), node)
    if node.kind in TEMPORAL_CLASSES_BY_NAME:
        budget.add(1024)
        if node.kind == "DATETIME" and scalar["zone"].is_valid:
            budget.add(16 * scalar["zone"].as_buffer().size)
        return temporal_from_components(node.kind, scalar.as_py())
    if node.kind == "TIMESTAMP":
        return Timestamp(scalar.cast(pa.int64()).as_py())
    if node.kind == "UUID":
        return Uuid(scalar.as_py())
    return scalar.as_py()


def _polars_collection_layout(actual, expected, pa):
    """Allow only offset-width/binary-width and MAP-to-entry-list representation changes."""
    if pa.types.is_map(expected):
        if pa.types.is_map(actual):
            return _polars_collection_layout(actual.key_type, expected.key_type, pa) and _polars_collection_layout(actual.item_type, expected.item_type, pa)
        if not (pa.types.is_list(actual) or pa.types.is_large_list(actual)):
            return False
        entries = actual.value_type
        return (pa.types.is_struct(entries) and [f.name for f in entries] == ["key", "value"]
                and _polars_collection_layout(entries[0].type, expected.key_type, pa)
                and _polars_collection_layout(entries[1].type, expected.item_type, pa))
    if pa.types.is_list(expected):
        return ((pa.types.is_list(actual) or pa.types.is_large_list(actual))
                and _polars_collection_layout(actual.value_type, expected.value_type, pa))
    if pa.types.is_struct(expected):
        return (pa.types.is_struct(actual) and len(actual) == len(expected)
                and all(a.name == e.name and _polars_collection_layout(a.type, e.type, pa) for a, e in zip(actual, expected)))
    if pa.types.is_string(expected):
        return pa.types.is_string(actual) or pa.types.is_large_string(actual)
    if pa.types.is_binary(expected) or pa.types.is_fixed_size_binary(expected):
        return (actual == expected or pa.types.is_binary(actual) or pa.types.is_large_binary(actual))
    return actual == expected
