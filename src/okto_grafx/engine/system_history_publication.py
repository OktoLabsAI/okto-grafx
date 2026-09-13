"""Native coordinator-owned temporal preparation; no independent write door."""

from __future__ import annotations
from okto_grafx.domain.model.table_selection import TableSelector, validate_table_selections, select_tables

from dataclasses import dataclass
from typing import TYPE_CHECKING
from okto_grafx.domain.txn.context import TransactionContext
from okto_grafx.domain.txn.commit_identity import CommitId
if TYPE_CHECKING:
    from okto_grafx.engine.txn_manager import TransactionManager, _RowWrite

__all__ = ["HistoryPublication"]

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxTransactionBudgetExceeded
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.txn.partitions import page_partition, partition_key
from okto_grafx.engine.catalog_store import read_catalog_page_images
from okto_grafx.engine.system_history_store import (
    HistoryChange, HistoryPageImage, PreparedHistoryAppend, SystemHistoryStore, _encode_changes,
)
from okto_grafx.engine.system_history_retention import PreparedHistoryRetention
from okto_grafx.engine.system_history_index_store import PreparedHistoryIndex, head_root


def _logical_type(catalog, table):
    if table.kind != "rel":
        return None
    name = catalog.relationship_type_name(table.table_id)
    return None if name == table.name else name


@dataclass(frozen=True, slots=True)
class _Activation:
    catalog: bytes
    table_ids: tuple[int, ...]
    baseline: tuple[HistoryChange, ...]
    reads: frozenset[int]
    sequence: int = 1


@dataclass(frozen=True, slots=True)
class _Attempt:
    txn_id: int
    prepared: PreparedHistoryAppend | PreparedHistoryRetention | None
    baseline: tuple[HistoryChange, ...]
    database_uuid: bytes
    page_size: int

    def bind(self, sequence: int) -> tuple[HistoryPageImage, ...]:
        """Regenerate immutable images with fixed cardinality and no storage access."""
        if self.prepared is not None:
            return self.prepared.bind(sequence)
        store = SystemHistoryStore(lambda *_: b"", database_uuid=self.database_uuid, page_size=self.page_size)
        return store.activation_images(self.baseline, sequence)


@dataclass(frozen=True, slots=True)
class _Control:
    catalog: bytes
    reads: frozenset[int]
    retention: PreparedHistoryRetention | None = None
    sequence: int = 1
    index: PreparedHistoryIndex | None = None


class HistoryPublication:
    """State owned by one native transaction manager, cleared with each attempt."""

    def __init__(self, manager: TransactionManager) -> None:
        self.manager = manager
        self.activations: dict[int, _Activation] = {}
        self.controls: dict[int, _Control] = {}
        self.attempt: _Attempt | None = None

    def forget(self, txn_id: int) -> None:
        """Discard private preparation, without modifying persistent history."""
        self.activations.pop(txn_id, None)
        self.controls.pop(txn_id, None)
        if self.attempt is not None and self.attempt.txn_id == txn_id:
            self.attempt = None

    def stage_activation(self, txn: TransactionContext, names: tuple[TableSelector, ...]) -> bool:
        """Capture a bounded baseline under a dedicated, fully partition-fenced DDL transaction."""
        manager = self.manager
        operation = "enable system history"
        manager._require_fresh_index_catalog_transaction(txn, operation=operation, purpose=operation)
        validate_table_selections(names)
        with manager._participant_section():
            manager._require_current_active(txn)
            manager._require_recovery_complete()
            with manager.schema_artifact_section(sync_if=lambda: True):
                source = manager._catalog.catalog
                if source.commit_catalog_activation is None:
                    raise GrafxConfigurationError("Enable commit history first.", field="commit_catalog_activation")
                existing = {key for key, _, _ in source.system_history_tables()}
                tables = select_tables(source, names)
                tables = tuple(table for table in tables if table.table_id not in existing)
                if not tables:
                    return False
                enabled = existing | {table.table_id for table in tables}
                for table in tables:
                    if table.kind == "rel" and any(source.table(name, kind="node").table_id not in enabled
                                                   for name in (table.from_table, table.to_table)):
                        raise GrafxConfigurationError("Enable history for both endpoint tables first or together.", field="tables")
                changes = []
                for table in tables:
                    for partition in range(manager.partitions_per_table):
                        txn.note_read(partition_key(table.table_id, partition))
                    changes.append(HistoryChange(table, 0, 4, (), logical_type=_logical_type(source, table)))
                    for _, version in manager._heap.scan(table, txn.snapshot):
                        changes.append(HistoryChange(table, version.record_id, 1, tuple(version.values),
                                                     logical_type=_logical_type(source, table), node_labels=version.node_labels))
                        if len(changes) > 4096:
                            raise GrafxTransactionBudgetExceeded("History baseline exceeds 4096 changes.", field="history_changes")
                baseline = tuple(changes)
                _encode_changes(baseline)  # Full type/byte preflight before catalog staging.
                ids = tuple(table.table_id for table in tables)
                candidate = source.copy().enable_system_history(ids, 1)
                for index, raw in manager._catalog.stage(candidate):
                    manager._stage_page_image(txn, manager._file_ids.catalog_file, index, raw)
                self.activations[txn.txn_id] = _Activation(candidate.serialize(), ids, baseline, frozenset(txn.read_partitions))
                return True

    def validate_staged(self, txn: TransactionContext) -> None:
        """Refuse caller mutation of the dedicated activation's sealed catalog/effects."""
        activation = self.activations.get(txn.txn_id)
        control = self.controls.get(txn.txn_id)
        if control is not None:
            catalog = Catalog.deserialize(control.catalog)
            if control.retention is not None:
                catalog._retarget_history_revision(1, control.sequence)
            expected = {(self.manager._file_ids.catalog_file, index): raw for index, raw in self.manager._catalog.stage(catalog)}
            if (txn.row_intents or txn.pending_records or txn.page_images != expected
                    or frozenset(txn.read_partitions) != control.reads):
                raise GrafxConfigurationError("History control requires an unchanged dedicated transaction.", field="system_history_control")
            return
        if activation is None:
            return
        manager = self.manager
        catalog = Catalog.deserialize(activation.catalog)
        catalog._retarget_system_history(activation.table_ids, 1, activation.sequence)
        expected = {(manager._file_ids.catalog_file, index): raw for index, raw in manager._catalog.stage(catalog)}
        if (txn.row_intents or txn.pending_records or txn.page_images != expected
                or frozenset(txn.read_partitions) != activation.reads):
            raise GrafxConfigurationError("System history activation requires an unchanged dedicated transaction.", field="system_history_activation")

    def rebind_activation(self, txn: TransactionContext, sequence: int) -> bool:
        """Retarget only the explicitly owned unpublished table activation coordinates."""
        activation = self.activations.get(txn.txn_id)
        control = self.controls.get(txn.txn_id)
        if control is not None and control.retention is not None:
            from dataclasses import replace
            catalog = Catalog.deserialize(control.catalog)
            catalog._retarget_history_revision(1, sequence)
            images = self.manager._catalog.stage(catalog)
            if {(self.manager._file_ids.catalog_file, index) for index, _ in images} != set(txn.page_images):
                raise GrafxConfigurationError("Retention changed catalog cardinality.", field="system_history_control")
            for index, raw in images:
                self.manager._stage_page_image(txn, self.manager._file_ids.catalog_file, index, raw)
            self.controls[txn.txn_id] = replace(control, sequence=sequence)
            return True
        if activation is None:
            return False
        from dataclasses import replace
        manager = self.manager
        catalog = Catalog.deserialize(activation.catalog)
        catalog._retarget_system_history(activation.table_ids, 1, sequence)
        images = manager._catalog.stage(catalog)
        expected = {(manager._file_ids.catalog_file, index) for index, _ in images}
        if expected != {key for key in txn.page_images if key[0] == manager._file_ids.catalog_file}:
            raise GrafxConfigurationError("History activation changed catalog cardinality.", field="system_history_activation")
        for index, raw in images:
            manager._stage_page_image(txn, manager._file_ids.catalog_file, index, raw)
        self.activations[txn.txn_id] = replace(activation, sequence=sequence)
        return True

    def prepare(self, txn: TransactionContext, current: int, rows: tuple[_RowWrite, ...] = ()) -> frozenset[tuple[str, int]]:
        """Capture settled native row/schema effects before the second physical OCC."""
        self.attempt = None
        manager = self.manager
        activation = self.activations.get(txn.txn_id)
        if not manager._commit_catalog_capable and activation is None:
            return frozenset()  # No extra catalog observation in the legacy publication path.
        source = getattr(manager._catalog, "catalog", None)
        if not isinstance(source, Catalog):
            return frozenset()
        if not source.system_history_tables() and activation is None:
            return frozenset()
        active = {key for key, _, _ in source.system_history_tables()}
        changes = list(activation.baseline if activation is not None else ())
        effective = source
        if activation is None:
            catalog_images = tuple((index, raw) for (file, index), raw in txn.page_images.items()
                                   if file == manager._file_ids.catalog_file)
            if catalog_images:
                # Staged page stamps are provisional; normal catalog decoding owns framing.
                from okto_grafx.domain.page import Page
                normalized = []
                for index, raw in catalog_images:
                    page = Page.from_bytes(raw)
                    page.page_lsn = current
                    normalized.append((index, page.to_bytes()))
                after = read_catalog_page_images(tuple(normalized), page_size=manager._pool.page_size,
                                                  sequence=current)
                effective = after
                for key in sorted(active):
                    table = after.table_by_id(key)
                    if table != source.table_by_id(key):
                        changes.append(HistoryChange(table, 0, 4, (), logical_type=_logical_type(after, table)))
        for row in rows:
            if row.table.table_id in active:
                operation = 3 if row.born is None else 1 if row.ended is None else 2
                table = effective.table_by_id(row.table.table_id)
                changes.append(HistoryChange(table, row.record_id, operation,
                                             tuple(row.born_values) if row.born is not None else (),
                                             logical_type=_logical_type(effective, table),
                                             node_labels=row.born_node_labels if row.born is not None else None))
        changes = tuple(changes)
        pool = manager._pool

        def read(file: str, index: int) -> bytes:
            """Capture fresh bytes only under the caller's current publication authority."""
            return pool.codec.encode_page(pool.read_fresh_page(file, index))

        store = SystemHistoryStore(read, database_uuid=manager._database_uuid, page_size=pool.page_size)
        first = not source.system_history_tables()
        control = self.controls.get(txn.txn_id)
        if control is not None and control.retention is not None:
            prepared = control.retention
            if prepared.previous_sequence != current:
                raise GrafxConfigurationError("Retention's original picture changed.", field="system_history_retention")
        else:
            index = None if control is None else control.index
            if "system_history_index_v1" in source.required_capabilities() or index is not None:
                from okto_grafx.engine.system_history_store import _HEAD
                root = head_root(store._page(0).read_slot(0), _HEAD.size)
                if index is None:
                    if root is None:
                        raise GrafxConfigurationError("Enabled temporal index has no native root.", field="system_history_index")
                    index = PreparedHistoryIndex(read, root)
                captured = {}
                original = index

                def capture(file: str, number: int) -> bytes:
                    """Capture the immutable predecessor paths once, before second OCC."""
                    if (file, number) not in captured:
                        captured[file, number] = original.read(file, number)
                    return captured[file, number]

                from dataclasses import replace
                index = replace(index, read=capture)
                preview = store.prepare(changes, expected_sequence=current,
                    page_count=pool.storage.page_count("system-history.dat"), index=index)
                preview.bind(current + 1)
                index = replace(index, read=lambda file, number: captured[file, number])
            prepared = None if first else store.prepare(changes, expected_sequence=current,
                page_count=pool.storage.page_count("system-history.dat"), index=index)
        attempt = _Attempt(txn.txn_id, prepared, changes, manager._database_uuid, pool.page_size)
        images = attempt.bind(current + 1)
        locations = frozenset((image.file, image.page_index) for image in images)
        limit = txn._max_transaction_bytes
        if limit is not None:
            txn.validate_budgets()
            journal = manager._journal_attempt
            journal_count = 0 if journal is None else journal[1].image_count + len(journal[2])
            observed = txn._staged_payload_bytes + (len(locations) + journal_count) * pool.page_size
            if observed > limit:
                raise GrafxTransactionBudgetExceeded("History and journal images exceed transaction quota.",
                                                     field="max_transaction_bytes", limit=limit, observed=observed)
        self.attempt = attempt
        for file, index in locations:
            txn.note_write(page_partition(file, index))
        return locations

    def images(self, txn_id: int, sequence: int) -> dict[tuple[str, int], bytes]:
        """Return the exact owned attempt images, never caller-supplied storage effects."""
        attempt = self.attempt
        if attempt is None or attempt.txn_id != txn_id:
            return {}
        return {(image.file, image.page_index): image.raw for image in attempt.bind(sequence)}

    def stage_index(self, txn: TransactionContext, *, max_bytes: int) -> bool:
        """Build an optional temporal access baseline in a sealed dedicated transaction."""
        manager = self.manager
        operation = "enable system history index"
        manager._require_fresh_index_catalog_transaction(txn, operation=operation, purpose=operation)
        with manager._participant_section():
            manager._require_current_active(txn)
            manager._require_recovery_complete()
            with manager.schema_artifact_section(sync_if=lambda: True):
                source = manager._catalog.catalog
                if "system_history_index_v1" in source.required_capabilities():
                    return False
                if not source.system_history_tables():
                    raise GrafxConfigurationError("Enable system history first.", field="system_history")
                pool = manager._pool
                if pool.storage.file_size("system-history.dat") > max_bytes:
                    raise GrafxTransactionBudgetExceeded("Index baseline exceeds capture budget.", field="max_bytes")
                txn.note_read(page_partition("system-history.dat", 0))
                store = SystemHistoryStore(lambda file, number: pool.codec.encode_page(pool.read_fresh_page(file, number)),
                    database_uuid=manager._database_uuid, page_size=pool.page_size)
                from okto_grafx.engine.system_history_reader import fold_history
                from okto_grafx.domain.temporal import TemporalLimits
                sequence = txn.snapshot.read_lsn
                count = pool.storage.page_count("system-history.dat")
                fold_history(store, expected_sequence=sequence, page_count=count,
                    target=CommitId(manager._database_uuid, sequence),
                    table_ids=tuple(key for key, _, _ in source.system_history_tables()),
                    retention_horizons={key: floor for key, _, floor in source.system_history_tables()},
                    limits=TemporalLimits(max_bytes=max_bytes), catalog=source)
                baseline = tuple((identity.sequence, changes) for identity, changes, _ in
                    store._iter_batches(expected_sequence=sequence, page_count=count))
                candidate = source.copy()
                candidate._enable_system_history_index()
                candidate.serialize()
                for number, raw in manager._catalog.stage(candidate):
                    manager._stage_page_image(txn, manager._file_ids.catalog_file, number, raw)
                self.controls[txn.txn_id] = _Control(candidate.serialize(), frozenset(txn.read_partitions),
                    index=PreparedHistoryIndex(lambda *_: b"", baseline=baseline))
                return True

    def stage_compaction(self, txn: TransactionContext, *, max_bytes: int) -> PreparedHistoryRetention | None:
        """Capture a quiescent full replacement with unchanged logical retention policy."""
        manager = self.manager
        operation = "compact system history"
        manager._require_fresh_index_catalog_transaction(txn, operation=operation, purpose=operation)
        with manager._participant_section():
            manager._require_current_active(txn)
            manager._require_recovery_complete()
            with manager.schema_artifact_section(sync_if=lambda: True):
                from dataclasses import replace
                from okto_grafx.engine.system_history_reader import fold_history
                from okto_grafx.engine.system_history_retention import prepare_retention
                from okto_grafx.domain.temporal import TemporalLimits
                source = manager._catalog.catalog
                if not source.system_history_tables():
                    raise GrafxConfigurationError("Enable history first.", field="system_history")
                pool = manager._pool
                original = pool.storage.file_size("system-history.dat")
                if original > max_bytes:
                    raise GrafxTransactionBudgetExceeded("Compaction capture exceeds byte budget.", field="max_bytes")
                sequence = txn.snapshot.read_lsn
                store = SystemHistoryStore(lambda file, number: pool.codec.encode_page(pool.read_fresh_page(file, number)),
                    database_uuid=manager._database_uuid, page_size=pool.page_size)
                count = pool.storage.page_count("system-history.dat")
                fold_history(store, expected_sequence=sequence, page_count=count,
                    target=CommitId(manager._database_uuid, sequence),
                    table_ids=tuple(key for key, _, _ in source.system_history_tables()),
                    retention_horizons={key: floor for key, _, floor in source.system_history_tables()},
                    limits=TemporalLimits(max_bytes=max_bytes), catalog=source)
                plan = prepare_retention(store, sequence=sequence, page_count=count, tables=(), before=0)
                plan = replace(plan, compact=True, batches=tuple((identity, tuple(
                    replace(change, redacted_bytes=0) if change.operation in (5, 6) else change for change in changes))
                    for identity, changes in plan.batches))
                if len(plan.bind(sequence + 1)) * pool.page_size >= original:
                    return None
                txn.note_read(page_partition("system-history.dat", 0))
                candidate = source.copy()
                candidate._stage_history_compaction()
                for number, raw in manager._catalog.stage(candidate):
                    manager._stage_page_image(txn, manager._file_ids.catalog_file, number, raw)
                self.controls[txn.txn_id] = _Control(candidate.serialize(), frozenset(txn.read_partitions), plan)
                return plan

    def stage_control(self, txn: TransactionContext, *, operation: str, names: tuple[TableSelector, ...] = (),
                      before: CommitId | None = None, pin_name: str | None = None,
                      max_bytes: int = 16 * 1024 * 1024) -> PreparedHistoryRetention | None:
        """Stage durable pin/retention metadata and a bounded, native-owned redaction plan."""
        manager = self.manager
        manager._require_fresh_index_catalog_transaction(txn, operation=operation, purpose=operation)
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2**31:
            raise GrafxConfigurationError("Invalid retention capture budget.", field="max_bytes")
        with manager._participant_section():
            manager._require_current_active(txn)
            manager._require_recovery_complete()
            with manager.schema_artifact_section(sync_if=lambda: True):
                source = manager._catalog.catalog
                if not source.system_history_tables():
                    raise GrafxConfigurationError("Enable system history first.", field="system_history")
                candidate = source.copy()
                validate_table_selections(names, allow_empty=True)
                selected = tuple(sorted(table.table_id for table in select_tables(source, names)))
                retention = None
                if operation == "unpin system history":
                    changed = candidate.remove_system_history_pin(pin_name)
                else:
                    from okto_grafx.domain.txn.commit_identity import CommitId
                    if type(before) is not CommitId or before.database_uuid != manager._database_uuid:
                        raise GrafxConfigurationError("Expected a same-store CommitId.", field="before")
                    if before.sequence > txn.snapshot.read_lsn:
                        raise GrafxConfigurationError("History coordinate exceeds snapshot.", field="before")
                    from okto_grafx.engine.commit_catalog_store import CommitCatalogStore
                    pool = manager._pool

                    def read(file: str, index: int) -> bytes:
                        """Read through the existing stable native page boundary."""
                        return pool.codec.encode_page(pool.read_fresh_page(file, index))

                    journal = CommitCatalogStore(read, database_uuid=manager._database_uuid, page_size=pool.page_size)
                    if journal.lookup(before, read_lsn=txn.snapshot.read_lsn) is None:
                        raise GrafxConfigurationError("History coordinate is not a tracked COMMIT.", field="before")
                    if operation == "pin system history":
                        changed = candidate.set_system_history_pin(pin_name, before.sequence, selected)
                    elif operation == "prune system history":
                        changed = candidate.advance_system_history(selected, before.sequence, 1)
                        if changed:
                            size = pool.storage.file_size("system-history.dat")
                            if size > max_bytes:
                                raise GrafxTransactionBudgetExceeded("Retention capture exceeds byte budget.", field="max_bytes",
                                                                     limit=max_bytes, observed=size)
                            txn.note_read(page_partition("system-history.dat", 0))
                            from okto_grafx.engine.system_history_retention import prepare_retention
                            from okto_grafx.engine.system_history_reader import fold_history
                            from okto_grafx.domain.temporal import TemporalLimits
                            store = SystemHistoryStore(read, database_uuid=manager._database_uuid, page_size=pool.page_size)
                            fold_history(store, expected_sequence=txn.snapshot.read_lsn,
                                page_count=pool.storage.page_count("system-history.dat"),
                                target=CommitId(manager._database_uuid, txn.snapshot.read_lsn),
                                table_ids=tuple(key for key, _, _ in source.system_history_tables()),
                                retention_horizons={key: floor for key, _, floor in source.system_history_tables()},
                                limits=TemporalLimits(max_bytes=max_bytes), catalog=source)
                            retention = prepare_retention(store, sequence=txn.snapshot.read_lsn,
                                page_count=pool.storage.page_count("system-history.dat"), tables=selected, before=before.sequence)
                    else:
                        raise GrafxConfigurationError("Unknown history control operation.", field="operation")
                if not changed:
                    return None
                for index, raw in manager._catalog.stage(candidate):
                    manager._stage_page_image(txn, manager._file_ids.catalog_file, index, raw)
                self.controls[txn.txn_id] = _Control(candidate.serialize(), frozenset(txn.read_partitions), retention)
                return retention
