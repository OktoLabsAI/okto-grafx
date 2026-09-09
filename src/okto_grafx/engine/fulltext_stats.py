"""Incremental snapshot statistics derived from a complete committed WAL interval.

No new durable authority: a missing/recycled/oversized proof declines to the existing
full index census. The caller brackets this work in the ordinary index certificate.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from okto_grafx.domain.index.fulltext import TextSearchLimits
from okto_grafx.domain.index.records import IndexOperation, change_of
from okto_grafx.domain.recovery.decision import committed_replay
from okto_grafx.domain.wal.record import WalRecordType
from okto_grafx.domain.txn.records import decode_page_write_location
from okto_grafx.engine.wal_manager import WalManager

__all__: list[str] = []

if TYPE_CHECKING:
    from collections.abc import Callable
    from okto_grafx.engine.database import Database
    from okto_grafx.engine.index_manager import IndexStore


def advance_statistics(
    database: Database,
    store: IndexStore,
    through: int,
    limits: TextSearchLimits,
    check: Callable[[], None],
    *,
    reserve: Callable[[int], None] | None = None,
) -> tuple[tuple[int, tuple[int, ...]], int] | None:
    """Advance the newest eligible seed; never assume that a prefix remains retained."""
    candidates = [
        (key[2], stats)
        for key, stats in database._text_stats_cache.items()
        if key[0] == store.file and key[2] < through
    ]
    if not candidates or type(database._wal) is not WalManager:
        return None
    previous, stats = max(candidates, key=lambda item: item[0])
    if reserve is not None:
        reserve(limits.max_statistics_wal_bytes + limits.max_statistics_wal_records * 256)
    records = database._wal.read_bounded(
        previous + 1,
        through,
        max_records=limits.max_statistics_wal_records,
        max_bytes=limits.max_statistics_wal_bytes,
    )
    if (
        not records
        or records[-1].record_type != WalRecordType.COMMIT
        or records[-1].lsn != through
    ):
        return None
    for record in records:
        check()
        if record.record_type not in (
            WalRecordType.BEGIN,
            WalRecordType.COMMIT,
            WalRecordType.ABORT,
            WalRecordType.WRITE_PAGE,
            WalRecordType.INDEX_WRITE,
            WalRecordType.INDEX_RECONCILE,
        ):
            return None
    replay = committed_replay(records)
    if replay.incomplete_effects or replay.last_committed_lsn != through:
        return None
    commits = {(r.epoch, r.txn_id): r.lsn for r in replay.commit_records}
    count, lengths = stats
    totals = list(lengths)
    seen = set()
    for record in replay.effects:
        check()
        if record.record_type == WalRecordType.WRITE_PAGE:
            location = decode_page_write_location(record.payload)
            if location.file in (store.file, database._catalog.file):
                return None
            continue
        if record.record_type not in (
            WalRecordType.INDEX_WRITE,
            WalRecordType.INDEX_RECONCILE,
        ):
            continue
        change = change_of(record)
        if change.index != store.name:
            continue
        if change.operation is IndexOperation.RESET:
            return None
        if (
            not change.key.startswith(b"\x00")
            or change.operation is IndexOperation.REMOVE
        ):
            continue
        commit = commits[record.epoch, record.txn_id]
        expected_csn = 0 if change.operation is IndexOperation.INSERT else commit
        if (
            change.versioned
            or change.csn != expected_csn
            or len(change.key) != 1 + 4 * len(totals)
        ):
            return None
        identity = (commit, change.operation, change.ref, change.key)
        if identity in seen:
            return None
        seen.add(identity)
        # The logical native WAL already carries complete length deltas. A physical
        # old-version reference can be reclaimed later; do not dereference it or infer
        # a current page image from its former address. Normal read admission/recovery
        # and the caller's pre/post certificate remain the authority for coverage.
        lengths = struct.unpack("<" + "I" * len(totals), change.key[1:])
        sign = 1 if change.operation is IndexOperation.INSERT else -1
        if change.operation not in (IndexOperation.INSERT, IndexOperation.TOMBSTONE):
            return None
        count += sign
        totals = [a + sign * b for a, b in zip(totals, lengths, strict=True)]
    if count < 0 or any(v < 0 for v in totals) or (count == 0 and any(totals)):
        return None
    return (count, tuple(totals)), len(records)
