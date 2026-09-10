"""Exact page-local dictionary postings; native WAL, generations and heap visibility."""

from __future__ import annotations

import struct

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.ids import NO_CSN, NO_PAGE, PROVISIONAL_CSN, RecordRef
from okto_grafx.domain.index.definition import COLUMN_KEY_DERIVATION
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.keys import bucket_of
from okto_grafx.domain.index.records import IndexOperation
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.page import PAGE_HEADER_SIZE, SLOT_ENTRY_SIZE
from okto_grafx.engine.index_manager import HashIndex, IndexStore

_POSTING = struct.Struct("<cHQQ")


class PostingHashIndex(HashIndex):
    """Opt-in property hash; keys are stored once per page, references retain slots."""

    def __init__(self, definition, pool, metrics):
        """Admit only the catalog-declared exact posting layout."""
        if (definition.layout is not IndexLayout.POSTING_HASH
                or definition.visibility is not IndexVisibility.EXACT
                or definition.key_derivation != COLUMN_KEY_DERIVATION):
            raise GrafxIndexError("Posting hash requires an exact property index.", field="layout")
        IndexStore.__init__(self, definition, pool, metrics)

    def _decode_page(self, page):
        """Validate a complete bounded dictionary and all postings before using any."""
        keys = {}
        unique = set()
        postings = []
        for slot, raw in page.iter_slot_views():
            tag = bytes(raw[:1])
            if tag == b"K":
                key = bytes(raw[1:])
                if len(key) > self.max_key_bytes or key in unique:
                    raise GrafxCorruptionDetected("Invalid posting dictionary key.", field="posting_dictionary", file=self.file, page=page.page_index)
                keys[slot] = key
                unique.add(key)
            elif tag == b"P" and len(raw) == _POSTING.size:
                _, dictionary, encoded, dead = _POSTING.unpack(raw)
                if dead == PROVISIONAL_CSN:
                    raise GrafxCorruptionDetected("Provisional posting tombstone.", field="dead_csn")
                reference = RecordRef.decode(encoded)
                if reference.page in (0, NO_PAGE):
                    raise GrafxCorruptionDetected("Posting does not name a heap data page.", field="ref")
                postings.append((slot, dictionary, reference, dead))
            else:
                raise GrafxCorruptionDetected("Invalid posting slot framing.", field="posting_slot", file=self.file, page=page.page_index, slot=slot)
        if any(dictionary not in keys for _, dictionary, _, _ in postings):
            raise GrafxCorruptionDetected("Posting references an absent dictionary slot.", field="posting_dictionary", file=self.file, page=page.page_index)
        return keys, postings

    def _entry_images(self, page):
        """Normalize postings for inherited maintenance; physical slot IDs are unchanged."""
        keys, postings = self._decode_page(page)
        for slot, dictionary, ref, dead in postings:
            yield slot, IndexEntry(keys[dictionary], ref, False, dead_csn=dead).encode()

    def _posting_matches(self, page, key, ref):
        """Materialize only matching candidates, after validating the complete page."""
        keys, postings = self._decode_page(page)
        for slot, dictionary, stored_ref, dead in postings:
            if keys[dictionary] == key and (ref is None or ref == stored_ref):
                yield IndexEntry(key, stored_ref, False, dead_csn=dead,
                                 page=page.page_index, slot=slot)

    @property
    def max_key_bytes(self):
        """Reserve a dictionary and one posting on an otherwise empty page."""
        return self._pool.page_size - PAGE_HEADER_SIZE - 2 * SLOT_ENTRY_SIZE - 1 - _POSTING.size

    def _require_ref(self, ref):
        """Refuse impossible heap targets before their INDEX_WRITE can become durable."""
        reference = super()._require_ref(ref)
        reference.encode()
        if reference.page in (0, NO_PAGE):
            raise GrafxIndexError("Posting requires a heap data-page reference.", field="ref")
        return reference

    def _insert_on(self, page, entry, lsn):
        """Check combined capacity before creating either dictionary or posting slots."""
        keys, _ = self._decode_page(page)
        dictionary = next((slot for slot, key in keys.items() if key == entry.key), None)
        needed = _POSTING.size + SLOT_ENTRY_SIZE
        if dictionary is None:
            needed += 1 + len(entry.key) + SLOT_ENTRY_SIZE
        if page.compactable_space() < needed:
            return False
        if dictionary is None:
            dictionary = page.insert_slot(b"K" + entry.key)
        page.insert_slot(_POSTING.pack(b"P", dictionary, entry.ref.encode(), entry.dead_csn))
        self._stamp(page, lsn)
        return True

    def _place(self, pages, entry, lsn):
        """First-fit placement under the inherited durable-COMMIT publication fence."""
        self._require_ref(entry.ref)
        if entry.versioned or entry.born_csn != NO_CSN:
            raise GrafxIndexError("Exact postings cannot carry birth stamps.", field="versioned")
        for number in pages:
            with self._pool.pinned(self.file, number) as page:
                self._require_index_page(page, number)
                if self._insert_on(page, entry, lsn):
                    return True
        if not pages:
            raise GrafxCorruptionDetected("Posting bucket has no head.", field="bucket")
        self._require_key(entry.key)
        page = self._pool.allocate(self.file, self.page_type)
        number = page.page_index
        try:
            if not self._insert_on(page, entry, lsn):
                raise GrafxIndexError("Posting cannot fit an empty page.", field="key")
        finally:
            self._pool.unpin(self.file, number, dirty=True)
        with self._pool.pinned(self.file, pages[-1]) as tail:
            self._require_index_page(tail, pages[-1])
            tail.next_page = number
            self._stamp(tail, lsn)
        return True

    def _rewrite(self, page_index, slot, entry, lsn):
        """Tombstone only the matching reference, retaining its dictionary and slot."""
        with self._pool.pinned(self.file, page_index) as page:
            self._require_index_page(page, page_index)
            keys, postings = self._decode_page(page)
            found = next((p for p in postings if p[0] == slot), None)
            if found is None or keys[found[1]] != entry.key or found[2] != entry.ref:
                raise GrafxCorruptionDetected("Posting changed inside commit fence.", field="posting_slot")
            page.update_slot(slot, _POSTING.pack(b"P", found[1], entry.ref.encode(), entry.dead_csn))
            self._stamp(page, lsn)
        return True

    def _erase(self, page_index, slot, lsn):
        """Reclaim one posting; dictionary slots remain until no postings survive."""
        with self._pool.pinned(self.file, page_index) as page:
            self._require_index_page(page, page_index)
            _, postings = self._decode_page(page)
            if not any(p[0] == slot for p in postings):
                raise GrafxCorruptionDetected("Reclamation did not name a posting.", field="posting_slot")
            page.free_slot(slot)
            if len(postings) == 1:
                page.clear()
            self._stamp(page, lsn)
        return True

    def _apply_empty_build_change(self, build, change, lsn):
        """Do not run scalar-slot physical shortcuts against dictionary postings."""
        return None

    def _prepare_live_hot_buckets(self, staged):
        """Bounded same-key INSERT batch: prove membership once inside the native fence."""
        if (self._stale_reason is not None or self._rebuild_authority is not None
                or self._short_commit is not None or staged.defer_clear
                or not 2 <= len(staged.changes) <= 4096):
            return {}
        key = staged.changes[0].key
        if any(c.operation is not IndexOperation.INSERT or c.key != key
               for c in staged.changes):
            return {}
        self._require_key(key)
        bucket = bucket_of(key, self.definition.bucket_count)
        # Refuse the accelerator, not the operation, before retaining an unbounded chain.
        pages = []
        def visit():
            """Decline optional batch preparation before its page-directory bound."""
            if len(pages) >= 512:
                raise _DeclinePostingBatch()
            pages.append(None)
        try:
            chain, _ = self._scan_bucket(bucket, visit=visit)
        except _DeclinePostingBatch:
            return {}
        targets = {c.ref for c in staged.changes}
        present = set()
        for number in chain:
            for entry in self._entries_on(number):
                if entry.key == key and entry.ref in targets:
                    if entry.ref in present:
                        return {}  # retain scalar behavior for duplicate physical candidates
                    present.add(entry.ref)
        return {bucket: {"pages": list(chain), "cursor": 0, "present": present}}

    def _apply_common_replay_hot_change(self, hot, change, lsn):
        """Apply one admitted INSERT without repeating membership or full-page probes."""
        self._require_ref(change.ref)
        if change.ref in hot["present"]:
            return False
        entry = IndexEntry(change.key, change.ref, False)
        pages = hot["pages"]
        while hot["cursor"] < len(pages):
            number = pages[hot["cursor"]]
            with self._pool.pinned(self.file, number) as page:
                self._require_index_page(page, number)
                if self._insert_on(page, entry, lsn):
                    hot["present"].add(change.ref)
                    return True
            hot["cursor"] += 1
        # The inherited scalar linker remains the only new-page publication protocol.
        self._place((pages[-1],), entry, lsn)
        with self._pool.pinned(self.file, pages[-1]) as tail:
            pages.append(tail.next_page)
        hot["present"].add(change.ref)
        return True

    def assisted_rehash_pressure(self):
        """Count logical postings, excluding dictionary slots, for advisory sizing."""
        self.open()
        entries = sum(len(self._entries_on(self._bucket_head(bucket)))
                      for bucket in range(self.definition.bucket_count))
        return entries, max(0, self._pool.storage.page_count(self.file) - self._minimum_pages())


class _DeclinePostingBatch(Exception):
    """Private bounded-work exit; never a corruption or public-operation failure."""
