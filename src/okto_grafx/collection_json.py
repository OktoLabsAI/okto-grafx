"""Exact, bounded JSON values for declared native collections, without type inference.

Known collection structure follows its StoredType. ANY uses explicit tags, including
map entry pairs, so user maps cannot accidentally become tagged native scalars.
This is a transport boundary; persisted rows still use the existing native codec.
"""

from __future__ import annotations

import base64
import json
import math
import uuid
from types import MappingProxyType
from typing import NoReturn, cast

from okto_grafx.domain.model.decimal_interchange import decimal_from_json_value, decimal_json_value
from okto_grafx.domain.model.decimal_values import DecimalValue
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.stored_types import StoredType, encode_stored_type, validate_typed_value
from okto_grafx.domain.model.temporal_interchange import (
    TEMPORAL_CLASSES_BY_NAME, temporal_from_json_value, temporal_json_value,
)
from okto_grafx.domain.model.value import (
    MAX_VALUE_DEPTH, Timestamp, Uuid, encode_value, value_type_of,
)
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded

__all__ = ["collection_json_value", "collection_from_json_value"]

_COLLECTIONS = frozenset({"LIST", "MAP", "ARRAY", "STRUCT"})
_LEAF_CLASSES = {"BOOL": (bool,), "INT64": (int,), "DOUBLE": (float,), "STRING": (str,),
                 "BYTES": (bytes, bytearray), "TIMESTAMP": (Timestamp,), "UUID": (Uuid,),
                 "DECIMAL": (DecimalValue,), **{name: (cls,) for name, cls in TEMPORAL_CLASSES_BY_NAME.items()}}


def _invalid() -> SchemaMismatchError:
    """Return the stable native collection transport error category."""
    return SchemaMismatchError("Invalid exact collection JSON value.", reason="collection_transport")


class _Budget:
    """Bound traversal/leaf construction as well as the final encoded UTF-8 cell."""

    def __init__(self, maximum: int) -> None:
        """Validate the bound before constructing any value output."""
        if type(maximum) is not int or not 1 <= maximum <= 2**31:
            raise GrafxConfigurationError("max_bytes must be 1..2^31.", field="max_bytes")
        self.maximum = maximum
        self.nodes = 0
        self.text_bytes = 0

    def node(self, depth: int, *, json_tree: bool = False) -> None:
        """Bound occurrences, including repeated aliases and encoded ANY wrappers."""
        self.nodes += 1
        if depth > (3 * MAX_VALUE_DEPTH + 4 if json_tree else MAX_VALUE_DEPTH):
            raise _invalid()
        if self.nodes > self.maximum:
            self.refuse()

    def text(self, value: str) -> None:
        """Charge UTF-8 leaf/key text before copying or rendering it."""
        if type(value) is not str:
            raise _invalid()
        if len(value) > self.maximum - self.text_bytes:
            self.refuse()
        self.text_bytes += len(value.encode("utf-8"))
        if self.text_bytes > self.maximum:
            self.refuse()

    def refuse(self) -> NoReturn:
        """Raise the public logical cell-budget error without partial output."""
        raise GrafxQueryBudgetExceeded("Collection JSON cell bound exceeded.", resource="collection_json")


def _descriptor(value: StoredType) -> None:
    """Require a bounded collection-root declaration without changing it."""
    encode_stored_type(value)  # Defensive shape, depth and descriptor-byte admission.
    if value.kind not in _COLLECTIONS:
        raise GrafxConfigurationError("Collection transport needs a collection-root StoredType.", field="types")


def _json_bound(value: object, maximum: int) -> None:
    """Reject cycles/host objects and bound construction before JSON serialization."""
    budget = _Budget(maximum)

    def visit(item: object, depth: int) -> None:
        """Charge and validate every nested JSON node before serialization."""
        budget.node(depth, json_tree=True)
        if type(item) is str:
            budget.text(item)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) is dict:
            for key, child in item.items():
                budget.text(key)
                visit(child, depth + 1)
        elif item is None or type(item) is bool:
            return
        elif type(item) is int:
            if not -(2**63) <= item < 2**63:
                raise _invalid()
        elif type(item) is float:
            if not math.isfinite(item):
                raise _invalid()
        else:
            raise _invalid()

    visit(value, 0)
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > maximum:
        budget.refuse()


def _integer(value: object) -> int:
    """Parse canonical signed-int64 text without JSON-number precision assumptions."""
    if type(value) is not str or not 1 <= len(value) <= 20:
        raise _invalid()
    digits = value[1:] if value.startswith("-") else value
    if not digits.isascii() or not digits.isdecimal() or (len(digits) > 1 and digits[0] == "0") or value == "-0":
        raise _invalid()
    number = int(value)
    if not -(2**63) <= number < 2**63:
        raise _invalid()
    return number


def _leaf_to(kind: str, value: object, budget: _Budget) -> object:
    """Encode one validated finite native leaf with exact integer/native tags."""
    if type(value) not in _LEAF_CLASSES.get(kind, ()):
        raise _invalid()
    if value_type_of(value).name != kind:
        raise _invalid()
    if kind == "STRING":
        budget.text(value)
    elif kind == "BYTES":
        # Charge all encoded binary leaves cumulatively before allocating Base64,
        # not only each leaf in isolation before a potentially huge parent list.
        budget.text_bytes += 4 * ((len(value) + 2) // 3)
        if budget.text_bytes > budget.maximum:
            budget.refuse()
    encode_value(value)  # Native UUID/temporal/decimal shape remains authoritative.
    if kind == "INT64":
        return str(value)
    if kind == "TIMESTAMP":
        return str(value.micros)
    if kind == "UUID":
        return str(uuid.UUID(bytes=value.raw))
    if kind == "BYTES":
        return base64.b64encode(value).decode("ascii")
    if kind == "DECIMAL":
        return decimal_json_value(value)
    if kind in TEMPORAL_CLASSES_BY_NAME:
        return temporal_json_value(value)
    if kind == "DOUBLE" and not math.isfinite(value):
        raise _invalid()
    if kind not in {"BOOL", "DOUBLE", "STRING"}:
        raise _invalid()
    return value


def _leaf_from(kind: str, value: object) -> object:
    """Decode an explicitly declared leaf; never guess types from user maps/text."""
    if kind == "INT64":
        return _integer(value)
    if kind == "TIMESTAMP":
        return Timestamp(_integer(value))
    if kind == "UUID":
        if type(value) is not str:
            raise _invalid()
        parsed = uuid.UUID(value)
        if str(parsed) != value:
            raise _invalid()
        return Uuid(parsed.bytes)
    if kind == "BYTES":
        if type(value) is not str:
            raise _invalid()
        decoded = base64.b64decode(value, validate=True)
        if base64.b64encode(decoded).decode("ascii") != value:
            raise _invalid()
        return decoded
    if kind == "DECIMAL":
        return decimal_from_json_value(value)
    if kind in TEMPORAL_CLASSES_BY_NAME:
        return temporal_from_json_value(kind, value)
    if kind == "BOOL" and type(value) is bool or kind == "STRING" and type(value) is str:
        return value
    if kind == "DOUBLE" and type(value) in (float, int):
        converted = float(value)
        if math.isfinite(converted) and (type(value) is float or int(converted) == value):
            return converted
    raise _invalid()


def _dynamic(value: object, depth: int, budget: _Budget, *, decode: bool) -> object:
    """Keep heterogeneous families and arbitrary native map keys unambiguous."""
    budget.node(depth)
    if value is None:
        return None
    if decode:
        if type(value) is not dict or type(value.get("type")) is not str:
            raise _invalid()
        tag = value["type"]
        kind = tag.upper()
        if tag != kind.lower():
            raise _invalid()
        if kind == "DECIMAL" or kind in TEMPORAL_CLASSES_BY_NAME:
            return _leaf_from(kind, value)
        field = "entries" if kind == "MAP" else "value"
        if set(value) != {"type", field}:
            raise _invalid()
        payload = value[field]
        if kind in ("LIST", "MAP"):
            if type(payload) is not list:
                raise _invalid()
            if kind == "LIST":
                return tuple(_dynamic(child, depth + 1, budget, decode=True) for child in payload)
            result = {}
            for pair in payload:
                if type(pair) is not list or len(pair) != 2:
                    raise _invalid()
                key, item = (_dynamic(child, depth + 1, budget, decode=True) for child in pair)
                if key in result:
                    raise _invalid()
                result[key] = item
            return result
        return _leaf_from(kind, payload)
    if type(value) in (list, tuple):
        return {"type": "list", "value": [_dynamic(child, depth + 1, budget, decode=False) for child in value]}
    if type(value) in (dict, MappingProxyType):
        return {"type": "map", "entries": [[_dynamic(key, depth + 1, budget, decode=False),
                                             _dynamic(child, depth + 1, budget, decode=False)] for key, child in value.items()]}
    kind = value_type_of(value).name
    result = _leaf_to(kind, value, budget)
    if kind == "DECIMAL" or kind in TEMPORAL_CLASSES_BY_NAME:
        return result
    return {"type": kind.lower(), "value": result}


def _convert(descriptor: StoredType, value: object, depth: int, budget: _Budget, *, decode: bool) -> object:
    """Build a detached value tree under fixed type and value-depth bounds."""
    if descriptor.kind == "ANY":
        return _dynamic(value, depth, budget, decode=decode)
    budget.node(depth)
    if value is None:
        return None
    kind = descriptor.kind
    if kind in ("LIST", "ARRAY"):
        if type(value) not in ((list,) if decode else (tuple, list)):
            raise _invalid()
        if kind == "ARRAY" and len(value) != descriptor.length:
            raise _invalid()
        result = [_convert(descriptor.element, child, depth + 1, budget, decode=decode) for child in value]
        return tuple(result) if decode else result
    if kind in ("MAP", "STRUCT"):
        if type(value) not in ((dict,) if decode else (dict, MappingProxyType)):
            raise _invalid()
        fields = dict(descriptor.fields) if kind == "STRUCT" else None
        if fields is not None and set(fields) != set(value):
            raise _invalid()
        result = {}
        for key, child in value.items():
            budget.text(key)
            result[key] = _convert(descriptor.element if fields is None else fields[key], child, depth + 1, budget, decode=decode)
        return result
    return _leaf_from(kind, value) if decode else _leaf_to(kind, value, budget)


def collection_json_value(descriptor: StoredType, value: object, *, max_bytes: int = 65536) -> object:
    """Encode a canonical stored collection to owned JSON primitives, at most max_bytes UTF-8.

    INT64/TIMESTAMP use decimal strings; decimal/temporals keep native tags. ANY
    tags every non-null value and represents maps as entry pairs. No schema
    normalization or native storage mutation occurs. Bounds are not an RSS promise.
    """
    _descriptor(descriptor)
    budget = _Budget(max_bytes)
    try:
        result = _convert(descriptor, value, 0, budget, decode=False)
        validate_typed_value(descriptor, value)
        _json_bound(result, max_bytes)
        return result
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError) as failure:
        raise _invalid() from failure


def collection_from_json_value(descriptor: StoredType, value: object, *, max_bytes: int = 65536) -> tuple[object, ...] | dict[str, object] | None:
    """Decode bounded exact JSON primitives; wrong types, metadata or missing fields refuse.

    The caller's JSON parser must reject duplicate object keys and nonfinite
    constants. Native import readers do so. DECIMAL p/s and STRUCT fields must
    already match this transport descriptor; later target assignment is separate.
    """
    _descriptor(descriptor)
    budget = _Budget(max_bytes)
    try:
        _json_bound(value, max_bytes)
        result = _convert(descriptor, value, 0, budget, decode=True)
        validate_typed_value(descriptor, result)
        return cast("tuple[object, ...] | dict[str, object] | None", result)
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError) as failure:
        raise _invalid() from failure
