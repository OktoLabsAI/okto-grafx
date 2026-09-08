"""Commit catalog logical record v1; paged storage/publication is a separate layer.

No runtime capability/WAL type is activated here. A valid record is not a receipt
of durability: callers must prove its physical source and publication separately.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.model import Timestamp
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.txn.commit_identity import CommitId, CommitTime
from okto_grafx.domain.txn.commit_metadata import (
    MAX_METADATA_BYTES, CommitMetadata, decode_commit_metadata,
)

_MAGIC = b"GXCMREC\0"
_HEADER = struct.Struct("<8sHH16sQqqI")
_CHECKSUM = struct.Struct("<I")
MAX_COMMIT_RECORD_BYTES = _HEADER.size + MAX_METADATA_BYTES + _CHECKSUM.size
_CLOCK_ADJUSTED = 1
_METADATA_PRESENT = 2
_MAINTENANCE = 4
_KNOWN_FLAGS = _CLOCK_ADJUSTED | _METADATA_PRESENT | _MAINTENANCE


class CommitKind(IntEnum):
    """Audit classification; never an authorization to bypass a transaction rule."""

    DATA = 1
    MAINTENANCE = 2


def _invalid(field: str) -> GrafxConfigurationError:
    return GrafxConfigurationError("Invalid commit catalog record value.", field=field)


def _corrupt(field: str) -> GrafxCorruptionDetected:
    return GrafxCorruptionDetected(
        "Invalid commit catalog record.", component="commit_catalog", field=field,
    )


def _capture(
    identity: CommitId, timing: CommitTime, metadata_bytes: bytes | None, kind: CommitKind,
) -> tuple[CommitId, CommitTime, CommitMetadata | None]:
    if type(identity) is not CommitId:
        raise _invalid("identity")
    if type(timing) is not CommitTime:
        raise _invalid("timing")
    if type(kind) is not CommitKind:
        raise _invalid("kind")
    identity = CommitId(identity.database_uuid, identity.sequence)
    timing = CommitTime(timing.observed_at, timing.ordered_at, timing.clock_adjusted)
    metadata = None if metadata_bytes is None else decode_commit_metadata(metadata_bytes)
    return identity, timing, metadata


@dataclass(frozen=True, slots=True)
class CommitCatalogEntry:
    """Immutable verified value of one record, not a store handle or physical proof."""

    identity: CommitId
    timing: CommitTime
    metadata_bytes: bytes | None = field(default=None, repr=False)
    kind: CommitKind = CommitKind.DATA
    _metadata: CommitMetadata | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        identity, timing, metadata = _capture(self.identity, self.timing, self.metadata_bytes, self.kind)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "timing", timing)
        object.__setattr__(self, "_metadata", metadata)

    @property
    def metadata(self) -> CommitMetadata | None:
        """The decoded immutable metadata; bytes/keys/content are absent from repr."""
        return self._metadata

    def encode(self) -> bytes:
        """Encode captured and revalidated values, not unchecked mutable host slots."""
        raw = self.metadata_bytes
        kind = self.kind
        identity, timing, _metadata = _capture(self.identity, self.timing, raw, kind)
        flags = _CLOCK_ADJUSTED if timing.clock_adjusted else 0
        if raw is not None:
            flags |= _METADATA_PRESENT
        if kind is CommitKind.MAINTENANCE:
            flags |= _MAINTENANCE
        payload = b"" if raw is None else raw
        header = _HEADER.pack(
            _MAGIC, 1, flags, identity.database_uuid, identity.sequence,
            timing.observed_at.micros, timing.ordered_at.micros, len(payload),
        )
        body = header + payload
        return body + _CHECKSUM.pack(crc32c(body))


def decode_commit_catalog_entry(
    raw: bytes, *, expected_store_uuid: bytes | None = None, expected_sequence: int | None = None,
) -> CommitCatalogEntry:
    """Prove one complete envelope and optional qualified directory expectations."""
    if type(raw) is not bytes:
        raise _invalid("record")
    # Caller expectations are inputs, not evidence read from the record itself.
    if expected_store_uuid is not None:
        CommitId(expected_store_uuid, 1)
    if expected_sequence is not None:
        CommitId(bytes(16), expected_sequence)
    if not _HEADER.size + _CHECKSUM.size <= len(raw) <= MAX_COMMIT_RECORD_BYTES:
        raise _corrupt("size")
    expected_crc = _CHECKSUM.unpack_from(raw, len(raw) - _CHECKSUM.size)[0]
    if crc32c(raw[:-_CHECKSUM.size]) != expected_crc:
        raise _corrupt("checksum")
    magic, version, flags, store_uuid, sequence, observed, ordered, size = _HEADER.unpack_from(raw)
    if magic != _MAGIC:
        raise _corrupt("magic")
    if version != 1 or flags & ~_KNOWN_FLAGS:
        raise GrafxSchemaVersionMismatch(
            "Unsupported commit catalog record semantics.", component="commit_catalog",
            field="version" if version != 1 else "flags", version=version, flags=flags,
        )
    if size != len(raw) - _HEADER.size - _CHECKSUM.size:
        raise _corrupt("metadata_length")
    present = bool(flags & _METADATA_PRESENT)
    if present != bool(size):
        raise _corrupt("metadata_presence")
    try:
        identity = CommitId(store_uuid, sequence)
        timing = CommitTime(Timestamp(observed), Timestamp(ordered), bool(flags & _CLOCK_ADJUSTED))
    except GrafxConfigurationError as failure:
        # Constructor fields are a fixed vocabulary, never metadata input text.
        raise _corrupt(str(failure.details["field"])) from None
    if expected_store_uuid is not None and identity.database_uuid != expected_store_uuid:
        raise _corrupt("database_uuid")
    if expected_sequence is not None and identity.sequence != expected_sequence:
        raise _corrupt("sequence")
    return CommitCatalogEntry(
        identity=identity, timing=timing,
        metadata_bytes=raw[_HEADER.size:-_CHECKSUM.size] if present else None,
        kind=CommitKind.MAINTENANCE if flags & _MAINTENANCE else CommitKind.DATA,
    )

__all__ = ["CommitKind","CommitCatalogEntry","decode_commit_catalog_entry","MAX_COMMIT_RECORD_BYTES"]
