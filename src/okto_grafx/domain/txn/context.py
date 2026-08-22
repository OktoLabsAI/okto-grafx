"""What one open transaction holds (CONTRACT.md section 8.5, SPEC-M1 FR-2, FR-3, FR-4).

A transaction context accumulates four things, named exactly as the contract names them:

* ``read_partitions`` and ``write_partitions`` -- the sets optimistic validation compares;
* ``pending_records`` -- log records staged by whoever owns that kind of work;
* ``page_images`` -- the pages this transaction wants, keyed by ``(file, page_index)``.

The page images are STAGED, never written. That is what makes rollback free and what makes
CONTRACT.md section 8.6 step 6 true: an uncommitted transaction needs no undo because nothing of
it ever reached a data file. It is also what makes the transaction manager the only writer of
pages, so the log record and the page write cannot come apart.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxTransactionStateError,
)
from okto_grafx.domain.ids import NO_CSN, Csn, Epoch, PageIndex, TxnId
from okto_grafx.domain.txn.partitions import page_partition
from okto_grafx.domain.txn.records import WalRecordLike
from okto_grafx.domain.txn.snapshot import Snapshot

__all__ = [
    "CommitReport",
    "RowIntent",
    "RowOperation",
    "TransactionContext",
    "TransactionMode",
    "TransactionState",
]


class RowOperation(str, Enum):
    """What a staged row intent will do to the heap once the commit number exists."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class RowIntent:
    """A row this transaction wants written, held until the commit number exists.

    A row cannot be written when the caller asks for it. ``HeapStore.insert`` stamps the version
    with the commit number that makes it visible, and that number is the LSN of the COMMIT
    record, which the log assigns inside the commit itself (CONTRACT.md section 8.5 step 3.4).
    A row written earlier would carry a birth stamp no snapshot rule can make correct: too low
    and transactions that must not see it do, too high and transactions that must see it do not.

    So the intent is staged and the row is written inside the commit section, at the number the
    log actually assigned. ``record_id`` of None means the identity is allocated then too, which
    is what keeps an abandoned commit from burning one: an id allocated and abandoned leaves a
    gap in the sequence, and a gap is harmless where a REUSED id is not.
    """

    table: object
    values: tuple[object, ...] = ()
    record_id: int | None = None
    operation: RowOperation = RowOperation.INSERT
    reference: object = None


class TransactionMode(str, Enum):
    """The two modes ``begin`` accepts (CONTRACT.md section 8.5)."""

    READ = "read"
    WRITE = "write"

    @classmethod
    def parse(cls, mode: object) -> TransactionMode:
        """Return the mode for this value, refusing anything that is not one of the two.

        The message lists what IS accepted, because a caller that passed ``"readonly"`` needs to
        be told the vocabulary rather than merely told no.
        """
        if isinstance(mode, cls):
            return mode
        if isinstance(mode, str):
            for candidate in cls:
                if candidate.value == mode:
                    return candidate
        allowed = ", ".join(repr(candidate.value) for candidate in cls)
        raise GrafxConfigurationError(
            f"A transaction mode must be one of {allowed}; got {mode!r}.",
            field="mode",
            value=repr(mode),
        )


class TransactionState(str, Enum):
    """Where a transaction is in its life: open, committed, or rolled back."""

    ACTIVE = "active"
    COMMITTED = "committed"
    ABORTED = "aborted"


class CommitReport:
    """What a commit tells its caller: the commit number, whether it is durable, whether it wrote.

    ``durable`` is only ever True after the log barrier of CONTRACT.md section 8.5 step 3.5 has
    RETURNED, which is BR-4 stated as a value: there is no path in this component that builds a
    durable report before that call comes back.
    """

    __slots__ = ("_csn", "_durable", "_wrote")

    def __init__(self, *, csn: Csn, durable: bool, wrote: bool) -> None:
        """Build a report over an already-settled commit."""
        self._csn: Csn = csn
        self._durable: bool = durable
        self._wrote: bool = wrote

    @property
    def csn(self) -> Csn:
        """Return the commit sequence number, which is the LSN of the COMMIT record."""
        return self._csn

    @property
    def durable(self) -> bool:
        """Return whether the commit survived a durability barrier before this report existed."""
        return self._durable

    @property
    def wrote(self) -> bool:
        """Return whether this transaction put anything in the log."""
        return self._wrote

    def __eq__(self, other: object) -> bool:
        """Compare reports by the three values they carry."""
        if not isinstance(other, CommitReport):
            return NotImplemented
        return (
            self._csn == other._csn
            and self._durable == other._durable
            and self._wrote == other._wrote
        )

    def __hash__(self) -> int:
        """Hash the three values, so a report can be used as a key."""
        return hash((self._csn, self._durable, self._wrote))

    def __repr__(self) -> str:
        """Return a representation naming the commit number and the two flags."""
        return (
            f"CommitReport(csn={self._csn}, durable={self._durable}, wrote={self._wrote})"
        )


class TransactionContext:
    """One open transaction: its snapshot, the sets it touched and the work it has staged."""

    __slots__ = (
        "_txn_id",
        "_mode",
        "_snapshot",
        "_epoch",
        "_owner",
        "_state",
        "_conflicts",
        "_commit_csn",
        "read_partitions",
        "write_partitions",
        "pending_records",
        "page_images",
        "row_intents",
        "row_refs",
    )

    def __init__(
        self,
        *,
        txn_id: TxnId,
        mode: TransactionMode,
        snapshot: Snapshot,
        epoch: Epoch,
        owner: object,
    ) -> None:
        """Open a transaction bound to the manager that created it.

        ``owner`` is that manager. It is kept because amendment A74 says a committing writer must
        validate with the coordinator that granted its lease, never with a fresh one: binding the
        transaction to its manager is how that becomes a property of the object rather than a
        habit of the caller.
        """
        self._txn_id: TxnId = txn_id
        self._mode: TransactionMode = mode
        self._snapshot: Snapshot = snapshot
        self._epoch: Epoch = epoch
        self._owner: object = owner
        self._state: TransactionState = TransactionState.ACTIVE
        self._conflicts: int = 0
        self._commit_csn: Csn = NO_CSN
        self.read_partitions: set[int] = set()
        self.write_partitions: set[int] = set()
        self.pending_records: list[WalRecordLike] = []
        self.page_images: dict[tuple[str, PageIndex], bytes] = {}
        self.row_intents: list[RowIntent] = []
        self.row_refs: list[object] = []

    # --- identity -------------------------------------------------------------------------

    @property
    def txn_id(self) -> TxnId:
        """Return the process-local number of this transaction."""
        return self._txn_id

    @property
    def mode(self) -> TransactionMode:
        """Return the mode this transaction was opened in."""
        return self._mode

    @property
    def snapshot(self) -> Snapshot:
        """Return the fixed view this transaction reads under."""
        return self._snapshot

    @property
    def epoch(self) -> Epoch:
        """Return the writer epoch this transaction may commit under, or 0 for a reader."""
        return self._epoch

    @property
    def owner(self) -> object:
        """Return the transaction manager that opened this transaction (amendment A74)."""
        return self._owner

    @property
    def state(self) -> TransactionState:
        """Return whether this transaction is still open, committed or rolled back."""
        return self._state

    @property
    def active(self) -> bool:
        """Return True while this transaction can still accept work."""
        return self._state is TransactionState.ACTIVE

    @property
    def conflicts(self) -> int:
        """Return how many times a commit of this transaction was refused by validation."""
        return self._conflicts

    @property
    def commit_csn(self) -> Csn:
        """Return the commit number this transaction reached, or NO_CSN when it has not."""
        return self._commit_csn

    @property
    def wrote(self) -> bool:
        """Return True when this transaction has staged anything the log would have to carry."""
        return bool(
            self.pending_records
            or self.page_images
            or self.row_intents
            or self.write_partitions
        )

    # --- accumulation ---------------------------------------------------------------------

    def note_read(self, partition: int) -> None:
        """Record that this transaction read a partition."""
        self._require_active()
        self.read_partitions.add(_require_partition(partition))

    def note_write(self, partition: int) -> None:
        """Record that this transaction wrote a partition."""
        self._require_active()
        self._require_write_mode("record a write")
        self.write_partitions.add(_require_partition(partition))

    def note_reads(self, partitions: Iterable[int]) -> None:
        """Record several read partitions at once."""
        for partition in partitions:
            self.note_read(partition)

    def note_writes(self, partitions: Iterable[int]) -> None:
        """Record several written partitions at once."""
        for partition in partitions:
            self.note_write(partition)

    def stage_record(self, record: WalRecordLike) -> None:
        """Stage a log record this transaction wants appended at commit.

        The transaction manager builds the WRITE_PAGE records and the COMMIT record itself. This
        door is for the records other components own -- an index write, a catalog write -- which
        the manager appends untouched inside the same commit, because BR-11 says an index write
        is covered by the same log as the heap write it belongs to.
        """
        self._require_active()
        self._require_write_mode("stage a log record")
        if not isinstance(record, WalRecordLike):
            raise GrafxConfigurationError(
                "A staged record must carry a record type, an LSN and a payload; got "
                f"{type(record).__name__}.",
                field="record",
                value=type(record).__name__,
            )
        self.pending_records.append(record)

    def stage_page_image(self, file: str, page_index: PageIndex, image: bytes) -> None:
        """Stage the bytes this transaction wants page ``page_index`` of ``file`` to hold.

        Staging the LAST image for a page replaces the previous one on purpose: a transaction
        that changed one page twice wants one write of the final bytes, not two writes racing to
        be last.
        """
        self._require_active()
        self._require_write_mode("stage a page image")
        if not isinstance(file, str) or not file:
            raise GrafxConfigurationError(
                "A staged page image must name a non-empty file.",
                field="file",
                value=repr(file),
            )
        if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
            raise GrafxConfigurationError(
                f"A staged page image must name a page index of zero or more; got "
                f"{page_index!r}.",
                field="page_index",
                value=repr(page_index),
            )
        if not isinstance(image, (bytes, bytearray, memoryview)):
            raise GrafxConfigurationError(
                f"A staged page image must be bytes; got {type(image).__name__}.",
                field="image",
                value=type(image).__name__,
            )
        self.page_images[(file, page_index)] = bytes(image)
        # Staging a page IS declaring interest in it. A commit that wrote a whole page image and
        # declared interest in nothing could never be refused by optimistic validation -- the
        # predicate short-circuits on an empty set -- so two participants writing the same page
        # both acknowledged a durable commit and the second image silently replaced the first.
        # The partition names the page, so the frozen COMMIT payload carries it and any
        # participant reading the log can compare against it (defect E1).
        self.write_partitions.add(page_partition(file, page_index))

    def stage_row_insert(
        self, table: object, values: Iterable[object], *, record_id: int | None = None
    ) -> None:
        """Stage a row to be written at the number that makes it visible.

        Nothing reaches the heap here. The row is written inside the commit section, once the
        log has assigned the commit number, because that number is the row's birth stamp and it
        does not exist yet (CONTRACT.md section 8.5 step 3.4, section 8.2). Staging it is the
        same discipline the page images already follow, for the same reason: an uncommitted
        transaction must leave nothing behind, and a row that is already in the heap is not
        nothing.
        """
        self._require_active()
        self._require_write_mode("stage a row")
        if record_id is not None and (
            isinstance(record_id, bool) or not isinstance(record_id, int) or record_id < 1
        ):
            raise GrafxConfigurationError(
                f"A staged row identity must be a positive integer; got {record_id!r}.",
                field="record_id",
                value=repr(record_id),
            )
        self.row_intents.append(
            RowIntent(table=_require_table(table), values=tuple(values), record_id=record_id)
        )

    def stage_row_update(
        self, table: object, reference: object, values: Iterable[object]
    ) -> None:
        """Stage a new version of an existing row, to be written at the commit number.

        An update is two stamps, not one: the new version is born at the commit number and the
        old one ends at it, so a snapshot below the commit still finds exactly one live version
        and a snapshot at or above it finds exactly one. Neither number exists until the log
        assigns it, so neither write can happen before the commit section (CONTRACT.md section
        8.5 step 3.4, section 8.2).
        """
        self._require_active()
        self._require_write_mode("stage a row update")
        self.row_intents.append(
            RowIntent(
                table=_require_table(table),
                values=tuple(values),
                operation=RowOperation.UPDATE,
                reference=_require_reference(reference),
            )
        )

    def stage_row_delete(self, table: object, reference: object) -> None:
        """Stage the end of an existing row, to be written at the commit number.

        A delete ends a version rather than removing it: the bytes stay where they are and the
        header says when they stopped being current, which is what lets a snapshot opened before
        the delete go on reading the row it was entitled to see (BR-9).
        """
        self._require_active()
        self._require_write_mode("stage a row delete")
        self.row_intents.append(
            RowIntent(
                table=_require_table(table),
                operation=RowOperation.DELETE,
                reference=_require_reference(reference),
            )
        )

    def staging_mark(self) -> tuple[int, int, int]:
        """Return a mark naming how much this transaction has staged so far.

        The unit a caller discards is the STATEMENT, not the whole transaction: a statement that
        refuses halfway through must leave nothing of itself behind, or a caller that catches the
        refusal and commits makes half a statement durable. A caller takes a mark before a
        statement and discards back to it if the statement refuses.
        """
        return (len(self.row_intents), len(self.pending_records), len(self.page_images))

    def discard_since(self, mark: tuple[int, int, int]) -> None:
        """Drop everything staged after the mark, leaving what was staged before it untouched.

        Page images are keyed by location rather than ordered, so a statement that REPLACED an
        image staged by an earlier statement cannot be unwound by count alone; discarding is
        therefore refused when the map has not grown, and the caller rolls the transaction back
        instead. A refusal here is honest where a partial unwind would be silent.
        """
        rows, records, pages = mark
        if (
            not isinstance(rows, int)
            or not isinstance(records, int)
            or not isinstance(pages, int)
            or rows > len(self.row_intents)
            or records > len(self.pending_records)
            or pages > len(self.page_images)
        ):
            raise GrafxTransactionStateError(
                "That mark does not describe a point this transaction has passed through.",
                txn_id=self._txn_id,
                field="mark",
                value=repr(mark),
            )
        del self.row_intents[rows:]
        del self.pending_records[records:]
        if len(self.page_images) != pages:
            for key in sorted(self.page_images)[pages:]:
                del self.page_images[key]

    def staged_pages(self) -> Sequence[tuple[str, PageIndex]]:
        """Return the staged page locations in a fixed order, so a commit is reproducible."""
        return sorted(self.page_images)

    # --- life -------------------------------------------------------------------------------

    def bind_epoch(self, epoch: Epoch) -> None:
        """Record the writer epoch this transaction committed under.

        The epoch is not known when a transaction opens. A write transaction stages its work
        with nothing on the device, and the lease that names the epoch is taken for the commit
        window itself -- which is what lets two processes hold write transactions at the same
        time and still have BR-7 decide every byte that is written.
        """
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise GrafxConfigurationError(
                f"A writer epoch must be a non-negative integer; got {epoch!r}.",
                field="epoch",
                value=repr(epoch),
            )
        self._epoch = epoch

    def mark_conflicted(self) -> None:
        """Record that a commit attempt was refused by optimistic validation.

        The transaction stays ACTIVE and keeps everything it staged, because a conflict is a
        refusal with no side effect at all -- nothing of this transaction reached the device,
        which is what BR-6 means by retryable.

        The retry itself is a NEW transaction at a NEWER snapshot, opened through
        :meth:`~okto_grafx.engine.txn_manager.TransactionManager.retry`: committing THIS one
        again would compare the same snapshot against the same record and be refused for the
        same reason. Proved by test_the_loser_of_a_conflict_commits_on_its_retry.
        """
        self._conflicts += 1

    def adopt_conflicts(self, count: int) -> None:
        """Carry the refusal count of an earlier attempt into this transaction.

        A transaction refused by optimistic validation cannot usefully be committed again: the
        record that refused it is still in the log and still after its snapshot, so the same
        comparison gives the same answer forever. A retry is therefore a NEW transaction at a
        NEWER snapshot, and this is how it knows it is one.
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise GrafxConfigurationError(
                f"A refusal count must be a non-negative integer; got {count!r}.",
                field="count",
                value=repr(count),
            )
        self._conflicts += count

    def mark_committed(self, csn: Csn) -> None:
        """Move the transaction to its committed end state."""
        self._require_active()
        self._state = TransactionState.COMMITTED
        self._commit_csn = csn

    def mark_aborted(self) -> None:
        """Move the transaction to its rolled-back end state and drop everything it staged."""
        self._require_active()
        self._state = TransactionState.ABORTED
        self.read_partitions.clear()
        self.write_partitions.clear()
        self.pending_records.clear()
        self.page_images.clear()
        self.row_intents.clear()
        self.row_refs.clear()

    def _require_active(self) -> None:
        """Refuse any use of a transaction that has already ended."""
        if self._state is not TransactionState.ACTIVE:
            raise GrafxTransactionStateError(
                f"Transaction {self._txn_id} is {self._state.value} and cannot be used again.",
                txn_id=self._txn_id,
                state=self._state.value,
            )

    def _require_write_mode(self, action: str) -> None:
        """Refuse a write-only action on a read transaction."""
        if self._mode is not TransactionMode.WRITE:
            raise GrafxTransactionStateError(
                f"A read transaction cannot {action}.",
                txn_id=self._txn_id,
                mode=self._mode.value,
            )

    def __repr__(self) -> str:
        """Return a representation naming the transaction, its mode, its snapshot and its state."""
        return (
            f"TransactionContext(txn_id={self._txn_id}, mode={self._mode.value!r}, "
            f"read_lsn={self._snapshot.read_lsn}, state={self._state.value!r})"
        )


def _require_partition(partition: int) -> int:
    """Return the partition key when it is one, else refuse it as a caller mistake."""
    if isinstance(partition, bool) or not isinstance(partition, int):
        raise GrafxConfigurationError(
            f"A partition key must be an integer; got {type(partition).__name__}.",
            field="partition",
            value=repr(partition),
        )
    if partition < 0:
        raise GrafxConfigurationError(
            f"A partition key must not be negative; got {partition}.",
            field="partition",
            value=partition,
        )
    return partition


def _require_table(table: object) -> object:
    """Return the table when a staged row names one, else refuse the staging."""
    if table is None:
        raise GrafxConfigurationError(
            "A staged row must name the table it belongs to.", field="table", value=repr(table)
        )
    return table


def _require_reference(reference: object) -> object:
    """Return the reference when a staged update or delete names a version to act on."""
    if reference is None:
        raise GrafxConfigurationError(
            "A staged update or delete must name the version it acts on.",
            field="reference",
            value=repr(reference),
        )
    return reference
