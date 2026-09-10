"""Bounded logical temporal folding over fully verified immutable history batches."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.txn.context import TransactionContext
if TYPE_CHECKING:
    from okto_grafx.engine.database import Database
    from okto_grafx.engine.commit_catalog_store import CommitCatalogStore

__all__ = ["read_system_history", "fold_history"]

from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxHistoryExpired,
    GrafxHistoryUnavailable, GrafxQueryBudgetExceeded, GrafxUnsupportedOperation,
)
from okto_grafx.domain.index.keys import index_key
from okto_grafx.domain.model import Timestamp
from okto_grafx.domain.temporal import TemporalGraph, TemporalLimits, TemporalVersion, TemporalVersions
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.engine.system_history_store import SystemHistoryStore


def _corrupt(field):
    return GrafxCorruptionDetected("Temporal lineage or interval invariants failed.", field=field, component="system_history")


def _inputs(database, at, tables, limits, record_id):
    if type(limits) is not TemporalLimits:
        raise GrafxConfigurationError("Expected TemporalLimits.", field="limits")
    if type(tables) is not tuple or not tables or any(type(name) is not str or not name for name in tables) or len(set(tables)) != len(tables):
        raise GrafxConfigurationError("Specify distinct temporal table names.", field="tables")
    if record_id is not None and (type(record_id) is not int or not 0 < record_id < 2**64):
        raise GrafxConfigurationError("Expected a positive record identity.", field="record_id")
    if type(at) is CommitId:
        return database._history_identity(at)
    if type(at) is Timestamp:
        return Timestamp(at.micros)
    raise GrafxConfigurationError("Expected CommitId or Timestamp.", field="at")


def read_system_history(database: Database, context: TransactionContext, *,
                        at: CommitId | Timestamp, tables: tuple[str, ...],
                        limits: TemporalLimits, record_id: int | None = None) -> TemporalGraph | TemporalVersions:
    """Read a complete qualified historical picture inside an existing public operation."""
    at = _inputs(database, at, tables, limits, record_id)
    if type(at) is CommitId and at.sequence > context.snapshot.read_lsn:
        raise GrafxConfigurationError("Temporal coordinate exceeds the owning snapshot.", field="at")

    def collect(journal: CommitCatalogStore) -> TemporalGraph | TemporalVersions:
        """Fold only a publication whose journal and temporal roots agree exactly."""
        boundary = journal.read_head().last_sequence
        entry = (journal.resolve_time(at, read_lsn=context.snapshot.read_lsn) if type(at) is Timestamp
                 else journal.lookup(at, read_lsn=context.snapshot.read_lsn))
        if entry is None:
            raise GrafxHistoryUnavailable("No tracked commit at the requested coordinate.", field="at")
        target = entry.identity
        catalog = database._catalog.catalog
        selected = tuple(catalog.table(name) for name in tables)
        policy = {key: (start, horizon) for key, start, horizon in catalog.system_history_tables()}
        for table in selected:
            start, horizon = policy.get(table.table_id, (0, 0))
            if not start or target.sequence < start:
                raise GrafxHistoryUnavailable("Table history was not active at the requested commit.", table=table.name,
                                              activation=start)
            if target.sequence < horizon:
                raise GrafxHistoryExpired("Requested commit precedes retained history.", table=table.name, retained_from=horizon)
            if record_id is None and table.kind == "rel" and any(name not in tables for name in (table.from_table, table.to_table)):
                raise GrafxConfigurationError("Historical graphs require selected endpoint tables.", field="tables")

        def read(file: str, index: int) -> bytes:
            """Read through the native participant's current, verified page boundary."""
            raw = database._pool.codec.encode_page(database._pool.read_fresh_page(file, index))
            from okto_grafx.domain.page import PageHeader
            if PageHeader.decode(raw).page_lsn > boundary:
                raise GrafxUnsupportedOperation("History publication is moving; retry the read.", retryable=True,
                                                reason="publication_in_progress", field="system_history_observation")
            return raw

        store = SystemHistoryStore(read, database_uuid=database.identity.database_uuid, page_size=database._pool.page_size)
        length = database._storage.file_size("system-history.dat")
        if length % database._pool.page_size:
            raise _corrupt("history_extent")
        graph, versions = fold_history(store, expected_sequence=boundary, page_count=length // database._pool.page_size,
                                       target=target, table_ids=tuple(table.table_id for table in selected),
                                       limits=limits, record_id=record_id,
                                       retention_horizons={key: value[1] for key, value in policy.items()})
        if record_id is None:
            return graph
        start, horizon = policy[selected[0].table_id]
        return TemporalVersions(target, selected[0].name, record_id, versions,
                                CommitId(target.database_uuid, start), CommitId(target.database_uuid, horizon))

    return database._observe_history(context, collect, None)


def fold_history(store: SystemHistoryStore, *, expected_sequence: int, page_count: int,
                 target: CommitId, table_ids: tuple[int, ...], limits: TemporalLimits, record_id: int | None = None,
                 retention_horizons: dict[int, int] | None = None) -> tuple[TemporalGraph, tuple[TemporalVersion, ...]]:
    """Validate settled intervals/endpoint lineage, capture the requested picture, then finish chain verification.

    Work is linear in encoded retained events plus affected incident relationships,
    not a rescan of the entire graph after every batch. All result construction is
    private until the chain has been completely consumed and verified.
    """
    live = {}
    schemas = {}
    names = {}
    identities = set()
    primary = {}
    incident = {}
    redacted_live = set()
    horizons = {} if retention_horizons is None else retention_horizons
    output_versions = []
    snapshot_rows = snapshot_schemas = None
    events = consumed = 0

    def row_key(table: TableDef, values: tuple[object, ...]) -> tuple[int, bytes] | None:
        """Use the native exact-index value grammar for primary-key uniqueness."""
        if table.primary_key is None:
            return None
        return table.table_id, index_key(values, (table.column_index(table.primary_key),))

    def endpoints(table: TableDef, values: tuple[object, ...]) -> tuple[tuple[int, object], tuple[int, object]]:
        """Resolve endpoint table IDs through the historical schema, never a recreated PK."""
        if table.from_table not in names or table.to_table not in names:
            raise _corrupt("endpoint_schema")
        return ((names[table.from_table], values[0]), (names[table.to_table], values[1]))

    for identity, changes, size in store._iter_batches(expected_sequence=expected_sequence, page_count=page_count):
        consumed += size
        events += len(changes)
        if events > limits.max_events or consumed > limits.max_bytes:
            raise GrafxQueryBudgetExceeded("Temporal scan budget exhausted; no partial result returned.", resource="history_scan")
        if identity.sequence > target.sequence and snapshot_rows is None:
            snapshot_rows, snapshot_schemas = dict(live), dict(schemas)
        affected_edges = set()
        for change in changes:
            if change.operation != 4:
                continue
            previous = schemas.get(change.table.table_id)
            table = change.table
            if table.name in names and names[table.name] != table.table_id:
                raise _corrupt("schema_identity")
            if previous is not None:
                if (table.name != previous.name or table.kind != previous.kind or table.primary_key != previous.primary_key
                        or table.from_table != previous.from_table or table.to_table != previous.to_table
                        or table.schema_version != previous.schema_version + 1
                        or table.columns[:len(previous.columns)] != previous.columns
                        or any(not column.nullable for column in table.columns[len(previous.columns):])):
                    raise _corrupt("schema_evolution")
            schemas[table.table_id] = table
            names[table.name] = table.table_id
        # A COMMIT is a settled set, not an order of individually visible row
        # mutations: release all ended PKs before admitting any new values.
        for change in changes:
            if change.operation not in (2, 3, 6):
                continue
            old = live.get((change.table.table_id, change.record_id))
            if old is not None and (change.table.table_id, change.record_id) not in redacted_live:
                pk = row_key(change.table, old.values)
                if pk is not None:
                    primary.pop(pk, None)
        for change in changes:
            if change.operation == 4:
                continue
            key = (change.table.table_id, change.record_id)
            table = schemas.get(key[0])
            if table != change.table:
                raise _corrupt("event_schema")
            old = live.get(key)
            if change.operation in (1, 5):
                if key in identities:
                    raise _corrupt("lineage_reuse")
                identities.add(key)
            elif old is None:
                raise _corrupt("interval_gap")
            if old is not None:
                was_redacted = key in redacted_live
                if was_redacted and identity.sequence > horizons.get(key[0], 0):
                    raise _corrupt("redacted_retained_version")
                if table.kind == "rel" and not was_redacted:
                    for endpoint in endpoints(table, old.values):
                        incident.get(endpoint, set()).discard(key)
                if (not was_redacted and horizons.get(key[0], 0) < identity.sequence <= target.sequence
                        and record_id is not None and key == (table_ids[0], record_id)):
                    output_versions.append(replace(old, system_to=identity))
                redacted_live.discard(key)
                del live[key]
            if table.kind == "node":
                affected_edges.update(incident.get(key, ()))
            if change.operation != 3:
                redacted = change.operation in (5, 6)
                pk = None if redacted else row_key(table, change.values)
                if pk is not None:
                    if pk in primary:
                        raise _corrupt("primary_key_overlap")
                    primary[pk] = key
                row = TemporalVersion(table.name, table.table_id, change.record_id, change.values,
                                      table.schema_version, identity)
                live[key] = row
                if redacted:
                    redacted_live.add(key)
                if table.kind == "rel" and not redacted:
                    affected_edges.add(key)
                    for endpoint in endpoints(table, row.values):
                        incident.setdefault(endpoint, set()).add(key)
            if len(live) + len(output_versions) > limits.max_rows:
                raise GrafxQueryBudgetExceeded("Temporal retained-row budget exhausted.", resource="history_rows")
        for key in affected_edges:
            row = live.get(key)
            if row is not None and key not in redacted_live and any(endpoint not in live for endpoint in endpoints(schemas[key[0]], row.values)):
                raise _corrupt("historical_endpoint")
    if redacted_live:
        raise _corrupt("redacted_current_version")
    if snapshot_rows is None:
        snapshot_rows, snapshot_schemas = live, schemas
    selected_schemas = tuple(snapshot_schemas[key] for key in table_ids if key in snapshot_schemas)
    if len(selected_schemas) != len(table_ids):
        raise _corrupt("missing_historical_schema")
    selected = []
    selected_ids = frozenset(table_ids)
    for key, row in sorted(snapshot_rows.items()):
        if key[0] not in selected_ids:
            continue
        table = snapshot_schemas[key[0]]
        if len(row.values) < len(table.columns):
            row = replace(row, values=(*row.values, *((None,) * (len(table.columns) - len(row.values)))),
                          schema_version=table.schema_version)
        selected.append(row)
    if record_id is not None:
        current = snapshot_rows.get((table_ids[0], record_id))
        if current is not None:
            output_versions.append(current)
    return TemporalGraph(target, selected_schemas, tuple(selected), events, consumed), tuple(output_versions)
