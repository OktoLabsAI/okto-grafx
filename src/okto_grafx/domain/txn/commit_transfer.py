"""Bounded provenance hooks for a caller-coordinated logical transfer.

Graph payload selection/streaming remains the consumer's responsibility. These
values neither acknowledge a commit nor authenticate its source. Import uses a
new target identity and publishes the source mapping inside ordinary metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.txn.commit_catalog import CommitCatalogEntry, decode_commit_catalog_entry
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.domain.txn.commit_metadata import CommitMetadata, MetadataLimits, MetadataValue


def _invalid() -> GrafxConfigurationError:
    return GrafxConfigurationError("Invalid commit transfer reference or mapping.", field="commit_transfer")


def _thaw(value: MetadataValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if type(value) is tuple:
        return [_thaw(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class CommitMapping:
    """Qualified source-to-target logical import mapping, not a durability receipt."""

    source: CommitId
    target: CommitId

    def __post_init__(self) -> None:
        if type(self.source) is not CommitId or type(self.target) is not CommitId:
            raise _invalid()
        source = CommitId(self.source.database_uuid, self.source.sequence)
        target = CommitId(self.target.database_uuid, self.target.sequence)
        if source.database_uuid == target.database_uuid:
            raise _invalid()
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "target", target)


@dataclass(frozen=True, slots=True)
class CommitImport:
    """Metadata to pass to target.begin; mapping is recovered from the target entry."""

    source: CommitId
    metadata: CommitMetadata

    def mapping(self, target: CommitCatalogEntry) -> CommitMapping:
        """Validate a returned/decoded target record's atomically persisted source link."""
        if type(target) is not CommitCatalogEntry or type(self.source) is not CommitId:
            raise _invalid()
        captured = decode_commit_catalog_entry(target.encode())
        source = CommitId(self.source.database_uuid, self.source.sequence)
        if captured.metadata is None or captured.metadata.attributes.get("grafx_source_commit") != source.to_token():
            raise _invalid()
        return CommitMapping(source, captured.identity)


def prepare_commit_import(
    source: CommitCatalogEntry, *, preserve_metadata: bool = True,
    limits: MetadataLimits | None = None,
) -> CommitImport:
    """Capture a source entry before IO, retaining its identity in target metadata.

    By default preserve source fields and nest its attributes without collisions.
    This adds bounded overhead/depth; over-budget input refuses before a target
    transaction opens. preserve_metadata=False retains only the qualified mapping
    and is an explicit privacy/size choice, never a silent truncation.
    """
    if type(source) is not CommitCatalogEntry or type(preserve_metadata) is not bool:
        raise _invalid()
    captured = decode_commit_catalog_entry(source.encode())
    original = captured.metadata if preserve_metadata else None
    attributes: dict[str, object] = {"grafx_source_commit": captured.identity.to_token()}
    if original is not None:
        attributes["source_attributes"] = _thaw(original.attributes)
    metadata = CommitMetadata(
        actor=None if original is None else original.actor,
        origin=None if original is None else original.origin,
        reason=None if original is None else original.reason,
        correlation_id=None if original is None else original.correlation_id,
        attributes=attributes, limits=MetadataLimits() if limits is None else limits,
    )
    return CommitImport(captured.identity, metadata)

__all__ = ["CommitMapping","CommitImport","prepare_commit_import"]
