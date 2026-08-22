"""The secondary-index contract every index implements (CONTRACT.md section 8.7, SPEC-M1 FR-12).

The protocol is reproduced here, in the domain, so that it is a value-level contract rather than
a property of one engine class: an index is free to keep its structure however it likes -- a hash
bucket chain, a navigable small world graph -- provided it answers these seven questions. The
engine module re-exports it under the name CONTRACT.md gives it, so
``from okto_grafx.engine.index_manager import SecondaryIndex`` resolves exactly as the contract
says it does.

Two shapes are taken structurally rather than by class, for the reason amendment A19 already
settled for the heap: the snapshot predicate belongs to the transaction manager and the framing
of a log record belongs to the log, so an index that asked for those by class would tie two more
components together for nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from okto_grafx.domain.ids import Csn, Lsn, RecordRef
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.index.visibility import (
    IndexVisibility,
    ReconcileReport,
    SnapshotLike,
)
from okto_grafx.domain.wal.record import WalRecord

__all__ = [
    "SecondaryIndex",
    "StagingTransaction",
]


@runtime_checkable
class StagingTransaction(Protocol):
    """The part of a transaction an index needs: its identity and a door to stage a record.

    ``stage_record`` is the door CONTRACT.md section 8.5 gives to components that own a kind of
    work the transaction manager does not: the record is appended untouched inside the same
    commit as the heap writes, which is exactly what BR-11 asks for.
    """

    @property
    def txn_id(self) -> int:
        """Return the process-local number of this transaction."""
        ...

    def stage_record(self, record: object) -> None:
        """Stage a log record this transaction wants appended at commit."""
        ...


@runtime_checkable
class SecondaryIndex(Protocol):
    """A derived structure over one table, with a declared visibility contract.

    The two contracts are stated in :mod:`okto_grafx.domain.index.visibility` and are the whole
    substance of this protocol: everything else here is bookkeeping that makes them durable,
    replayable and verifiable.
    """

    @property
    def name(self) -> str:
        """Return the name of this index, which is also the name of its file."""
        ...

    @property
    def visibility(self) -> IndexVisibility:
        """Return the visibility contract this index offers its callers."""
        ...

    def apply(self, record: WalRecord) -> None:
        """Redo one log record against this index, idempotently."""
        ...

    def stage_insert(
        self, txn: StagingTransaction, key: bytes, ref: RecordRef, csn: Csn
    ) -> WalRecord:
        """Stage the entry a row insertion owes this index and return the record that carries it."""
        ...

    def stage_delete(
        self, txn: StagingTransaction, key: bytes, ref: RecordRef, csn: Csn
    ) -> WalRecord:
        """Stage the end of an entry and return the record that carries it."""
        ...

    def lookup(self, key: bytes, snapshot: SnapshotLike) -> Iterable[RecordRef]:
        """Return the heap locations this index offers for the key, under its own contract."""
        ...

    def reconcile(self, horizon: Lsn) -> ReconcileReport:
        """Remove the entries no live snapshot can still want, logging every removal."""
        ...

    def walk(self) -> Iterable[IndexEntry]:
        """Yield every stored entry, for verification."""
        ...
