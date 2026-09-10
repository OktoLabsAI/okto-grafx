"""Typed system-time values, distinct from retained physical MVCC snapshots."""

from __future__ import annotations

__all__ = ["TemporalGraph", "TemporalLimits", "TemporalPin", "TemporalPruneReport", "TemporalVersion", "TemporalVersions"]

from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.txn.commit_identity import CommitId


@dataclass(frozen=True, slots=True)
class TemporalLimits:
    """Aggregate encoded-input, event and live/output-row bounds; not an RSS ceiling."""

    max_events: int = 100_000
    max_bytes: int = 64 * 1024 * 1024
    max_rows: int = 100_000

    def __post_init__(self) -> None:
        """Reject bools, unbounded values and invalid budgets before database access."""
        for field, maximum in (("max_events", 10_000_000), ("max_bytes", 2**31), ("max_rows", 1_000_000)):
            value = getattr(self, field)
            if type(value) is not int or not 1 <= value <= maximum:
                raise GrafxConfigurationError("Invalid temporal read budget.", field=field, maximum=maximum)


@dataclass(frozen=True, slots=True)
class TemporalVersion:
    """One row lineage interval [system_from, system_to); None is unbounded."""

    table: str
    table_id: int
    record_id: int
    values: tuple[object, ...]
    schema_version: int
    system_from: CommitId
    system_to: CommitId | None = None


@dataclass(frozen=True, slots=True)
class TemporalGraph:
    """Complete bounded historical rows/schemas for a requested closed table set."""

    as_of: CommitId
    schemas: tuple[TableDef, ...]
    rows: tuple[TemporalVersion, ...]
    events_scanned: int
    encoded_bytes_scanned: int


@dataclass(frozen=True, slots=True)
class TemporalVersions:
    """Versions visible to the owning read snapshot, never a claim beyond its boundary."""

    read_commit: CommitId
    table: str
    record_id: int
    versions: tuple[TemporalVersion, ...]
    activation: CommitId
    retained_from: CommitId


@dataclass(frozen=True, slots=True)
class TemporalPin:
    """Durable named retention protection; explicitly released, with no implicit TTL."""

    name: str
    at: CommitId
    tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TemporalPruneReport:
    """Atomic retention result; payload redaction does not reclaim physical file space."""

    before: CommitId
    tables: tuple[str, ...]
    commit: CommitId | None
    redacted_versions: int
    redacted_bytes: int
    physical_bytes_reclaimed: int = 0
