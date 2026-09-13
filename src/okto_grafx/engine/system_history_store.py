"""Pure temporal codecs and bounded image plans used by native history publication.

This module performs no writes and acquires no authority. The native coordinator
and recovery preflight own qualified views, both OCC checks, WAL and application.
Applications consume Database's typed APIs, never these internal image helpers.
"""

from __future__ import annotations

__all__ = ["HistoryChange", "HistoryPageImage", "PreparedHistoryAppend", "SystemHistoryStore"]

import hashlib
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from okto_grafx.engine.system_history_index_store import PreparedHistoryIndex

from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxError, GrafxQueryBudgetExceeded,
)
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.model.catalog import Catalog, _decode_table, _encode_table
from okto_grafx.domain.model.schema import TableDef, decode_tuple, encode_tuple, is_identifier
from okto_grafx.domain.model.relationship_type import RelationshipTypeDef
from okto_grafx.domain.model.node_labels import (
    NODE_LABELS_CAPABILITY, decode_node_labels, encode_node_labels, validate_node_labels,
)
from okto_grafx.domain.page import Page, PageType
from okto_grafx.domain.page.layout import validate_page_size
from okto_grafx.domain.txn.commit_identity import CommitId

_FILE = "system-history.dat"
_HEAD = struct.Struct("<8s16sQQQQ32s")
_BLOCK = struct.Struct("<8s16sQIIII32s32s")
_ROW = struct.Struct("<BQII")
_U32 = struct.Struct("<I")
_HEAD_MAGIC = b"GXHYHD01"
_COMPACT_HEAD_MAGIC = b"GXHYHD03"
_BLOCK_MAGIC = b"GXHYBL01"
_MAX_BATCH = 16 * 1024 * 1024
_MAX_ROW = 1024 * 1024
_MAX_SCHEMA = 65536
_MAX_CHANGES = 4096


def _invalid(field: str) -> GrafxConfigurationError:
    return GrafxConfigurationError("Invalid internal history append input.", field=field)


def _corrupt(field: str) -> GrafxCorruptionDetected:
    return GrafxCorruptionDetected("Invalid internal history image.", field=field, component="system_history")


def _schema_bytes(table: TableDef, logical_type: str | None = None, *, explicit_labels: bool = False) -> bytes:
    raw = (_encode_table(table) + struct.pack("<H", len(table.schema_layouts))
           + b"".join(struct.pack("<HH", *pair) for pair in table.schema_layouts))
    flags = int(table.flexible_properties) | (int(table.unlabeled) << 1) | (int(table.vector_identity_names) << 2)
    if table.extra_node_labels or explicit_labels:
        name = b"" if logical_type is None else logical_type.encode("ascii")
        raw += b"GXHM03" + struct.pack("<BH", flags | (int(explicit_labels) << 3), len(name)) + name
        raw += encode_node_labels(table.extra_node_labels)
    elif flags or logical_type is not None:
        name = b"" if logical_type is None else logical_type.encode("ascii")
        marker = b"GXHM02" if table.vector_identity_names else b"GXHM01"
        raw += marker + struct.pack("<BH", flags, len(name)) + name
    return raw


def historical_relationship_types(names: dict[int, str | None]) -> tuple[RelationshipTypeDef, ...]:
    """Describe only the selected historical members, never current catalog membership."""
    grouped = {}
    for key, name in names.items():
        if name is not None:
            grouped.setdefault(name, []).append(key)
    return tuple(RelationshipTypeDef(name, tuple(sorted(keys))) for name, keys in sorted(grouped.items()))


def validate_history_model(change: HistoryChange, catalog: Catalog) -> None:
    """Prove immutable model metadata against catalog authority; never fill lost metadata."""
    current = catalog.table_by_id(change.table.table_id)
    name = catalog.relationship_type_name(current.table_id) if current.kind == "rel" else current.name
    logical = None if name == current.name else name
    if (change.table.flexible_properties != current.flexible_properties
            or change.table.vector_identity_names != current.vector_identity_names
            or change.table.unlabeled != current.unlabeled or change.logical_type != logical):
        raise _corrupt("historical_model_authority")
    if change.table.extra_node_labels or change.node_labels is not None or change.redacted_node_labels:
        if (NODE_LABELS_CAPABILITY not in catalog.required_capabilities()
                or change.table.kind != "node"
                or not current.admits_node_labels(change.table.extra_node_labels)
                or change.node_labels is not None and not change.table.admits_node_labels(change.node_labels)):
            raise _corrupt("historical_label_authority")


@dataclass(frozen=True, slots=True)
class HistoryChange:
    """Settled row effect; RecordId is lineage, not a reusable physical reference.

    Operations: 1=create, 2=update, 3=delete, 4=schema (RecordId zero),
    5=redacted create, 6=redacted update. Interval closure and
    endpoint lineage is proved by native publication/recovery and the temporal reader,
    not by this codec in isolation.
    """

    table: TableDef
    record_id: int
    operation: int
    values: tuple[object, ...]
    redacted_bytes: int = 0
    logical_type: str | None = None
    node_labels: tuple[str, ...] | None = None
    redacted_node_labels: bool = False

    def encode(self) -> bytes:
        """Detach a full historical schema plus native values, with explicit bounds."""
        if (type(self.table) is not TableDef or type(self.record_id) is not int
                or not 0 <= self.record_id < 2**64 or type(self.operation) is not int
                or self.operation not in (1, 2, 3, 4, 5, 6) or type(self.values) is not tuple
                or (self.record_id == 0) != (self.operation == 4)
                or type(self.redacted_bytes) is not int or not 0 <= self.redacted_bytes <= _MAX_ROW
                or type(self.redacted_node_labels) is not bool
                or self.redacted_node_labels and (self.operation not in (5, 6)
                    or self.table.kind != "node" or self.node_labels is not None)
                or self.operation not in (5, 6) and self.redacted_bytes != 0):
            raise _invalid("change")
        if self.logical_type is not None and (
            type(self.logical_type) is not str or not is_identifier(self.logical_type)
            or self.table.kind != "rel" or self.logical_type == self.table.name
        ):
            raise _invalid("logical_type")
        if self.node_labels is not None:
            validate_node_labels(self.node_labels)
            if (self.operation not in (1, 2) or self.table.kind != "node"
                    or not self.table.admits_node_labels(self.node_labels)):
                raise _invalid("node_labels")
        schema = _schema_bytes(self.table, self.logical_type,
                               explicit_labels=self.node_labels is not None or self.redacted_node_labels)
        if len(schema) > _MAX_SCHEMA:
            raise _invalid("schema_bytes")
        if self.operation in (3, 4, 5, 6):
            if self.values:
                raise _invalid("delete_values")
            payload = bytes(self.redacted_bytes)
        else:
            payload = encode_tuple(self.table, self.values)
            if self.node_labels is not None:
                payload = encode_node_labels(self.node_labels) + payload
        if len(payload) > _MAX_ROW:
            raise _invalid("row_bytes")
        return _ROW.pack(self.operation, self.record_id, len(schema), len(payload)) + schema + payload


def _decode_change(raw: bytes) -> HistoryChange:
    try:
        operation, record_id, schema_size, payload_size = _ROW.unpack_from(raw)
        if (schema_size > _MAX_SCHEMA or payload_size > _MAX_ROW
                or len(raw) != _ROW.size + schema_size + payload_size):
            raise _corrupt("change_length")
        encoded_schema = raw[_ROW.size:_ROW.size + schema_size]
        table, offset = _decode_table(encoded_schema, 0)
        count = struct.unpack_from("<H", encoded_schema, offset)[0]
        offset += 2
        if count > 64 or offset + count * 4 > len(encoded_schema):
            raise _corrupt("schema_layouts")
        table = replace(table, schema_layouts=tuple(
            struct.unpack_from("<HH", encoded_schema, offset + position * 4)
            for position in range(count)))
        offset += count * 4
        logical_type = None
        explicit_labels = False
        if offset < len(encoded_schema):
            marker = encoded_schema[offset:offset+6]
            if marker not in (b"GXHM01", b"GXHM02", b"GXHM03"):
                raise _corrupt("schema_model")
            flags, size = struct.unpack_from("<BH", encoded_schema, offset+6)
            allowed_flags = 15 if marker == b"GXHM03" else 7 if marker == b"GXHM02" else 3
            end = offset+9+size
            if flags & ~allowed_flags or size > 128 or end > len(encoded_schema):
                raise _corrupt("schema_model")
            logical_type = encoded_schema[offset+9:end].decode("ascii") if size else None
            candidates = ()
            if marker == b"GXHM03":
                candidates, end = decode_node_labels(encoded_schema, end)
                explicit_labels = bool(flags & 8)
            if end != len(encoded_schema):
                raise _corrupt("schema_model")
            table = replace(table, flexible_properties=bool(flags & 1), unlabeled=bool(flags & 2),
                            vector_identity_names=bool(flags & 4), extra_node_labels=candidates)
        # Reuse the native binary schema grammar; reject normalization or trailing bytes.
        if _schema_bytes(table, logical_type, explicit_labels=explicit_labels) != encoded_schema:
            raise _corrupt("schema_framing")
        payload = raw[_ROW.size + schema_size:]
        node_labels = None
        redacted_node_labels = explicit_labels and operation in (5, 6)
        if explicit_labels and not redacted_node_labels:
            node_labels, position = decode_node_labels(payload)
            payload = payload[position:]
        change = HistoryChange(table, record_id, operation,
                               () if operation in (3, 4, 5, 6) else decode_tuple(table, payload),
                               len(payload) if operation in (5, 6) else 0, logical_type, node_labels,
                               redacted_node_labels)
        if change.encode() != raw:
            raise _corrupt("change_framing")
        return change
    except (GrafxError, ValueError, TypeError, KeyError, struct.error, UnicodeError, RecursionError) as failure:
        raise _corrupt("change") from failure


def _encode_changes(changes: Sequence[HistoryChange]) -> bytes:
    if type(changes) not in (tuple, list) or len(changes) > _MAX_CHANGES:
        raise _invalid("changes")
    pieces = [_U32.pack(len(changes))]
    size = 4
    seen = set()
    for change in changes:
        if type(change) is not HistoryChange:
            raise _invalid("change")
        raw = change.encode()
        identity = (change.table.table_id, change.record_id)
        if identity in seen:
            raise _invalid("unsettled_row_effects")
        seen.add(identity)
        size += 4 + len(raw)
        if size > _MAX_BATCH:
            raise _invalid("batch_bytes")
        pieces.extend((_U32.pack(len(raw)), raw))
    return b"".join(pieces)


def _decode_changes(raw: bytes) -> tuple[HistoryChange, ...]:
    if not 4 <= len(raw) <= _MAX_BATCH:
        raise _corrupt("batch_length")
    count = _U32.unpack_from(raw)[0]
    if count > _MAX_CHANGES:
        raise _corrupt("change_count")
    changes = []
    offset = 4
    for _ in range(count):
        if offset + 4 > len(raw):
            raise _corrupt("change_length")
        size = _U32.unpack_from(raw, offset)[0]
        offset += 4
        if size > _ROW.size + _MAX_SCHEMA + _MAX_ROW or offset + size > len(raw):
            raise _corrupt("change_length")
        changes.append(_decode_change(raw[offset:offset + size]))
        offset += size
    if offset != len(raw):
        raise _corrupt("batch_tail")
    try:
        if _encode_changes(changes) != raw:
            raise _corrupt("batch_framing")
    except GrafxConfigurationError as failure:
        raise _corrupt("batch_framing") from failure
    return tuple(changes)


@dataclass(frozen=True, slots=True)
class HistoryPageImage:
    """Detached bytes, never a WAL/publication proof."""

    file: str
    page_index: int
    raw: bytes


@dataclass(frozen=True, slots=True)
class PreparedHistoryAppend:
    """One captured append input; final sequence rebinding requires no host I/O."""

    database_uuid: bytes
    page_size: int
    activation: int
    previous_sequence: int
    batch_count: int
    first_page: int
    previous_digest: bytes
    payload: bytes
    index: PreparedHistoryIndex | None = None
    compacted: bool = False

    def __post_init__(self) -> None:
        """Refuse forged/unbounded detached inputs; this still grants no write authority."""
        CommitId(self.database_uuid, self.activation)
        CommitId(self.database_uuid, self.previous_sequence)
        validate_page_size(self.page_size)
        if (self.previous_sequence < self.activation or type(self.batch_count) is not int
                or not 0 <= self.batch_count < 2**32 or type(self.first_page) is not int
                or not 1 <= self.first_page < NO_PAGE or self.batch_count > self.first_page - 1
                or type(self.previous_digest) is not bytes or len(self.previous_digest) != 32
                or type(self.payload) is not bytes or not 4 <= len(self.payload) <= _MAX_BATCH
                or (not self.batch_count and (self.first_page != 1 or self.previous_digest != bytes(32)
                                              or self.previous_sequence != self.activation))):
            raise _invalid("prepared_append")
        _decode_changes(self.payload)

    @property
    def image_count(self) -> int:
        """Return fixed cardinality independent of the terminal WAL sequence."""
        capacity = self.page_size - 32 - 4 - _BLOCK.size
        count = (len(self.payload) + capacity - 1) // capacity
        if self.index is None:
            return 1 + count
        _, _, indexed = self.index.bind(database_uuid=self.database_uuid, page_size=self.page_size,
            sequence=self.previous_sequence + 1, first_page=self.first_page + count,
            changes=_decode_changes(self.payload))
        return 1 + count + len(indexed)

    def bind(self, sequence: int) -> tuple[HistoryPageImage, ...]:
        """Produce immutable head/chunks at one qualified future COMMIT coordinate."""
        CommitId(self.database_uuid, sequence)
        if sequence <= self.previous_sequence:
            raise _invalid("commit_order")
        capacity = self.page_size - 32 - 4 - _BLOCK.size
        count = (len(self.payload) + capacity - 1) // capacity
        if self.first_page + count >= NO_PAGE:
            raise _invalid("page_extent")
        digest = hashlib.sha256(self.payload).digest()
        extra = ()
        extension = proof = b""
        block_magic = _BLOCK_MAGIC
        if self.index is not None:
            from okto_grafx.engine.system_history_index_store import _EXT, _EXT_MAGIC, _INDEX_BLOCK_MAGIC
            root, proof, extra = self.index.bind(database_uuid=self.database_uuid, page_size=self.page_size,
                sequence=sequence, first_page=self.first_page + count, changes=_decode_changes(self.payload))
            extension = _EXT.pack(_EXT_MAGIC, *root)
            block_magic = _INDEX_BLOCK_MAGIC
        chain = hashlib.sha256(self.previous_digest + struct.pack("<Q", sequence) + digest + proof).digest()
        header = _HEAD.pack(_COMPACT_HEAD_MAGIC if self.compacted else _HEAD_MAGIC, self.database_uuid, self.activation,
                            sequence, self.batch_count + 1, self.first_page + count + len(extra), chain)
        images = [_image(self.page_size, 0, sequence, header + extension)]
        for part in range(count):
            chunk = self.payload[part * capacity:(part + 1) * capacity]
            block = _BLOCK.pack(block_magic, self.database_uuid, sequence,
                                self.batch_count, part, count, len(self.payload),
                                self.previous_digest, digest) + chunk
            images.append(_image(self.page_size, self.first_page + part, sequence, block))
        return (*images, *extra)


def _image(page_size: int, number: int, sequence: int, payload: bytes) -> HistoryPageImage:
    page = Page(PageType.META, page_size=page_size, page_index=number, page_lsn=sequence)
    page.insert_slot(payload)
    return HistoryPageImage(_FILE, number, page.to_bytes())


class SystemHistoryStore:
    """Pure bounded image planning/validation consumed by native publication and recovery."""

    def __init__(self, read_page: Callable[[str, int], bytes], *, database_uuid: bytes, page_size: int) -> None:
        self.database_uuid = CommitId(database_uuid, 1).database_uuid
        self.page_size = validate_page_size(page_size)
        self.read_page = read_page

    def initialize(self, activation: int) -> HistoryPageImage:
        """Plan an empty root; caller has not been granted authority to persist it."""
        CommitId(self.database_uuid, activation)
        return _image(self.page_size, 0, activation, _HEAD.pack(
            _HEAD_MAGIC, self.database_uuid, activation, activation, 0, 1, bytes(32)))

    def activation_images(self, changes: Sequence[HistoryChange], sequence: int) -> tuple[HistoryPageImage, ...]:
        """Plan the initial baseline at its actual activation COMMIT, without I/O."""
        CommitId(self.database_uuid, sequence)
        if sequence <= 1:
            raise _invalid("activation_sequence")
        plan = PreparedHistoryAppend(self.database_uuid, self.page_size, sequence - 1,
                                     sequence - 1, 0, 1, bytes(32), _encode_changes(changes))
        images = plan.bind(sequence)
        page = Page.from_bytes(images[0].raw)
        fields = list(_HEAD.unpack(page.read_slot(0)))
        fields[2] = sequence
        return (_image(self.page_size, 0, sequence, _HEAD.pack(*fields)), *images[1:])

    def _page(self, number: int) -> Page:
        raw = self.read_page(_FILE, number)
        if type(raw) is not bytes or len(raw) != self.page_size:
            raise _corrupt("page_length")
        page = Page.from_bytes(raw, page_index=number)
        if (page.page_type != PageType.META or page.flags or page.header().reserved
                or page.next_page != NO_PAGE or page.slot_count != 1 or page.is_slot_free(0)
                or page.seq % 2):
            raise _corrupt("page_shape")
        return page

    def _head_state(self, expected_sequence: int, page_count: int):
        CommitId(self.database_uuid, expected_sequence)
        if type(page_count) is not int or not 1 <= page_count < NO_PAGE:
            raise _invalid("page_count")
        page = self._page(0)
        raw = page.read_slot(0)
        from okto_grafx.engine.system_history_index_store import head_root
        head_root(raw, _HEAD.size)
        magic, identity, activation, sequence, batches, next_page, digest = _HEAD.unpack_from(raw)
        if (magic not in (_HEAD_MAGIC, _COMPACT_HEAD_MAGIC) or identity != self.database_uuid or sequence != expected_sequence
                or page.page_lsn != sequence or not 0 < activation <= sequence
                or next_page > page_count or next_page != page_count and magic != _COMPACT_HEAD_MAGIC
                or batches > next_page - 1 or batches >= 2**32
                or (batches == 0 and (next_page != 1 or digest != bytes(32) or activation != sequence))):
            raise _corrupt("head_coverage")
        return (activation, sequence, batches, next_page, digest), magic == _COMPACT_HEAD_MAGIC

    def _head(self, expected_sequence: int, page_count: int):
        return self._head_state(expected_sequence, page_count)[0]

    def prepare(self, changes: Sequence[HistoryChange], *, expected_sequence: int,
                page_count: int, index: PreparedHistoryIndex | None = None) -> PreparedHistoryAppend:
        """Capture bounded event bytes and one exact root, without allocation or writes."""
        payload = _encode_changes(changes)
        head, compacted = self._head_state(expected_sequence, page_count)
        activation, sequence, batches, next_page, digest = head
        return PreparedHistoryAppend(self.database_uuid, self.page_size, activation, sequence,
                                     batches, next_page, digest, payload, index,
                                     compacted)

    def validate_append_images(
        self, images: tuple[HistoryPageImage, ...], *, previous_sequence: int,
        page_count: int, commit: CommitId, index: PreparedHistoryIndex | None = None,
    ) -> tuple[HistoryChange, ...]:
        """Check one complete append against a separately supplied previous root.

        This is a transition validator, NOT proof that ``commit`` is durable. The
        future native caller must prove activation, exact COMMIT lineage, settled
        current-row coverage and the previous root's authority before invoking it.
        An already-applied root is not accepted as its own predecessor. Only root
        page zero and consecutive NEW chunks may change; no old chunk is writable.
        Work is bounded by this append, not the size of the retained history.
        """
        if type(commit) is not CommitId or commit.database_uuid != self.database_uuid:
            raise _invalid("commit_identity")
        CommitId(self.database_uuid, previous_sequence)
        if commit.sequence <= previous_sequence:
            raise _invalid("commit_order")
        if type(page_count) is not int or not 1 <= page_count < NO_PAGE:
            raise _invalid("page_count")
        capacity = self.page_size - 32 - 4 - _BLOCK.size
        max_images = 1 + (_MAX_BATCH + capacity - 1) // capacity
        if index is not None:
            max_images = 2**31 // self.page_size
        if type(images) is not tuple or not 2 <= len(images) <= max_images:
            raise _corrupt("append_image_count")
        supplied = {}
        for image in images:
            if (type(image) is not HistoryPageImage or image.file != _FILE
                    or type(image.page_index) is not int
                    or not 0 <= image.page_index < NO_PAGE
                    or type(image.raw) is not bytes or len(image.raw) != self.page_size
                    or image.page_index in supplied):
                raise _corrupt("append_image")
            supplied[image.page_index] = image.raw
        extent = page_count + len(images) - 1
        if extent >= NO_PAGE or set(supplied) != {0, *range(page_count, extent)}:
            raise _corrupt("append_extent")

        # All input/resource checks above precede even the single predecessor read.
        head, compacted = self._head_state(previous_sequence, page_count)
        activation, sequence, batches, next_page, digest = head

        def read_image(file: str, number: int) -> bytes:
            """Read captured images only; never fill a missing chunk from storage."""
            if file != _FILE or number not in supplied:
                raise _corrupt("missing_append_image")
            return supplied[number]

        incoming = SystemHistoryStore(read_image, database_uuid=self.database_uuid,
                                      page_size=self.page_size)
        after = incoming._head(commit.sequence, extent)
        if after[:3] != (activation, commit.sequence, batches + 1):
            raise _corrupt("append_root_transition")
        pieces = []
        total = 0
        first_payload = incoming._page(page_count).read_slot(0)
        if len(first_payload) <= _BLOCK.size:
            raise _corrupt("append_chunk")
        history_chunks = _BLOCK.unpack_from(first_payload)[5]
        if not 1 <= history_chunks <= (_MAX_BATCH + capacity - 1) // capacity:
            raise _corrupt("append_chunk_count")
        for number in range(page_count, page_count + history_chunks):
            page = incoming._page(number)
            raw = page.read_slot(0)
            if page.page_lsn != commit.sequence or len(raw) <= _BLOCK.size:
                raise _corrupt("append_chunk")
            chunk = raw[_BLOCK.size:]
            total += len(chunk)
            if total > _MAX_BATCH:
                raise _corrupt("append_bytes")
            pieces.append(chunk)
        payload = b"".join(pieces)
        changes = _decode_changes(payload)
        expected = PreparedHistoryAppend(self.database_uuid, self.page_size, activation,
                                         sequence, batches, next_page, digest, payload, index,
                                         compacted)
        if expected.image_count != len(images):
            raise _corrupt("append_image_count")
        # Canonical regeneration binds every chunk header/hash and the whole root
        # to this predecessor and terminal. A CRC-valid splice is still refused.
        if any(supplied[image.page_index] != image.raw for image in expected.bind(commit.sequence)):
            raise _corrupt("append_transition")
        return changes

    def read_batches(self, *, expected_sequence: int, page_count: int,
                     max_pages: int = 65536, max_bytes: int = 32 * 1024 * 1024,
                     max_changes: int = 4096) -> tuple[tuple[CommitId, tuple[HistoryChange, ...]], ...]:
        """Validate a complete bounded chain; never return a partial historical picture."""
        if type(max_pages) is not int or not 1 <= max_pages <= 65536:
            raise _invalid("max_pages")
        if type(page_count) is not int or page_count > max_pages:
            raise _invalid("max_pages")
        if (type(max_bytes) is not int or not 1 <= max_bytes <= 2**31
                or type(max_changes) is not int or not 1 <= max_changes <= 65536):
            raise _invalid("read_budget")
        output = []
        captured_bytes = captured_changes = 0
        for identity, changes, size in self._iter_batches(expected_sequence=expected_sequence, page_count=page_count):
            captured_bytes += size
            captured_changes += len(changes)
            if captured_bytes > max_bytes:
                raise GrafxQueryBudgetExceeded("History read byte budget exceeded.", resource="history_bytes")
            if captured_changes > max_changes:
                raise GrafxQueryBudgetExceeded("History change budget exceeded.", resource="history_changes")
            output.append((identity, changes))
        return tuple(output)

    def _index_start(self, *, expected_sequence: int, page_count: int) -> int | None:
        """Locate activation through bounded batch descriptors, skipping immutable tree pages."""
        from okto_grafx.engine.system_history_index_store import _INDEX_BLOCK_MAGIC
        _, _, batches, _, _ = self._head(expected_sequence, page_count)
        number = 1
        for _ in range(batches):
            fields = _BLOCK.unpack_from(self._page(number).read_slot(0))
            if fields[0] == _INDEX_BLOCK_MAGIC:
                return fields[2]
            number += fields[5]
        return None

    def _iter_batches(self, *, expected_sequence: int, page_count: int):
        """Stream private validation work, bounded per batch, with terminal chain proof.

        Exhaustion is mandatory: yielded batches are not a complete-history proof.
        Public callers materialize under their own aggregate budgets; recovery
        can validate arbitrary retained extent without keeping old events in RAM.
        """
        from okto_grafx.engine.system_history_index_store import head_root, trailer, _INDEX_BLOCK_MAGIC
        activation, _, batches, page_count, expected_digest = self._head(expected_sequence, page_count)
        root = None
        number = 1
        previous_sequence = activation - 1 if batches else activation
        chain = bytes(32)
        for ordinal in range(batches):
            pieces = []
            wanted = None
            part = 0
            while True:
                if number >= page_count:
                    raise _corrupt("missing_chunk")
                page = self._page(number)
                raw = page.read_slot(0)
                if len(raw) <= _BLOCK.size:
                    raise _corrupt("chunk_length")
                magic, identity, sequence, batch, position, count, size, previous, digest = _BLOCK.unpack_from(raw)
                capacity = self.page_size - 32 - 4 - _BLOCK.size
                descriptor = (sequence, count, size, previous, digest, magic)
                if (magic not in (_BLOCK_MAGIC, _INDEX_BLOCK_MAGIC) or identity != self.database_uuid or batch != ordinal
                        or position != part or not previous_sequence < sequence <= expected_sequence
                        or not sequence <= page.page_lsn <= expected_sequence or previous != chain
                        or not 4 <= size <= _MAX_BATCH or count != (size + capacity - 1) // capacity
                        or len(raw) - _BLOCK.size != min(capacity, size - part * capacity)
                        or wanted is not None and descriptor != wanted):
                    raise _corrupt("chunk_coverage")
                wanted = descriptor
                pieces.append(raw[_BLOCK.size:])
                number += 1
                part += 1
                if part == count:
                    break
            payload = b"".join(pieces)
            if hashlib.sha256(payload).digest() != digest:
                raise _corrupt("batch_digest")
            changes = _decode_changes(payload)
            proof = b""
            if magic == _INDEX_BLOCK_MAGIC:
                marker = self._page(number)
                if not sequence <= marker.page_lsn <= expected_sequence:
                    raise _corrupt("index_transition_stamp")
                raw_marker = marker.read_slot(0)
                previous_root, current_root, node_count = trailer(raw_marker, self.database_uuid, sequence)
                if root is not None and previous_root != root or root is None and previous_root != (0, bytes(32)):
                    raise _corrupt("index_transition_chain")
                root = current_root
                number += 1 + node_count
                if number > page_count or root[0] >= number:
                    raise _corrupt("index_transition_extent")
                proof = hashlib.sha256(raw_marker).digest()
            elif root is not None:
                raise _corrupt("index_transition_missing")
            yield CommitId(self.database_uuid, sequence), changes, len(payload)
            chain = hashlib.sha256(chain + struct.pack("<Q", sequence) + digest + proof).digest()
            previous_sequence = sequence
        if number != page_count or chain != expected_digest or previous_sequence != expected_sequence:
            raise _corrupt("chain_coverage")
        if root != head_root(self._page(0).read_slot(0), _HEAD.size):
            raise _corrupt("index_terminal_root")
