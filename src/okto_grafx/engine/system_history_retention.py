"""Atomic payload redaction plans; preserves lineage framing and current versions."""

from __future__ import annotations

__all__ = ["PreparedHistoryRetention", "prepare_retention"]

from dataclasses import dataclass, replace

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.model.schema import encode_tuple
from okto_grafx.domain.page import Page
from okto_grafx.engine.system_history_store import HistoryPageImage, SystemHistoryStore


@dataclass(frozen=True, slots=True)
class PreparedHistoryRetention:
    """Captured complete history rewrite, no authority or writes until native COMMIT."""

    database_uuid: bytes
    page_size: int
    previous_sequence: int
    batches: tuple
    redacted_versions: int
    redacted_bytes: int
    original_extent: int
    index_activation: int | None = None
    compact: bool = False
    allows_tail: bool = False

    def bind(self, sequence: int) -> tuple[HistoryPageImage, ...]:
        """Rebuild identical extents/intervals and append the retention COMMIT at exact LSN."""
        pages = {}
        store = SystemHistoryStore(lambda file, number: pages[file, number],
                                   database_uuid=self.database_uuid, page_size=self.page_size)
        previous = None
        baseline = []
        root = None
        for identity, changes in self.batches:
            if previous is None:
                images = store.activation_images(changes, identity.sequence)
            else:
                from okto_grafx.engine.system_history_index_store import PreparedHistoryIndex, head_root
                from okto_grafx.engine.system_history_store import _HEAD
                index = None
                if not self.compact and identity.sequence == self.index_activation:
                    index = PreparedHistoryIndex(store.read_page, baseline=tuple(baseline))
                elif root is not None:
                    index = PreparedHistoryIndex(store.read_page, root)
                images = store.prepare(changes, expected_sequence=previous, page_count=len(pages), index=index).bind(identity.sequence)
            for image in images:
                pages[image.file, image.page_index] = image.raw
            previous = identity.sequence
            baseline.append((identity.sequence, changes))
            from okto_grafx.engine.system_history_index_store import head_root
            from okto_grafx.engine.system_history_store import _HEAD
            root = head_root(store._page(0).read_slot(0), _HEAD.size)
        if (not self.compact and len(pages) != self.original_extent) or previous != self.previous_sequence:
            raise GrafxCorruptionDetected("Retention changed immutable batch extents.", field="system_history_retention")
        from okto_grafx.engine.system_history_index_store import PreparedHistoryIndex
        index = None if root is None else PreparedHistoryIndex(store.read_page, root)
        if self.compact and self.index_activation is not None:
            index = PreparedHistoryIndex(store.read_page, baseline=tuple(baseline))
        for image in store.prepare((), expected_sequence=previous, page_count=len(pages), index=index).bind(sequence):
            pages[image.file, image.page_index] = image.raw
        output = []
        for (file, index), raw in sorted(pages.items()):
            page = Page.from_bytes(raw)
            if index == 0 and (self.compact or self.allows_tail):
                from okto_grafx.engine.system_history_store import _COMPACT_HEAD_MAGIC
                page.update_slot(0, _COMPACT_HEAD_MAGIC + page.read_slot(0)[8:])
            page.page_lsn = sequence
            output.append(HistoryPageImage(file, index, page.to_bytes()))
        return tuple(output)


def prepare_retention(store: SystemHistoryStore, *, sequence: int, page_count: int,
                      tables: tuple[int, ...], before: int) -> PreparedHistoryRetention:
    """Redact only versions closed at/before the retained boundary, never current values.

    Replacement bytes have the exact original length, filled with zeroes. That
    preserves page cardinality and permits ordinary atomic full-image WAL without
    introducing online truncation or a partially published compaction generation.
    Physical file space and lineage/schema envelopes remain; this is not secure
    erasure of prior WAL, backups or filesystem snapshots.
    """
    captured = []
    active = {}
    redacted_count = redacted_bytes = 0
    selected = frozenset(tables)
    for identity, changes, _ in store._iter_batches(expected_sequence=sequence, page_count=page_count):
        mutable = list(changes)
        batch_number = len(captured)
        captured.append((identity, mutable))
        for index, change in enumerate(changes):
            if change.operation == 4:
                continue
            key = (change.table.table_id, change.record_id)
            prior = active.get(key)
            if prior is not None and identity.sequence <= before and key[0] in selected:
                old = captured[prior[0]][1][prior[1]]
                if old.operation in (1, 2):
                    size = len(encode_tuple(old.table, old.values))
                    if old.node_labels is not None:
                        from okto_grafx.domain.model.node_labels import encode_node_labels
                        size += len(encode_node_labels(old.node_labels))
                    captured[prior[0]][1][prior[1]] = replace(old, operation=old.operation + 4, values=(),
                                                           redacted_bytes=size, node_labels=None,
                                                           redacted_node_labels=old.node_labels is not None)
                    redacted_count += 1
                    redacted_bytes += size
            if change.operation == 3:
                active.pop(key, None)
            else:
                active[key] = (batch_number, index)
    return PreparedHistoryRetention(store.database_uuid, store.page_size, sequence,
        tuple((identity, tuple(changes)) for identity, changes in captured), redacted_count, redacted_bytes,
        store._head(sequence, page_count)[3], store._index_start(expected_sequence=sequence, page_count=page_count),
        allows_tail=store._page(0).read_slot(0)[:8] == b"GXHYHD03")
