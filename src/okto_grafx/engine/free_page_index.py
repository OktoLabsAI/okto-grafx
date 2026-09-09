"""Immutable candidate directories; allocation never mutates discovery authority."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_PAGE, is_committed_csn
from okto_grafx.domain.page import Page, PageType
from okto_grafx.domain.page.layout import PAGE_HEADER_SIZE, SLOT_ENTRY_SIZE
from okto_grafx.domain.model.catalog import HEAP_FREE_INDEX_CAPABILITY as CAPABILITY

if TYPE_CHECKING:
    from okto_grafx.engine.heap_store import HeapStore

INITIALIZED = 1
_HEADER = struct.Struct("<8sQI")
_MAGIC = b"GRFXFPI1"
__all__ = ["CAPABILITY"]


def _bad() -> GrafxCorruptionDetected:
    """Return a typed refusal, never permission to repair or adopt a missing page."""
    return GrafxCorruptionDetected("Invalid free-page candidate directory.", field="free_page_index")


def initialized(heap: HeapStore) -> bool:
    """Read the capability-qualified initialization marker without mutation."""
    with heap._pinned_validated_header() as (page, header):
        if page.flags & ~INITIALIZED or (not page.flags and page.next_page != NO_PAGE):
            raise _bad()
        if page.flags and not heap._decode_reclaim_floor(header):
            raise _bad()
        return bool(page.flags & INITIALIZED)


def _decode(page: Page, floor: int, extent: int, directory: int, horizon: int) -> tuple[int, ...]:
    """Validate one directory page and its strictly increasing candidate IDs."""
    if (not 0 < floor <= page.page_lsn <= horizon
            or not is_committed_csn(page.page_lsn)
            or page.page_type != int(PageType.META) or page.flags or page.slot_count != 1
            or page.header().reserved or page.next_page == directory
            or (page.next_page != NO_PAGE and not 0 < page.next_page < extent)):
        raise _bad()
    try:
        raw = page.read_slot(0)
        magic, recorded_floor, count = _HEADER.unpack_from(raw)
        if magic != _MAGIC or recorded_floor != floor or len(raw) != _HEADER.size + 4 * count:
            raise _bad()
        values = struct.unpack_from("<" + "I" * count, raw, _HEADER.size)
        if any(not 0 < number < extent or number == directory for number in values):
            raise _bad()
        if any(a >= b for a, b in zip(values, values[1:])):
            raise _bad()
        return values
    except (struct.error, ValueError) as failure:
        raise _bad() from failure


def _usable(page: Page, horizon: int) -> bool:
    """Revalidate physical truth; a consumed candidate can legitimately be overflow."""
    if page.page_lsn > horizon:
        raise _bad()
    if page.page_type == int(PageType.OVERFLOW):
        return False
    if (page.page_type != int(PageType.FREE) or page.slot_count or page.flags
            or page.header().reserved or page.next_page != NO_PAGE
            or not is_committed_csn(page.page_lsn) or page.page_lsn > horizon):
        raise _bad()
    return True


def pop_candidates(heap: HeapStore, count: int, horizon: int) -> tuple[int, ...] | None:
    """Consume local hints only, with no root mutation before a proved COMMIT.

    The fixed directory changes only during quiescent vacuum. Candidates may have
    been used by another writer; validate each current page and skip OVERFLOW.
    A failed/crashed attempt cannot lose any directory membership.
    """
    if not initialized(heap):
        return None
    pool, file = heap._pool, heap._file
    extent = pool.storage.page_count(file)
    floor = heap.reclaim_floor()
    with heap._pinned_validated_header() as (page, _):
        head = page.next_page
    cursor = heap._free_index_cursor
    directory, offset, traversed = (head, 0, 0) if cursor is None or cursor[0] != floor else cursor[1:]
    selected = []
    seen = set()
    while directory != NO_PAGE and len(selected) < count:
        if not 0 < directory < extent or directory in seen or traversed >= extent:
            raise _bad()
        seen.add(directory)
        with pool.pinned(file, directory) as page:
            values = _decode(page, floor, extent, directory, horizon)
            following = page.next_page
        while offset < len(values) and len(selected) < count:
            candidate = values[offset]
            offset += 1
            with pool.pinned(file, candidate) as page:
                if _usable(page, horizon):
                    selected.append(candidate)
        if offset == len(values):
            directory, offset, traversed = following, 0, traversed + 1
        heap._free_index_cursor = (floor, directory, offset, traversed)
    if len(selected) != len(set(selected)):
        raise _bad()
    return tuple(selected)


def _directory_inventory(heap: HeapStore, floor: int, horizon: int) -> tuple[set[int], set[int]]:
    """Read directory membership independently from the device for maintenance."""
    pool, file = heap._pool, heap._file
    extent = pool.storage.page_count(file)
    root = pool.read_fresh_page(file, 0)
    heap._require_header_page(root)
    if root.flags & ~INITIALIZED or (not root.flags and root.next_page != NO_PAGE):
        raise _bad()
    pages, candidates = set(), set()
    current = root.next_page
    while current != NO_PAGE:
        if not 0 < current < extent or current in pages:
            raise _bad()
        pages.add(current)
        page = pool.read_fresh_page(file, current)
        values = _decode(page, floor, extent, current, horizon)
        if candidates.intersection(values):
            raise _bad()
        candidates.update(values)
        current = page.next_page
    if pages.intersection(candidates):
        raise _bad()
    return pages, candidates


def census(heap: HeapStore, horizon: int) -> set[int]:
    """Validate immutable candidates against current pages and retained FREE coverage."""
    pool, file = heap._pool, heap._file
    directories, candidates = _directory_inventory(heap, heap.reclaim_floor(), horizon)
    indexed = initialized(heap)
    free = set()
    for number in range(1, pool.storage.page_count(file)):
        page = pool.read_fresh_page(file, number)
        if number in candidates:
            _usable(page, horizon)
        if page.page_type == int(PageType.FREE) and page.page_lsn:
            _usable(page, horizon)
            if indexed and number not in candidates:
                raise _bad()
            free.add(number)
        if indexed and page.page_type == int(PageType.META) and number not in directories:
            raise _bad()
    # Directory pages belong to allocator metadata, not to any heap version.
    # They can be reused when the next immutable directory is atomically rebuilt.
    for number in directories:
        free.add(number)
    return free


def plan(heap: HeapStore, images: tuple[tuple[int, bytes], ...], horizon: int) -> tuple[dict[int, bytes], int]:
    """Build a detached immutable directory from existing and newly retired space."""
    pool = heap._pool
    free = census(heap, horizon)
    merged = dict(images)
    for number, raw in images:
        page = pool.codec.decode_page(raw)
        if page.page_type == int(PageType.FREE):
            free.add(number)
    ordered = sorted(free)
    capacity = (pool.page_size - PAGE_HEADER_SIZE - SLOT_ENTRY_SIZE - _HEADER.size) // 4
    directory_count = (len(ordered) + capacity) // (capacity + 1)
    directories, candidates = ordered[:directory_count], ordered[directory_count:]
    for number in candidates:
        merged[number] = pool.codec.encode_page(
            Page(int(PageType.FREE), page_size=pool.page_size, page_index=number))
    for offset, number in enumerate(directories):
        values = candidates[offset * capacity:(offset + 1) * capacity]
        page = Page(int(PageType.META), page_size=pool.page_size, page_index=number)
        page.next_page = directories[offset + 1] if offset + 1 < len(directories) else NO_PAGE
        page.insert_slot(_HEADER.pack(_MAGIC, horizon, len(values)) + struct.pack("<" + "I" * len(values), *values))
        merged[number] = pool.codec.encode_page(page)
    return merged, directories[0] if directories else NO_PAGE
