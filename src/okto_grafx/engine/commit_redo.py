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
from okto_grafx.domain.ids import Lsn, NO_LSN, NO_PAGE, PROVISIONAL_CSN
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.records import IndexOperation, change_of
from okto_grafx.domain.page.layout import PageType
from okto_grafx.domain.page.slotted import Page
from okto_grafx.domain.recovery.decision import (
    CommittedReplay, committed_replay, validate_commit_boundaries,
)
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.domain.txn.records import (
    COMMIT_CATALOG_PAGE_FILES, decode_page_write, is_redoable_page_file,
)
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.buffer_pool import (
    MAX_REDO_GAP_PAGES,
    BufferPool,
    apply_page_image,
)
from okto_grafx.engine.catalog_store import CATALOG_FILE, CatalogStore, read_catalog_page_images
from okto_grafx.engine.commit_catalog_store import CommitCatalogPageImage, CommitCatalogStore
from okto_grafx.engine.fulltext_durable import replay_statistics
from okto_grafx.engine.system_history_recovery import validate_system_history
from okto_grafx.domain.txn.records import SYSTEM_HISTORY_FILE

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


@dataclass(frozen=True, slots=True)
class _PreparedPageEffect:
    """One page effect decoded and verified by the mandatory full preflight."""

    record: WalRecord
    file: str
    page_index: int
    image: bytes
    page_lsn: Lsn
    watermark_scope_known: bool
    watermark_table_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class _PreflightedReplay:
    """One internal, passage-bound proof of an exact immutable replay shape."""

    seal: object
    owner: object
    passage: object
    replay: CommittedReplay
    effects: tuple[WalRecord, ...]
    incomplete_effects: tuple[WalRecord, ...]
    commit_records: tuple[WalRecord, ...]
    commit_signature: tuple[tuple[object, ...], ...]
    last_committed_lsn: Lsn
    checkpoint_lsn: Lsn | None
    allow_unregistered_indexes: bool
    allow_page_coalescing: bool
    record_signature: tuple[tuple[object, ...], ...]
    signature_verified: bool
    contains_index_reset: bool
    prepared_pages: tuple[tuple[int, _PreparedPageEffect], ...]


_PREFLIGHT_SEAL: object = object()


class CommitRedo:
    """Replay committed page and logical-index effects through their idempotent doors."""

    __slots__ = ("_pool", "_index_manager", "_database_uuid")

    def __init__(
        self, pool: BufferPool, index_manager: IndexManager | None = None,
        *, database_uuid: bytes | None = None,
    ) -> None:
        """Bind the pool and, when this database has indexes, its index registry."""
        self._pool = pool
        self._index_manager = index_manager
        self._database_uuid = None if database_uuid is None else CommitId(database_uuid, 1).database_uuid

    def replay(self, records: Iterable[WalRecord]) -> CommitRedoResult:
        """Select committed effects from WAL-order ``records`` and apply them without flushing."""
        return self.apply(committed_replay(records))

    def apply(
        self,
        replay: CommittedReplay,
        *,
        _preflighted: object | None = None,
        _passage: object | None = None,
        _checkpoint_lsn: Lsn | None = None,
    ) -> CommitRedoResult:
        """Apply ``replay.effects`` in order, leaving durability publication to the caller.

        The shape is checked before the first effect is applied.  A logical record with no index
        manager is mandatory work that this process cannot complete, and an unexpected record
        type means the supposedly selected replay is not a committed-effect replay at all.  Both
        refuse before touching a page.  Payload corruption is not caught or downgraded: the
        typed damage raised by the decoder stops the pass immediately.
        """
        if not isinstance(replay, CommittedReplay):
            raise GrafxRecoveryRefused(
                f"Committed redo needs a CommittedReplay; got {type(replay).__name__}.",
                field="replay",
                value=type(replay).__name__,
            )
        proof = self._compatible_preflight(
            replay,
            _preflighted,
            allow_unregistered_indexes=False,
            passage=_passage,
            checkpoint_lsn=_checkpoint_lsn,
        )
        if proof is not None:
            prepared_pages = proof.prepared_pages
        else:
            validate_commit_boundaries(replay)
            self._validate_replay_floor(replay, _checkpoint_lsn)
            commit_records = replay.commit_records
            commit_signature = self._record_signature(commit_records)
            prepared_pages, _signature, _contains_index_reset = self._preflight(
                replay.effects,
                allow_unregistered_indexes=False,
                allow_commit_catalog=_checkpoint_lsn is not None and self._database_uuid is not None,
            )
            self._validate_native_catalog(replay, prepared_pages, checkpoint_lsn=_checkpoint_lsn)
            if self._record_signature(replay.effects) != _signature:
                raise GrafxRecoveryRefused("Replay changed during preflight.", field="preflighted_replay")
            self._require_unchanged_commit_records(replay, commit_records, commit_signature)

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

        manager = self._index_manager
        if (
            replay.effects
            and not prepared_pages
            and manager is not None
            and all(record.record_type in _INDEX_EFFECTS for record in replay.effects)
        ):
            apply_batch = getattr(manager, "apply_partitioned_replay_batch", None)
            if not callable(apply_batch):
                apply_batch = getattr(manager, "apply_common_replay_batch", None)
            if callable(apply_batch):
                batch_files = apply_batch(replay.effects)
                if batch_files is not None:
                    for file in batch_files:
                        remember(file)
                    replay_statistics(manager, replay)
                    return CommitRedoResult(
                        effects_replayed=len(replay.effects),
                        index_effects_replayed=len(replay.effects),
                        index_effects_dispatched=len(replay.effects),
                        last_committed_lsn=replay.last_committed_lsn,
                        touched_files=tuple(touched),
                    )

        page_only = len(prepared_pages) == len(replay.effects)
        may_coalesce = proof.allow_page_coalescing if proof is not None else page_only
        page_plan = (
            self._coalesce_page_plan(prepared_pages)
            if page_only and may_coalesce
            else None
        )
        prepared_by_position = {
            position: prepared for position, prepared in prepared_pages
        }
        effects: Iterable[tuple[int, WalRecord, _PreparedPageEffect | None]]
        if page_plan is not None:
            effects = (
                (position, prepared.record, prepared)
                for position, prepared in page_plan
            )
        else:
            effects = (
                (position, record, prepared_by_position.get(position))
                for position, record in enumerate(replay.effects)
            )

        for _position, record, prepared in effects:
            if record.record_type == _PAGE_EFFECT:
                assert (
                    prepared is not None
                )  # every page effect was prepared by preflight
                # Offered files are flushed even when their image is already current.  A prior
                # attempt may have installed the image into this same pool and failed before
                # flushing; page_lsn then makes this attempt a no-op, but publication still owes
                # the dirty frame a flush.
                remember(prepared.file)
                if apply_page_image(
                    self._pool,
                    prepared.file,
                    prepared.page_index,
                    prepared.image,
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

        replay_statistics(manager, replay)
        return CommitRedoResult(
            effects_replayed=len(replay.effects),
            page_effects_replayed=len(prepared_pages),
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
        _passage: object | None = None,
        _checkpoint_lsn: Lsn | None = None,
    ) -> object:
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
        validate_commit_boundaries(replay)
        self._validate_replay_floor(replay, _checkpoint_lsn)
        commit_records = replay.commit_records
        commit_signature = self._record_signature(commit_records)
        prepared_pages, signature, contains_index_reset = self._preflight(
            replay.effects,
            allow_unregistered_indexes=allow_unregistered_indexes,
            allow_commit_catalog=_checkpoint_lsn is not None and self._database_uuid is not None,
        )
        self._validate_native_catalog(replay, prepared_pages, checkpoint_lsn=_checkpoint_lsn)
        if self._record_signature(replay.effects) != signature:
            raise GrafxRecoveryRefused("Replay changed during preflight.", field="preflighted_replay")
        self._require_unchanged_commit_records(replay, commit_records, commit_signature)
        return _PreflightedReplay(
            seal=_PREFLIGHT_SEAL,
            owner=self,
            passage=_passage,
            replay=replay,
            effects=replay.effects,
            incomplete_effects=replay.incomplete_effects,
            commit_records=commit_records,
            commit_signature=commit_signature,
            last_committed_lsn=replay.last_committed_lsn,
            checkpoint_lsn=_checkpoint_lsn,
            allow_unregistered_indexes=allow_unregistered_indexes,
            allow_page_coalescing=len(prepared_pages) == len(replay.effects),
            record_signature=signature,
            signature_verified=False,
            contains_index_reset=contains_index_reset,
            prepared_pages=prepared_pages,
        )

    @staticmethod
    def _validate_replay_floor(replay: CommittedReplay, checkpoint_lsn: Lsn | None) -> None:
        """Bind native replay to its caller-proved checkpoint, before decoding or I/O."""
        if checkpoint_lsn is None:
            return
        if type(checkpoint_lsn) is not int or not 0 <= checkpoint_lsn < PROVISIONAL_CSN:
            raise GrafxRecoveryRefused("Invalid replay checkpoint.", field="checkpoint_lsn")
        if (
            replay.commit_records and replay.commit_records[0].lsn <= checkpoint_lsn
            or not replay.commit_records and (
                replay.effects or replay.last_committed_lsn not in {0, checkpoint_lsn}
            )
        ):
            raise GrafxRecoveryRefused(
                "Replay COMMIT boundaries do not start strictly after the checkpoint.",
                field="checkpoint_lsn", checkpoint_lsn=checkpoint_lsn,
            )

    def _validate_native_catalog(
        self, replay: CommittedReplay,
        prepared_pages: tuple[tuple[int, _PreparedPageEffect], ...],
        *, checkpoint_lsn: Lsn | None,
    ) -> None:
        catalogs = []
        horizon = self._validate_catalog_transitions(replay, prepared_pages, checkpoint_lsn=checkpoint_lsn,
                                                       _catalogs=catalogs)
        validate_system_history(self._pool, replay, prepared_pages, checkpoint_lsn=checkpoint_lsn,
                                database_uuid=self._database_uuid, native_catalogs=catalogs)
        images = tuple(CommitCatalogPageImage(page.file, page.page_index, page.image)
                       for _position, page in prepared_pages if page.file in COMMIT_CATALOG_PAGE_FILES)
        if horizon is None:
            if images or checkpoint_lsn is not None and any(
                self._pool.storage.exists(file) for file in COMMIT_CATALOG_PAGE_FILES
            ):
                raise GrafxRecoveryRefused("Journal effects require schema activation.", field="commit_catalog_activation")
            return
        # Unqualified historical dispatcher use retains its ordinary-pages-only contract.
        if checkpoint_lsn is None:
            return
        storage = self._pool.storage
        final_sequence = replay.last_committed_lsn if replay.commit_records else checkpoint_lsn
        if final_sequence <= horizon:
            if images or any(storage.exists(file) for file in COMMIT_CATALOG_PAGE_FILES):
                raise GrafxRecoveryRefused("History exists before its first tracked COMMIT.", field="commit_catalog_activation")
            return
        if self._database_uuid is None:
            raise GrafxRecoveryRefused("Commit history replay requires database identity.", field="commit_catalog_replay")
        store = CommitCatalogStore(storage.read_page, database_uuid=self._database_uuid, page_size=self._pool.page_size)
        if not replay.commit_records:
            store.validate_published_head(sequence=checkpoint_lsn, activation_sequence=horizon, file_size=storage.file_size)
            return
        store.validate_redo(replay, previous_sequence=checkpoint_lsn, activation_sequence=horizon)
        store.validate_redo_targets(
            images, previous_sequence=checkpoint_lsn, sequence=final_sequence, activation_sequence=horizon,
            file_size=lambda file: storage.file_size(file) if storage.exists(file) else 0,
            resident_image=lambda file, index: cast(bytes | None, self._pool._resident_page_image(file, index)),
        )

    def _validate_catalog_transitions(
        self, replay: CommittedReplay,
        prepared_pages: tuple[tuple[int, _PreparedPageEffect], ...],
        *, checkpoint_lsn: Lsn | None = None, _catalogs: list | None = None,
    ) -> int | None:
        """Prove complete schema-catalog snapshots before native replay can mutate.

        Legacy hand-composed effect-only plans retain the dispatcher contract;
        actual WAL selectors provide terminal records. Missing physical tails
        cannot supply a missing catalog image. With a caller-proved checkpoint, an activation after that floor
        must be introduced by its own complete schema snapshot in this range.
        """
        if not replay.commit_records:
            return self._validate_catalog_without_schema_effects(replay, checkpoint_lsn, _catalogs=_catalogs)
        grouped: dict[tuple[int, int], list[tuple[int, bytes]]] = {}
        for _position, prepared in prepared_pages:
            if prepared.file == CATALOG_FILE:
                record = prepared.record
                grouped.setdefault((record.epoch, record.txn_id), []).append((prepared.page_index, prepared.image))
        if not grouped:
            return self._validate_catalog_without_schema_effects(replay, checkpoint_lsn, _catalogs=_catalogs)
        seen = False
        previous_horizon: int | None = None
        for terminal in replay.commit_records:
            images = grouped.get((terminal.epoch, terminal.txn_id))
            if images is None:
                continue
            catalog = read_catalog_page_images(tuple(images), page_size=self._pool.page_size, sequence=terminal.lsn)
            if _catalogs is not None:
                _catalogs.append((terminal.lsn, catalog))
            horizon = catalog.commit_catalog_activation
            if (
                horizon is not None and horizon > terminal.lsn
                or not seen and checkpoint_lsn is not None and horizon is not None
                and checkpoint_lsn < horizon != terminal.lsn
                or seen and previous_horizon is not None and horizon != previous_horizon
                or seen and previous_horizon is None and horizon is not None and horizon != terminal.lsn
            ):
                raise GrafxRecoveryRefused(
                    "Catalog replay changes or invents the commit-history activation horizon.",
                    field="commit_catalog_activation", lsn=terminal.lsn,
                )
            previous_horizon = horizon
            seen = True
        return previous_horizon

    def _validate_catalog_without_schema_effects(
        self, replay: CommittedReplay, checkpoint_lsn: Lsn | None, *, _catalogs: list | None = None,
    ) -> int | None:
        """Use current pages, never a mutable/stale adopted catalog, for native gaps.

        No schema image exists to repair or establish activation in this range.
        Reading the canonical catalog through this already-fenced pool is therefore
        mandatory. This is one schema read per native replay, not a graph/history
        walk, and creates no persistent or cached authority.
        """
        if checkpoint_lsn is None:
            return None  # Legacy standalone dispatcher has no native control context.
        storage = self._pool.storage
        exists = storage.exists(CATALOG_FILE)
        empty = not exists or storage.page_count(CATALOG_FILE) == 0
        if empty:
            if any(storage.exists(file) for file in COMMIT_CATALOG_PAGE_FILES):
                raise GrafxRecoveryRefused(
                    "Commit-history files have no schema catalog establishing activation.",
                    field="commit_catalog_activation",
                )
        if not exists:
            return None  # Uninitialized/legacy stack; no history may be inferred.
        if empty and checkpoint_lsn == 0 and not replay.commit_records:
            return None  # Fresh empty file, before bootstrap; no COMMIT is being certified.
        catalog = CatalogStore(self._pool).read_from_pages()
        if _catalogs is not None:
            _catalogs.append((None, catalog))
        horizon = catalog.commit_catalog_activation
        if horizon is None:
            if any(storage.exists(file) for file in COMMIT_CATALOG_PAGE_FILES):
                raise GrafxRecoveryRefused(
                    "Commit-history files require an activated schema catalog.",
                    field="commit_catalog_activation",
                )
            return None
        if horizon > checkpoint_lsn:
            raise GrafxRecoveryRefused(
                "Post-checkpoint activation has no schema snapshot in the selected WAL range.",
                field="commit_catalog_activation", checkpoint_lsn=checkpoint_lsn,
                activation_lsn=horizon,
            )
        return horizon

    def _verify_preflight_for(
        self,
        replay: CommittedReplay,
        preflighted: object,
        *,
        allow_unregistered_indexes: bool,
        passage: object,
        checkpoint_lsn: Lsn | None = None,
    ) -> object | None:
        """Consume one exact proof once so later private facts need no second decode.

        A caller that cannot prove the object belongs to this redo instance, replay and passage
        receives ``None``. That is a conservative optimization miss: canonical replay remains
        responsible for the effects.
        """
        compatible = self._compatible_preflight(
            replay,
            preflighted,
            allow_unregistered_indexes=allow_unregistered_indexes,
            passage=passage,
            checkpoint_lsn=checkpoint_lsn,
        )
        return None if compatible is None else self._verified_preflight(compatible)

    def _verified_contains_index_reset(self, preflighted: object) -> bool | None:
        """Return the RESET fact only from this instance's already-verified private proof."""
        if (
            not isinstance(preflighted, _PreflightedReplay)
            or preflighted.seal is not _PREFLIGHT_SEAL
            or preflighted.owner is not self
            or not preflighted.signature_verified
        ):
            return None
        return preflighted.contains_index_reset

    def _verified_watermark_table_ids(
        self, preflighted: object
    ) -> frozenset[int] | None:
        """Return exact heap owners only from this instance's verified full proof.

        ``None`` deliberately means "take the canonical full photograph".  The shortcut is
        available only when every page image was classified by the concrete IndexManager while
        it was already being decoded by the mandatory preflight.  A custom manager, forged
        proof, or image whose scope cannot be established therefore cannot suppress a table
        watermark walk.
        """
        if (
            not isinstance(preflighted, _PreflightedReplay)
            or preflighted.seal is not _PREFLIGHT_SEAL
            or preflighted.owner is not self
            or not preflighted.signature_verified
            or any(
                not prepared.watermark_scope_known
                for _position, prepared in preflighted.prepared_pages
            )
        ):
            return None
        table_ids: set[int] = set()
        for _position, prepared in preflighted.prepared_pages:
            table_ids.update(prepared.watermark_table_ids)
        return frozenset(table_ids)

    def _watermark_scope_for_page(
        self,
        file: str,
        page: Page,
        *,
        meta_baselines: dict[tuple[str, int], Page | None] | None = None,
    ) -> tuple[bool, frozenset[int]]:
        """Classify one decoded page without granting authority to manager lookalikes."""
        if file in COMMIT_CATALOG_PAGE_FILES or file == SYSTEM_HISTORY_FILE:
            # Full journal validation is mandatory before this fact is consumed.
            # Audit history changes no heap/MVCC table watermark; treating it as
            # unknown would make every journal append scan all indexed tables.
            return True, frozenset()
        manager = self._index_manager
        if manager is None:
            return False, frozenset()
        # This is a freshness shortcut, not the replay dispatcher compatibility surface.  Only
        # the concrete manager whose heap/page invariants are defined in this package can prove
        # that an untouched table remained untouched.  All adapters and test doubles retain the
        # complete scan.
        from okto_grafx.engine.index_manager import IndexManager

        if type(manager) is not IndexManager:
            return False, frozenset()
        if meta_baselines is None:
            meta_baselines = {}
        baseline = None
        if page.page_type == int(PageType.META):
            key = (file, page.page_index)
            if key not in meta_baselines:
                meta_baselines[key] = IndexManager.replay_watermark_meta_baseline(
                    manager, file, page
                )
            baseline = meta_baselines[key]
        return IndexManager.replay_watermark_scope(
            manager, file, page, meta_baseline=baseline
        )

    def _ensure_preflight(
        self,
        replay: CommittedReplay,
        preflighted: object | None,
        *,
        allow_unregistered_indexes: bool,
        passage: object,
        checkpoint_lsn: Lsn | None = None,
    ) -> object:
        """Return a compatible proof, performing the full preflight when necessary."""
        compatible = self._compatible_preflight(
            replay,
            preflighted,
            allow_unregistered_indexes=allow_unregistered_indexes,
            passage=passage,
            checkpoint_lsn=checkpoint_lsn,
        )
        if compatible is not None:
            return self._verified_preflight(compatible)
        fresh = self.preflight(
            replay,
            allow_unregistered_indexes=allow_unregistered_indexes,
            _passage=passage,
            _checkpoint_lsn=checkpoint_lsn,
        )
        assert isinstance(fresh, _PreflightedReplay)
        return self._verified_preflight(fresh)

    def _project_page_preflight(
        self,
        source_replay: CommittedReplay,
        page_replay: CommittedReplay,
        preflighted: object,
        *,
        allow_unregistered_indexes: bool,
        passage: object,
        checkpoint_lsn: Lsn | None = None,
    ) -> object | None:
        """Project a valid full proof onto its exact all-page subplan without decoding again."""
        source = self._compatible_preflight(
            source_replay,
            preflighted,
            allow_unregistered_indexes=allow_unregistered_indexes,
            passage=passage,
            checkpoint_lsn=checkpoint_lsn,
        )
        if source is None:
            return None
        if (
            page_replay.last_committed_lsn != source_replay.last_committed_lsn
            or page_replay.incomplete_effects
            or page_replay.commit_records is not source_replay.commit_records
            or len(page_replay.effects) != len(source.prepared_pages)
        ):
            return None
        projected: list[tuple[int, _PreparedPageEffect]] = []
        for position, actual in enumerate(page_replay.effects):
            prepared = source.prepared_pages[position][1]
            if actual is not prepared.record:
                return None
            projected.append((position, prepared))
        prepared_pages = tuple(projected)
        return _PreflightedReplay(
            seal=_PREFLIGHT_SEAL,
            owner=self,
            passage=passage,
            replay=page_replay,
            effects=page_replay.effects,
            incomplete_effects=page_replay.incomplete_effects,
            commit_records=page_replay.commit_records,
            commit_signature=source.commit_signature,
            last_committed_lsn=page_replay.last_committed_lsn,
            checkpoint_lsn=source.checkpoint_lsn,
            allow_unregistered_indexes=False,
            allow_page_coalescing=source.allow_page_coalescing,
            record_signature=tuple(
                source.record_signature[source_position]
                for source_position, _prepared in source.prepared_pages
            ),
            signature_verified=True,
            contains_index_reset=False,
            prepared_pages=prepared_pages,
        )

    def _preflight_index_subplan(
        self, source_replay: CommittedReplay, index_replay: CommittedReplay,
        preflighted: object, *, allow_unregistered_indexes: bool,
        passage: object, checkpoint_lsn: Lsn | None = None,
    ) -> object:
        """Inherit full catalog validation, then strictly revalidate index dispatch.

        The catalog was proved on the complete range and its pages have now been
        adopted. Only the exact index complement may use that proof; registry
        lookups are NOT inherited from the earlier unregistered-index allowance.
        """
        source = self._compatible_preflight(
            source_replay, preflighted,
            allow_unregistered_indexes=allow_unregistered_indexes,
            passage=passage, checkpoint_lsn=checkpoint_lsn,
        )
        if (
            source is None
            or index_replay.last_committed_lsn != source_replay.last_committed_lsn
            or index_replay.commit_records is not source_replay.commit_records
            or index_replay.incomplete_effects
        ):
            raise GrafxRecoveryRefused("Index replay does not match its complete preflight.", field="preflighted_replay")
        expected = tuple(record for record in source.effects if record.record_type in _INDEX_EFFECTS)
        if len(expected) != len(index_replay.effects) or any(
            actual is not original for actual, original in zip(index_replay.effects, expected)
        ):
            raise GrafxRecoveryRefused("Index replay is not the exact index subplan.", field="preflighted_replay")
        index_effects = index_replay.effects
        prepared, signature, contains_reset = self._preflight(
            index_effects, allow_unregistered_indexes=False,
        )
        self._require_unchanged_commit_records(source_replay, source.commit_records, source.commit_signature)
        if (
            self._compatible_preflight(source_replay, source,
                allow_unregistered_indexes=allow_unregistered_indexes,
                passage=passage, checkpoint_lsn=checkpoint_lsn) is None
            or index_replay.effects is not index_effects
            or index_replay.commit_records is not source.commit_records
            or index_replay.last_committed_lsn != source.last_committed_lsn
            or index_replay.incomplete_effects
            or self._record_signature(source_replay.effects) != source.record_signature
        ):
            raise GrafxRecoveryRefused("Complete replay changed during index preflight.", field="preflighted_replay")
        return _PreflightedReplay(
            seal=_PREFLIGHT_SEAL, owner=self, passage=passage, replay=index_replay,
            effects=index_replay.effects, incomplete_effects=index_replay.incomplete_effects,
            commit_records=source.commit_records, commit_signature=source.commit_signature,
            last_committed_lsn=index_replay.last_committed_lsn,
            checkpoint_lsn=source.checkpoint_lsn, allow_unregistered_indexes=False,
            allow_page_coalescing=False, record_signature=signature, signature_verified=True,
            contains_index_reset=contains_reset, prepared_pages=prepared,
        )

    def _compatible_preflight(
        self,
        replay: CommittedReplay,
        preflighted: object | None,
        *,
        allow_unregistered_indexes: bool,
        passage: object | None,
        checkpoint_lsn: Lsn | None = None,
    ) -> _PreflightedReplay | None:
        """Return a genuine exact proof or None so the caller revalidates normally."""
        if (
            not isinstance(replay, CommittedReplay)
            or passage is None
            or not isinstance(preflighted, _PreflightedReplay)
            or preflighted.seal is not _PREFLIGHT_SEAL
            or preflighted.owner is not self
            or preflighted.passage is not passage
            or preflighted.allow_unregistered_indexes is not allow_unregistered_indexes
            or preflighted.replay is not replay
            or preflighted.effects is not replay.effects
            or preflighted.incomplete_effects is not replay.incomplete_effects
            or preflighted.commit_records is not replay.commit_records
            or preflighted.last_committed_lsn != replay.last_committed_lsn
            or preflighted.checkpoint_lsn != checkpoint_lsn
            or checkpoint_lsn is not None and type(checkpoint_lsn) is not int
        ):
            return None
        if (
            not preflighted.signature_verified
            and (
                preflighted.record_signature != self._record_signature(replay.effects)
                or any(not isinstance(record.payload, bytes) for record in replay.commit_records)
                or preflighted.commit_signature != self._record_signature(replay.commit_records)
            )
        ):
            return None
        return preflighted

    @staticmethod
    def _verified_preflight(proof: _PreflightedReplay) -> _PreflightedReplay:
        """Mark a just-compared or just-created proof trusted inside its private passage."""
        if proof.signature_verified:
            return proof
        return _PreflightedReplay(
            seal=proof.seal,
            owner=proof.owner,
            passage=proof.passage,
            replay=proof.replay,
            effects=proof.effects,
            incomplete_effects=proof.incomplete_effects,
            commit_records=proof.commit_records,
            commit_signature=proof.commit_signature,
            last_committed_lsn=proof.last_committed_lsn,
            checkpoint_lsn=proof.checkpoint_lsn,
            allow_unregistered_indexes=proof.allow_unregistered_indexes,
            allow_page_coalescing=proof.allow_page_coalescing,
            record_signature=proof.record_signature,
            signature_verified=True,
            contains_index_reset=proof.contains_index_reset,
            prepared_pages=proof.prepared_pages,
        )

    def _require_unchanged_commit_records(
        self, replay: CommittedReplay, records: tuple[WalRecord, ...],
        signature: tuple[tuple[object, ...], ...],
    ) -> None:
        """Do not seal callback-mutated terminal values after validating earlier ones."""
        if (
            replay.commit_records is not records
            or any(not isinstance(record.payload, bytes) for record in records)
            or signature != self._record_signature(records)
        ):
            raise GrafxRecoveryRefused(
                "Replay commit boundaries changed during preflight; no effect was applied.",
                field="commit_boundaries",
            )

    @staticmethod
    def _record_signature(
        effects: tuple[WalRecord, ...],
    ) -> tuple[tuple[object, ...], ...]:
        """Snapshot immutable payload identity and every dispatch-relevant scalar field."""
        return tuple(
            (
                id(record),
                record.record_type,
                id(record.payload),
                len(record.payload),
                record.descriptor,
                record.lsn,
                record.epoch,
                record.txn_id,
                record.flags,
                record.format_version,
            )
            for record in effects
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
        allow_commit_catalog: bool = False,
    ) -> tuple[
        tuple[tuple[int, _PreparedPageEffect], ...],
        tuple[tuple[object, ...], ...],
        bool,
    ]:
        """Refuse an incomplete or malformed dispatch plan before the first mutation."""
        if allow_commit_catalog:
            assert self._database_uuid is not None
            CommitCatalogStore(self._pool.storage.read_page, database_uuid=self._database_uuid,
                               page_size=self._pool.page_size)._capture_redo_payloads(effects)
        missing_manager_lsn: Lsn | None = None
        contains_index_reset = False
        simulated_page_counts: dict[str, int] = {}
        prepared_pages: list[tuple[int, _PreparedPageEffect]] = []
        watermark_meta_baselines: dict[tuple[str, int], Page | None] = {}
        signature: list[tuple[object, ...]] = []
        for position, record in enumerate(effects):
            if not isinstance(record, WalRecord):
                raise GrafxRecoveryRefused(
                    f"A committed replay effect must be a WalRecord; got "
                    f"{type(record).__name__}.",
                    field="effects",
                    value=type(record).__name__,
                )
            signature.append(
                (
                    id(record),
                    record.record_type,
                    id(record.payload),
                    len(record.payload),
                    record.descriptor,
                    record.lsn,
                    record.epoch,
                    record.txn_id,
                    record.flags,
                    record.format_version,
                )
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
                write = decode_page_write(
                    record.payload,
                    format_version=record.format_version,
                    flags=record.flags,
                )
                journal = write.file in COMMIT_CATALOG_PAGE_FILES or write.file == SYSTEM_HISTORY_FILE
                if journal and not allow_commit_catalog:
                    raise GrafxRecoveryRefused(
                        "Commit catalog replay is not enabled by this build; no effect was applied.",
                        field="commit_catalog_replay", lsn=record.lsn,
                    )
                if not journal and not is_redoable_page_file(write.file):
                    raise GrafxRecoveryRefused(
                        f"Committed page record {record.lsn} names non-data file "
                        f"{write.file!r}; no effect was applied.",
                        field="file",
                        file=write.file,
                        lsn=record.lsn,
                    )
                decoded = self._validate_page_image(
                    write.file,
                    write.page_index,
                    write.image,
                )
                watermark_scope_known, watermark_table_ids = (
                    self._watermark_scope_for_page(
                        write.file,
                        decoded,
                        meta_baselines=watermark_meta_baselines,
                    )
                )
                prepared_pages.append(
                    (
                        position,
                        _PreparedPageEffect(
                            record=record,
                            file=write.file,
                            page_index=write.page_index,
                            image=write.image,
                            page_lsn=decoded.page_lsn,
                            watermark_scope_known=watermark_scope_known,
                            watermark_table_ids=watermark_table_ids,
                        ),
                    )
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
                simulated_page_counts[write.file] = max(present, write.page_index + 1)
            if record.record_type in _INDEX_EFFECTS:
                change = change_of(record)
                contains_index_reset = (
                    contains_index_reset or change.operation is IndexOperation.RESET
                )
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
                if (getattr(index.definition, "layout", None) is IndexLayout.POSTING_HASH
                        and change.operation is not IndexOperation.RESET
                        and change.ref.page in (0, NO_PAGE)):
                    raise GrafxCorruptionDetected(
                        "Posting WAL references no heap data page; no effect was applied.",
                        field="ref", index=change.index, lsn=record.lsn,
                    )
                if isinstance(max_key_bytes, int) and len(change.key) > max_key_bytes:
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
        return tuple(prepared_pages), tuple(signature), contains_index_reset

    @staticmethod
    def _coalesce_page_plan(
        prepared_pages: tuple[tuple[int, _PreparedPageEffect], ...],
    ) -> tuple[tuple[int, _PreparedPageEffect], ...]:
        """Return a page-only plan with safe repeated locations reduced to their final image.

        A location is reduced only when its embedded page LSN never regresses and equal LSNs
        carry identical bytes.  The final image occupies the first occurrence's position, which
        preserves the validated redo-gap growth order between distinct physical pages.  Any
        ambiguous location retains every original occurrence in order.
        """
        by_location: dict[tuple[str, int], list[tuple[int, _PreparedPageEffect]]] = {}
        for item in prepared_pages:
            prepared = item[1]
            by_location.setdefault((prepared.file, prepared.page_index), []).append(
                item
            )

        replacements: dict[int, _PreparedPageEffect] = {}
        skipped: set[int] = set()
        for occurrences in by_location.values():
            if len(occurrences) < 2:
                continue
            safe = True
            previous = occurrences[0][1]
            for _position, current in occurrences[1:]:
                if current.page_lsn < previous.page_lsn or (
                    current.page_lsn == previous.page_lsn
                    and current.image != previous.image
                ):
                    safe = False
                    break
                previous = current
            if not safe:
                continue
            first_position = occurrences[0][0]
            replacements[first_position] = occurrences[-1][1]
            skipped.update(position for position, _prepared in occurrences[1:])

        return tuple(
            (position, replacements.get(position, prepared))
            for position, prepared in prepared_pages
            if position not in skipped
        )

    def _validate_page_image(self, file: str, page_index: int, image: bytes) -> Page:
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
        # Page indices live in the storage port rather than the encoded page header. The codec
        # therefore cannot recover this location unless its extended keyword is available; redo
        # already owns the authoritative record location and attaches it before any heap-layout
        # classifier can make a decision from the image.
        decoded.page_index = page_index
        return decoded
