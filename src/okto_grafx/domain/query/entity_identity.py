"""Capability-free identities and provenance for FP-3 detached entity results.

These values are separate from live RowBinding/PendingRowRef authority. Native
result-boundary wiring owns allocation of provisional identities; callers cannot
use an identity value to acquire a transaction or mutate a record.
"""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ids import PROVISIONAL_CSN


def _unsigned(value: object, field: str, maximum: int, *, minimum: int = 1) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise GrafxConfigurationError("Entity metadata needs an integer within its declared bounds.",
                                       field=field, minimum=minimum, maximum=maximum)


def _uuid_bytes(value: object, field: str) -> None:
    if type(value) is not bytes or len(value) != 16:
        raise GrafxConfigurationError("Entity metadata needs exactly 16 immutable bytes.", field=field)


@dataclass(frozen=True, slots=True)
class EntityIdentity:
    """One logical graph element, independent of properties and observation time.

    Committed table IDs are append-only in the current catalog. Record IDs are
    allocated monotonically and survive updates but not delete/recreate, so they
    identify record incarnations rather than user primary keys. A provisional
    nonce belongs to the owning transaction's entity, not a fake durable record
    ID, and must never compare equal to a later committed identity by accident.
    """

    database_uuid: bytes
    table_id: int
    kind: str
    record_id: int | None = None
    provisional_id: bytes | None = None

    def __post_init__(self) -> None:
        _uuid_bytes(self.database_uuid, "database_uuid")
        _unsigned(self.table_id, "table_id", (1 << 32) - 1)
        if type(self.kind) is not str or self.kind not in {"node", "relationship"}:
            raise GrafxConfigurationError("An entity kind is node or relationship.", field="kind")
        if (self.record_id is None) == (self.provisional_id is None):
            raise GrafxConfigurationError("An entity identity is either committed or provisional, never both.",
                                           field="entity_identity")
        if self.record_id is not None:
            # MAX_U64 is the allocator exhaustion marker, not a usable record ID.
            _unsigned(self.record_id, "record_id", (1 << 64) - 2)
        else:
            _uuid_bytes(self.provisional_id, "provisional_id")

    @property
    def committed(self) -> bool:
        """Whether this names a committed record namespace, not current liveness."""
        return self.record_id is not None

    def to_dict(self) -> dict[str, object]:
        """Return owned JSON-safe data without narrowing u64 IDs to JS numbers."""
        return {
            "format": "grafx.entity-id.v1", "database_uuid": self.database_uuid.hex(),
            "table_id": str(self.table_id), "kind": self.kind,
            "record_id": None if self.record_id is None else str(self.record_id),
            "provisional_id": None if self.provisional_id is None else self.provisional_id.hex(),
        }


@dataclass(frozen=True, slots=True)
class EntityProvenance:
    """Observation metadata; none of these fields participates in entity identity.

    read_lsn names the transaction snapshot, schema_version the observed layout.
    version_lsn is the committed source version when known, not a promised future
    commit. pending marks transaction-private values, including an update to an
    already committed record. This DTO has no transaction or page reference.
    """

    read_lsn: int
    schema_version: int
    version_lsn: int | None
    pending: bool = False

    def __post_init__(self) -> None:
        _unsigned(self.read_lsn, "read_lsn", PROVISIONAL_CSN - 1, minimum=0)
        _unsigned(self.schema_version, "schema_version", (1 << 32) - 1)
        if type(self.pending) is not bool:
            raise GrafxConfigurationError("Entity pending provenance must be boolean.", field="pending")
        if self.version_lsn is not None:
            _unsigned(self.version_lsn, "version_lsn", PROVISIONAL_CSN - 1, minimum=0)
            if self.version_lsn > self.read_lsn:
                raise GrafxConfigurationError("An observed committed version cannot be newer than its snapshot.",
                                               field="version_lsn")
        elif not self.pending:
            raise GrafxConfigurationError("A non-pending entity must name its committed source version.",
                                           field="version_lsn")

    def to_dict(self) -> dict[str, object]:
        """Return owned JSON-safe metadata, without implying current existence."""
        return {
            "read_lsn": str(self.read_lsn), "schema_version": self.schema_version,
            "version_lsn": None if self.version_lsn is None else str(self.version_lsn),
            "pending": self.pending,
        }
