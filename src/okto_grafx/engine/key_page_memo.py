"""Bounded repeated-key decoding, never a replacement for page/view authority."""

from __future__ import annotations

from collections import OrderedDict

from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.page import Page

__all__: list[str] = []


class KeyPageMemo:
    """Retain one requested key per page, requiring exact current slot bytes on reuse."""

    def __init__(self) -> None:
        """Start an empty logical-memory-accounted LRU."""
        self.entries = OrderedDict()
        self.bytes = 0

    def matches(self, page: Page, key: bytes, ref: RecordRef | None) -> tuple[IndexEntry, ...]:
        """Reuse only byte-identical images with the same decoder and request."""
        images = tuple((slot, bytes(raw)) for slot, raw in page.iter_slot_views())
        hook = IndexEntry.decode_if_matches
        decoder = getattr(hook, "__func__", hook)
        previous = self.entries.pop(page.page_index, None)
        if previous is not None:
            self.bytes -= previous[-1]
            if previous[:4] == (images, key, ref, decoder):
                self.entries[page.page_index] = previous
                self.bytes += previous[-1]
                return previous[4]
        found = []
        for slot, image in images:
            entry = IndexEntry.decode_if_matches(image, key, ref)
            if entry is not None:
                found.append(entry.located_at(page.page_index, slot))
        answer = tuple(found)
        cost = 512 + len(key) + sum(128 + len(raw) for _, raw in images)
        cost += sum(256 + len(entry.key) for entry in answer)
        if cost <= 1024 * 1024:
            while self.entries and (len(self.entries) >= 64 or self.bytes + cost > 1024 * 1024):
                _, removed = self.entries.popitem(last=False)
                self.bytes -= removed[-1]
            self.entries[page.page_index] = (images, key, ref, decoder, answer, cost)
            self.bytes += cost
        return answer
