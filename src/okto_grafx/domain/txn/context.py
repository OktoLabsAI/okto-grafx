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

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
)
from okto_grafx.domain.ids import NO_CSN, Csn, Epoch, PageIndex, RecordRef, TxnId
from okto_grafx.domain.model.schema import encode_tuple
from okto_grafx.domain.txn.partitions import page_partition
from okto_grafx.domain.txn.records import (
    WalRecordLike,
    WalRecordType,
    is_redoable_page_file,
)
from okto_grafx.domain.txn.snapshot import Snapshot

__all__ = [
    "CommitReport",
    "PendingRowRef",
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
class PendingRowRef:
    """Transaction-local identity of an insert that has no physical ``RecordId`` yet.

    The token is deliberately negative. Durable record identities are positive, and a pending
    token must never be mistaken for one or escape to the heap, WAL or an index. ``txn_id`` and
    ``table_id`` make accidental cross-transaction/table reuse fail equality even before the
    commit path performs its stronger validation.
    """

    txn_id: TxnId
    table_id: int
    token: int

    def __post_init__(self) -> None:
        """Refuse values outside the private, non-durable identity domain."""
        for field, value in (("txn_id", self.txn_id), ("table_id", self.table_id)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise GrafxConfigurationError(
                    f"A pending row {field} must be a positive integer; got {value!r}.",
                    field=field,
                    value=repr(value),
                )
        if isinstance(self.token, bool) or not isinstance(self.token, int) or self.token >= 0:
            raise GrafxConfigurationError(
                f"A pending row token must be a negative integer; got {self.token!r}.",
                field="token",
                value=repr(self.token),
            )


@dataclass(frozen=True, slots=True)
class RowIntent:
    """A row this transaction wants written, held until the commit number exists.

    A row cannot be written when the caller asks for it. ``HeapStore.insert`` stamps the version
    with the commit number that makes it visible, and that number is the LSN of the COMMIT
    record, which the log assigns inside the commit itself (CONTRACT.md section 8.5 step 3.4).
    A row written earlier would carry a birth stamp no snapshot rule can make correct: too low
    and transactions that must not see it do, too high and transactions that must see it do not.

    So the intent is staged and the row is written inside the commit section, at the number the
    log actually assigned. ``record_id`` of None means the durable identity is allocated then
    too, which is what keeps an abandoned commit from burning one: an id allocated and abandoned
    leaves a gap in the sequence, and a gap is harmless where a REUSED id is not. An INSERT's
    ``reference`` is only a :class:`PendingRowRef`; it names the intent inside its transaction and
    is never a durable identity.
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
        "_page_image_proofs",
        "_page_staging_capability",
        "_max_transaction_rows",
        "_max_transaction_bytes",
        "_staged_payload_bytes",
        "_staging_marks",
        "_next_pending_token",
        "_pending_row_refs",
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
        page_staging_capability: object,
        max_transaction_rows: int | None = None,
        max_transaction_bytes: int | None = None,
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
        # Physical page images are a private engine capability, not caller-authored data.  The
        # public map remains observable for the frozen TransactionContext contract, so a proof
        # covers both its complete key set and the exact bytes accepted through the private
        # staging door.  Commit recomputes every digest before touching the heap or WAL: adding,
        # replacing or deleting an entry directly makes the whole attempt fail closed.
        self._page_image_proofs: dict[tuple[str, PageIndex], bytes] = {}
        self._page_staging_capability: object = page_staging_capability
        self._max_transaction_rows = _require_optional_positive_limit(
            "max_transaction_rows", max_transaction_rows
        )
        self._max_transaction_bytes = _require_optional_positive_limit(
            "max_transaction_bytes", max_transaction_bytes
        )
        self._staged_payload_bytes: int = 0
        self._staging_marks: list[
            tuple[
                tuple[int, int, int],
                dict[tuple[str, PageIndex], bytes],
                dict[tuple[str, PageIndex], bytes],
                set[int],
                int,
            ]
        ] = []
        self._next_pending_token: int = -1
        # A PendingRowRef is authentic only when this exact object was emitted by this context.
        # txn_id is process-local and tokens restart in every context, so value equality alone
        # cannot distinguish another handle's first insert from this handle's first insert.
        self._pending_row_refs: dict[int, PendingRowRef] = {}
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
        if record.record_type not in (
            int(WalRecordType.INDEX_WRITE),
            int(WalRecordType.INDEX_RECONCILE),
        ):
            raise GrafxConfigurationError(
                "A transaction may stage only logical index effects; WRITE_PAGE and outcome "
                "records are built by the transaction manager.",
                field="record_type",
                value=record.record_type,
            )
        if record.lsn != 0:
            raise GrafxConfigurationError(
                "A staged index record cannot carry a log position before commit assigns it.",
                field="lsn",
                value=record.lsn,
            )
        record_txn_id = getattr(record, "txn_id", self.txn_id)
        if record_txn_id != self.txn_id:
            raise GrafxConfigurationError(
                "A staged index record must belong to the transaction staging it.",
                field="txn_id",
                value=record_txn_id,
                txn_id=self.txn_id,
            )
        record_bytes = self._record_payload_bytes(record)
        self._require_payload_capacity(record_bytes)
        self.pending_records.append(record)
        self._staged_payload_bytes += record_bytes

    def stage_page_image(self, file: str, page_index: PageIndex, image: bytes) -> None:
        """Refuse caller-authored physical pages.

        A page image is not a value-level mutation: it can replace the heap or catalog header
        while remaining checksum-valid.  Only the stores/query engine may therefore stage one,
        through the transaction manager's private capability.  Callers write through statements
        and row/index APIs, whose invariants can be checked at their own abstraction level.
        """
        self._require_active()
        self._require_write_mode("stage a page image")
        raise GrafxConfigurationError(
            "Physical page images are an internal engine capability; use a statement or a "
            "value-level store operation instead of staging encoded bytes.",
            field="page_image_provenance",
            file=file,
            page_index=page_index,
        )

    def _stage_page_image(
        self,
        file: str,
        page_index: PageIndex,
        image: bytes,
        *,
        capability: object,
    ) -> None:
        """Stage a store-produced page after authenticating the manager capability.

        Staging the LAST image for a page replaces the previous one on purpose: a transaction
        that changed one page twice wants one write of the final bytes, not two writes racing to
        be last.
        """
        self._require_active()
        self._require_write_mode("stage a page image")
        if capability is not self._page_staging_capability:
            raise GrafxConfigurationError(
                "A physical page image must be staged by the manager that opened this "
                "transaction.",
                field="page_image_capability",
                txn_id=self.txn_id,
            )
        if not is_redoable_page_file(file):
            raise GrafxConfigurationError(
                "A staged page image must name heap.dat, catalog.dat or a canonical "
                "index/<identifier>.idx file.",
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
        key = (file, page_index)
        accepted = bytes(image)
        previous = self.page_images.get(key)
        if previous is accepted:
            delta = 0
        elif previous is not None and self._image_is_retained_by_mark(key, previous):
            # A live statement mark owns the rollback preimage. Replacing the current map does
            # not release those bytes, so the new image is additional memory, not a delta.
            delta = len(accepted)
        else:
            delta = len(accepted) - (0 if previous is None else len(previous))
        self._require_payload_capacity(delta)
        self.page_images[key] = accepted
        self._page_image_proofs[key] = hashlib.sha256(accepted).digest()
        self._staged_payload_bytes += delta
        # Staging a page IS declaring interest in it. A commit that wrote a whole page image and
        # declared interest in nothing could never be refused by optimistic validation -- the
        # predicate short-circuits on an empty set -- so two participants writing the same page
        # both acknowledged a durable commit and the second image silently replaced the first.
        # The partition names the page, so the frozen COMMIT payload carries it and any
        # participant reading the log can compare against it (defect E1).
        self.write_partitions.add(page_partition(file, page_index))

    def unproved_page_images(self) -> tuple[str, ...]:
        """Return every physical image not exactly covered by a private staging proof.

        The map is deliberately treated as hostile here: it is observable through the engine
        context and Python lets a caller put values outside its annotation into it.  Reporting
        representations keeps the refusal typed even when a malformed key could not be sorted
        beside a legitimate ``(file, page)`` tuple.
        """
        locations = set(self.page_images) | set(self._page_image_proofs)
        unproved: list[str] = []
        for location in locations:
            image = self.page_images.get(location)
            proof = self._page_image_proofs.get(location)
            if (
                not isinstance(location, tuple)
                or len(location) != 2
                or not isinstance(location[0], str)
                or isinstance(location[1], bool)
                or not isinstance(location[1], int)
                or not isinstance(image, bytes)
                or not isinstance(proof, bytes)
                or hashlib.sha256(image).digest() != proof
            ):
                unproved.append(repr(location))
        return tuple(sorted(unproved))

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
        self._require_row_count_capacity()
        accepted_table = _require_table(table)
        accepted_values = tuple(values)
        payload_bytes = self._row_payload_bytes(accepted_table, accepted_values)
        self._require_payload_capacity(payload_bytes)
        pending = self._allocate_pending_row_ref(accepted_table)
        self.row_intents.append(
            RowIntent(
                table=accepted_table,
                values=accepted_values,
                record_id=record_id,
                reference=pending,
            )
        )
        self._staged_payload_bytes += payload_bytes

    def _allocate_pending_row_ref(self, table: object) -> PendingRowRef:
        """Return the next private insert identity without allocating a durable record id."""
        table_id = getattr(table, "table_id", None)
        if isinstance(table_id, bool) or not isinstance(table_id, int) or table_id < 1:
            raise GrafxConfigurationError(
                "A staged row table must carry a positive integer table_id before it can "
                "receive a pending identity.",
                field="table_id",
                value=repr(table_id),
                txn_id=self._txn_id,
            )
        reference = PendingRowRef(
            txn_id=self._txn_id,
            table_id=table_id,
            token=self._next_pending_token,
        )
        self._next_pending_token -= 1
        self._pending_row_refs[reference.token] = reference
        return reference

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
        accepted_table = _require_table(table)
        accepted_reference = self._require_row_reference(accepted_table, reference)
        self._require_row_count_capacity()
        accepted_values = tuple(values)
        payload_bytes = self._row_payload_bytes(accepted_table, accepted_values)
        self._require_payload_capacity(payload_bytes)
        self.row_intents.append(
            RowIntent(
                table=accepted_table,
                values=accepted_values,
                operation=RowOperation.UPDATE,
                reference=accepted_reference,
            )
        )
        self._staged_payload_bytes += payload_bytes

    def stage_row_delete(self, table: object, reference: object) -> None:
        """Stage the end of an existing row, to be written at the commit number.

        A delete ends a version rather than removing it: the bytes stay where they are and the
        header says when they stopped being current, which is what lets a snapshot opened before
        the delete go on reading the row it was entitled to see (BR-9).
        """
        self._require_active()
        self._require_write_mode("stage a row delete")
        accepted_table = _require_table(table)
        accepted_reference = self._require_row_reference(accepted_table, reference)
        self._require_row_count_capacity()
        self.row_intents.append(
            RowIntent(
                table=accepted_table,
                operation=RowOperation.DELETE,
                reference=accepted_reference,
            )
        )

    def staging_mark(self) -> tuple[int, int, int]:
        """Return a mark naming how much this transaction has staged so far.

        The unit a caller discards is the STATEMENT, not the whole transaction: a statement that
        refuses halfway through must leave nothing of itself behind, or a caller that catches the
        refusal and commits makes half a statement durable. A caller takes a mark before a
        statement and discards back to it if the statement refuses. The returned tuple keeps its
        established shape, while its exact object identity authenticates the internal snapshot.
        """
        mark = (len(self.row_intents), len(self.pending_records), len(self.page_images))
        self._staging_marks.append(
            (
                mark,
                dict(self.page_images),
                dict(self._page_image_proofs),
                set(self.write_partitions),
                self._staged_payload_bytes,
            )
        )
        return mark

    def discard_since(self, mark: tuple[int, int, int]) -> None:
        """Drop everything staged after the mark, leaving what was staged before it untouched.

        Page images are keyed by location rather than ordered, so the mark seals an internal
        shallow snapshot of the exact map, its proofs and write partitions. Bytes are immutable:
        restoring that state restores both a replaced image and additions whose key sorts before
        an older key, without copying page payloads or guessing insertion order from a count.
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
        if not self._staging_marks or self._staging_marks[-1][0] is not mark:
            raise GrafxTransactionStateError(
                "That mark is not the most recent unsettled point of this transaction.",
                txn_id=self._txn_id,
                field="mark",
                value=repr(mark),
            )
        (
            _issued,
            page_images,
            page_proofs,
            write_partitions,
            payload_bytes,
        ) = self._staging_marks.pop()
        discarded_intents = tuple(self.row_intents[rows:])
        del self.row_intents[rows:]
        remaining_insert_ids = {
            id(intent.reference)
            for intent in self.row_intents
            if intent.operation is RowOperation.INSERT
            and isinstance(intent.reference, PendingRowRef)
        }
        for intent in discarded_intents:
            reference = intent.reference
            if (
                intent.operation is RowOperation.INSERT
                and isinstance(reference, PendingRowRef)
                and id(reference) not in remaining_insert_ids
                and self._pending_row_refs.get(reference.token) is reference
            ):
                del self._pending_row_refs[reference.token]
        del self.pending_records[records:]
        self.page_images.clear()
        self.page_images.update(page_images)
        self._page_image_proofs.clear()
        self._page_image_proofs.update(page_proofs)
        self.write_partitions.clear()
        self.write_partitions.update(write_partitions)
        self._staged_payload_bytes = payload_bytes

    def settle_staging_mark(self, mark: tuple[int, int, int]) -> None:
        """Forget the exact snapshot after its statement transferred successfully."""
        if not self._staging_marks or self._staging_marks[-1][0] is not mark:
            raise GrafxTransactionStateError(
                "That mark is not the most recent unsettled point of this transaction.",
                txn_id=self._txn_id,
                field="mark",
                value=repr(mark),
            )
        snapshot = self._staging_marks.pop()
        try:
            self._reconcile_staged_payload_bytes()
        except BaseException:
            # Settlement is part of the statement's atomic handover.  Keep its snapshot live
            # when reconciliation refuses (or detects hostile direct mutation), so the caller's
            # failure path can still discard the whole statement rather than commit its rows.
            self._staging_marks.append(snapshot)
            raise

    def validate_budgets(self) -> None:
        """Recompute enabled budgets so direct mutation cannot bypass admission checks."""
        self._require_active()
        if self._max_transaction_rows is not None:
            self._raise_if_over_budget(
                "max_transaction_rows",
                len(self.row_intents),
                self._max_transaction_rows,
            )
        self._reconcile_staged_payload_bytes()

    def _reconcile_staged_payload_bytes(self) -> None:
        """Rebuild retained-payload accounting after an unwind or hostile direct mutation."""
        if self._max_transaction_bytes is None:
            return
        observed = sum(self._intent_payload_bytes(intent) for intent in self.row_intents)
        observed += sum(self._record_payload_bytes(record) for record in self.pending_records)
        observed += self._retained_page_payload_bytes()
        self._raise_if_over_budget(
            "max_transaction_bytes", observed, self._max_transaction_bytes
        )
        self._staged_payload_bytes = observed

    def _retained_page_payload_bytes(self) -> int:
        """Count current images and distinct rollback preimages still held by live marks."""
        seen: set[tuple[tuple[str, PageIndex], int]] = set()
        total = 0
        for location, image in self.page_images.items():
            seen.add((location, id(image)))
            total += len(image)
        for (
            _mark,
            images,
            _proofs,
            _partitions,
            _payload_bytes,
        ) in self._staging_marks:
            for location, image in images.items():
                identity = (location, id(image))
                if identity in seen:
                    continue
                seen.add(identity)
                total += len(image)
        return total

    def _image_is_retained_by_mark(
        self, location: tuple[str, PageIndex], image: bytes
    ) -> bool:
        """Return whether a live mark keeps this exact page generation as a preimage."""
        return any(
            images.get(location) is image
            for _mark, images, _proofs, _partitions, _size in self._staging_marks
        )

    def _require_row_count_capacity(self) -> None:
        """Refuse the next row before consuming or retaining its values."""
        if self._max_transaction_rows is not None:
            self._raise_if_over_budget(
                "max_transaction_rows",
                len(self.row_intents) + 1,
                self._max_transaction_rows,
            )

    def _require_payload_capacity(self, additional_bytes: int) -> None:
        """Refuse an addition that would cross the configured retained-payload budget."""
        if self._max_transaction_bytes is None:
            return
        self._raise_if_over_budget(
            "max_transaction_bytes",
            self._staged_payload_bytes + additional_bytes,
            self._max_transaction_bytes,
        )

    def _raise_if_over_budget(self, field: str, observed: int, limit: int) -> None:
        """Raise the stable transaction-budget refusal for one exceeded limit."""
        if observed <= limit:
            return
        raise GrafxTransactionBudgetExceeded(
            f"Transaction {self._txn_id} would exceed {field}: limit {limit}, "
            f"observed {observed}.",
            field=field,
            limit=limit,
            observed=observed,
            txn_id=self._txn_id,
        )

    def _row_payload_bytes(self, table: object, values: tuple[object, ...]) -> int:
        """Return the canonical encoded payload size of one inserted or updated row."""
        if self._max_transaction_bytes is None:
            return 0
        return len(encode_tuple(table, values))  # type: ignore[arg-type]

    def _intent_payload_bytes(self, intent: RowIntent) -> int:
        """Return retained payload bytes for one validated row intent."""
        if not isinstance(intent, RowIntent):
            raise GrafxConfigurationError(
                "A transaction row budget can account only for RowIntent values.",
                field="row_intents",
                value=type(intent).__name__,
                txn_id=self._txn_id,
            )
        if intent.operation is RowOperation.DELETE:
            return 0
        return self._row_payload_bytes(intent.table, intent.values)

    def _record_payload_bytes(self, record: WalRecordLike) -> int:
        """Return the exact encoded size retained by one staged logical WAL record."""
        if self._max_transaction_bytes is None:
            return 0
        encoded_length = getattr(record, "encoded_length", None)
        if not callable(encoded_length):
            raise GrafxConfigurationError(
                "A staged logical record needs encoded_length() when a transaction byte "
                "budget is configured.",
                field="pending_records",
                value=type(record).__name__,
                txn_id=self._txn_id,
            )
        observed = encoded_length()
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise GrafxConfigurationError(
                "A staged logical record reported an invalid encoded length.",
                field="pending_records",
                value=repr(observed),
                txn_id=self._txn_id,
            )
        return observed

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
        self._page_image_proofs.clear()
        self.row_intents.clear()
        self.row_refs.clear()
        self._staged_payload_bytes = 0
        self._next_pending_token = -1
        self._pending_row_refs.clear()
        self._staging_marks.clear()

    def owns_pending_row_ref(self, reference: PendingRowRef) -> bool:
        """Return whether this exact live pending identity was emitted by this context."""
        return self._pending_row_refs.get(reference.token) is reference

    def _require_row_reference(self, table: object, reference: object) -> object:
        """Accept a physical ref or an authentic pending ref for this exact table."""
        accepted = _require_reference(reference)
        if isinstance(accepted, RecordRef):
            return accepted
        if not isinstance(accepted, PendingRowRef):
            raise GrafxConfigurationError(
                "A staged update or delete must name a physical or pending row reference.",
                field="reference",
                value=repr(accepted),
                txn_id=self._txn_id,
            )
        table_id = getattr(table, "table_id", None)
        if (
            accepted.txn_id != self._txn_id
            or accepted.table_id != table_id
            or not self.owns_pending_row_ref(accepted)
        ):
            raise GrafxTransactionStateError(
                "A pending row reference may be used only by the transaction and table that "
                "issued its still-live insert.",
                field="pending_row_reference",
                value=repr(accepted),
                txn_id=self._txn_id,
                table_id=table_id,
            )
        return accepted

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


def _require_optional_positive_limit(field: str, value: int | None) -> int | None:
    """Return an exact optional positive limit for direct context composition."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GrafxConfigurationError(
            f"{field} must be a positive integer or None; got {value!r}.",
            field=field,
            value=repr(value),
        )
    return int(value)


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
