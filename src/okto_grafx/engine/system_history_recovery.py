"""Complete temporal-image preflight under the native replay coordinator's authority."""

from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from okto_grafx.engine.buffer_pool import BufferPool
    from okto_grafx.engine.commit_redo import _PreparedPageEffect
    from okto_grafx.domain.recovery.decision import CommittedReplay
    from okto_grafx.domain.model.catalog import Catalog

__all__ = ["validate_system_history"]

from okto_grafx.domain.errors import GrafxRecoveryRefused
from okto_grafx.domain.page import Page, PageType
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.engine.system_history_store import (
    HistoryPageImage, SystemHistoryStore, _BLOCK, _BLOCK_MAGIC, _FILE, _HEAD, _HEAD_MAGIC,
    _MAX_BATCH, _decode_changes, _image,
)


def _refuse(field: str):
    return GrafxRecoveryRefused("System history replay has incomplete or conflicting authority.", field=field)


def _canonical(raw: bytes) -> bytes:
    page = Page.from_bytes(raw)
    if page.seq % 2:
        raise _refuse("system_history_seqlock")
    page.seq = 0
    return page.to_bytes()


def validate_system_history(pool: BufferPool, replay: CommittedReplay,
                            prepared_pages: tuple[tuple[int, _PreparedPageEffect], ...], *,
                            checkpoint_lsn: int | None, database_uuid: bytes,
                            native_catalogs: tuple[tuple[int | None, Catalog], ...]) -> None:
    """Validate every temporal transition/target before ANY native effect is applied.

    Native COMMIT selection and WAL decoding precede this function. Input values
    are not an authority cache; all captured images and roots live for this one
    preflight only. An interrupted apply can use exact supplied after-images, not
    a resident page's greater LSN as permission to skip validation.
    """
    if checkpoint_lsn is None:
        return
    storage = pool.storage
    history_groups = {}
    for _, item in prepared_pages:
        owner = (item.record.epoch, item.record.txn_id)
        if item.file == _FILE:
            history_groups.setdefault(owner, []).append(HistoryPageImage(_FILE, item.page_index, item.image))
    # These exact values were decoded by this passage's native catalog validator.
    # Do not re-read schema pages or retain them as cross-operation authority.
    schemas = [(sequence, catalog) for sequence, catalog in native_catalogs if sequence is not None]
    catalog = native_catalogs[-1][1] if native_catalogs else None
    enabled = () if catalog is None else catalog.system_history_tables()
    if not enabled:
        if history_groups or storage.exists(_FILE):
            raise _refuse("system_history_activation")
        return
    if database_uuid is None:
        raise _refuse("system_history_identity")
    activation = min(item[1] for item in enabled)
    terminals = {record.lsn: record for record in replay.commit_records}
    if activation > checkpoint_lsn and activation not in terminals:
        raise _refuse("system_history_activation")
    prior_map = None
    prior_revision = None
    schema_by_sequence = dict(schemas)
    for sequence, snapshot in schemas:
        mapping = {key: (start, horizon) for key, start, horizon in snapshot.system_history_tables()}
        if any(start > sequence or horizon > sequence for start, horizon in mapping.values()):
            raise _refuse("system_history_horizon")
        if prior_map is not None and any(key not in mapping or mapping[key][0] != value[0] or mapping[key][1] < value[1]
                                         for key, value in prior_map.items()):
            raise _refuse("system_history_horizon")
        revision = snapshot.system_history_revision
        if revision > sequence or prior_revision is not None and revision < prior_revision:
            raise _refuse("system_history_revision")
        if prior_map is not None and any(mapping[key][1] > value[1] for key, value in prior_map.items()) and revision != sequence:
            raise _refuse("system_history_revision")
        if any(start > checkpoint_lsn and start != sequence and (prior_map is None or key not in prior_map)
               for key, (start, _) in mapping.items()):
            raise _refuse("system_history_activation")
        prior_map = mapping
        prior_revision = revision
    revision = catalog.system_history_revision
    if revision > checkpoint_lsn and (revision not in schema_by_sequence or revision not in terminals):
        raise _refuse("system_history_revision")
    final = replay.last_committed_lsn if replay.commit_records else checkpoint_lsn
    store = SystemHistoryStore(storage.read_page, database_uuid=database_uuid, page_size=pool.page_size)
    if not replay.commit_records:
        if not storage.exists(_FILE) or storage.file_size(_FILE) % pool.page_size:
            raise _refuse("system_history_extent")
        head = store._head(final, storage.page_count(_FILE))
        if head[0] != activation:
            raise _refuse("system_history_activation")
        return
    previous = checkpoint_lsn
    previous_root = None
    expected_by_page = {}
    rewritten = set()
    final_extent = 0
    for terminal in replay.commit_records:
        images = tuple(history_groups.get((terminal.epoch, terminal.txn_id), ()))
        if terminal.lsn < activation:
            if images:
                raise _refuse("system_history_early_effect")
            previous = terminal.lsn
            continue
        if not images or len({image.page_index for image in images}) != len(images):
            raise _refuse("system_history_commit_coverage")
        roots = [image for image in images if image.page_index == 0]
        chunks = sorted((image for image in images if image.page_index != 0), key=lambda image: image.page_index)
        if len(roots) != 1 or not chunks:
            raise _refuse("system_history_commit_coverage")
        schema = schema_by_sequence.get(terminal.lsn)
        rewrite = schema is not None and schema.system_history_revision == terminal.lsn
        if rewrite:
            incoming = {image.page_index: image.raw for image in images}
            if set(incoming) != set(range(len(incoming))):
                raise _refuse("system_history_rewrite_extent")
            after = SystemHistoryStore(lambda _file, index: incoming[index], database_uuid=database_uuid, page_size=pool.page_size)
            head = after._head(terminal.lsn, len(incoming))
            if head[0] != activation or any(after._page(index).page_lsn != terminal.lsn for index in incoming):
                raise _refuse("system_history_rewrite_stamps")
            from okto_grafx.domain.temporal import TemporalLimits
            from okto_grafx.engine.system_history_reader import fold_history
            fold_history(after, expected_sequence=terminal.lsn, page_count=len(incoming),
                         target=CommitId(database_uuid, terminal.lsn),
                         table_ids=tuple(key for key, _, _ in schema.system_history_tables()),
                         retention_horizons={key: floor for key, _, floor in schema.system_history_tables()},
                         limits=TemporalLimits(max_events=10_000_000, max_bytes=2**31, max_rows=1_000_000))
            for identity, _, _ in after._iter_batches(expected_sequence=terminal.lsn, page_count=len(incoming)):
                if identity.sequence > checkpoint_lsn and identity.sequence not in terminals:
                    raise _refuse("system_history_rewrite_lineage")
            previous_root = roots[0].raw
            final_extent = len(incoming)
            for image in images:
                expected_by_page.setdefault(image.page_index, {})[terminal.lsn] = image.raw
                rewritten.add(image.page_index)
            previous = terminal.lsn
            continue
        first = Page.from_bytes(chunks[0].raw).read_slot(0)
        if len(first) <= _BLOCK.size:
            raise _refuse("system_history_chunk")
        magic, identity, sequence, ordinal, position, count, size, digest, _ = _BLOCK.unpack_from(first)
        if magic != _BLOCK_MAGIC or identity != database_uuid or sequence != terminal.lsn or position != 0:
            raise _refuse("system_history_lineage")
        capacity = pool.page_size - 32 - 4 - _BLOCK.size
        if not 4 <= size <= _MAX_BATCH or count != (size + capacity - 1) // capacity or count != len(chunks):
            raise _refuse("system_history_batch_bound")
        if terminal.lsn == activation:
            if chunks[0].page_index != 1 or ordinal != 0:
                raise _refuse("system_history_activation")
            payload = b"".join(Page.from_bytes(image.raw).read_slot(0)[_BLOCK.size:] for image in chunks)
            changes = _decode_changes(payload)
            expected = store.activation_images(changes, terminal.lsn)
            if {image.page_index: image.raw for image in expected} != {image.page_index: image.raw for image in images}:
                raise _refuse("system_history_activation_images")
        else:
            root = _image(pool.page_size, 0, previous, _HEAD.pack(
                _HEAD_MAGIC, database_uuid, activation, previous, ordinal, chunks[0].page_index, digest)).raw
            if previous_root is not None:
                if root != previous_root:
                    raise _refuse("system_history_transition_chain")
            else:
                # The overwritten root is reconstructed only from a proved COMMIT
                # plus its append's predecessor descriptor. If resident root is not
                # that exact predecessor, validate the complete untouched prefix.
                try:
                    resident = storage.read_page(_FILE, 0)
                except (OSError, KeyError):
                    resident = None
                superseded = any(item_sequence >= terminal.lsn and item.system_history_revision == item_sequence
                                 for item_sequence, item in schemas)
                if (resident is None or _canonical(resident) != root) and not superseded:
                    def prefix_read(file: str, index: int) -> bytes:
                        """Recover only the predecessor root; old chunks are never inferred."""
                        return root if index == 0 else storage.read_page(file, index)

                    prefix = SystemHistoryStore(prefix_read, database_uuid=database_uuid, page_size=pool.page_size)
                    for _ in prefix._iter_batches(expected_sequence=previous, page_count=chunks[0].page_index):
                        pass  # Full chain proof, bounded one-batch memory, no retained-size ceiling.
                # A later native retention COMMIT replaces the complete prefix.
                # Its exact full image and logical lineage are proved below,
                # before this preflight permits application of ANY page. Reading
                # resident old chunks here would mix pre/post-redaction digests.
            transition = SystemHistoryStore(lambda _file, _index: root, database_uuid=database_uuid, page_size=pool.page_size)
            transition.validate_append_images(images, previous_sequence=previous, page_count=chunks[0].page_index,
                                              commit=CommitId(database_uuid, terminal.lsn))
        previous_root = roots[0].raw
        final_extent = _HEAD.unpack(Page.from_bytes(previous_root).read_slot(0))[5]
        for image in images:
            expected_by_page.setdefault(image.page_index, {})[terminal.lsn] = image.raw
        previous = terminal.lsn
    if previous_root is None:
        raise _refuse("system_history_commit_coverage")
    if storage.exists(_FILE):
        length = storage.file_size(_FILE)
        if length % pool.page_size or length // pool.page_size > final_extent:
            raise _refuse("system_history_extent")
    for index, versions in expected_by_page.items():
        candidates = []
        if storage.exists(_FILE) and index < storage.page_count(_FILE):
            candidates.append(storage.read_page(_FILE, index))
        resident = pool._resident_page_image(_FILE, index)
        if resident is not None:
            candidates.append(resident)
        for raw in candidates:
            if not any(raw):
                continue  # An allocated, not-yet-installed page has no authority.
            page = Page.from_bytes(raw)
            if page.page_type == PageType.FREE:
                if _canonical(raw) != Page(PageType.FREE, page_size=pool.page_size).to_bytes():
                    raise _refuse("system_history_free_page")
                continue
            payload = page.read_slot(0)
            if len(payload) < 24 or payload[8:24] != database_uuid:
                raise _refuse("system_history_target_identity")
            if page.page_lsn > checkpoint_lsn:
                if versions.get(page.page_lsn) != _canonical(raw):
                    raise _refuse("system_history_target_lsn")
            elif index != 0 and index not in rewritten:
                raise _refuse("system_history_old_chunk_overwrite")
