"""Bounded system-time graph differences, independent of valid-time or mutation APIs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.model.value import encode_values
from okto_grafx.domain.temporal import TemporalLimits, TemporalVersion
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded

if TYPE_CHECKING:
    from okto_grafx.engine.database import Transaction

__all__ = ["TemporalDiff", "TemporalPropertyChange", "TemporalRowChange", "TemporalSchemaChange", "diff_graph"]


@dataclass(frozen=True, slots=True)
class TemporalPropertyChange:
    """Absent and NULL are distinct; endpoint slots use names _from/_to from native schema."""

    name: str
    before_present: bool
    after_present: bool
    before: object
    after: object


@dataclass(frozen=True, slots=True)
class TemporalRowChange:
    """One node/relationship lineage change; recreate is a separate added identity."""

    table: str
    table_kind: str
    record_id: int
    operation: str
    before: TemporalVersion | None
    after: TemporalVersion | None
    properties: tuple[TemporalPropertyChange, ...]


@dataclass(frozen=True, slots=True)
class TemporalSchemaChange:
    """Explicit historical schema change, even for tables containing no rows."""

    table: str
    before: TableDef | None
    after: TableDef | None


@dataclass(frozen=True, slots=True)
class TemporalDiff:
    """Complete same-store diff; scan counters cover both captured pictures."""

    before: CommitId
    after: CommitId
    schemas: tuple[TemporalSchemaChange, ...]
    rows: tuple[TemporalRowChange, ...]
    events_scanned: int
    encoded_bytes_scanned: int


def _validate(before, after, limits, max_changes):
    if type(before) is not CommitId or type(after) is not CommitId:
        raise GrafxConfigurationError("Diff requires qualified CommitId coordinates.", field="diff")
    before = CommitId(before.database_uuid, before.sequence)
    after = CommitId(after.database_uuid, after.sequence)
    if before.database_uuid != after.database_uuid or before.sequence > after.sequence:
        raise GrafxConfigurationError("Diff requires one store and before <= after.", field="diff")
    if type(limits) is not TemporalLimits:
        raise GrafxConfigurationError("Expected TemporalLimits.", field="limits")
    if type(max_changes) is not int or not 1 <= max_changes <= 1_000_000:
        raise GrafxConfigurationError("Invalid diff change bound.", field="max_changes")


def diff_graph(reader: Transaction, before: CommitId, after: CommitId, *, tables: tuple[str, ...],
               limits: TemporalLimits = TemporalLimits(), max_changes: int = 100_000) -> TemporalDiff:
    """Capture two retained pictures under one owning transaction and return no partial diff."""
    _validate(before, after, limits, max_changes)
    from okto_grafx.engine.database import Transaction
    if type(reader) is not Transaction:
        raise GrafxConfigurationError("Diff requires a native owning Transaction.", field="reader")
    left = reader.system_as_of(before, tables=tables, limits=limits)
    if before == after:
        return TemporalDiff(before, after, (), (), left.events_scanned, left.encoded_bytes_scanned)
    remaining_events = limits.max_events - left.events_scanned
    remaining_bytes = limits.max_bytes - left.encoded_bytes_scanned
    remaining_rows = limits.max_rows - len(left.rows)
    if min(remaining_events, remaining_bytes, remaining_rows) < 1:
        raise GrafxQueryBudgetExceeded("Diff capture budget exhausted.", resource="temporal_diff")
    right = reader.system_as_of(after, tables=tables, limits=replace(limits,
        max_events=remaining_events, max_bytes=remaining_bytes, max_rows=remaining_rows))
    old_schema = {s.table_id: s for s in left.schemas}
    new_schema = {s.table_id: s for s in right.schemas}
    used = 0

    def charge() -> None:
        """Charge schema, row and property entries before retaining output structure."""
        nonlocal used
        used += 1
        if used > max_changes:
            raise GrafxQueryBudgetExceeded("Diff change budget exhausted.", resource="temporal_diff_changes")

    schemas = []
    for key in sorted(old_schema.keys() | new_schema.keys()):
        old, new = old_schema.get(key), new_schema.get(key)
        if old != new:
            charge()
            schemas.append(TemporalSchemaChange((new or old).name, old, new))
    old_rows = {(r.table_id, r.record_id): r for r in left.rows}
    new_rows = {(r.table_id, r.record_id): r for r in right.rows}
    changes = []
    for key in sorted(old_rows.keys() | new_rows.keys()):
        old, new = old_rows.get(key), new_rows.get(key)
        old_values = {} if old is None else dict(zip((c.name for c in old_schema[key[0]].columns), old.values))
        new_values = {} if new is None else dict(zip((c.name for c in new_schema[key[0]].columns), new.values))
        properties = []
        for name in sorted(old_values.keys() | new_values.keys()):
            present_old, present_new = name in old_values, name in new_values
            old_value, new_value = old_values.get(name), new_values.get(name)
            if present_old != present_new or encode_values((old_value,)) != encode_values((new_value,)):
                charge()
                properties.append(TemporalPropertyChange(name, present_old, present_new, old_value, new_value))
        if old is None or new is None or properties:
            charge()
            schema = new_schema.get(key[0], old_schema.get(key[0]))
            changes.append(TemporalRowChange(schema.name, schema.kind, key[1],
                "added" if old is None else "removed" if new is None else "updated", old, new, tuple(properties)))
    return TemporalDiff(before, after, tuple(schemas), tuple(changes),
                        left.events_scanned + right.events_scanned,
                        left.encoded_bytes_scanned + right.encoded_bytes_scanned)
