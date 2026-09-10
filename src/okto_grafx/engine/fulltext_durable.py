"""Native page-0 aggregate, published only after complete committed index effects."""

from __future__ import annotations
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

import struct

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import PROVISIONAL_CSN
from okto_grafx.domain.index.fulltext import decode_options, has_durable_statistics
from okto_grafx.domain.index.records import IndexChange, IndexOperation, change_of
from okto_grafx.domain.recovery.decision import CommittedReplay
from okto_grafx.domain.wal.record import WalRecordType

_SLOT = 2
_FORMAT = struct.Struct("<8sB7xQQ4Q")
_MAGIC = b"GRFXFTS1"
_HISTORY_HEADER = struct.Struct("<8sH6x")
_HISTORY_MAGIC = b"GRFXFTH1"

__all__: list[str] = []

if TYPE_CHECKING:
    from okto_grafx.engine.index_manager import IndexStore, IndexManager
    from okto_grafx.domain.page import Page


def initial_statistics(derivation: str) -> bytes:
    """Encode the empty private-generation scalar with the declared field width."""
    options = decode_options(derivation)
    fields = len(options.field_weights)
    empty = _FORMAT.pack(_MAGIC, fields, 0, 0, 0, 0, 0, 0)
    capacity = options.statistics_history_entries
    return empty if not capacity else _encode_series([empty], capacity)


def require_statistics_capacity(derivation: str, page_size: int) -> None:
    """Refuse an impossible page-0 reservation before any DDL staging/effects."""
    from okto_grafx.domain.page.layout import PAGE_HEADER_SIZE, SLOT_ENTRY_SIZE
    from okto_grafx.domain.page.file_header import FILE_HEADER_SIZE
    from okto_grafx.domain.index.header import INDEX_HEADER_SIZE
    required = (PAGE_HEADER_SIZE + 3 * SLOT_ENTRY_SIZE + FILE_HEADER_SIZE
                + INDEX_HEADER_SIZE + len(initial_statistics(derivation)))
    if required > page_size:
        raise GrafxConfigurationError(
            "The declared FTS statistics history does not fit page 0.",
            field="statistics_history_entries", required_bytes=required, page_size=page_size,
        )


def _encode_series(records: Sequence[bytes], capacity: int) -> bytes:
    """Encode oldest-first records in one fixed-size, zero-padded page slot."""
    selected = records[-(capacity + 1):]
    return (_HISTORY_HEADER.pack(_HISTORY_MAGIC, len(selected)) + b"".join(selected)
            + bytes((capacity + 1 - len(selected)) * _FORMAT.size))


def read_statistics(store: IndexStore) -> tuple[int, int, tuple[int, ...]]:
    """Read a scalar under the caller's native index certificate/commit fence."""
    with store._pool.pinned(store.file, 0) as page:
        return _decode_statistics(store, page)


def read_statistics_from_device(store: IndexStore) -> tuple[int, int, tuple[int, ...]]:
    """Independent verifier observation; a warm pool cannot conceal disk divergence."""
    raw = store._pool.storage.read_page(store.file, 0)
    return _decode_statistics(store, store._pool.codec.decode_page(raw))


def _decode_statistics(store: IndexStore, page: Page) -> tuple[int, int, tuple[int, ...]]:
    """Decode the current scalar, validating every retained historical record too."""
    return _decode_series(store, page)[-1]


def _decode_series(store: IndexStore, page: Page) -> tuple[tuple[int, int, tuple[int, ...]], ...]:
    """Reject malformed, future, noncanonical or nonmonotonic committed summaries."""
    options = decode_options(store.definition.key_derivation)
    fields = len(options.field_weights)
    try:
        header = store._decode_header_page(page)
        raw = page.read_slot(_SLOT)
        capacity = options.statistics_history_entries
        if capacity:
            magic, used = _HISTORY_HEADER.unpack_from(raw)
            if (magic != _HISTORY_MAGIC or not 1 <= used <= capacity + 1 or any(raw[10:16])
                    or len(raw) != _HISTORY_HEADER.size + (capacity + 1) * _FORMAT.size
                    or any(raw[_HISTORY_HEADER.size + used * _FORMAT.size:])):
                raise ValueError("invalid history envelope")
            records = [raw[16 + i * _FORMAT.size:16 + (i + 1) * _FORMAT.size] for i in range(used)]
        else:
            records = [raw]
        result = []
        for record in records:
            magic, width, mark, count, *lengths = _FORMAT.unpack(record)
            if (magic != _MAGIC or width != fields or any(record[9:16])
                    or mark >= PROVISIONAL_CSN or mark > header.built_through_lsn
                    or any(lengths[fields:]) or (count == 0 and any(lengths))
                    or (result and mark <= result[-1][0])
                    or (mark == 0 and (len(records) != 1 or count))):
                raise ValueError("invalid scalar coverage/shape")
            result.append((mark, count, tuple(lengths[:fields])))
        return tuple(result)
    except (ValueError, struct.error) as failure:
        raise GrafxCorruptionDetected("Invalid durable FTS statistics.",
                                     file=store.file, page=0, field="text_statistics") from failure


def finish_statistics(store: IndexStore, changes: Iterable[IndexChange], commit_lsn: int) -> None:
    """Idempotently reduce a COMPLETE proved commit, never individual redo effects."""
    if not has_durable_statistics(store.definition.key_derivation):
        return
    # During redo header coverage may be below the complete COMMIT marker being
    # installed; the scalar is read before advancing, then both are updated in page 0.
    previous, count, lengths = read_statistics(store)
    if previous >= commit_lsn:
        return
    totals = list(lengths)
    for change in changes:
        if change.operation is IndexOperation.RESET:
            count = 0
            totals = [0] * len(totals)
        elif change.key.startswith(b"\x00") and change.operation in (
            IndexOperation.INSERT, IndexOperation.TOMBSTONE,
        ):
            try:
                values = struct.unpack("<" + "I" * len(totals), change.key[1:])
            except struct.error as failure:
                raise GrafxCorruptionDetected("Invalid FTS length effect.", field="text_statistics") from failure
            delta = 1 if change.operation is IndexOperation.INSERT else -1
            count += delta
            totals = [left + delta * right for left, right in zip(totals, values, strict=True)]
    if (not 0 <= count < 2**64 or any(not 0 <= n < 2**64 for n in totals)
            or (count == 0 and any(totals)) or not 0 < commit_lsn < PROVISIONAL_CSN):
        raise GrafxCorruptionDetected("Invalid reduced FTS statistics.", field="text_statistics")
    publish_statistics(store, commit_lsn, count, totals)


def publish_statistics(store: IndexStore, commit_lsn: int, count: int, totals: Sequence[int]) -> None:
    """Install one complete reduction, or a verified private-generation build census."""
    raw = _FORMAT.pack(_MAGIC, len(totals), commit_lsn, count, *(list(totals) + [0] * (4 - len(totals))))
    with store._pool.pinned(store.file, 0) as page:
        header = store._decode_header_page(page)
        capacity = decode_options(store.definition.key_derivation).statistics_history_entries
        if capacity:
            existing = _decode_series(store, page)
            if commit_lsn < existing[-1][0]:
                raise GrafxCorruptionDetected("Historical FTS publication moved backwards.", field="text_statistics")
            records = [_FORMAT.pack(_MAGIC, len(lengths), mark, docs,
                                    *(list(lengths) + [0] * (4 - len(lengths))))
                       for mark, docs, lengths in existing if 0 < mark < commit_lsn]
            raw = _encode_series([*records, raw], capacity)
        page.update_slot(1, header.advanced_to(commit_lsn).encode())
        page.update_slot(_SLOT, raw)
    store._cache_certificate = None


def collect_build_statistics(accumulator: tuple[int, tuple[int, ...]], key: bytes,
                             ended_at: int | None) -> tuple[int, tuple[int, ...]]:
    """Accumulate live length postings during the already-fenced canonical build."""
    if ended_at is None and key.startswith(b"\x00"):
        values = struct.unpack("<" + "I" * len(accumulator[1]), key[1:])
        return accumulator[0] + 1, tuple(a + b for a, b in zip(accumulator[1], values, strict=True))
    return accumulator


def replay_statistics(manager: IndexManager | None, replay: CommittedReplay) -> None:
    """The CommitRedo caller has already validated the complete COMMIT boundaries."""
    if manager is None:
        return
    from okto_grafx.engine.index_manager import IndexManager
    from okto_grafx.domain.index.fulltext import FULLTEXT_STATISTICS_CAPABILITY
    if not isinstance(manager, IndexManager):
        return
    catalog = manager._catalog_authority()
    if catalog is None or not catalog.requires_capability(FULLTEXT_STATISTICS_CAPABILITY):
        return
    commits = {(r.epoch, r.txn_id): r.lsn for r in replay.commit_records}
    groups = {}
    for record in replay.effects:
        if record.record_type not in (WalRecordType.INDEX_WRITE, WalRecordType.INDEX_RECONCILE):
            continue
        change = change_of(record)
        store = manager.active_index(change.index)
        if has_durable_statistics(store.definition.key_derivation):
            key = (commits[(record.epoch, record.txn_id)], store)
            groups.setdefault(key, []).append(change)
    for (lsn, store), changes in sorted(groups.items(), key=lambda item: item[0][0]):
        finish_statistics(store, changes, lsn)


def snapshot_statistics(store: IndexStore, read_lsn: int) -> tuple[int, tuple[int, ...]] | None:
    """Return totals only when their complete committed coverage fits this snapshot."""
    if not has_durable_statistics(store.definition.key_derivation):
        return None
    with store._pool.pinned(store.file, 0) as page:
        series = _decode_series(store, page)
    for (mark, count, totals), (next_mark, _, _) in zip(series, series[1:]):
        if mark <= read_lsn < next_mark:
            return count, totals
    mark, count, totals = series[-1]
    required = store._required_table_position(read_lsn)
    return (count, totals) if required <= mark <= read_lsn else None


def read_history_from_device(store: IndexStore) -> tuple[tuple[int, int, tuple[int, ...]], ...]:
    """Read the full bounded series independently of warm buffer contents."""
    raw = store._pool.storage.read_page(store.file, 0)
    return _decode_series(store, store._pool.codec.decode_page(raw))
