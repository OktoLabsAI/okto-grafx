"""Internal temporal append-image prototype, NOT connected to database publication.

This module performs no writes, acquires no authority and exposes no consumer API.
Its caller must eventually supply a proved durable view and publish complete images
through native OCC/WAL/redo. Until that integration is implemented, no database can
activate or use this format. See SPEC-GX-CAP-3 and its explicit remaining work.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxError, GrafxQueryBudgetExceeded,
)
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.model.catalog import _decode_table, _encode_table
from okto_grafx.domain.model.schema import TableDef, decode_tuple, encode_tuple
from okto_grafx.domain.page import Page, PageType
from okto_grafx.domain.page.layout import validate_page_size
from okto_grafx.domain.txn.commit_identity import CommitId

_FILE = "system-history.dat"  # Deliberately NOT an admitted native WRITE_PAGE target yet.
_HEAD = struct.Struct("<8s16sQQQQ32s")
_BLOCK = struct.Struct("<8s16sQIIII32s32s")
_ROW = struct.Struct("<BQII")
_U32 = struct.Struct("<I")
_HEAD_MAGIC = b"GXHYHD01"
_BLOCK_MAGIC = b"GXHYBL01"
_MAX_BATCH = 16 * 1024 * 1024
_MAX_ROW = 1024 * 1024
_MAX_SCHEMA = 65536
_MAX_CHANGES = 4096


def _invalid(field: str) -> GrafxConfigurationError:
    return GrafxConfigurationError("Invalid internal history append input.", field=field)


def _corrupt(field: str) -> GrafxCorruptionDetected:
    return GrafxCorruptionDetected("Invalid internal history image.", field=field, component="system_history")


def _schema_bytes(table: TableDef) -> bytes:
    return (_encode_table(table) + struct.pack("<H", len(table.schema_layouts))
            + b"".join(struct.pack("<HH", *pair) for pair in table.schema_layouts))


@dataclass(frozen=True, slots=True)
class HistoryChange:
    """Settled row effect; RecordId is lineage, not a reusable physical reference.

    Operations: 1=create, 2=update, 3=delete. Update/delete interval closure and
    endpoint lineage must be proved by the future native integration, not this codec.
    """

    table: TableDef
    record_id: int
    operation: int
    values: tuple[object, ...]

    def encode(self) -> bytes:
        """Detach a full historical schema plus native values, with explicit bounds."""
        if (type(self.table) is not TableDef or type(self.record_id) is not int
                or not 0 < self.record_id < 2**64 or type(self.operation) is not int
                or self.operation not in (1, 2, 3) or type(self.values) is not tuple):
            raise _invalid("change")
        schema = _schema_bytes(self.table)
        if len(schema) > _MAX_SCHEMA:
            raise _invalid("schema_bytes")
        if self.operation == 3:
            if self.values:
                raise _invalid("delete_values")
            payload = b""
        else:
            payload = encode_tuple(self.table, self.values)
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
        if count > 64 or offset + count * 4 != len(encoded_schema):
            raise _corrupt("schema_layouts")
        table = replace(table, schema_layouts=tuple(
            struct.unpack_from("<HH", encoded_schema, offset + position * 4)
            for position in range(count)))
        # Reuse the native binary schema grammar; reject normalization or trailing bytes.
        if _schema_bytes(table) != encoded_schema:
            raise _corrupt("schema_framing")
        payload = raw[_ROW.size + schema_size:]
        change = HistoryChange(table, record_id, operation,
                               () if operation == 3 else decode_tuple(table, payload))
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

    def __post_init__(self):
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
        return 1 + (len(self.payload) + capacity - 1) // capacity

    def bind(self, sequence: int) -> tuple[HistoryPageImage, ...]:
        """Produce immutable head/chunks at one qualified future COMMIT coordinate."""
        CommitId(self.database_uuid, sequence)
        if sequence <= self.previous_sequence:
            raise _invalid("commit_order")
        count = self.image_count - 1
        capacity = self.page_size - 32 - 4 - _BLOCK.size
        if self.first_page + count >= NO_PAGE:
            raise _invalid("page_extent")
        digest = hashlib.sha256(self.payload).digest()
        chain = hashlib.sha256(self.previous_digest + struct.pack("<Q", sequence) + digest).digest()
        header = _HEAD.pack(_HEAD_MAGIC, self.database_uuid, self.activation,
                            sequence, self.batch_count + 1, self.first_page + count, chain)
        images = [_image(self.page_size, 0, sequence, header)]
        for part in range(count):
            chunk = self.payload[part * capacity:(part + 1) * capacity]
            block = _BLOCK.pack(_BLOCK_MAGIC, self.database_uuid, sequence,
                                self.batch_count, part, count, len(self.payload),
                                self.previous_digest, digest) + chunk
            images.append(_image(self.page_size, self.first_page + part, sequence, block))
        return tuple(images)


def _image(page_size: int, number: int, sequence: int, payload: bytes) -> HistoryPageImage:
    page = Page(PageType.META, page_size=page_size, page_index=number, page_lsn=sequence)
    page.insert_slot(payload)
    return HistoryPageImage(_FILE, number, page.to_bytes())


class SystemHistoryStore:
    """Pure bounded image planning/validation; not registered with Database or recovery."""

    def __init__(self, read_page: Callable[[str, int], bytes], *, database_uuid: bytes, page_size: int):
        self.database_uuid = CommitId(database_uuid, 1).database_uuid
        self.page_size = validate_page_size(page_size)
        self.read_page = read_page

    def initialize(self, activation: int) -> HistoryPageImage:
        """Plan an empty root; caller has not been granted authority to persist it."""
        CommitId(self.database_uuid, activation)
        return _image(self.page_size, 0, activation, _HEAD.pack(
            _HEAD_MAGIC, self.database_uuid, activation, activation, 0, 1, bytes(32)))

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

    def _head(self, expected_sequence: int, page_count: int):
        CommitId(self.database_uuid, expected_sequence)
        if type(page_count) is not int or not 1 <= page_count < NO_PAGE:
            raise _invalid("page_count")
        page = self._page(0)
        raw = page.read_slot(0)
        if len(raw) != _HEAD.size:
            raise _corrupt("head_length")
        magic, identity, activation, sequence, batches, next_page, digest = _HEAD.unpack(raw)
        if (magic != _HEAD_MAGIC or identity != self.database_uuid or sequence != expected_sequence
                or page.page_lsn != sequence or not 0 < activation <= sequence
                or next_page != page_count or batches > next_page - 1 or batches >= 2**32
                or (batches == 0 and (next_page != 1 or digest != bytes(32) or activation != sequence))):
            raise _corrupt("head_coverage")
        return activation, sequence, batches, next_page, digest

    def prepare(self, changes: Sequence[HistoryChange], *, expected_sequence: int,
                page_count: int) -> PreparedHistoryAppend:
        """Capture bounded event bytes and one exact root, without allocation or writes."""
        payload = _encode_changes(changes)
        activation, sequence, batches, next_page, digest = self._head(expected_sequence, page_count)
        return PreparedHistoryAppend(self.database_uuid, self.page_size, activation, sequence,
                                     batches, next_page, digest, payload)

    def validate_append_images(
        self, images: tuple[HistoryPageImage, ...], *, previous_sequence: int,
        page_count: int, commit: CommitId,
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
        activation, sequence, batches, next_page, digest = self._head(previous_sequence, page_count)

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
        for number in range(page_count, extent):
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
                                         sequence, batches, next_page, digest, payload)
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
        activation, _, batches, _, expected_digest = self._head(expected_sequence, page_count)
        number = 1
        previous_sequence = activation
        chain = bytes(32)
        output = []
        captured_bytes = captured_changes = 0
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
                descriptor = (sequence, count, size, previous, digest)
                if (magic != _BLOCK_MAGIC or identity != self.database_uuid or batch != ordinal
                        or position != part or not previous_sequence < sequence <= expected_sequence
                        or page.page_lsn != sequence or previous != chain
                        or not 4 <= size <= _MAX_BATCH or count != (size + capacity - 1) // capacity
                        or len(raw) - _BLOCK.size != min(capacity, size - part * capacity)
                        or wanted is not None and descriptor != wanted):
                    raise _corrupt("chunk_coverage")
                wanted = descriptor
                if part == 0:
                    captured_bytes += size
                    if captured_bytes > max_bytes:
                        raise GrafxQueryBudgetExceeded("History read byte budget exceeded.", resource="history_bytes")
                pieces.append(raw[_BLOCK.size:])
                number += 1
                part += 1
                if part == count:
                    break
            payload = b"".join(pieces)
            if hashlib.sha256(payload).digest() != digest:
                raise _corrupt("batch_digest")
            captured_changes += _U32.unpack_from(payload)[0]
            if captured_changes > max_changes:
                raise GrafxQueryBudgetExceeded("History change budget exceeded.", resource="history_changes")
            changes = _decode_changes(payload)
            output.append((CommitId(self.database_uuid, sequence), changes))
            chain = hashlib.sha256(chain + struct.pack("<Q", sequence) + digest).digest()
            previous_sequence = sequence
        if number != page_count or chain != expected_digest or previous_sequence != expected_sequence:
            raise _corrupt("chain_coverage")
        return tuple(output)
