"""Owned, bounded native values at a trusted procedure argument/result boundary."""

from __future__ import annotations

from math import isfinite
from collections.abc import Callable
from types import MappingProxyType

from okto_grafx.domain.errors import GrafxError, GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.domain.model.temporal_codec import encode_temporal_value, decode_temporal_value
from okto_grafx.domain.model.decimal_codec import encode_decimal_value, decode_decimal_value
from okto_grafx.domain.model.decimal_values import DecimalValue
from okto_grafx.domain.model.temporal_values import (
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue,
)
from okto_grafx.domain.model.value import (
    INT64_MIN, INT64_MAX, MAX_VALUE_DEPTH, MAX_VECTOR_DIMENSION,
    Timestamp, Uuid, VectorValue, encode_value,
)
from okto_grafx.domain.query.limits import MAX_LIST_ELEMENTS, MAX_MAP_ENTRIES, MAX_QUERY_VALUE_CHARACTERS
from okto_grafx.domain.query.entity_values import NodeValue, RelationshipValue, PathValue

__all__ = ["EXTENDED_PROCEDURE_TYPES", "ENTITY_PROCEDURE_TYPES", "owned_procedure_value", "procedure_result_size"]

_TEMPORAL = {"DATE": DateValue, "LOCALTIME": LocalTimeValue, "TIME": TimeValue,
             "LOCALDATETIME": LocalDateTimeValue, "DATETIME": DateTimeValue, "DURATION": DurationValue}
ENTITY_PROCEDURE_TYPES = frozenset(("NODE", "RELATIONSHIP", "PATH", "LIST<NODE>", "LIST<RELATIONSHIP>"))
"""Entity signatures require an invocation-local native reference resolver."""
EXTENDED_PROCEDURE_TYPES = frozenset(_TEMPORAL) | {"DECIMAL", "LIST", "MAP", "ANY", "VECTOR_F32", "VECTOR_F64"} | ENTITY_PROCEDURE_TYPES
"""Native procedure signatures beyond the original strict scalar and NUMBER contracts."""


def owned_procedure_value(value: object, signature: str, *, max_bytes: int, procedure: str,
                          entity_resolver: Callable[[object], object] | None = None) -> object:
    """Detach one native value with pre-allocation budgets and no host conversion protocols.

    LIST returns a new list and MAP a new string-keyed dict. Shared input children
    are copied per occurrence; cycles are rejected rather than retained. This
    grants no entity/transaction authority and changes no persisted value tag.
    """
    def refuse(reason: str) -> GrafxPlanError:
        """Report a closed value-contract violation without rendering host payloads."""
        return GrafxPlanError("Procedure value violates its native signature contract.",
                              field="procedure_value", reason=reason, procedure=procedure)

    if type(signature) is not str or signature not in EXTENDED_PROCEDURE_TYPES:
        raise refuse("signature")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 2**31:
        raise refuse("budget")
    if value is not None:
        expected = {**_TEMPORAL, "DECIMAL": DecimalValue, "NODE": NodeValue, "RELATIONSHIP": RelationshipValue, "PATH": PathValue}.get(signature)
        if ((expected is not None and type(value) is not expected)
                or ((signature == "LIST" or signature.startswith("LIST<")) and type(value) is not tuple and type(value) is not list)
                or (signature == "MAP" and type(value) is not dict)
                or (signature in ("VECTOR_F32", "VECTOR_F64") and type(value) is not VectorValue)):
            raise refuse("type_mismatch")
        if signature.startswith("LIST<"):
            if len(value) > MAX_LIST_ELEMENTS:
                raise refuse("list_elements")
            wanted = NodeValue if signature == "LIST<NODE>" else RelationshipValue
            if any(item is not None and type(item) is not wanted for item in value):
                raise refuse("type_mismatch")
    used = 0
    active: set[int] = set()

    def charge(amount: int) -> None:
        """Refuse before allocating the next owned container, string or vector image."""
        nonlocal used
        used += amount
        if used > max_bytes:
            raise GrafxQueryBudgetExceeded("Procedure value byte budget exceeded.", resource="procedure_value",
                                           procedure=procedure, limit=max_bytes, observed=used)

    def text(item: object) -> str:
        """Admit exact string values/keys with the native character limit and UTF-8 proof."""
        if type(item) is not str:
            raise refuse("map_key_type")
        if len(item) > MAX_QUERY_VALUE_CHARACTERS:
            raise refuse("text_limit")
        charge(5 + 4 * len(item))
        encode_value(item)  # Validate UTF-8 after its maximum image was budgeted.
        return item

    def copy(item: object, depth: int) -> object:
        """Copy closed native leaves and bounded containers without trusting subclasses."""
        if depth > MAX_VALUE_DEPTH:
            raise refuse("depth")
        kind = type(item)
        if kind is NodeValue or kind is RelationshipValue or kind is PathValue:
            if entity_resolver is None:
                raise refuse("entity_authority")
            observed = entity_resolver(item)
            if type(observed) is not kind:
                raise refuse("entity_authority")
            # The native resolver reconstructs properties under their byte/depth
            # limits; immutable nested property maps need no callback-owned copy.
            charge(procedure_result_size(observed))
            return observed
        if item is None:
            charge(1)
            return None
        if kind is bool:
            charge(2)
            return item
        if kind is int:
            if not INT64_MIN <= item <= INT64_MAX:
                raise refuse("int64_range")
            charge(9)
            return item
        if kind is float:
            if not isfinite(item):
                raise refuse("nonfinite")
            charge(9)
            return item
        if kind is str:
            return text(item)
        if kind is bytes:
            charge(5 + len(item))
            return item
        if kind is DecimalValue:
            charge(19)  # Fixed native frame; budget before encoding or copying.
            return decode_decimal_value(encode_decimal_value(item))[0]
        if any(kind is native for native in _TEMPORAL.values()):
            # This image is bounded by the temporal codec (including zone length),
            # and reconstructs all frozen components rather than retaining aliases.
            raw = encode_temporal_value(item)
            charge(len(raw))
            return decode_temporal_value(raw)[0]
        if kind is Timestamp:
            if type(item.micros) is not int:
                raise refuse("timestamp")
            charge(9)
            return Timestamp(item.micros)
        if kind is Uuid:
            if type(item.raw) is not bytes:
                raise refuse("uuid")
            charge(17)
            return Uuid(item.raw)
        if kind is VectorValue:
            if (type(item.dtype) is not str or item.dtype not in ("float32", "float64")
                    or type(item.space_ref) is not int
                    or (type(item.values) is not tuple and type(item.values) is not list)
                    or not 1 <= len(item.values) <= MAX_VECTOR_DIMENSION
                    or any(type(component) is not int and type(component) is not float for component in item.values)):
                raise refuse("vector")
            charge(9 + 8 * len(item.values))
            owned = VectorValue(tuple(item.values), item.space_ref, item.dtype)
            encode_value(owned)
            return owned
        if kind is not list and kind is not tuple and kind is not dict:
            raise refuse("unsupported_value")
        if id(item) in active:
            raise refuse("cycle")
        limit = MAX_MAP_ENTRIES if kind is dict else MAX_LIST_ELEMENTS
        if len(item) > limit:
            raise refuse("map_entries" if kind is dict else "list_elements")
        charge(5)
        active.add(id(item))
        try:
            if kind is dict:
                return {text(key): copy(child, depth + 1) for key, child in item.items()}
            return [copy(child, depth + 1) for child in item]
        finally:
            active.remove(id(item))

    try:
        result = copy(value, 0)
    except (GrafxPlanError, GrafxQueryBudgetExceeded):
        raise
    except (GrafxError, ValueError, TypeError, OverflowError, AttributeError) as failure:
        raise refuse("invalid_native_value") from failure
    if value is not None and signature in ("VECTOR_F32", "VECTOR_F64"):
        if result.dtype != ("float32" if signature == "VECTOR_F32" else "float64"):
            raise refuse("type_mismatch")
    return result


def procedure_result_size(value: object) -> int:
    """Charge native cells plus bounded entity observation headers, never a stored entity tag."""
    if type(value) is NodeValue or type(value) is RelationshipValue:
        labels = value.labels if type(value) is NodeValue else value.label
        return 256 + len(encode_value(labels)) + procedure_result_size(value.properties)
    if type(value) is PathValue:
        return 64 + sum(procedure_result_size(entity) for entity in (*value.nodes, *value.relationships))
    if type(value) is list or type(value) is tuple:
        return 5 + sum(procedure_result_size(item) for item in value)
    if type(value) is dict or type(value) is MappingProxyType:
        return 5 + sum(len(encode_value(key)) + procedure_result_size(item) for key, item in value.items())
    return len(encode_value(value))
