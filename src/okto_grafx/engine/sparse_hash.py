"""Opt-in exact hash indexes with compact directories and lazy bucket heads."""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterator
from itertools import islice

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError, GrafxError
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.index.definition import COLUMN_KEY_DERIVATION, IndexDefinition
from okto_grafx.domain.index.keys import bucket_of
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.records import IndexOperation
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.page import PageType
from okto_grafx.domain.page.layout import PAGE_HEADER_SIZE, SLOT_ENTRY_SIZE
from okto_grafx.engine.index_manager import HashIndex, IndexStore
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.domain.ports.metrics import MetricsSink

_HEADER = struct.Struct("<8sII")
_MAGIC = b"GRFXSHD1"
__all__: list[str] = []


class SparseHashIndex(HashIndex):
    """Native exact equality semantics with lazy, explicitly persisted placement."""

    def __init__(self, definition: IndexDefinition, pool: BufferPool, metrics: MetricsSink) -> None:
        """Refuse any definition outside explicit exact sparse property indexes."""
        if (definition.layout is not IndexLayout.SPARSE_HASH
                or definition.visibility is not IndexVisibility.EXACT
                or definition.key_derivation != COLUMN_KEY_DERIVATION):
            raise GrafxIndexError("Sparse hash requires an exact property index.", field="layout")
        IndexStore.__init__(self, definition, pool, metrics)
        self._directory_memo = None

    def _directory_capacity(self) -> int:
        """Return the fixed number of u32 pointers one directory page can hold."""
        return (self._pool.page_size - PAGE_HEADER_SIZE - SLOT_ENTRY_SIZE - _HEADER.size) // 4

    def _minimum_pages(self) -> int:
        """Require only the header and compact pointer pages, not empty buckets."""
        capacity = self._directory_capacity()
        return 1 + (self.definition.bucket_count + capacity - 1) // capacity

    def _decode_directory(self, page, ordinal: int) -> tuple[int, ...]:
        """Validate layout, exact slice identity, pointer extent and reserved fields."""
        try:
            if (page.page_type != int(PageType.META) or page.flags or page.header().reserved
                    or page.next_page != NO_PAGE or page.slot_count != 1):
                raise ValueError("directory shape")
            raw = page.read_slot(0)
            memo = self._directory_memo
            if memo is not None and memo[0] == ordinal and memo[1] == raw:
                return memo[2]
            magic, first, count = _HEADER.unpack_from(raw)
            expected_first = ordinal * self._directory_capacity()
            expected_count = min(self._directory_capacity(), self.definition.bucket_count - expected_first)
            if (magic != _MAGIC or first != expected_first or count != expected_count
                    or len(raw) != _HEADER.size + 4 * count):
                raise ValueError("directory identity")
            values = struct.unpack_from("<" + "I" * count, raw, _HEADER.size)
            minimum = self._minimum_pages()
            if any(value != NO_PAGE and value < minimum for value in values):
                raise ValueError("directory pointer")
            present = [value for value in values if value != NO_PAGE]
            if len(present) != len(set(present)):
                raise ValueError("duplicate head")
            self._directory_memo = (ordinal, raw, values)
            return values
        except (struct.error, ValueError) as failure:
            raise GrafxCorruptionDetected("Invalid sparse hash directory.", file=self.file,
                                         field="sparse_hash_directory") from failure

    def _grow_buckets(self) -> None:
        """Initialize only the compact directory while a generation is private."""
        present = self._pool.storage.page_count(self.file)
        wanted = self._minimum_pages()
        if present < wanted:
            self._pool.allocate_run(self.file, int(PageType.META), wanted - present)
        for ordinal in range(wanted - 1):
            with self._pool.pinned(self.file, ordinal + 1) as page:
                if page.is_pristine() or (
                    page.page_type == int(PageType.META) and not page.slot_count
                    and not page.page_lsn and not page.flags and page.next_page == NO_PAGE
                ):
                    first = ordinal * self._directory_capacity()
                    count = min(self._directory_capacity(), self.definition.bucket_count - first)
                    page.page_type = int(PageType.META)
                    page.insert_slot(_HEADER.pack(_MAGIC, first, count) + struct.pack("<" + "I" * count, *([NO_PAGE] * count)))
                self._decode_directory(page, ordinal)

    def _bucket_head(self, bucket: int) -> int:
        """Resolve one head only through its native checksummed pointer directory."""
        ordinal, position = divmod(bucket, self._directory_capacity())
        with self._pool.pinned(self.file, ordinal + 1) as page:
            head = self._decode_directory(page, ordinal)[position]
        if head != NO_PAGE and head >= self._pool.storage.page_count(self.file):
            raise GrafxCorruptionDetected("Sparse head is outside the file.", field="sparse_hash_directory")
        return head

    def _ensure_bucket(self, bucket: int, lsn: int) -> None:
        """Barrier an unreachable empty head before publishing its directory pointer."""
        self._ensure_buckets(iter((bucket,)), lsn)

    def _ensure_buckets(self, buckets: Iterator[int], lsn: int) -> None:
        """Durably initialize <=64 unreachable heads before any pointer publication."""
        while chunk := tuple(islice(buckets, 64)):
            pending = {}
            for bucket in chunk:
                if bucket in pending or self._bucket_head(bucket) != NO_PAGE:
                    continue
                page = self._pool.allocate(self.file, self.page_type, reuse=False)
                number = page.page_index
                self._stamp(page, lsn)
                self._pool.unpin(self.file, number, dirty=True)
                pending[bucket] = number
            if not pending:
                continue
            self._pool.flush(self.file)
            self._pool.storage.durable_barrier(self.file)
            for bucket, number in pending.items():
                ordinal, position = divmod(bucket, self._directory_capacity())
                with self._pool.pinned(self.file, ordinal + 1) as directory:
                    values = list(self._decode_directory(directory, ordinal))
                    if values[position] != NO_PAGE:
                        raise GrafxCorruptionDetected("Sparse bucket moved inside publication fence.", field="sparse_hash_directory")
                    values[position] = number
                    directory.update_slot(0, _HEADER.pack(_MAGIC, ordinal * self._directory_capacity(), len(values))
                                          + struct.pack("<" + "I" * len(values), *values))
                    self._stamp(directory, lsn)

    def _commit_staged(self, txn_id, staged, stamp, *, reset, live_hot):
        """Share head barriers only within an already authorized complete COMMIT."""
        if reset is None and len(staged.changes) > 1:
            try:
                self._ensure_buckets((bucket_of(change.key, self.definition.bucket_count)
                                      for change in staged.changes
                                      if change.operation is IndexOperation.INSERT), stamp)
            except GrafxError as failure:
                self._note_commit_failure(txn_id, staged, 0, failure)
                raise
        return super()._commit_staged(txn_id, staged, stamp, reset=reset, live_hot=live_hot)

    def _prepare_build_entries(self, entries, lsn):
        """Retain <=64 source entries while sharing private-build head publication."""
        iterator = iter(entries)
        while chunk := tuple(islice(iterator, 64)):
            self._ensure_buckets((bucket_of(key, self.definition.bucket_count)
                                  for _ref, key, _end in chunk), lsn)
            yield from chunk

    def _populated_buckets(self, visit: Callable[[], None] | None = None) -> Iterator[int]:
        """Read each directory once; retain only one decoded page, never page pins.

        Callers still validate each selected head and chain under their existing
        publication/stable-view fence. No cached directory is an authority proof.
        """
        capacity = self._directory_capacity()
        for ordinal in range(self._minimum_pages() - 1):
            if visit is not None:
                visit()
            with self._pool.pinned(self.file, ordinal + 1) as page:
                values = self._decode_directory(page, ordinal)
            for position, head in enumerate(values):
                if head != NO_PAGE:
                    yield ordinal * capacity + position

    def _apply_change(self, change, lsn):
        """Materialize only an INSERT's absent head; other operations remain canonical."""
        if change.operation is IndexOperation.INSERT:
            self._require_key(change.key)
            self._ensure_bucket(bucket_of(change.key, self.definition.bucket_count), lsn)
        return super()._apply_change(change, lsn)

    def _apply_empty_build_change(self, build, change, lsn):
        """Use the scalar sparse protocol, not eager-head-only build shortcuts."""
        return None

    def assisted_rehash_pressure(self) -> tuple[int, int]:
        """Sample populated heads while accounting compact-directory metadata separately."""
        self.open()
        entries = heads = 0
        for bucket in self._populated_buckets():
            head = self._bucket_head(bucket)
            if head == NO_PAGE:
                continue
            heads += 1
            with self._pool.pinned(self.file, head) as page:
                self._require_index_page(page, head)
                entries += len(page.live_slots())
        return entries, max(0, self._pool.storage.page_count(self.file) - self._minimum_pages() - heads)
