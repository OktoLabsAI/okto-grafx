"""Qualified, budgeted temporal reads using the immutable native access tree."""

from __future__ import annotations

from dataclasses import replace

from okto_grafx.domain.errors import GrafxQueryBudgetExceeded
from okto_grafx.domain.index.keys import index_key
from okto_grafx.domain.temporal import TemporalGraph, TemporalLimits, TemporalVersion
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.engine.system_history_index import HistoryAccessTree
from okto_grafx.engine.system_history_store import HistoryChange, SystemHistoryStore, _decode_change, _corrupt

__all__ = ["read_indexed_history"]


def read_indexed_history(store: SystemHistoryStore, *, root: tuple[int, bytes], boundary: int,
                         target: CommitId, table_ids: tuple[int, ...], limits: TemporalLimits,
                         horizons: dict[int, int], record_id: int | None) -> tuple[TemporalGraph, tuple[TemporalVersion, ...]]:
    """Read a complete lineage range without decoding unrelated historical batches.

    The caller owns the native journal observation and capability/horizon checks.
    A missing/corrupt index does not silently turn into an unverified result.
    """
    consumed = events = 0

    def charge(amount: int) -> None:
        """Charge every visited page before returning a historical picture."""
        nonlocal consumed
        consumed += amount
        if consumed > limits.max_bytes:
            raise GrafxQueryBudgetExceeded("Temporal index byte budget exhausted.", resource="history_scan")

    from okto_grafx.engine.system_history_store import _HEAD
    tree = HistoryAccessTree(store.read_page, database_uuid=target.database_uuid,
        page_size=store.page_size, sequence=boundary, root=root, charge=charge,
        read_extent=_HEAD.unpack_from(store._page(0).read_slot(0))[5])

    def event(entry: tuple[tuple[int, int, int], tuple[int, bytes]]) -> HistoryChange:
        """Decode one bounded event and bind its identity to the authenticated key."""
        nonlocal events
        events += 1
        if events > limits.max_events:
            raise GrafxQueryBudgetExceeded("Temporal index event budget exhausted.", resource="history_scan")
        key, ref = entry
        change = _decode_change(tree.value(ref))
        if (change.table.table_id, change.record_id) != key[:2]:
            raise _corrupt("index_event_identity")
        return change

    if record_id is None:
        schemas = []
        rows = []
        identities = set()
        primary = set()
        for table_id in table_ids:
            entry = tree.floor((table_id, 0, target.sequence))
            if entry is None or entry[0][:2] != (table_id, 0):
                raise _corrupt("missing_historical_schema")
            schema_change = event(entry)
            if schema_change.operation != 4:
                raise _corrupt("index_schema")
            schema = schema_change.table
            schemas.append(schema)
            candidate = tree.ceiling((table_id, 1, 0))
            while candidate is not None and candidate[0][0] == table_id:
                rid = candidate[0][1]
                entry = tree.floor((table_id, rid, target.sequence))
                if entry is not None and entry[0][:2] == (table_id, rid):
                    change = event(entry)
                    if change.operation in (4, 5, 6):
                        raise _corrupt("index_retained_event")
                    if change.operation != 3:
                        if (change.table.schema_version > schema.schema_version
                                or change.table.columns != schema.columns[:len(change.table.columns)]):
                            raise _corrupt("event_schema")
                        values = (*change.values, *((None,) * (len(schema.columns) - len(change.values))))
                        row = TemporalVersion(schema.name, table_id, rid, values,
                            schema.schema_version, CommitId(target.database_uuid, entry[0][2]))
                        identities.add((table_id, rid))
                        if schema.primary_key is not None:
                            pk = (table_id, index_key(values, (schema.column_index(schema.primary_key),)))
                            if pk in primary:
                                raise _corrupt("primary_key_overlap")
                            primary.add(pk)
                        rows.append(row)
                        if len(rows) > limits.max_rows:
                            raise GrafxQueryBudgetExceeded("Temporal index row budget exhausted.", resource="history_rows")
                # Jump to the next lineage, not the next stored version. Charge
                # candidates even when born after target or already deleted.
                charge(1)
                events += 1
                if events > limits.max_events:
                    raise GrafxQueryBudgetExceeded("Temporal index candidate budget exhausted.", resource="history_scan")
                candidate = None if rid == 2**64 - 1 else tree.ceiling((table_id, rid + 1, 0))
        names = {schema.name: schema.table_id for schema in schemas}
        schema_by_id = {schema.table_id: schema for schema in schemas}
        for row in rows:
            schema = schema_by_id[row.table_id]
            if schema.kind == "rel":
                if (names.get(schema.from_table), row.values[0]) not in identities or (
                        names.get(schema.to_table), row.values[1]) not in identities:
                    raise _corrupt("historical_endpoint")
        return TemporalGraph(target, tuple(schemas), tuple(sorted(rows, key=lambda row: (row.table_id, row.record_id))),
                             events, consumed), ()
    output = []
    current = None
    seen = False
    table_id = table_ids[0]
    horizon = horizons[table_id]
    redacted = False
    for key, ref in tree.entries((table_id, record_id, 0), (table_id, record_id, target.sequence)):
        change = event((key, ref))
        if (change.table.table_id, change.record_id) != key[:2] or change.operation == 4:
            raise _corrupt("index_event_identity")
        identity = CommitId(target.database_uuid, key[2])
        if change.operation in (1, 5):
            if seen:
                raise _corrupt("lineage_reuse")
            seen = True
        elif current is None:
            raise _corrupt("interval_gap")
        if current is not None:
            if redacted and identity.sequence > horizon:
                raise _corrupt("redacted_retained_version")
            if not redacted and identity.sequence > horizon:
                output.append(replace(current, system_to=identity))
        redacted = change.operation in (5, 6)
        current = None if change.operation == 3 else TemporalVersion(change.table.name, table_id,
            record_id, change.values, change.table.schema_version, identity)
        if len(output) + (current is not None) > limits.max_rows:
            raise GrafxQueryBudgetExceeded("Temporal index row budget exhausted.", resource="history_rows")
    if redacted:
        raise _corrupt("redacted_current_version")
    if current is not None:
        output.append(current)
    return TemporalGraph(target, (), (), events, consumed), tuple(output)
