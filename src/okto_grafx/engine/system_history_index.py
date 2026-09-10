"""Immutable authenticated temporal access tree; publication belongs to native history.

Nodes and value chunks are copy-on-write pages in system-history.dat. A root is
usable only when supplied by a qualified history head. Neither this codec nor a
page checksum grants COMMIT authority. Keys are (table, lineage, commit), not heap
locations, and entries survive ordinary MVCC reclamation.
"""

from __future__ import annotations

import hashlib
import struct
from bisect import bisect_left
from collections.abc import Callable, Iterator

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.page import Page, PageType

__all__ = ["HistoryAccessTree"]

_KEY = struct.Struct(">IQQ")
_REF = struct.Struct("<I32s")
_PREFIX = struct.Struct("<8s16sBQ")
_MAGIC = b"GXHYIX01"
_COUNT = struct.Struct("<H")
_EMPTY = (0, bytes(32))
_Key = tuple[int, int, int]
_Ref = tuple[int, bytes]
_Entry = tuple[_Key, _Ref]


def _bad(field):
    return GrafxCorruptionDetected("Invalid authenticated temporal access path.",
                                  component="system_history_index", field=field)


class HistoryAccessTree:
    """Bounded-fanout immutable B+tree with authenticated child/value references.

    A builder keeps only newly constructed pages and the paths it reads; binders
    may replay those captured inputs with a different final COMMIT sequence.
    Readers verify every traversed edge, including value chunks, before yielding.
    """

    def __init__(self, read: Callable[[str, int], bytes], *, database_uuid: bytes,
                 page_size: int, sequence: int, root: tuple[int, bytes] = _EMPTY,
                 first_page: int = 1, charge: Callable[[int], None] | None = None,
                 read_extent: int | None = None) -> None:
        self.read = read
        self.database_uuid = database_uuid
        self.page_size = page_size
        self.sequence = sequence
        self.root = root
        self.next_page = first_page
        self.images: dict[int, bytes] = {}
        self.charge = charge
        self.read_extent = read_extent
        self.capacity = page_size - 36 - _PREFIX.size
        self.fanout = (self.capacity - _COUNT.size) // (_KEY.size + _REF.size)
        if self.fanout < 3:
            raise _bad("page_size")

    def _put(self, kind, body):
        number = self.next_page
        if not 0 < number < NO_PAGE:
            raise _bad("extent")
        self.next_page += 1
        payload = _PREFIX.pack(_MAGIC, self.database_uuid, kind, self.sequence) + body
        page = Page(PageType.META, page_size=self.page_size, page_index=number,
                    page_lsn=self.sequence)
        page.insert_slot(payload)
        self.images[number] = page.to_bytes()
        return number, hashlib.sha256(payload).digest()

    def _get(self, ref):
        number, digest = ref
        if type(number) is not int or not 0 < number < NO_PAGE or len(digest) != 32:
            raise _bad("reference")
        raw = self.images.get(number)
        if raw is None:
            if self.read_extent is not None and number >= self.read_extent:
                raise _bad("reference_extent")
            raw = self.read("system-history.dat", number)
        if self.charge is not None:
            self.charge(len(raw))
        page = Page.from_bytes(raw, page_index=number)
        if (len(raw) != self.page_size or page.page_type != PageType.META or page.flags
                or page.header().reserved or page.next_page != NO_PAGE or page.slot_count != 1
                or page.is_slot_free(0) or page.seq % 2):
            raise _bad("page")
        payload = page.read_slot(0)
        if len(payload) < _PREFIX.size or hashlib.sha256(payload).digest() != digest:
            raise _bad("digest")
        magic, identity, kind, sequence = _PREFIX.unpack_from(payload)
        if (magic != _MAGIC or identity != self.database_uuid or kind not in (0, 1, 2)
                or not 0 < sequence <= page.page_lsn <= self.sequence):
            raise _bad("identity")
        return kind, payload[_PREFIX.size:]

    def _entries(self, ref):
        kind, body = self._get(ref)
        if kind == 2 or len(body) < 2:
            raise _bad("node")
        count = _COUNT.unpack_from(body)[0]
        width = _KEY.size + _REF.size
        if not 1 <= count <= self.fanout or len(body) != 2 + count * width:
            raise _bad("node_count")
        entries = []
        for offset in range(2, len(body), width):
            key = _KEY.unpack_from(body, offset)
            ref = _REF.unpack_from(body, offset + _KEY.size)
            if entries and key <= entries[-1][0]:
                raise _bad("key_order")
            entries.append((key, ref))
        return kind, entries

    def _node(self, kind, entries):
        body = _COUNT.pack(len(entries)) + b"".join(
            _KEY.pack(*key) + _REF.pack(*ref) for key, ref in entries)
        return entries[-1][0], self._put(kind, body)

    def insert(self, key: tuple[int, int, int], value: bytes) -> None:
        """Append one immutable event, copying only its search path; duplicates fail closed."""
        _KEY.pack(*key)
        if not value:
            raise _bad("empty_value")
        ref = _EMPTY
        width = self.capacity - _REF.size
        for offset in reversed(range(0, len(value), width)):
            ref = self._put(2, _REF.pack(*ref) + value[offset:offset + width])

        def add(node: _Ref, depth: int) -> list[_Entry]:
            """Copy one insertion path and return its replacement subtree boundaries."""
            if depth > 64:
                raise _bad("depth")
            kind, entries = self._entries(node)
            slot = bisect_left([entry[0] for entry in entries], key)
            if kind == 0:
                if slot < len(entries) and entries[slot][0] == key:
                    raise _bad("duplicate_key")
                entries.insert(slot, (key, ref))
            else:
                slot = min(slot, len(entries) - 1)
                entries[slot:slot + 1] = add(entries[slot][1], depth + 1)
            if len(entries) <= self.fanout:
                return [self._node(kind, entries)]
            middle = len(entries) // 2
            return [self._node(kind, entries[:middle]), self._node(kind, entries[middle:])]

        if self.root == _EMPTY:
            self.root = self._node(0, [(key, ref)])[1]
        else:
            roots = add(self.root, 0)
            self.root = roots[0][1] if len(roots) == 1 else self._node(1, roots)[1]

    def value(self, ref: tuple[int, bytes], *, max_bytes: int = 2 * 1024 * 1024) -> bytes:
        """Read an authenticated, bounded value chain; reject cycles and oversize values."""
        pieces = []
        size = 0
        seen = set()
        while ref != _EMPTY:
            if ref[0] in seen:
                raise _bad("value_cycle")
            seen.add(ref[0])
            kind, body = self._get(ref)
            if kind != 2 or len(body) <= _REF.size:
                raise _bad("value_chunk")
            ref = _REF.unpack_from(body)
            chunk = body[_REF.size:]
            size += len(chunk)
            if size > max_bytes:
                raise _bad("value_limit")
            pieces.append(chunk)
        return b"".join(pieces)

    def bulk(self, items: tuple[tuple[tuple[int, int, int], bytes], ...]) -> None:
        """Build a fresh ordered baseline without retaining intermediate copied paths."""
        if self.root != _EMPTY or self.images:
            raise _bad("bulk_nonempty")
        entries = []
        width = self.capacity - _REF.size
        for key, value in sorted(items):
            if entries and key <= entries[-1][0] or not value:
                raise _bad("bulk_key")
            ref = _EMPTY
            for offset in reversed(range(0, len(value), width)):
                ref = self._put(2, _REF.pack(*ref) + value[offset:offset + width])
            entries.append((key, ref))
        kind = 0
        while entries:
            parents = [self._node(kind, entries[offset:offset + self.fanout])
                       for offset in range(0, len(entries), self.fanout)]
            if len(parents) == 1:
                self.root = parents[0][1]
                return
            entries, kind = parents, 1

    def entries(self, lower: _Key, upper: _Key) -> Iterator[_Entry]:
        """Yield an inclusive ordered range, verifying visited subtree boundaries."""
        def walk(ref: _Ref, minimum: _Key | None, maximum: _Key, depth: int) -> Iterator[_Entry]:
            """Traverse only overlapping subtrees, enforcing their inherited bounds."""
            if depth > 64:
                raise _bad("depth")
            kind, entries = self._entries(ref)
            if entries[-1][0] != maximum or minimum is not None and entries[0][0] <= minimum:
                raise _bad("subtree_bounds")
            previous = minimum
            for key, child in entries:
                if key >= lower and (previous is None or previous < upper):
                    if kind == 0:
                        if key <= upper:
                            yield key, child
                    else:
                        yield from walk(child, previous, key, depth + 1)
                previous = key
        if self.root == _EMPTY:
            return
        _, root = self._entries(self.root)
        yield from walk(self.root, None, root[-1][0], 0)

    def floor(self, key: _Key) -> _Entry | None:
        """Find the greatest key <= a coordinate in logarithmic tree height."""
        def descend(ref: _Ref, ceiling: _Key, depth: int) -> _Entry | None:
            """Locate a predecessor, visiting a prior sibling only when necessary."""
            if depth > 64:
                raise _bad("depth")
            kind, entries = self._entries(ref)
            slot = bisect_left([entry[0] for entry in entries], ceiling)
            if kind == 0:
                if slot == len(entries) or entries[slot][0] > ceiling:
                    slot -= 1
                return None if slot < 0 else entries[slot]
            slot = min(slot, len(entries) - 1)
            found = descend(entries[slot][1], ceiling, depth + 1)
            return found if found is not None or slot == 0 else descend(entries[slot - 1][1], ceiling, depth + 1)
        return None if self.root == _EMPTY else descend(self.root, key, 0)

    def ceiling(self, key: _Key) -> _Entry | None:
        """Find the least key >= a coordinate without walking earlier versions."""
        def descend(ref: _Ref, depth: int) -> _Entry | None:
            """Follow the first subtree whose maximum can satisfy the lower bound."""
            if depth > 64:
                raise _bad("depth")
            kind, entries = self._entries(ref)
            slot = bisect_left([entry[0] for entry in entries], key)
            if slot == len(entries):
                return None
            return entries[slot] if kind == 0 else descend(entries[slot][1], depth + 1)
        return None if self.root == _EMPTY else descend(self.root, 0)
