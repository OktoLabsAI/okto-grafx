"""The durable unapplied-work ledger (CONTRACT.md section 8.6, SPEC-M1 FR-9, TR-5, AC-12).

``ledger/ledger.log`` is an append-only run of the entries of section 6.6, each carrying its own
checksum and its own digest. Nothing in this component ever rewrites an entry: the only door that
removes bytes is :meth:`LedgerStore.purge`, which is an explicit operator act with a dedicated
token, and even that refuses to touch the receipts exactly-once rests on.

Four decisions are worth stating, because each is the reason a class of defect cannot happen.

**A partial entry at the tail is expected, not exceptional.** TR-5 asks the ledger to survive a
crash during recovery itself, and a crash during an append leaves exactly that. The load stops at
the first entry that will not decode, remembers where it stopped, and REFUSES further appends
until the tail has been dealt with. It does not quietly overwrite the bytes:
:meth:`discard_damaged_tail` copies the whole unreadable range into quarantine ITSELF before it
cuts, and refuses when there is no quarantine to copy into. Preserve-then-destroy is one door
rather than two calls, so no caller can perform the second half alone.

**Idempotence is a durable receipt, not a memory.** ``reprocess`` writes a
``REPROCESS_RECEIPT`` entry keyed on the origin sequence number, so repeating it after a restart
is still a no-op (AC-12). A receipt is a ledger entry like any other, which is what makes it
survive the crash the whole feature exists for.

**A retry decision reads ``details["retryable"]``** (A47). A ledger append that meets a transient
device condition is retried inside a bounded budget, because a missing entry is a test failure
under G8 and an antivirus touch is not a reason to lose the trace. A permanent failure is raised
at once; nothing sleeps, because the domain has no clock to sleep on.

**Nothing here holds a lock, so nothing here can deadlock on host code** (A91, LESSONS L2). The
applier ``reprocess`` calls is host-supplied and is called with no internal state in flight: the
entry is read, the receipt search is finished, and only then is the applier invoked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxLedgerError,
    GrafxPortNotConfigured,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.ledger.entry import (
    LEDGER_ENTRY_HEADER_LENGTH,
    LedgerEntry,
    LedgerEntryType,
    LedgerOriginClass,
    LedgerReason,
)
from okto_grafx.domain.ledger.payload import (
    LedgerPayload,
    decode_envelope,
    decode_payload,
    encode_payload,
)
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.recovery.retry import is_retryable
from okto_grafx.domain.wal.codec import decode_record
from okto_grafx.domain.wal.record import WalRecord
from okto_grafx.engine.metrics_catalog import metric
from okto_grafx.engine.quarantine import QuarantineStore

__all__ = [
    "APPEND_ATTEMPTS",
    "DEFAULT_LIST_LIMIT",
    "LEDGER_DIRECTORY",
    "LEDGER_FILE",
    "LEDGER_METRICS",
    "LEDGER_DEPTH",
    "LEDGER_OLDEST_ENTRY_AGE_SECONDS",
    "MAX_LEDGER_BYTES",
    "PURGE_CONFIRM_TOKEN",
    "STORAGE_PORT_METHODS",
    "CLOCK_PORT_METHODS",
    "METRICS_PORT_METHODS",
    "DamagedTail",
    "LedgerApplier",
    "LedgerStore",
    "ReprocessReport",
    "TailDiscard",
]

LEDGER_DIRECTORY: str = "ledger"
"""Where the ledger lives inside a database directory (CONTRACT.md section 6.1)."""

LEDGER_FILE: str = "ledger/ledger.log"
"""The append-only file every entry is written to."""

PURGE_CONFIRM_TOKEN: str = "purge-ledger-entries"
"""The exact token an operator must pass to remove entries (FR-9: an explicit act only)."""

DEFAULT_LIST_LIMIT: int = 100
"""How many entries ``list`` returns when the caller does not say."""

APPEND_ATTEMPTS: int = 3
"""How many times an append rides out a device condition its details declare retryable (A47)."""

MAX_LEDGER_BYTES: int = 1 << 31
"""Largest ledger this reader will load in one pass, so a damaged size buys no unbounded work."""

LEDGER_DEPTH: str = "oktografx_ledger_depth"
"""Gauge of how many entries are waiting, by origin class (CONTRACT.md section 9)."""

LEDGER_OLDEST_ENTRY_AGE_SECONDS: str = "oktografx_ledger_oldest_entry_age_seconds"
"""Gauge of the age of the oldest entry, by origin class (CONTRACT.md section 9)."""

LEDGER_METRICS: tuple[MetricDescriptor, ...] = (
    metric(LEDGER_DEPTH),
    metric(LEDGER_OLDEST_ENTRY_AGE_SECONDS),
)
"""Every metric this store emits, taken from the frozen catalogue by name and never invented."""

STORAGE_PORT_METHODS: tuple[str, ...] = (
    "exists",
    "create",
    "append_log",
    "read_log",
    "log_size",
    "truncate_log",
    "atomic_replace",
    "durable_barrier",
    "remove",
)
"""The storage doors this store opens. A port missing one of them is refused at construction."""

CLOCK_PORT_METHODS: tuple[str, ...] = ("wall",)
"""The clock reading an entry is stamped with. Never ``monotonic``: this is a human-facing time."""

METRICS_PORT_METHODS: tuple[str, ...] = ("enabled", "register", "set_gauge")
"""The metrics doors this store uses."""

_ORIGIN_LABELS: dict[LedgerOriginClass, Mapping[str, str]] = {
    LedgerOriginClass.REAPPLICABLE: {"origin_class": "reapplicable"},
    LedgerOriginClass.FORENSIC: {"origin_class": "forensic"},
}

LedgerApplier = Callable[[WalRecord], None]
"""What ``reprocess`` hands a decoded operation to. Host-supplied, called with no lock held."""


@dataclass(frozen=True, slots=True)
class DamagedTail:
    """Where the ledger stopped reading, and how many bytes past that point are unreadable."""

    offset: int
    length: int
    detail: str


@dataclass(frozen=True, slots=True)
class TailDiscard:
    """What discarding an unreadable ledger tail preserved, and then removed."""

    offset: int
    removed_bytes: int
    quarantine: str


@dataclass(frozen=True, slots=True)
class ReprocessReport:
    """What one attempt to reapply a ledger entry did (FR-9, AC-12)."""

    entry_id: int
    origin_lsn: Lsn
    applied: bool
    already_applied: bool
    receipt_id: int = 0


def _require_port(slot: str, instance: object, methods: Sequence[str]) -> None:
    """Refuse a port that cannot answer the doors this store opens (G5, fail-closed)."""
    missing = [name for name in methods if not hasattr(instance, name)]
    if missing:
        raise GrafxPortNotConfigured(
            f"The {slot} port of the ledger is missing {', '.join(missing)}.",
            slot=slot,
            missing=tuple(missing),
        )


def _require_identifier(field: str, value: object) -> int:
    """Return a positive entry identifier, refusing anything that could not be one."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"A ledger {field} must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if value <= 0:
        raise GrafxConfigurationError(
            f"A ledger {field} is positive; got {value}.", field=field, value=value
        )
    return value


class LedgerStore:
    """The append-only ledger of one database."""

    __slots__ = (
        "_storage",
        "_clock",
        "_metrics",
        "_file",
        "_loaded",
        "_entries",
        "_offsets",
        "_next_id",
        "_size",
        "_damage",
        "_quarantine",
        "_identities",
    )

    def __init__(
        self,
        storage: StorageDevice,
        clock: Clock,
        metrics: MetricsSink,
        *,
        file: str = LEDGER_FILE,
        quarantine: QuarantineStore | None = None,
    ) -> None:
        """Build the store over its three ports. Nothing touches the device until it is used.

        The quarantine is optional and is needed for exactly one door:
        :meth:`discard_damaged_tail`, the only place this store destroys bytes. Without it that
        door refuses, so a store built without a quarantine can read, append and purge but can
        never destroy an unreadable tail -- which is the fail-closed direction, and the one that
        cannot lose evidence by omission.
        """
        _require_port("storage", storage, STORAGE_PORT_METHODS)
        _require_port("clock", clock, CLOCK_PORT_METHODS)
        _require_port("metrics", metrics, METRICS_PORT_METHODS)
        self._storage: StorageDevice = storage
        self._clock: Clock = clock
        self._metrics: MetricsSink = metrics
        self._file: str = _validate_file(file)
        self._loaded: bool = False
        self._entries: list[LedgerEntry] = []
        self._offsets: list[int] = []
        self._next_id: int = 1
        self._size: int = 0
        self._damage: DamagedTail | None = None
        self._identities: dict[tuple[int, str, int, int], int] = {}
        if quarantine is not None and not isinstance(quarantine, QuarantineStore):
            raise GrafxConfigurationError(
                f"The ledger takes a QuarantineStore; got {type(quarantine).__name__}.",
                field="quarantine",
                value=type(quarantine).__name__,
            )
        self._quarantine: QuarantineStore | None = quarantine
        if self._metrics.enabled:
            for declared in LEDGER_METRICS:
                self._metrics.register(declared)

    # --- state -------------------------------------------------------------------------------

    @property
    def file(self) -> str:
        """Return the name of the file this ledger appends to."""
        return self._file

    @property
    def damage(self) -> DamagedTail | None:
        """Return the unreadable tail the last load stopped at, or None when the file reads clean.

        While this is set every append is refused. The way out is
        :meth:`discard_damaged_tail`, and recovery calls it only after the bytes have been copied
        into quarantine -- preserving evidence before destroying it is the whole of FR-10.
        """
        self._ensure_loaded()
        return self._damage

    def open(self) -> None:
        """Read the file and rebuild the index of what it holds."""
        self._load()

    def entries(self) -> tuple[LedgerEntry, ...]:
        """Return every entry the ledger currently holds, oldest first."""
        self._ensure_loaded()
        return tuple(self._entries)

    def depth(self) -> Mapping[str, int]:
        """Return how many entries are waiting, by origin class (CONTRACT.md section 8.6).

        The keys are exactly the two label values of ``oktografx_ledger_depth``, so the gauge and
        this answer can never disagree about what is being counted.
        """
        self._ensure_loaded()
        counts = {label["origin_class"]: 0 for label in _ORIGIN_LABELS.values()}
        for entry in self._entries:
            if entry.entry_type is LedgerEntryType.DISCARD:
                counts[_ORIGIN_LABELS[entry.origin_class]["origin_class"]] += 1
        return counts

    # --- writing -----------------------------------------------------------------------------

    def append(self, entry: LedgerEntry) -> int:
        """Append one entry, make it durable, and return the identifier it was given.

        An entry that carries no identifier is given the next one; an entry that carries one must
        not collide with an entry already stored, because an identifier is how ``inspect`` and
        ``reprocess`` name their subject. The capture time is stamped from the wall clock when the
        caller left it at zero -- a human-facing timestamp, which section 4.2 says is what
        ``wall`` is for.
        """
        if not isinstance(entry, LedgerEntry):
            raise GrafxConfigurationError(
                f"A ledger append takes a LedgerEntry; got {type(entry).__name__}.",
                field="entry",
                value=type(entry).__name__,
            )
        self._ensure_loaded()
        self._require_healthy()
        self._require_unrecorded(entry)
        entry_id = entry.entry_id or self._next_id
        if any(stored.entry_id == entry_id for stored in self._entries):
            raise GrafxLedgerError(
                f"Ledger entry {entry_id} already exists, so appending it again would give one "
                "identifier two meanings.",
                field="entry_id",
                entry_id=entry_id,
            )
        captured = entry.captured_at_wall or float(self._clock.wall())
        stored_entry = LedgerEntry(
            entry_id=entry_id,
            origin_class=entry.origin_class,
            reason=entry.reason,
            payload=entry.payload,
            entry_type=entry.entry_type,
            lsn_start=entry.lsn_start,
            lsn_end=entry.lsn_end,
            epoch=entry.epoch,
            captured_at_wall=captured,
            format_version=entry.format_version,
            reserved=entry.reserved,
        )
        encoded = stored_entry.encode()
        offset = self._size
        self._append_bytes(encoded)
        self._entries.append(stored_entry)
        self._offsets.append(offset)
        self._size += len(encoded)
        self._next_id = max(self._next_id, entry_id + 1)
        self._remember(stored_entry)
        self._publish_depth()
        return entry_id

    def record_discard(
        self,
        *,
        origin_class: LedgerOriginClass,
        reason: LedgerReason,
        payload: LedgerPayload,
        lsn_start: Lsn = NO_LSN,
        lsn_end: Lsn = NO_LSN,
        epoch: int = 0,
    ) -> int:
        """Append the entry one discarded item owes, and return its identifier (G8, BR-3).

        This is the door recovery uses, and it exists so that no caller has to assemble an
        envelope by hand: the provenance and the bytes go in together, which is what makes the
        digest in the frozen header cover both.
        """
        return self._record_once(
            LedgerEntry(
                entry_id=0,
                origin_class=origin_class,
                reason=reason,
                payload=encode_payload(payload),
                entry_type=LedgerEntryType.DISCARD,
                lsn_start=lsn_start,
                lsn_end=lsn_end,
                epoch=epoch,
            ),
            payload,
        )

    def record_retirement(
        self, *, payload: LedgerPayload, reason: LedgerReason = LedgerReason.QUARANTINED_SEGMENT
    ) -> int:
        """Append the forensic entry a control-record retirement owes (carried finding CF-1).

        C4 states the order and this component honours it: quarantine the record with a manifest,
        write THIS entry, retire the name, report it. The entry is written before the name goes,
        so a crash between the two leaves the trace and not the silence.
        """
        return self._record_once(
            LedgerEntry(
                entry_id=0,
                origin_class=LedgerOriginClass.FORENSIC,
                reason=reason,
                payload=encode_payload(payload),
                entry_type=LedgerEntryType.RETIREMENT,
            ),
            payload,
        )

    def _record_once(self, entry: LedgerEntry, payload: LedgerPayload) -> int:
        """Append this entry, or return the identifier of the one already describing that damage.

        Recovery is re-run every time a database opens, and it can be interrupted between the
        quarantine copy and the cut. Without this, each re-run appended ANOTHER entry for the same
        record: the quarantine stayed at one copy while the ledger grew without bound, and the
        report said one discard and one entry while the file held four. That breaks CONTRACT.md
        section 8.6 step 4 -- *exactly* one entry per discarded record (G8, BR-3) -- and it breaks
        the idempotence recovery rests on.

        The key is the damage identity, and it is deliberately the SAME identity the quarantine
        keys its capture on: the origin, the offset and the length of the range, plus what kind of
        entry describes it. Keying the two stores the same way is what makes them agree by
        construction rather than by coincidence -- a range whose copy is recognised is a range
        whose entry is recognised.

        Proven by ``test_recovery_interrupted_before_the_cut_writes_one_entry_not_many`` and
        ``test_a_crash_at_every_write_point_of_recovery_leaves_one_entry_per_discard``.
        """
        self._ensure_loaded()
        identity = _identity_of(entry.entry_type, payload)
        existing = self._identities.get(identity)
        if existing is not None:
            return existing
        return self.append(entry)

    # --- reading -----------------------------------------------------------------------------

    def list(
        self,
        *,
        origin_class: LedgerOriginClass | str | None = None,
        reason: LedgerReason | str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> tuple[LedgerEntry, ...]:
        """Return a page of entries, oldest first, filtered by class and by reason.

        The filters accept either the enumeration member or its name, because the operator-facing
        surface of section 10 passes strings (``ledger.list(origin_class="forensic")``) and the
        engine passes members.
        """
        self._ensure_loaded()
        wanted_class = _as_origin_class(origin_class)
        wanted_reason = _as_reason(reason)
        limit = _require_count("limit", limit)
        offset = _require_count("offset", offset)
        selected = [
            entry
            for entry in self._entries
            if (wanted_class is None or entry.origin_class is wanted_class)
            and (wanted_reason is None or entry.reason is wanted_reason)
        ]
        return tuple(selected[offset : offset + limit])

    def inspect(self, entry_id: int) -> LedgerEntry:
        """Return one entry by identifier, refusing an identifier the ledger does not hold."""
        self._ensure_loaded()
        wanted = _require_identifier("entry_id", entry_id)
        for entry in self._entries:
            if entry.entry_id == wanted:
                return entry
        raise GrafxLedgerError(
            f"The ledger holds no entry {wanted}.", field="entry_id", entry_id=wanted
        )

    def export(self, entry_id: int) -> bytes:
        """Return the raw bytes a forensic entry preserved, after proving they are unchanged.

        The digest in the frozen header covers the whole payload -- provenance and bytes together
        -- so verifying it here and returning only the body is what makes the export a claim
        rather than a copy: these are the bytes that were captured, and the entry says so.

        A range too large to carry in an entry is refused by NAMING the quarantine entry that
        holds it, never by returning what happens to be there. Returning a truncated range under
        a digest that says the entry is intact is the one thing an export must not do.
        """
        entry = self.inspect(entry_id)
        if entry.reapplicable:
            raise GrafxLedgerError(
                f"Ledger entry {entry.entry_id} is reapplicable, so it carries an operation "
                "rather than preserved bytes; reprocess it instead of exporting it.",
                field="origin_class",
                entry_id=entry.entry_id,
            )
        stored = self._read_entry_at(self._offset_of(entry.entry_id))
        if stored.digest != entry.digest:
            raise GrafxLedgerError(
                f"The payload of ledger entry {entry.entry_id} no longer matches its digest.",
                field="digest",
                entry_id=entry.entry_id,
            )
        provenance = decode_payload(stored.payload)
        if not provenance.body and provenance.length:
            raise GrafxLedgerError(
                f"Ledger entry {entry.entry_id} preserves {provenance.length} bytes that were too "
                f"large to carry here; read them from quarantine entry "
                f"{provenance.quarantine!r} instead.",
                field="quarantine",
                entry_id=entry.entry_id,
                quarantine=provenance.quarantine,
            )
        return provenance.body

    def provenance(self, entry_id: int) -> LedgerPayload:
        """Return the envelope of one entry: where the bytes came from and why they were lost."""
        return decode_payload(self.inspect(entry_id).payload)

    # --- reapplying --------------------------------------------------------------------------

    def reprocess(self, entry_id: int, applier: LedgerApplier) -> ReprocessReport:
        """Hand a reapplicable entry back to an applier, exactly once (FR-9, AC-12).

        Exactly-once is a durable receipt keyed on the origin sequence number, not a flag in
        memory: repeating the call after a restart still finds the receipt and still does
        nothing. A forensic entry is refused by CLASS and never by inspection -- offering an
        applier bytes that never decoded is how a replay of garbage starts.

        The applier is host-supplied code. It is called with nothing of this store's state in
        flight and with no lock held anywhere in the component, which is what A91 and LESSONS L2
        ask of every boundary where our code calls the host's.
        """
        entry = self.inspect(entry_id)
        if not callable(applier):
            raise GrafxConfigurationError(
                f"A ledger applier must be callable; got {type(applier).__name__}.",
                field="applier",
                value=type(applier).__name__,
            )
        if not entry.reapplicable:
            raise GrafxLedgerError(
                f"Ledger entry {entry.entry_id} is forensic: its bytes never decoded, so there "
                "is no operation to reapply. Export it instead.",
                field="origin_class",
                entry_id=entry.entry_id,
            )
        if entry.entry_type is not LedgerEntryType.DISCARD:
            raise GrafxLedgerError(
                f"Ledger entry {entry.entry_id} is a {entry.entry_type.name.lower()} receipt and "
                "not lost work.",
                field="entry_type",
                entry_id=entry.entry_id,
            )
        origin_lsn = entry.lsn_start
        if origin_lsn == NO_LSN:
            raise GrafxLedgerError(
                f"Ledger entry {entry.entry_id} carries no origin sequence number, so applying it "
                "exactly once could not be established.",
                field="lsn_start",
                entry_id=entry.entry_id,
            )
        existing = self._receipt_for(origin_lsn)
        if existing is not None:
            return ReprocessReport(
                entry_id=entry.entry_id,
                origin_lsn=origin_lsn,
                applied=False,
                already_applied=True,
                receipt_id=existing.entry_id,
            )
        record = self._decode_operation(entry)
        try:
            applier(record)
        except GrafxError:
            raise
        except Exception as failure:  # noqa: BLE001 - only Grafx errors leave a public door
            raise GrafxLedgerError(
                f"The applier of ledger entry {entry.entry_id} failed: {failure}",
                field="applier",
                entry_id=entry.entry_id,
                applier_error=type(failure).__name__,
            ) from failure
        receipt_id = self.append(
            LedgerEntry(
                entry_id=0,
                origin_class=LedgerOriginClass.REAPPLICABLE,
                reason=entry.reason,
                payload=encode_payload(
                    LedgerPayload(
                        origin=decode_payload(entry.payload).origin,
                        expected_lsn=origin_lsn,
                        detail=f"Ledger entry {entry.entry_id} was reapplied.",
                    )
                ),
                entry_type=LedgerEntryType.REPROCESS_RECEIPT,
                lsn_start=origin_lsn,
                lsn_end=origin_lsn,
                epoch=entry.epoch,
            )
        )
        return ReprocessReport(
            entry_id=entry.entry_id,
            origin_lsn=origin_lsn,
            applied=True,
            already_applied=False,
            receipt_id=receipt_id,
        )

    # --- operator acts -----------------------------------------------------------------------

    def purge(self, *, confirm_token: str, entry_ids: Sequence[int] | None = None) -> int:
        """Remove entries, and return how many went. An explicit operator act only (FR-9).

        Two refusals guard it. The token must match exactly, so no code path can purge by
        accident; and a ``REPROCESS_RECEIPT`` is never removed, because a receipt is what makes
        ``reprocess`` exactly-once and purging one would let an operation be applied a second
        time. Naming a receipt explicitly is refused rather than ignored -- silently keeping
        something the caller asked to delete is its own kind of lie.
        """
        if confirm_token != PURGE_CONFIRM_TOKEN:
            raise GrafxLedgerError(
                "Purging the ledger needs the exact confirmation token; nothing was removed.",
                field="confirm_token",
            )
        self._ensure_loaded()
        self._require_healthy()
        if entry_ids is None:
            doomed = {
                entry.entry_id
                for entry in self._entries
                if entry.entry_type is not LedgerEntryType.REPROCESS_RECEIPT
            }
        else:
            doomed = set()
            for raw in _require_identifier_sequence(entry_ids):
                wanted = _require_identifier("entry_id", raw)
                entry = self.inspect(wanted)
                if entry.entry_type is LedgerEntryType.REPROCESS_RECEIPT:
                    raise GrafxLedgerError(
                        f"Ledger entry {wanted} is the receipt that keeps its operation from "
                        "being applied twice, so it is never purged.",
                        field="entry_type",
                        entry_id=wanted,
                    )
                doomed.add(wanted)
        if not doomed:
            return 0
        survivors = [entry for entry in self._entries if entry.entry_id not in doomed]
        self._rewrite(survivors)
        self._publish_depth()
        return len(doomed)

    def discard_damaged_tail(self) -> TailDiscard:
        """Preserve the unreadable tail in quarantine, then cut it, and say what happened.

        This is the only door of this component that destroys ledger bytes, and the preservation
        is not a separate call a caller could forget: the copy happens HERE, before the
        truncation, and the door refuses outright when no quarantine is configured to copy into.
        The reason is specific rather than defensive. The load stops at the FIRST entry that will
        not decode, so a single flipped byte early in the file makes everything after it
        unreadable too -- and cutting there would destroy entries that are perfectly good, which
        is precisely the loss G8 exists to prevent. Copying the whole range first turns that from
        a loss into a move.

        Calling it on a healthy ledger is refused rather than treated as a no-op: a caller that
        believes there is damage and is wrong should learn that here.

        Proven by ``test_discarding_the_damaged_tail_preserves_it_in_quarantine_first``,
        ``test_a_damaged_first_entry_does_not_destroy_the_entries_behind_it`` and
        ``test_discarding_a_tail_with_no_quarantine_to_copy_into_is_refused``.
        """
        self._ensure_loaded()
        damage = self._damage
        if damage is None:
            raise GrafxLedgerError(
                f"The ledger {self._file!r} reads clean, so there is no damaged tail to discard.",
                field="damage",
                file=self._file,
            )
        if self._quarantine is None:
            raise GrafxLedgerError(
                f"The unreadable tail of {self._file!r} cannot be discarded because no quarantine "
                "is configured to preserve it first; nothing was destroyed.",
                field="quarantine",
                file=self._file,
            )
        entry = self._quarantine.capture(
            origin=self._file,
            offset=damage.offset,
            length=damage.length,
            reason=LedgerReason.TRUNCATED_TAIL.name.lower(),
            detail=damage.detail,
        )
        cut = damage.offset
        removed = self._storage.log_size(self._file) - cut
        self._storage.truncate_log(self._file, cut)
        self._storage.durable_barrier(self._file)
        self._load()
        return TailDiscard(offset=cut, removed_bytes=max(removed, 0), quarantine=entry.name)

    # --- internals ---------------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Read the file once, so every public door works over the same index."""
        if not self._loaded:
            self._load()

    def _load(self) -> None:
        """Rebuild the index from what the device currently holds."""
        self._entries = []
        self._offsets = []
        self._identities = {}
        self._damage = None
        self._next_id = 1
        self._size = 0
        self._loaded = True
        if not self._storage.exists(self._file):
            self._publish_depth()
            return
        size = self._storage.log_size(self._file)
        if size > MAX_LEDGER_BYTES:
            raise GrafxCorruptionDetected(
                f"The ledger {self._file!r} holds {size} bytes, past the {MAX_LEDGER_BYTES} this "
                "reader will load.",
                file=self._file,
                field="log_size",
                size=size,
            )
        data = self._storage.read_log(self._file, 0, size) if size else b""
        offset = 0
        while offset < len(data):
            try:
                entry = LedgerEntry.decode(data, offset)
            except (GrafxCorruptionDetected, GrafxSchemaVersionMismatch) as failure:
                self._damage = DamagedTail(
                    offset=offset, length=len(data) - offset, detail=failure.message
                )
                break
            self._entries.append(entry)
            self._offsets.append(offset)
            offset += entry.encoded_length()
            self._next_id = max(self._next_id, entry.entry_id + 1)
            self._remember(entry)
        self._size = offset
        self._publish_depth()

    def _require_unrecorded(self, entry: LedgerEntry) -> None:
        """Refuse a second entry describing damage an entry already describes (G8, BR-3).

        The typed doors short-circuit before reaching this: they return the identifier that
        already describes the range, which is what makes an interrupted recovery converge. This
        is the same rule stated at the RAW door, where there is no sensible identifier to return
        -- a caller that assembled an entry by hand and appended it twice gets told, rather than
        quietly doubling the ledger. An entry whose envelope will not parse carries no identity
        and is appended unexamined.

        Proven by ``test_appending_the_same_damage_twice_through_the_raw_door_is_refused``, which
        goes through ``append`` rather than through a typed door: the typed doors short-circuit
        above this line, so a test that used one would be satisfied by ``_record_once`` and would
        say nothing about this guard (A62).
        """
        if entry.entry_type is LedgerEntryType.REPROCESS_RECEIPT:
            return
        try:
            envelope = decode_envelope(entry.payload)
        except GrafxError:
            return
        existing = self._identities.get(_identity_of(entry.entry_type, envelope))
        if existing is not None:
            raise GrafxLedgerError(
                f"Ledger entry {existing} already describes {envelope.length} bytes at byte "
                f"{envelope.offset} of {envelope.origin!r}, and lost work leaves exactly one "
                "entry.",
                field="identity",
                entry_id=existing,
                file=envelope.origin,
                offset=envelope.offset,
            )

    def _remember(self, entry: LedgerEntry) -> None:
        """Index one entry by the damage it describes, so a re-run recognises it.

        An entry whose envelope will not parse is simply left out of the index: it is already on
        the device and nothing here rewrites it, and refusing to index it only means a re-run
        would add a second entry -- which is the state before this index existed, not a worse one.
        """
        if entry.entry_type is LedgerEntryType.REPROCESS_RECEIPT:
            return
        try:
            envelope = decode_envelope(entry.payload)
        except GrafxError:
            return
        self._identities.setdefault(_identity_of(entry.entry_type, envelope), entry.entry_id)

    def _require_healthy(self) -> None:
        """Refuse to write while an unreadable tail sits between here and the end of the file."""
        if self._damage is not None:
            raise GrafxLedgerError(
                f"The ledger {self._file!r} has {self._damage.length} unreadable bytes at byte "
                f"{self._damage.offset}; quarantine them and discard the damaged tail before "
                "appending, or a new entry would sit behind bytes no reader can pass.",
                file=self._file,
                field="damage",
                offset=self._damage.offset,
            )

    def _append_bytes(self, encoded: bytes) -> None:
        """Append one encoded entry durably, riding out a device condition that says retry.

        The retry predicate reads ``details["retryable"]`` and never the exception class, which is
        exactly what A47 requires: a sharing violation reported through a barrier failure and the
        same condition reported through an append must be treated the same way.
        """
        if not self._storage.exists(self._file):
            self._storage.create(self._file, exclusive=False)
        attempt = 1
        while True:
            try:
                self._storage.append_log(self._file, encoded)
                self._storage.durable_barrier(self._file)
                return
            except GrafxError as failure:
                if attempt >= APPEND_ATTEMPTS or not is_retryable(failure):
                    raise
                attempt += 1

    def _rewrite(self, survivors: Sequence[LedgerEntry]) -> None:
        """Publish a new ledger holding exactly these entries, atomically.

        The replacement is built beside the ledger and published by rename, so an interruption
        leaves either the whole old ledger or the whole new one. A half-written purge would be
        the one way this component could lose an entry it promised to keep.
        """
        staging = f"{self._file}.compact"
        if self._storage.exists(staging):
            self._storage.remove(staging)
        self._storage.create(staging, exclusive=False)
        for entry in survivors:
            self._storage.append_log(staging, entry.encode())
        self._storage.durable_barrier(staging)
        self._storage.atomic_replace(staging, self._file)
        self._storage.durable_barrier(self._file)
        self._load()

    def _offset_of(self, entry_id: int) -> int:
        """Return the byte offset of one entry inside the file."""
        for entry, offset in zip(self._entries, self._offsets):
            if entry.entry_id == entry_id:
                return offset
        raise GrafxLedgerError(
            f"The ledger holds no entry {entry_id}.", field="entry_id", entry_id=entry_id
        )

    def _read_entry_at(self, offset: int) -> LedgerEntry:
        """Read one entry straight off the device, so an export proves the stored bytes."""
        available = self._storage.log_size(self._file) - offset
        if available < LEDGER_ENTRY_HEADER_LENGTH:
            raise GrafxCorruptionDetected(
                f"The ledger {self._file!r} ends before the entry at byte {offset}.",
                file=self._file,
                offset=offset,
                field="total_length",
            )
        head = self._storage.read_log(self._file, offset, LEDGER_ENTRY_HEADER_LENGTH)
        declared = int.from_bytes(head[8:12], "little")
        if declared > available:
            raise GrafxCorruptionDetected(
                f"The entry at byte {offset} of {self._file!r} declares {declared} bytes and "
                f"only {available} remain.",
                file=self._file,
                offset=offset,
                field="total_length",
            )
        return LedgerEntry.decode(self._storage.read_log(self._file, offset, declared))

    def _receipt_for(self, origin_lsn: Lsn) -> LedgerEntry | None:
        """Return the receipt that already reapplied this origin, or None when there is none."""
        for entry in self._entries:
            if (
                entry.entry_type is LedgerEntryType.REPROCESS_RECEIPT
                and entry.lsn_start == origin_lsn
            ):
                return entry
        return None

    def _decode_operation(self, entry: LedgerEntry) -> WalRecord:
        """Return the record a reapplicable entry preserved, refusing bytes that are not one."""
        body = decode_payload(entry.payload).body
        outcome = decode_record(body, 0)
        if outcome.record is None:
            raise GrafxLedgerError(
                f"Ledger entry {entry.entry_id} is marked reapplicable and its bytes do not "
                f"decode into a log record: {outcome.detail}",
                field="payload",
                entry_id=entry.entry_id,
            )
        return outcome.record

    def _publish_depth(self) -> None:
        """Publish the two ledger gauges of CONTRACT.md section 9."""
        if not self._metrics.enabled:
            return
        now = float(self._clock.wall())
        for origin, labels in _ORIGIN_LABELS.items():
            waiting = [
                entry
                for entry in self._entries
                if entry.entry_type is LedgerEntryType.DISCARD and entry.origin_class is origin
            ]
            self._metrics.set_gauge(LEDGER_DEPTH, float(len(waiting)), labels)
            oldest = min((entry.captured_at_wall for entry in waiting), default=None)
            age = max(now - oldest, 0.0) if oldest is not None else 0.0
            self._metrics.set_gauge(LEDGER_OLDEST_ENTRY_AGE_SECONDS, age, labels)

    def __repr__(self) -> str:
        """Return a short description naming the file and how much it holds."""
        return f"LedgerStore(file={self._file!r}, entries={len(self._entries)})"


def _identity_of(entry_type: LedgerEntryType, payload: LedgerPayload) -> tuple[int, str, int, int]:
    """Return what makes two entries descriptions of the SAME lost work.

    A byte range is located by its file, its offset and its length, which is exactly the identity
    :func:`okto_grafx.domain.recovery.manifest.entry_suffix` builds a quarantine entry name from.
    The kind of entry joins them because a retirement and a discard are different statements
    about a file even when they name the same bytes.
    """
    return (int(entry_type), payload.origin, payload.offset, payload.length)


def _validate_file(file: object) -> str:
    """Return the ledger file name, refusing one that could escape its own directory."""
    if not isinstance(file, str) or not file:
        raise GrafxConfigurationError(
            "The ledger needs a non-empty file name.", field="file", value=repr(file)
        )
    if "\\" in file or ".." in file.split("/"):
        raise GrafxConfigurationError(
            f"The ledger file {file!r} must be a forward-slash name inside the database.",
            field="file",
            value=file,
        )
    return file


def _as_origin_class(value: LedgerOriginClass | str | None) -> LedgerOriginClass | None:
    """Return the origin class a filter names, accepting the member or its lowercase name."""
    if value is None or isinstance(value, LedgerOriginClass):
        return value
    if isinstance(value, str):
        for member in LedgerOriginClass:
            if member.name.lower() == value.lower():
                return member
    raise GrafxConfigurationError(
        f"{value!r} is not an origin class; expected one of "
        f"{tuple(member.name.lower() for member in LedgerOriginClass)}.",
        field="origin_class",
        value=repr(value),
    )


def _as_reason(value: LedgerReason | str | None) -> LedgerReason | None:
    """Return the reason a filter names, accepting the member or its lowercase name."""
    if value is None or isinstance(value, LedgerReason):
        return value
    if isinstance(value, str):
        for member in LedgerReason:
            if member.name.lower() == value.lower():
                return member
    raise GrafxConfigurationError(
        f"{value!r} is not a ledger reason; expected one of "
        f"{tuple(member.name.lower() for member in LedgerReason)}.",
        field="reason",
        value=repr(value),
    )


def _require_identifier_sequence(value: object) -> tuple[object, ...]:
    """Return the identifiers a purge names, refusing anything that is not a run of them.

    A bare integer, a float or an object is not a sequence, and iterating one raises a plain
    TypeError out of a FROZEN public signature -- which section 11 item 5 forbids and which is
    doubly wrong on the one door of this component that destroys entries. A string is refused
    with them: iterating ``"12"`` would silently purge entries one and two.
    """
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        raise GrafxConfigurationError(
            f"The entry identifiers of a purge are a sequence of integers; got "
            f"{type(value).__name__}.",
            field="entry_ids",
            value=type(value).__name__,
        )
    try:
        return tuple(value)  # type: ignore[call-overload]
    except TypeError as failure:
        raise GrafxConfigurationError(
            f"The entry identifiers of a purge are a sequence of integers; got "
            f"{type(value).__name__}.",
            field="entry_ids",
            value=type(value).__name__,
        ) from failure


def _require_count(field: str, value: object) -> int:
    """Return a non-negative page size or offset, refusing anything that could not be one."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"The {field} of a ledger listing must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if value < 0:
        raise GrafxConfigurationError(
            f"The {field} of a ledger listing cannot be negative; got {value}.",
            field=field,
            value=value,
        )
    return value
