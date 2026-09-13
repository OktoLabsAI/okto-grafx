"""Owned native node-label sets, independent of physical tables and properties.

The GXL1 prefix can precede a heap tuple or carry a catalog candidate set. Native
sets are canonical, case-sensitive UTF-8 names. Decode never repairs stored order,
duplicates, invalid text or resource-limit violations.
"""

from __future__ import annotations

import struct

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected

NODE_LABELS_CAPABILITY: str = "node_labels_v1"
NODE_LABELS_CAPABILITY_BIT: int = 1 << 28
MAX_NODE_LABEL_BYTES: int = 65535
MAX_NODE_LABEL_COUNT: int = (MAX_NODE_LABEL_BYTES - 6) // 3
NODE_LABEL_MAGIC: bytes = b"GXL1"
_U16 = struct.Struct("<H")


def normalize_node_labels(labels: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Own a bounded input sequence, removing duplicates without folding names."""
    if type(labels) not in (tuple, list) or len(labels) > MAX_NODE_LABEL_COUNT:
        raise GrafxConfigurationError("Node labels require a bounded tuple/list of names.", field="node_labels")
    names: set[str] = set()
    size = 6
    for name in labels:
        if type(name) is not str or not name or len(name) > MAX_NODE_LABEL_BYTES - 8:
            raise GrafxConfigurationError("Node label names must be nonempty bounded text.", field="node_labels")
        if name in names:
            continue
        try:
            encoded = name.encode("utf-8")
        except UnicodeEncodeError as failure:
            raise GrafxConfigurationError("Node label names require valid UTF-8 text.", field="node_labels") from failure
        size += 2 + len(encoded)
        if size > MAX_NODE_LABEL_BYTES:
            raise GrafxConfigurationError("Node label set exceeds its encoded byte limit.", field="node_labels")
        names.add(name)
    return tuple(sorted(names))


def validate_node_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    """Require an already canonical immutable set at an authority boundary."""
    if type(labels) is not tuple or normalize_node_labels(labels) != labels:
        raise GrafxConfigurationError("Native node labels must be a canonical tuple.", field="node_labels")
    return labels


def encode_node_labels(labels: tuple[str, ...]) -> bytes:
    """Encode a canonical set; an empty set is distinct from implicit table labels."""
    validate_node_labels(labels)
    parts = [NODE_LABEL_MAGIC, _U16.pack(len(labels))]
    for name in labels:
        encoded = name.encode("utf-8")
        parts.extend((_U16.pack(len(encoded)), encoded))
    return b"".join(parts)


def decode_node_labels(raw: bytes, offset: int = 0) -> tuple[tuple[str, ...], int]:
    """Decode one bounded GXL1 prefix, returning the following tuple's offset."""
    if (type(raw) is not bytes or type(offset) is not int or offset < 0
            or offset + 6 > len(raw) or raw[offset:offset + 4] != NODE_LABEL_MAGIC):
        raise GrafxCorruptionDetected("Invalid or truncated native node-label prefix.", field="node_labels")
    begin = offset
    count = _U16.unpack_from(raw, offset + 4)[0]
    offset += 6
    if count > MAX_NODE_LABEL_COUNT or offset + count * 3 > len(raw):
        raise GrafxCorruptionDetected("Invalid native node-label count.", field="node_labels")
    labels: list[str] = []
    previous = ""
    for _ in range(count):
        if offset + 2 > len(raw):
            raise GrafxCorruptionDetected("Truncated node-label length.", field="node_labels")
        size = _U16.unpack_from(raw, offset)[0]
        offset += 2
        end = offset + size
        if not size or end > len(raw) or end - begin > MAX_NODE_LABEL_BYTES:
            raise GrafxCorruptionDetected("Invalid node-label byte length.", field="node_labels")
        try:
            name = raw[offset:end].decode("utf-8")
        except UnicodeDecodeError as failure:
            raise GrafxCorruptionDetected("Invalid node-label UTF-8.", field="node_labels") from failure
        if labels and name <= previous:
            raise GrafxCorruptionDetected("Node labels must be distinct and canonical.", field="node_labels")
        labels.append(name)
        previous = name
        offset = end
    return tuple(labels), offset


__all__ = ["NODE_LABELS_CAPABILITY", "NODE_LABELS_CAPABILITY_BIT", "NODE_LABEL_MAGIC",
           "MAX_NODE_LABEL_BYTES", "MAX_NODE_LABEL_COUNT", "normalize_node_labels",
           "validate_node_labels", "encode_node_labels", "decode_node_labels"]
