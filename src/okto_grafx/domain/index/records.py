"""The log records an index writes (SPEC-M1 BR-11, FR-12; CONTRACT.md section 6.5).

BR-11 says an index is never less safe than the heap, and the mechanism is that every change to
an index travels in the same log, inside the same commit, as the heap change that caused it.
Recovery therefore has exactly two outcomes and no third: either the COMMIT record is intact and
both the row and its entry are redone, or it is not and neither exists.

A record here is LOGICAL -- it says what changed, not which bytes a page ended up holding -- and
that is a deliberate departure from the note in CONTRACT.md section 8.7 that reads
``apply(record) -> None  # redo path, idempotent by page_lsn``. The reason is that an index entry
has no fixed address: a key lands wherever its bucket chain has room, that page differs after a
reconciliation pass has compacted the chain, and an entry can move between pages without any row
changing. Keying redo on the page LSN would therefore either skip an insert the file still needs
or duplicate one it already has, both of which are wrong results. Redo is instead idempotent on
the ENTRY: an insert of a key and location already present is a no-op, a tombstone of an entry
already ended is a no-op, and a removal of an entry already gone is a no-op. That property holds
however many times the log is replayed and whichever page the entry happens to live on. The page
LSN is still stamped, because CONTRACT.md section 6.3 defines it as the log position of the last
record applied to the page and a verifier reads it; it is simply not the thing redo asks.

Payload layout, little-endian::

    format_version u16 | operation u8 | flags u8 | ref u64 | csn u64 |
    name_length u16 | key_length u16 | index_name UTF-8 | key bytes
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import IntEnum

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.ids import (
    NO_CSN,
    PROVISIONAL_CSN,
    Csn,
    Epoch,
    Lsn,
    RecordRef,
    TxnId,
)
from okto_grafx.domain.index.definition import require_index_name
from okto_grafx.domain.index.entry import MAX_INDEX_KEY_BYTES
from okto_grafx.domain.wal.record import WalRecord, WalRecordType

__all__ = [
    "CHANGE_FLAG_VERSIONED",
    "INDEX_CHANGE_FORMAT_VERSION",
    "INDEX_CHANGE_HEADER_SIZE",
    "IndexChange",
    "IndexOperation",
    "change_of",
    "lsn_of",
    "wal_record_for",
]

INDEX_CHANGE_FORMAT_VERSION: int = 1
"""The version this build writes. The decoder accepts this one and every earlier one."""

CHANGE_FLAG_VERSIONED: int = 0x01
"""Bit 0 of the change flags: the entry this change is about carries a birth stamp."""

_CHANGE_STRUCT: struct.Struct = struct.Struct("<HBBQQHH")

INDEX_CHANGE_HEADER_SIZE: int = _CHANGE_STRUCT.size
"""Bytes of fixed header that precede the index name and the key in a change payload."""

_MAX_U64: int = 0xFFFFFFFFFFFFFFFF
_MAX_NAME_BYTES: int = 0xFFFF


class IndexOperation(IntEnum):
    """What one index change does, with the numeric codes frozen into the payload format."""

    INSERT = 1
    TOMBSTONE = 2
    REMOVE = 3
    RESET = 4


_RECORD_TYPE_OF: dict[IndexOperation, WalRecordType] = {
    IndexOperation.INSERT: WalRecordType.INDEX_WRITE,
    IndexOperation.TOMBSTONE: WalRecordType.INDEX_WRITE,
    IndexOperation.RESET: WalRecordType.INDEX_WRITE,
    IndexOperation.REMOVE: WalRecordType.INDEX_RECONCILE,
}
"""Which of the two frozen record types carries each operation.

CONTRACT.md section 8.7 says every removal made by reconciliation is an ``INDEX_RECONCILE``
record, so physical removal is the one operation that travels under that type; everything a
transaction does to an index is an ``INDEX_WRITE``.
"""


@dataclass(frozen=True, slots=True)
class IndexChange:
    """One change to one index, in the form the log carries it.

    ``csn`` means a different thing per operation, and each meaning is checked rather than
    assumed:

    * ``INSERT`` -- the commit number that created the entry, or zero for an unversioned entry,
      which has no birth stamp by construction;
    * ``TOMBSTONE`` -- the commit number at which the row stopped carrying this key; never zero,
      because zero is the value that means "not ended";
    * ``REMOVE`` -- the snapshot horizon that authorised the physical removal, so a replay of the
      reconciliation can be read back against the rule that permitted it;
    * ``RESET`` -- the log position the rebuilt index is declared to cover. ``ref.page`` is the
      even page-0 sequence of the durable stale mark that authorised this rebuild and
      ``ref.slot`` is zero. Older records wrote the null reference; token zero remains the
      backward-compatible recovery form and is rebound to the stale certificate observed by
      the replaying process.
    """

    index: str
    operation: IndexOperation
    key: bytes = b""
    ref: RecordRef = RecordRef(0, 0)
    csn: Csn = NO_CSN
    versioned: bool = False
    format_version: int = INDEX_CHANGE_FORMAT_VERSION

    def __post_init__(self) -> None:
        """Refuse a change that could not be encoded, or that contradicts its own operation."""
        require_index_name(self.index)
        if not isinstance(self.operation, IndexOperation):
            raise GrafxIndexError(
                f"An index change needs an IndexOperation; got {self.operation!r}.",
                field="operation",
                value=repr(self.operation),
            )
        if not isinstance(self.key, (bytes, bytearray, memoryview)):
            raise GrafxIndexError(
                f"An index change key must be bytes; got {type(self.key).__name__}.",
                field="key",
                value=type(self.key).__name__,
            )
        if not isinstance(self.key, bytes):
            object.__setattr__(self, "key", bytes(self.key))
        if len(self.key) > MAX_INDEX_KEY_BYTES:
            raise GrafxIndexError(
                f"An index key holds at most {MAX_INDEX_KEY_BYTES} bytes; got {len(self.key)}.",
                field="key",
                value=len(self.key),
            )
        if not isinstance(self.ref, RecordRef):
            raise GrafxIndexError(
                f"An index change must carry a RecordRef; got {type(self.ref).__name__}.",
                field="ref",
                value=type(self.ref).__name__,
            )
        if not isinstance(self.versioned, bool):
            raise GrafxIndexError(
                f"An index change needs a boolean versioned flag; got {self.versioned!r}.",
                field="versioned",
                value=repr(self.versioned),
            )
        for field, value, ceiling in (
            ("csn", self.csn, _MAX_U64),
            ("format_version", self.format_version, 0xFFFF),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= ceiling
            ):
                raise GrafxIndexError(
                    f"Index change field {field!r} is outside its width: {value!r}.",
                    field=field,
                    value=repr(value),
                )
        if self.csn == PROVISIONAL_CSN:
            raise GrafxIndexError(
                "The provisional heap stamp is not a commit and cannot be written to an index "
                "WAL change.",
                field="csn",
                value=PROVISIONAL_CSN,
                operation=self.operation.name,
            )
        if (
            self.operation is IndexOperation.INSERT
            and self.versioned
            and self.csn == NO_CSN
        ):
            raise GrafxIndexError(
                "A versioned entry is created by the commit that made it visible, so an insert "
                "into a proximity index must carry a commit number.",
                field="csn",
                operation=self.operation.name,
            )
        if (
            self.operation is IndexOperation.INSERT
            and not self.versioned
            and self.csn != NO_CSN
        ):
            raise GrafxIndexError(
                "An unversioned entry has no birth stamp, so an insert into an exact index must "
                f"not carry one; got csn={self.csn}.",
                field="csn",
                operation=self.operation.name,
            )
        if self.operation is IndexOperation.TOMBSTONE and self.csn == NO_CSN:
            raise GrafxIndexError(
                "A tombstone records the commit number at which the entry stopped applying, and "
                "zero is the value that means it never did.",
                field="csn",
                operation=self.operation.name,
            )
        if self.operation is IndexOperation.RESET and self.key:
            raise GrafxIndexError(
                "A reset clears the whole index, so it names no key.",
                field="key",
                operation=self.operation.name,
            )
        if self.operation is IndexOperation.RESET and (
            self.ref.slot != 0 or self.ref.page & 1
        ):
            raise GrafxIndexError(
                "A reset rebuild token is an even page-0 sequence stored in ref.page and "
                "uses ref.slot zero.",
                field="rebuild_token",
                value=self.ref.encode(),
                operation=self.operation.name,
            )

    @property
    def record_type(self) -> WalRecordType:
        """Return the frozen log record type that carries this operation."""
        return _RECORD_TYPE_OF[self.operation]

    def encode(self) -> bytes:
        """Return the payload bytes of the log record that carries this change."""
        name = self.index.encode("utf-8")
        if len(name) > _MAX_NAME_BYTES:
            raise GrafxIndexError(
                f"An index name holds at most {_MAX_NAME_BYTES} bytes once encoded; got "
                f"{len(name)}.",
                field="index",
                value=len(name),
            )
        head = _CHANGE_STRUCT.pack(
            self.format_version,
            int(self.operation),
            CHANGE_FLAG_VERSIONED if self.versioned else 0,
            self.ref.encode(),
            self.csn,
            len(name),
            len(self.key),
        )
        return b"".join((head, name, self.key))

    @classmethod
    def decode(cls, payload: bytes) -> IndexChange:
        """Return the change stored in a log payload, refusing bytes that are not one."""
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise GrafxCorruptionDetected(
                f"An index change payload must be bytes; got {type(payload).__name__}.",
                field="payload",
                value=type(payload).__name__,
            )
        raw = bytes(payload)
        if len(raw) < INDEX_CHANGE_HEADER_SIZE:
            raise GrafxCorruptionDetected(
                f"An index change needs {INDEX_CHANGE_HEADER_SIZE} bytes of header; this one has "
                f"{len(raw)}.",
                field="payload",
                value=len(raw),
            )
        (
            format_version,
            operation,
            flags,
            ref,
            csn,
            name_length,
            key_length,
        ) = _CHANGE_STRUCT.unpack_from(raw, 0)
        if format_version > INDEX_CHANGE_FORMAT_VERSION:
            raise GrafxCorruptionDetected(
                f"This build reads index change format {INDEX_CHANGE_FORMAT_VERSION} and below; "
                f"the record declares {format_version}.",
                field="format_version",
                value=format_version,
            )
        expected = INDEX_CHANGE_HEADER_SIZE + name_length + key_length
        if len(raw) != expected:
            raise GrafxCorruptionDetected(
                f"An index change declaring a name of {name_length} bytes and a key of "
                f"{key_length} occupies {expected} bytes; this record carries {len(raw)}.",
                field="payload",
                value=len(raw),
                declared=expected,
            )
        if operation not in tuple(int(member) for member in IndexOperation):
            raise GrafxCorruptionDetected(
                f"An index change declares the unknown operation {operation}.",
                field="operation",
                value=operation,
            )
        if flags & ~CHANGE_FLAG_VERSIONED:
            raise GrafxCorruptionDetected(
                f"An index change carries the unknown flag bits {flags:#04x}.",
                field="flags",
                value=flags,
            )
        start = INDEX_CHANGE_HEADER_SIZE
        try:
            index = raw[start : start + name_length].decode("utf-8")
        except UnicodeDecodeError as damaged:
            raise GrafxCorruptionDetected(
                "The index name of a change payload is not valid UTF-8.",
                field="index",
                value=name_length,
            ) from damaged
        try:
            return cls(
                index=index,
                operation=IndexOperation(operation),
                key=raw[start + name_length :],
                ref=RecordRef.decode(ref),
                csn=csn,
                versioned=bool(flags & CHANGE_FLAG_VERSIONED),
                format_version=format_version,
            )
        except GrafxIndexError as refused:
            # A payload that decodes into a shape the constructor refuses came off a device, so
            # it is damaged bytes and not a caller mistake (amendment A11-revised). The refusal
            # keeps its own message: the field it names is what a forensic ledger entry needs.
            raise GrafxCorruptionDetected(
                f"An index change payload does not describe a usable change: {refused.message}",
                field=str(refused.details.get("field", "payload")),
                operation=IndexOperation(operation).name,
                index=index,
            ) from refused


def wal_record_for(
    change: IndexChange,
    *,
    epoch: Epoch = 0,
    txn_id: TxnId = 0,
    descriptor: str = "",
) -> WalRecord:
    """Return the log record that carries this change, under the type its operation demands.

    There is one builder rather than one per record type on purpose: the mapping from operation
    to record type is a fact of the format and belongs in one place, and a second door would be a
    second chance for a removal to be logged as something other than ``INDEX_RECONCILE``.
    """
    if not isinstance(change, IndexChange):
        raise GrafxIndexError(
            f"A log record is built from an IndexChange; got {type(change).__name__}.",
            field="change",
            value=type(change).__name__,
        )
    if type(change) is IndexChange:
        # The record is born from this exact change and its payload is that change's own
        # encoding, so the change is the record's proof: the readers that follow -- the
        # mandatory staging validation, a retarget, the commit path -- reuse it instead of
        # decoding bytes this process produced.  A subclass is not carried; its bytes are
        # decoded and validated like any other record's.
        return _sealed_record(change, descriptor=descriptor, epoch=epoch, txn_id=txn_id)
    return WalRecord(
        record_type=int(change.record_type),
        payload=change.encode(),
        descriptor=descriptor,
        epoch=epoch,
        txn_id=txn_id,
    )


def _proof_protocol() -> tuple[
    Callable[[WalRecord], IndexChange],
    Callable[..., WalRecord],
]:
    """Build the record proof protocol around one token that never leaves this closure.

    A proof is ``(payload, change, token)`` in the record's private slot.  The token is created
    here and is not a module attribute, so no import can name it; and the closure exposes only
    two COMPLETE operations, neither of which accepts a proof from its caller:

    * ``proved_change(record)`` reads the sealed proof or decodes the record's bytes itself and
      seals what it decoded -- the value always derives from the bytes;
    * ``sealed_record(change, ...)`` encodes the change itself, builds the record around those
      bytes (a fresh record, or ``replace`` of a template for a retarget) and seals the pair it
      just produced -- the bytes always derive from the value.

    No visible callable seals a (payload, change) pair supplied from outside, so an ordinary
    caller -- including one that imports every private name of this module -- cannot plant a
    change in an intact record: the only way to reach a different change is to hold a different
    record whose payload IS that change's encoding.  A slot entry without the token, or naming a
    payload object other than the record's current ``bytes``, is not a proof and is ignored.
    """
    token = object()

    def proved_change(record: WalRecord) -> IndexChange:
        payload = record.payload
        entry = record._decoded
        if (
            type(entry) is tuple
            and len(entry) == 3
            and entry[2] is token
            and entry[0] is payload
        ):
            change = entry[1]
            # The token keeps ordinary callers out; this equivalence keeps a reflective one
            # out as well.  A change that encodes to exactly these bytes says nothing the
            # bytes do not, so the proof is absolute -- and one encode plus one bytes
            # comparison costs about a tenth of a decode.
            if type(change) is IndexChange and change.encode() == payload:
                return change
        change = IndexChange.decode(payload)
        if type(payload) is bytes:
            object.__setattr__(record, "_decoded", (payload, change, token))
        return change

    def sealed_record(
        change: IndexChange,
        *,
        template: WalRecord | None = None,
        descriptor: str = "",
        epoch: Epoch = 0,
        txn_id: TxnId = 0,
    ) -> WalRecord:
        payload = change.encode()
        if template is None:
            record = WalRecord(
                record_type=int(change.record_type),
                payload=payload,
                descriptor=descriptor,
                epoch=epoch,
                txn_id=txn_id,
            )
        else:
            record = replace(template, payload=payload)
        object.__setattr__(record, "_decoded", (record.payload, change, token))
        return record

    return proved_change, sealed_record


_proved_change, _sealed_record = _proof_protocol()


def record_for_change(record: WalRecord, change: IndexChange) -> WalRecord:
    """Return a copy of ``record`` whose payload is ``change``, already proved for its readers.

    The payload is exactly ``change.encode()`` and the codec is canonical -- decoding those
    bytes rebuilds an equal change, which the log's own redo already relies on -- so the new
    record carries ``change`` as its proof.  The mandatory staging validation and the commit
    path that follow a retarget then reuse this process's own proof instead of decoding bytes it
    produced a moment ago.  The proof cannot say anything the bytes do not: the record returned
    is a new one whose payload IS the encoding of the carried change, and the record given is
    left untouched.  A stand-in record or a change subclass is refused.
    """
    if type(record) is not WalRecord or type(change) is not IndexChange:
        raise GrafxIndexError(
            "Only an exact WalRecord can carry an exact IndexChange as its proved payload.",
            field="record",
            value=f"{type(record).__name__}/{type(change).__name__}",
        )
    return _sealed_record(change, template=record)


def change_of(record: object) -> IndexChange:
    """Return the change a log record carries, refusing a record of any other type.

    The record is taken structurally -- a type and a payload -- because what crosses this
    boundary is a value, and the framing of a record on the device belongs to the log.
    """
    record_type = getattr(record, "record_type", None)
    payload = getattr(record, "payload", None)
    if not isinstance(record_type, int) or isinstance(record_type, bool):
        raise GrafxIndexError(
            f"An index record needs an integer record type; got {type(record).__name__}.",
            field="record_type",
            value=type(record).__name__,
        )
    if record_type not in (
        int(WalRecordType.INDEX_WRITE),
        int(WalRecordType.INDEX_RECONCILE),
    ):
        raise GrafxIndexError(
            f"Record type {record_type} is not an index record; the index reads only "
            f"{int(WalRecordType.INDEX_WRITE)} and {int(WalRecordType.INDEX_RECONCILE)}.",
            field="record_type",
            value=record_type,
        )
    if type(record) is WalRecord and type(payload) is bytes:
        # An exact, frozen log record over immutable bytes is decoded once: the first decode is
        # the full refusing validation, and every later reader of the same record object --
        # preflight, redo dispatch, the store's own apply, staging validation and retargeting --
        # reuses that very result under the private proof protocol.  A stand-in record decodes
        # every time, and a failed decode seals nothing, so a refusal repeats for every reader.
        change = _proved_change(record)
    else:
        change = IndexChange.decode(payload if payload is not None else b"")
    if int(change.record_type) != record_type:
        raise GrafxCorruptionDetected(
            f"An index change of operation {change.operation.name} travels under record type "
            f"{int(change.record_type)}, but this record declares {record_type}.",
            field="record_type",
            value=record_type,
            operation=change.operation.name,
        )
    return change


def lsn_of(record: object) -> Lsn:
    """Return the log position a record was assigned, or zero when it has none yet."""
    lsn = getattr(record, "lsn", 0)
    if isinstance(lsn, bool) or not isinstance(lsn, int) or lsn < 0:
        raise GrafxIndexError(
            f"A log record must carry a non-negative log position; got {lsn!r}.",
            field="lsn",
            value=repr(lsn),
        )
    return lsn
