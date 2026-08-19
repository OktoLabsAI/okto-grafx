"""Splitting a payload that is larger than a page across a chain of overflow pages.

A slot cannot be larger than the page that holds it, so a record whose encoded tuple exceeds
what one page can carry stores its payload in a chain of pages of type OVERFLOW instead. Each
link holds one slot with one chunk and points at the next link through the next_page header
field; the last link points at NO_PAGE.

The split and the join are pure functions of the payload and the chunk capacity, which is what
lets a test prove the round trip without a device: the engine side only has to allocate the
pages and follow the pointers.
"""

from __future__ import annotations

from collections.abc import Iterable

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.page.layout import (
    PAGE_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    validate_page_size,
)

__all__ = ["chunk_capacity", "split_payload", "join_chunks"]


def chunk_capacity(page_size: int) -> int:
    """Return how many payload bytes one overflow page can carry.

    An overflow page holds exactly one slot, so the cost is the page header plus one directory
    entry and nothing else.
    """
    return validate_page_size(page_size) - PAGE_HEADER_SIZE - SLOT_ENTRY_SIZE


def split_payload(payload: bytes, capacity: int) -> tuple[bytes, ...]:
    """Cut the payload into chunks of at most capacity bytes, in order.

    An empty payload still produces one empty chunk, so a chain always has a first page and
    "the chain is missing" never has to be encoded as a special case.
    """
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise GrafxCorruptionDetected(
            f"An overflow chunk capacity must be a positive integer; got {capacity!r}.",
            field="capacity",
            value=repr(capacity),
        )
    content = bytes(payload)
    if not content:
        return (b"",)
    return tuple(
        content[start : start + capacity] for start in range(0, len(content), capacity)
    )


def join_chunks(chunks: Iterable[bytes]) -> bytes:
    """Join the chunks of an overflow chain back into the payload they came from."""
    return b"".join(bytes(chunk) for chunk in chunks)
