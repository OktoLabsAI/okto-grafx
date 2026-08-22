"""The canonical COMMIT payload (CONTRACT.md section 6.5).

A COMMIT record carries what the optimistic validator of section 8.5 needs to decide a conflict
without reading anything else: the snapshot the transaction read at, the partitions it read, the
partitions it wrote, and the pages it touched. The layout is frozen:

``snapshot_lsn u64 | read_partition_count u32 | write_partition_count u32 |
read_partitions[u64...] | write_partitions[u64...] | page_touch_count u32 |
(file_id u16, page_index u32)[...]``

A partition key is ``u64 = (table_id << 32) | partition_index``, so one integer identifies a
partition of a table across the whole database and two transactions intersect exactly when their
key sets intersect.

The payload lives here rather than with the transaction manager because it is a WAL FORMAT, and
the format has one owner. Two definitions of a frozen layout is the integration failure
amendment A24 was written about, arriving through a different door.

Canonical means the partition sets are stored sorted and without repeats. :meth:`CommitPayload.build`
is the door that canonicalises; the decoder deliberately does not, because a payload written by
another build is evidence about what it wrote and must round-trip byte for byte.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import Lsn, PageIndex

__all__ = [
    "PARTITION_INDEX_BITS",
    "MAX_TABLE_ID",
    "MAX_PARTITION_INDEX",
    "MAX_FILE_ID",
    "MAX_TOUCHED_PAGE_INDEX",
    "MAX_PARTITIONS_PER_COMMIT",
    "MAX_PAGE_TOUCHES_PER_COMMIT",
    "PageTouch",
    "CommitPayload",
    "partition_key",
    "split_partition_key",
]

PARTITION_INDEX_BITS: int = 32
"""Low bits of a partition key; the table id occupies the high bits."""

MAX_TABLE_ID: int = 0xFFFFFFFF
"""Largest table id a partition key can carry."""

MAX_PARTITION_INDEX: int = 0xFFFFFFFF
"""Largest partition index a partition key can carry."""

MAX_FILE_ID: int = 0xFFFF
"""The file id of a page touch is a u16."""

MAX_TOUCHED_PAGE_INDEX: int = 0xFFFFFFFF
"""The page index of a page touch is a u32."""

MAX_PARTITIONS_PER_COMMIT: int = 0xFFFFFFFF
"""Partition counts are u32 fields, so neither set may be longer than this."""

MAX_PAGE_TOUCHES_PER_COMMIT: int = 0xFFFFFFFF
"""The page touch count is a u32 field."""

_MAX_U64: int = 0xFFFFFFFFFFFFFFFF

_HEAD = struct.Struct("<QII")
_KEY = struct.Struct("<Q")
_COUNT = struct.Struct("<I")
_TOUCH = struct.Struct("<HI")


def _require_range(field: str, value: object, ceiling: int) -> int:
    """Return an unsigned integer inside its field width, or refuse it by name."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"The {field} of a commit payload must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if not 0 <= value <= ceiling:
        raise GrafxConfigurationError(
            f"The {field} of a commit payload must be between 0 and {ceiling}; got {value}.",
            field=field,
            value=value,
        )
    return value


def partition_key(table_id: int, partition_index: int) -> int:
    """Return the single integer that identifies one partition of one table."""
    _require_range("table_id", table_id, MAX_TABLE_ID)
    _require_range("partition_index", partition_index, MAX_PARTITION_INDEX)
    return (table_id << PARTITION_INDEX_BITS) | partition_index


def split_partition_key(key: int) -> tuple[int, int]:
    """Return the table id and partition index a partition key carries."""
    _require_range("partition_key", key, _MAX_U64)
    return key >> PARTITION_INDEX_BITS, key & MAX_PARTITION_INDEX


@dataclass(frozen=True, slots=True)
class PageTouch:
    """One page a committing transaction wrote, identified by file and page index."""

    file_id: int
    page_index: PageIndex

    def __post_init__(self) -> None:
        """Refuse a file id or a page index that the frozen layout cannot store."""
        _require_range("file_id", self.file_id, MAX_FILE_ID)
        _require_range("page_index", self.page_index, MAX_TOUCHED_PAGE_INDEX)


@dataclass(frozen=True, slots=True)
class CommitPayload:
    """The payload of a COMMIT record, in the frozen layout of CONTRACT.md section 6.5."""

    snapshot_lsn: Lsn
    read_partitions: tuple[int, ...] = ()
    write_partitions: tuple[int, ...] = ()
    page_touches: tuple[PageTouch, ...] = ()

    def __post_init__(self) -> None:
        """Refuse anything the layout cannot store, before a single byte is packed."""
        _require_range("snapshot_lsn", self.snapshot_lsn, _MAX_U64)
        for name, keys in (
            ("read_partitions", self.read_partitions),
            ("write_partitions", self.write_partitions),
        ):
            if not isinstance(keys, tuple):
                raise GrafxConfigurationError(
                    f"The {name} of a commit payload must be a tuple; got "
                    f"{type(keys).__name__}.",
                    field=name,
                    value=type(keys).__name__,
                )
            if len(keys) > MAX_PARTITIONS_PER_COMMIT:
                raise GrafxConfigurationError(
                    f"A commit payload carries at most {MAX_PARTITIONS_PER_COMMIT} entries in "
                    f"{name}; got {len(keys)}.",
                    field=name,
                    value=len(keys),
                )
            for key in keys:
                _require_range(name, key, _MAX_U64)
        if not isinstance(self.page_touches, tuple):
            raise GrafxConfigurationError(
                "The page touches of a commit payload must be a tuple; got "
                f"{type(self.page_touches).__name__}.",
                field="page_touches",
                value=type(self.page_touches).__name__,
            )
        if len(self.page_touches) > MAX_PAGE_TOUCHES_PER_COMMIT:
            raise GrafxConfigurationError(
                f"A commit payload carries at most {MAX_PAGE_TOUCHES_PER_COMMIT} page touches; "
                f"got {len(self.page_touches)}.",
                field="page_touches",
                value=len(self.page_touches),
            )
        for touch in self.page_touches:
            if not isinstance(touch, PageTouch):
                raise GrafxConfigurationError(
                    "Every page touch of a commit payload must be a PageTouch; got "
                    f"{type(touch).__name__}.",
                    field="page_touches",
                    value=type(touch).__name__,
                )

    @classmethod
    def build(
        cls,
        *,
        snapshot_lsn: Lsn,
        read_partitions: Iterable[int] = (),
        write_partitions: Iterable[int] = (),
        page_touches: Iterable[PageTouch] = (),
    ) -> CommitPayload:
        """Return a payload in canonical form: both partition sets sorted and free of repeats.

        The transaction manager accumulates sets, and a set has no order. Fixing one here means
        two runs of the same transaction produce the same bytes, which is what makes a log
        comparable between a crash run and its replay.
        """
        return cls(
            snapshot_lsn=snapshot_lsn,
            read_partitions=tuple(sorted(set(read_partitions))),
            write_partitions=tuple(sorted(set(write_partitions))),
            page_touches=tuple(
                sorted({touch for touch in page_touches}, key=lambda t: (t.file_id, t.page_index))
            ),
        )

    def encoded_length(self) -> int:
        """Return how many bytes this payload occupies inside a COMMIT record."""
        return (
            _HEAD.size
            + _KEY.size * (len(self.read_partitions) + len(self.write_partitions))
            + _COUNT.size
            + _TOUCH.size * len(self.page_touches)
        )

    def encode(self) -> bytes:
        """Return the payload bytes of a COMMIT record."""
        parts: list[bytes] = [
            _HEAD.pack(self.snapshot_lsn, len(self.read_partitions), len(self.write_partitions))
        ]
        for key in self.read_partitions:
            parts.append(_KEY.pack(key))
        for key in self.write_partitions:
            parts.append(_KEY.pack(key))
        parts.append(_COUNT.pack(len(self.page_touches)))
        for touch in self.page_touches:
            parts.append(_TOUCH.pack(touch.file_id, touch.page_index))
        return b"".join(parts)

    @classmethod
    def decode(cls, payload: bytes) -> CommitPayload:
        """Return the payload a COMMIT record carries, refusing bytes that do not describe one.

        These bytes came off a device, so a short or inconsistent payload is damage and gets
        ``corruption_detected``. The record checksum has usually already passed by the time this
        runs, which makes a failure here a genuine format disagreement rather than a bit flip --
        and either way it is not something a caller can fix by passing different arguments.
        """
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise GrafxConfigurationError(
                f"A commit payload is decoded from bytes, not from {type(payload).__name__}.",
                field="payload",
                value=type(payload).__name__,
            )
        view = memoryview(payload)
        size = len(view)
        if size < _HEAD.size:
            raise _refuse_payload(size, _HEAD.size, "the fixed head")
        snapshot_lsn, read_count, write_count = _HEAD.unpack_from(view, 0)
        offset = _HEAD.size
        needed = _KEY.size * (read_count + write_count) + _COUNT.size
        if size - offset < needed:
            raise _refuse_payload(size - offset, needed, "the partition sets")
        read_keys: list[int] = []
        for _ in range(read_count):
            read_keys.append(_KEY.unpack_from(view, offset)[0])
            offset += _KEY.size
        write_keys: list[int] = []
        for _ in range(write_count):
            write_keys.append(_KEY.unpack_from(view, offset)[0])
            offset += _KEY.size
        touch_count = _COUNT.unpack_from(view, offset)[0]
        offset += _COUNT.size
        wanted = _TOUCH.size * touch_count
        if size - offset < wanted:
            raise _refuse_payload(size - offset, wanted, "the page touches")
        touches: list[PageTouch] = []
        for _ in range(touch_count):
            file_id, page_index = _TOUCH.unpack_from(view, offset)
            touches.append(PageTouch(file_id=file_id, page_index=page_index))
            offset += _TOUCH.size
        if offset != size:
            raise GrafxCorruptionDetected(
                f"A commit payload of {size} bytes describes only {offset} of them.",
                reason="trailing_bytes",
                expected=offset,
                actual=size,
            )
        return cls(
            snapshot_lsn=snapshot_lsn,
            read_partitions=tuple(read_keys),
            write_partitions=tuple(write_keys),
            page_touches=tuple(touches),
        )


def _refuse_payload(available: int, wanted: int, what: str) -> GrafxCorruptionDetected:
    """Return the refusal for a commit payload that stops before it has described itself."""
    return GrafxCorruptionDetected(
        f"A commit payload needs {wanted} bytes for {what} and only {available} remain.",
        reason="short_commit_payload",
        expected=wanted,
        actual=available,
    )
