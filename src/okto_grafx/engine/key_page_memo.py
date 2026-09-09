"""Bounded repeated-key decoding, never a replacement for page/view authority."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.page import Page

__all__ = ["KeyPageCacheUsage"]


@dataclass(frozen=True, slots=True)
class KeyPageCacheUsage:
    """Handle-local decoded-page retention and counters, not freshness or RSS."""

    max_pages: int
    max_bytes: int
    pages: int
    logical_bytes: int
    hits: int
    misses: int
    evictions: int
    admission_refusals: int


class KeyPageMemo:
    """Retain one requested key per page, requiring exact current slot bytes on reuse."""

    def __init__(self, max_pages: int = 64, max_bytes: int = 1024 * 1024) -> None:
        """Start an empty logical-memory-accounted LRU."""
        self.entries = OrderedDict()
        self.bytes = 0
        from okto_grafx.domain.errors import GrafxConfigurationError
        for name, value, ceiling in (("max_pages", max_pages, 65536), ("max_bytes", max_bytes, 2**31)):
            if type(value) is not int or not 0 <= value <= ceiling:
                raise GrafxConfigurationError("Invalid decoded-page cache limit.", field=name)
        self.max_pages, self.max_bytes = max_pages, max_bytes
        self.hits = self.misses = self.evictions = self.admission_refusals = 0

    def usage(self) -> KeyPageCacheUsage:
        """Copy immutable observations without reading storage."""
        return KeyPageCacheUsage(self.max_pages, self.max_bytes, len(self.entries), self.bytes,
                                 self.hits, self.misses, self.evictions, self.admission_refusals)

    def matches(self, page: Page, key: bytes, ref: RecordRef | None) -> tuple[IndexEntry, ...]:
        """Reuse only byte-identical images with the same decoder and request."""
        images = tuple((slot, bytes(raw)) for slot, raw in page.iter_slot_views())
        hook = IndexEntry.decode_if_matches
        decoder = getattr(hook, "__func__", hook)
        previous = self.entries.pop(page.page_index, None)
        if previous is not None:
            self.bytes -= previous[-1]
            if previous[:4] == (images, key, ref, decoder):
                self.hits += 1
                self.entries[page.page_index] = previous
                self.bytes += previous[-1]
                return previous[4]
        self.misses += 1
        found = []
        for slot, image in images:
            entry = IndexEntry.decode_if_matches(image, key, ref)
            if entry is not None:
                found.append(entry.located_at(page.page_index, slot))
        answer = tuple(found)
        cost = 512 + len(key) + sum(128 + len(raw) for _, raw in images)
        cost += sum(256 + len(entry.key) for entry in answer)
        if self.max_pages and cost <= self.max_bytes:
            while self.entries and (len(self.entries) >= self.max_pages or self.bytes + cost > self.max_bytes):
                _, removed = self.entries.popitem(last=False)
                self.bytes -= removed[-1]
                self.evictions += 1
            self.entries[page.page_index] = (images, key, ref, decoder, answer, cost)
            self.bytes += cost
        else:
            self.admission_refusals += 1
        return answer
