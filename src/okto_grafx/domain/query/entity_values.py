"""Owned entity materialization for FP-3; separate from executable row bindings.

Nodes, relationships and paths are public observations, not stored column types
or writable handles. General named-path execution remains in progress. Result-door
validation controls admission without widening parameter authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias, Union

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.model.temporal_values import (
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue,
)
from okto_grafx.domain.model.temporal_codec import encode_temporal_value, decode_temporal_value
from okto_grafx.domain.model.decimal_codec import encode_decimal_value, decode_decimal_value
from okto_grafx.domain.model.decimal_values import DecimalValue
from okto_grafx.domain.model.decimal_interchange import decimal_json_value
from okto_grafx.domain.model.temporal_interchange import temporal_json_value
from okto_grafx.domain.model.value import (
    INT64_MAX, INT64_MIN, MAX_VALUE_DEPTH, MAX_VECTOR_DIMENSION, Timestamp, Uuid, VectorValue, encode_value,
)
from okto_grafx.domain.query.entity_identity import EntityIdentity, EntityProvenance
from okto_grafx.domain.model.node_labels import validate_node_labels
from okto_grafx.domain.query.limits import MAX_LIST_ELEMENTS, MAX_MAP_ENTRIES, MAX_QUERY_VALUE_CHARACTERS

_TEMPORAL_TYPES = (DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue)


def _refuse(field: str, reason: str) -> GrafxConfigurationError:
    return GrafxConfigurationError("Invalid detached entity materialization.", field=field, reason=reason)


def _text(value: object, field: str, *, nonempty: bool = False) -> str:
    if type(value) is not str or len(value) > MAX_QUERY_VALUE_CHARACTERS or (nonempty and not value):
        raise _refuse(field, "text")
    return value


def _owned(value: object, depth: int, active: set[int]) -> object:
    """Copy closed value families with the existing query container/depth bounds."""
    if depth > MAX_VALUE_DEPTH:
        raise _refuse("properties", "depth")
    kind = type(value)
    if value is None or kind is bool:
        return value
    if kind is int:
        if not INT64_MIN <= value <= INT64_MAX:
            raise _refuse("properties", "int64")
        return value
    if kind is float:
        if not isfinite(value):
            raise _refuse("properties", "nonfinite")
        return value
    if kind is str:
        return _text(value, "properties")
    if kind in (bytes, bytearray):
        return bytes(value)
    if kind in _TEMPORAL_TYPES:
        # Validate and detach every nested component even for a forged frozen DTO;
        # never retain its children or reinterpret a recorded zone using new rules.
        return decode_temporal_value(encode_temporal_value(value))[0]
    if kind is Timestamp:
        if type(value.micros) is not int:
            raise _refuse("properties", "timestamp")
        return Timestamp(value.micros)
    if kind is DecimalValue:
        return decode_decimal_value(encode_decimal_value(value))[0]
    if kind is Uuid:
        if type(value.raw) not in (bytes, bytearray):
            raise _refuse("properties", "uuid")
        return Uuid(bytes(value.raw))
    if kind is VectorValue:
        if type(value.values) not in (tuple, list) or not 1 <= len(value.values) <= MAX_VECTOR_DIMENSION:
            raise _refuse("properties", "vector")
        if any(type(x) not in (int, float) for x in value.values):
            raise _refuse("properties", "vector")
        if type(value.dtype) is not str or type(value.space_ref) is not int:
            raise _refuse("properties", "vector")
        copied = VectorValue(dtype=value.dtype, space_ref=value.space_ref, values=tuple(value.values))
        encode_value(copied)  # Share the finite/dimension/space/storage-precision contract.
        return copied
    if kind not in (dict, MappingProxyType, tuple, list):
        raise _refuse("properties", "unsupported_value")
    marker = id(value)
    if marker in active:
        raise _refuse("properties", "cycle")
    active.add(marker)
    try:
        if kind in (dict, MappingProxyType):
            if len(value) > MAX_MAP_ENTRIES:
                raise _refuse("properties", "map_entries")
            # A mapping proxy may wrap a host Mapping with a dishonest len().
            # Bound actual enumeration as well as the advertised cardinality.
            pairs = {}
            for key, item in value.items():
                if len(pairs) >= MAX_MAP_ENTRIES:
                    raise _refuse("properties", "map_entries")
                key = _text(key, "property_key")
                if key in pairs:
                    raise _refuse("properties", "duplicate_key")
                pairs[key] = item
            return MappingProxyType({_text(k, "property_key"): _owned(v, depth + 1, active)
                                     for k, v in pairs.items()})
        if len(value) > MAX_LIST_ELEMENTS:
            raise _refuse("properties", "list_elements")
        items = tuple(value)
        if len(items) > MAX_LIST_ELEMENTS:
            raise _refuse("properties", "list_elements")
        return tuple(_owned(item, depth + 1, active) for item in items)
    finally:
        active.remove(marker)


def _properties(value: object) -> Mapping[str, object]:
    if type(value) not in (dict, MappingProxyType):
        raise _refuse("properties", "map")
    return _owned(value, 0, set())  # type: ignore[return-value]


def _metadata(identity: object, provenance: object, kind: str) -> None:
    if type(identity) is not EntityIdentity or identity.kind != kind:
        raise _refuse("identity", "entity_kind")
    if type(provenance) is not EntityProvenance:
        raise _refuse("provenance", "type")
    if not identity.committed and not provenance.pending:
        raise _refuse("provenance", "provisional_not_pending")


def _json_value(value: object) -> object:
    """Versioned, lossless value grammar; map tagging prevents tag collisions."""
    kind = type(value)
    if value is None or kind in (str, bool, float):
        return value
    if kind is int:
        return {"type": "int64", "value": str(value)}
    if kind is bytes:
        return {"type": "bytes", "hex": value.hex()}
    if kind is Timestamp:
        return {"type": "timestamp", "micros": str(value.micros)}
    if kind is Uuid:
        return {"type": "uuid", "hex": value.hex}
    if kind is VectorValue:
        return {"type": "vector", "dtype": value.dtype, "space_ref": str(value.space_ref),
                "components": list(value.values)}
    if kind in _TEMPORAL_TYPES:
        return temporal_json_value(value)
    if kind is DecimalValue:
        return decimal_json_value(value)
    if kind is tuple:
        return {"type": "list", "items": [_json_value(item) for item in value]}
    if kind is MappingProxyType:
        return {"type": "map", "entries": {key: _json_value(item) for key, item in value.items()}}
    raise _refuse("properties", "unsupported_value")


@dataclass(frozen=True, slots=True)
class NodeValue:
    """One owned node observation; equality/hash use only qualified identity."""

    identity: EntityIdentity
    label: str | None = field(compare=False)
    properties: Mapping[str, object] = field(compare=False, hash=False)
    provenance: EntityProvenance = field(compare=False)
    node_labels: tuple[str, ...] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _metadata(self.identity, self.provenance, "node")
        if self.label is not None:
            _text(self.label, "label", nonempty=True)
        if self.node_labels is not None:
            validate_node_labels(self.node_labels)
            if self.label != (self.node_labels[0] if self.node_labels else None):
                raise _refuse("label", "not_first_canonical_label")
        object.__setattr__(self, "properties", _properties(self.properties))

    @property
    def labels(self) -> tuple[str, ...]:
        """Return the actual label set, including an empty set for unlabeled nodes."""
        return self.node_labels if self.node_labels is not None else (() if self.label is None else (self.label,))

    def to_dict(self) -> dict[str, object]:
        """Detach JSON-safe data; neither the DTO nor its serialization is authority."""
        return {"format": "grafx.node.v1", "identity": self.identity.to_dict(), "label": self.label,
                "labels": list(self.labels),
                "properties": {key: _json_value(item) for key, item in self.properties.items()},
                "provenance": self.provenance.to_dict()}


@dataclass(frozen=True, slots=True)
class RelationshipValue:
    """One owned relationship with qualified source/target node identities."""

    identity: EntityIdentity
    label: str = field(compare=False)
    source: EntityIdentity = field(compare=False)
    target: EntityIdentity = field(compare=False)
    properties: Mapping[str, object] = field(compare=False, hash=False)
    provenance: EntityProvenance = field(compare=False)

    def __post_init__(self) -> None:
        _metadata(self.identity, self.provenance, "relationship")
        _text(self.label, "label", nonempty=True)
        for endpoint in (self.source, self.target):
            if type(endpoint) is not EntityIdentity or endpoint.kind != "node":
                raise _refuse("endpoint", "node_identity")
            if endpoint.database_uuid != self.identity.database_uuid:
                raise _refuse("endpoint", "foreign_database")
            if not endpoint.committed and not self.provenance.pending:
                raise _refuse("endpoint", "provisional_not_pending")
        object.__setattr__(self, "properties", _properties(self.properties))

    def to_dict(self) -> dict[str, object]:
        """Export a detached relationship with qualified identities and JSON-safe properties."""
        return {"format": "grafx.relationship.v1", "identity": self.identity.to_dict(), "label": self.label,
                "source": self.source.to_dict(), "target": self.target.to_dict(),
                "properties": {key: _json_value(item) for key, item in self.properties.items()},
                "provenance": self.provenance.to_dict()}


@dataclass(frozen=True, slots=True)
class PathValue:
    """An ordered detached walk; traversal direction is represented by node order.

    Repeated nodes are valid. Relationship uniqueness belongs to the executing
    pattern's trail rules, not to this representation of a walk. A zero-hop path
    still owns its one node. Observations cannot mix databases or read snapshots.
    """

    nodes: tuple[NodeValue, ...]
    relationships: tuple[RelationshipValue, ...]

    def __post_init__(self) -> None:
        if type(self.nodes) not in (tuple, list) or type(self.relationships) not in (tuple, list):
            raise _refuse("path", "sequence")
        if not 1 <= len(self.nodes) <= MAX_LIST_ELEMENTS or len(self.relationships) != len(self.nodes) - 1:
            raise _refuse("path", "cardinality")
        nodes, relationships = tuple(self.nodes), tuple(self.relationships)
        if len(nodes) > MAX_LIST_ELEMENTS or len(relationships) != len(nodes) - 1:
            raise _refuse("path", "cardinality")
        if any(type(node) is not NodeValue for node in nodes) or any(type(rel) is not RelationshipValue for rel in relationships):
            raise _refuse("path", "entity_type")
        database, read_lsn = nodes[0].identity.database_uuid, nodes[0].provenance.read_lsn
        for entity in (*nodes, *relationships):
            if entity.identity.database_uuid != database or entity.provenance.read_lsn != read_lsn:
                raise _refuse("path", "mixed_observation")
        for source, rel, target in zip(nodes, relationships, nodes[1:]):
            if (source.identity, target.identity) not in ((rel.source, rel.target), (rel.target, rel.source)):
                raise _refuse("path", "disconnected")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "relationships", relationships)

    def __len__(self) -> int:
        return len(self.relationships)

    def to_dict(self) -> dict[str, object]:
        """Export ordered detached path nodes and relationships as versioned dictionaries."""
        return {"format": "grafx.path.v1", "nodes": [node.to_dict() for node in self.nodes],
                "relationships": [rel.to_dict() for rel in self.relationships]}


QueryValue: TypeAlias = Union[
    None, bool, int, float, str, bytes, Timestamp, Uuid, VectorValue, DecimalValue,
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue,
    NodeValue, RelationshipValue, PathValue, tuple["QueryValue", ...], dict["QueryValue", "QueryValue"],
]
"""Result-only values; the stored/parameter Value grammar does not admit entities."""


__all__ = [
    'NodeValue',
    'RelationshipValue',
    'PathValue',
    'QueryValue',
]
