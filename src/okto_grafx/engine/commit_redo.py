"""Apply the durable effects of committed WAL transactions through one shared door.

Recovery and checkpoint completion both have to turn the same WAL records into the same page
and index state.  Keeping the dispatcher here prevents either path from silently omitting one
of the logical index record types, and keeps durability publication outside replay: applying an
effect dirties the pool, while :meth:`CommitRedo.flush` is an explicit caller-controlled step.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxRecoveryRefused,
)
from okto_grafx.domain.ids import Lsn, NO_LSN
from okto_grafx.domain.index.records import change_of
from okto_grafx.domain.page.slotted import Page
from okto_grafx.domain.recovery.decision import CommittedReplay, committed_replay
from okto_grafx.domain.txn.records import decode_page_write, is_redoable_page_file
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.buffer_pool import (
    MAX_REDO_GAP_PAGES,
    BufferPool,
    apply_page_image,
)

if TYPE_CHECKING:
    from okto_grafx.engine.index_manager import IndexManager

__all__ = ["CommitRedo", "CommitRedoResult", "is_redoable_page_file"]


_PAGE_EFFECT: int = int(WalRecordType.WRITE_PAGE)
_INDEX_EFFECTS: frozenset[int] = frozenset(
    {
        int(WalRecordType.INDEX_WRITE),
        int(WalRecordType.INDEX_RECONCILE),
    }
)
_EFFECT_TYPES: frozenset[int] = frozenset({_PAGE_EFFECT, *_INDEX_EFFECTS})


@dataclass(frozen=True, slots=True)
class CommitRedoResult:
    """Counts and files produced by one committed-effect replay.

    ``effects_replayed`` counts records offered to their idempotent apply doors.  The applied
    counts are deliberately separate: an already-current page changes nothing. ``touched_files``
    contains every file an offered effect belongs to, in first-touch order, even when
    idempotence made the effect a no-op; this also flushes dirty state left by an interrupted
    earlier replay attempt.
    """

    effects_replayed: int = 0
    page_effects_replayed: int = 0
    page_images_applied: int = 0
    index_effects_replayed: int = 0
    index_effects_dispatched: int = 0
    last_committed_lsn: Lsn = NO_LSN
    touched_files: tuple[str, ...] = ()


class CommitRedo:
    """Replay committed page and logical-index effects through their idempotent doors."""

    __slots__ = ("_pool", "_index_manager")

    def __init__(
        self, pool: BufferPool, index_manager: IndexManager | None = None
    ) -> None:
        """Bind the pool and, when this database has indexes, its index registry."""
        self._pool = pool
        self._index_manager = index_manager

    def replay(self, records: Iterable[WalRecord]) -> CommitRedoResult:
        """Select committed effects from WAL-order ``records`` and apply them without flushing."""
        return self.apply(committed_replay(records))

    def apply(self, replay: CommittedReplay) -> CommitRedoResult:
        """Apply ``replay.effects`` in order, leaving durability publication to the caller.

        The shape is checked before the first effect is applied.  A logical record with no index
        manager is mandatory work that this process cannot complete, and an unexpected record
        type means the supposedly selected replay is not a committed-effect replay at all.  Both
        refuse before touching a page.  Payload corruption is not caught or downgraded: the
        typed damage raised by the decoder stops the pass immediately.
        """
        self.preflight(replay)

        page_effects = 0
        pages_applied = 0
        index_effects = 0
        indexes_dispatched = 0
        touched: list[str] = []
        touched_set: set[str] = set()

        def remember(file: str) -> None:
            """Remember one touched file once, preserving first-effect order."""
            if file not in touched_set:
                touched_set.add(file)
                touched.append(file)

        for record in replay.effects:
            if record.record_type == _PAGE_EFFECT:
                page_effects += 1
                write = decode_page_write(record.payload)
                # Offered files are flushed even when their image is already current.  A prior
                # attempt may have installed the image into this same pool and failed before
                # flushing; page_lsn then makes this attempt a no-op, but publication still owes
                # the dirty frame a flush.
                remember(write.file)
                if apply_page_image(
                    self._pool, write.file, write.page_index, write.image
                ):
                    pages_applied += 1
                continue

            # Preflight established that every remaining record is one of the two logical index
            # effects and that a manager is present.  Decode once here to retain the physical
            # file name when the manager confirms it dispatched the record.
            manager = self._index_manager
            assert (
                manager is not None
            )  # established by _preflight; not an external refusal
            index_effects += 1
            change = change_of(record)
            resolve = getattr(manager, "active_index", manager.index)
            index = resolve(change.index)
            remember(index.file)
            if not manager.apply(record):
                raise GrafxRecoveryRefused(
                    f"Index effect {record.lsn} for {change.index!r} was not dispatched; "
                    "commit-state publication is refused.",
                    field="index",
                    index=change.index,
                    lsn=record.lsn,
                )
            indexes_dispatched += 1

        return CommitRedoResult(
            effects_replayed=len(replay.effects),
            page_effects_replayed=page_effects,
            page_images_applied=pages_applied,
            index_effects_replayed=index_effects,
            index_effects_dispatched=indexes_dispatched,
            last_committed_lsn=replay.last_committed_lsn,
            touched_files=tuple(touched),
        )

    def preflight(
        self,
        replay: CommittedReplay,
        *,
        allow_unregistered_indexes: bool = False,
    ) -> None:
        """Validate a complete dispatch plan without applying any of its effects.

        ``allow_unregistered_indexes`` is the narrow catalog-replay dependency: a long-lived
        participant can receive a committed CREATE whose catalog page is what teaches its
        registry the new index name.  Payload shape is still decoded for those logical effects;
        only registry-dependent checks are deferred until catalog adoption, when the caller
        preflights the index-only subplan strictly before dispatching it.
        """
        if not isinstance(replay, CommittedReplay):
            raise GrafxRecoveryRefused(
                f"Committed redo needs a CommittedReplay; got {type(replay).__name__}.",
                field="replay",
                value=type(replay).__name__,
            )
        if not isinstance(allow_unregistered_indexes, bool):
            raise GrafxRecoveryRefused(
                "Committed redo's unregistered-index allowance must be boolean.",
                field="allow_unregistered_indexes",
                value=type(allow_unregistered_indexes).__name__,
            )
        self._preflight(
            replay.effects,
            allow_unregistered_indexes=allow_unregistered_indexes,
        )

    def flush(self, result: CommitRedoResult) -> int:
        """Flush files changed by ``result`` and return the number of pages written.

        This is intentionally separate from :meth:`apply`: recovery can finish every page and
        logical effect first, flush the complete state second, and only then publish its commit
        watermark.  ``BufferPool.flush`` writes pages but does not issue a durability barrier;
        the WAL remains the authority until the caller performs its publication protocol.
        """
        if not isinstance(result, CommitRedoResult):
            raise GrafxRecoveryRefused(
                f"Committed redo can flush a CommitRedoResult; got {type(result).__name__}.",
                field="result",
                value=type(result).__name__,
            )
        return sum(cast(int, self._pool.flush(file)) for file in result.touched_files)

    def _preflight(
        self,
        effects: tuple[WalRecord, ...],
        *,
        allow_unregistered_indexes: bool,
    ) -> None:
        """Refuse an incomplete or malformed dispatch plan before the first mutation."""
        missing_manager_lsn: Lsn | None = None
        simulated_page_counts: dict[str, int] = {}
        for record in effects:
            if not isinstance(record, WalRecord):
                raise GrafxRecoveryRefused(
                    f"A committed replay effect must be a WalRecord; got "
                    f"{type(record).__name__}.",
                    field="effects",
                    value=type(record).__name__,
                )
            if record.record_type not in _EFFECT_TYPES:
                raise GrafxRecoveryRefused(
                    f"Record {record.lsn} has type {record.record_type}, which is not a durable "
                    "page or index effect and cannot enter committed redo.",
                    field="record_type",
                    value=record.record_type,
                    lsn=record.lsn,
                )
            if record.record_type == _PAGE_EFFECT:
                # Decode every payload before mutating the first page, so corruption late in a
                # batch cannot leave a prefix applied and then be mistaken for a complete run.
                write = decode_page_write(record.payload)
                if not is_redoable_page_file(write.file):
                    raise GrafxRecoveryRefused(
                        f"Committed page record {record.lsn} names non-data file "
                        f"{write.file!r}; no effect was applied.",
                        field="file",
                        file=write.file,
                        lsn=record.lsn,
                    )
                self._validate_page_image(
                    write.file,
                    write.page_index,
                    write.image,
                )
                present = simulated_page_counts.get(write.file)
                if present is None:
                    storage = self._pool.storage
                    present = (
                        storage.page_count(write.file)
                        if storage.exists(write.file)
                        else 0
                    )
                if write.page_index >= present + MAX_REDO_GAP_PAGES:
                    raise GrafxCorruptionDetected(
                        f"A page image names page {write.page_index} of {write.file!r}, which "
                        f"holds {present} pages; a single redo may bridge at most "
                        f"{MAX_REDO_GAP_PAGES}.",
                        file=write.file,
                        page=write.page_index,
                        field="page_index",
                        page_count=present,
                        limit=MAX_REDO_GAP_PAGES,
                    )
                simulated_page_counts[write.file] = max(
                    present, write.page_index + 1
                )
            if record.record_type in _INDEX_EFFECTS:
                change = change_of(record)
                manager = self._index_manager
                if manager is None:
                    if missing_manager_lsn is None:
                        missing_manager_lsn = record.lsn
                    continue
                try:
                    resolve = getattr(manager, "active_index", manager.index)
                    index = resolve(change.index)
                except GrafxIndexError as failure:
                    if allow_unregistered_indexes:
                        continue
                    # The manager's typed index error is useful as the chained cause, while the
                    # recovery-level refusal states why no earlier effect was applied.
                    raise GrafxRecoveryRefused(
                        f"Committed redo names index {change.index!r} at record {record.lsn}, "
                        "but that index is not registered; no effect was applied.",
                        field="index",
                        index=change.index,
                        lsn=record.lsn,
                    ) from failure
                if change.versioned != index.definition.versioned:
                    raise GrafxCorruptionDetected(
                        f"A record for index {change.index!r} describes a "
                        f"{'versioned' if change.versioned else 'unversioned'} entry and this "
                        f"index stores "
                        f"{'versioned' if index.definition.versioned else 'unversioned'} "
                        "ones, so committed redo was refused before any effect was applied.",
                        field="versioned",
                        value=change.versioned,
                        index=change.index,
                        file=index.file,
                        operation=change.operation.name,
                        lsn=record.lsn,
                    )
                max_key_bytes = getattr(index, "max_key_bytes", None)
                if (
                    isinstance(max_key_bytes, int)
                    and len(change.key) > max_key_bytes
                ):
                    raise GrafxCorruptionDetected(
                        f"A record for index {change.index!r} carries a key of "
                        f"{len(change.key)} bytes, but this index stores at most "
                        f"{max_key_bytes}; committed redo was refused before any effect was "
                        "applied.",
                        field="key",
                        value=len(change.key),
                        limit=max_key_bytes,
                        index=change.index,
                        file=index.file,
                        operation=change.operation.name,
                        lsn=record.lsn,
                    )
        if missing_manager_lsn is not None:
            raise GrafxRecoveryRefused(
                f"Committed redo includes a logical index effect at record "
                f"{missing_manager_lsn}, but no IndexManager is configured; no effect was "
                "applied.",
                field="index_manager",
                lsn=missing_manager_lsn,
            )

    def _validate_page_image(self, file: str, page_index: int, image: bytes) -> None:
        """Decode one WAL page image and attach its known location to any damage."""
        try:
            decoded = self._pool.codec.decode_page(image, verify=True)
        except GrafxCorruptionDetected as damaged:
            details = {
                key: value
                for key, value in damaged.details.items()
                if key not in {"file", "page"}
            }
            raise GrafxCorruptionDetected(
                f"Page {page_index} of {file!r} could not be preflighted: "
                f"{damaged.message}",
                file=file,
                page=page_index,
                **details,
            ) from damaged
        if not isinstance(decoded, Page):
            raise GrafxCorruptionDetected(
                f"The codec returned a {type(decoded).__name__} instead of a page image.",
                file=file,
                page=page_index,
            )
