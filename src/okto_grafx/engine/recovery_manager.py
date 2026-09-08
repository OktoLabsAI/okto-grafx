"""Recovery at open (CONTRACT.md section 8.6, SPEC-M1 FR-8, BR-1, BR-2, BR-3, AC-4, AC-5).

This is the component every other component hands its damage to, and the whole of it is the
frozen algorithm of section 8.6 with one refinement stated out loud below.

    1. read the meta identity; a format this build cannot read stops the run;
    2. scan the log from the published checkpoint and stop believing it at the first stretch of
       bytes that is not a record, or the first record that breaks sequence contiguity;
    3. if a tail is discarded, copy the affected ranges into quarantine WITH A MANIFEST and then
       truncate. ``heap.dat``, ``catalog.dat`` and ``index/*`` are never touched (G6, BR-1);
    4. write exactly one ledger entry per discarded item, classified by SD-4 (G8, BR-3);
    5. redo the page writes of committed transactions, idempotently, through ``page_lsn``;
    6. uncommitted transactions need no undo, because pages are written only at commit;
    7. ``recovery_policy="refuse"`` raises ``recovery_refused`` INSTEAD of step 3, and leaves
       everything on disk exactly as it was -- including the ledger's own repair, which is a
       write like any other and therefore waits for a policy that permits writing.

**The refinement: quarantine, then ledger, then truncate.** Section 8.6 fixes quarantine before
truncation and leaves the ledger's position open. Writing the ledger entries before the cut is
strictly stronger: at every instant, the evidence for a byte that is about to disappear is
already durable. It is also the order carried finding CF-1 states for retirement -- quarantine
the record, write the forensic entry, retire the name, report it -- so the two paths of this
component destroy things the same way round.

**Running twice produces the same state.** Every step is keyed on what is on the device rather
than on what this pass has done: a quarantine capture of a range already captured returns the
entry that exists, a ledger entry is written only for damage still present, redo is idempotent
through ``page_lsn``, and a second run over a repaired log finds nothing to do and reports
``clean``. Interrupting recovery anywhere and re-running it converges on the same place, which is
what AC-4 asks for at every injected crash point.

**After replaying catalog pages, the catalog is re-derived and adopted, never loaded** (carried
finding CF-4). ``load()`` is destructive by definition -- it throws away the in-memory tables --
so recovery takes ``read_from_pages()`` and ``adopt()``: the store then holds what the replayed
pages actually say, and its structure epoch is current, so the caller's next ``save()`` is
accepted rather than refused. Recovery does not ``save()`` itself: a save writes pages the log
never covered, and nothing at this point wants the catalog written back -- what the route has to
achieve is a store that is not left refusing, and adopt is what achieves it. Proven by
``test_the_catalog_can_save_after_recovery_replayed_its_pages``.

**Nothing here holds a lock** (A91, LESSONS L2): the engine layer has no locks at all, so the
ports it calls -- every one of them host-supplied -- can never be re-entered under one.

**What this component needs from the log, stated as a dependency rather than assumed.** The only
truncation door is ``WalManager.truncate_after``, and it is reachable only through a manager that
opened. So a damaged log MUST still open: ``open()`` has to record what it found in ``damage``
and leave the manager usable, because a log that refuses to open is a log whose repair door
cannot be called -- and this component would then have no way to quarantine, record and cut the
very damage it exists for. C4's P2a is exactly that case (a checksum-valid record with an
oversized descriptor length) and is being fixed to record rather than raise. Recovery deliberately
does NOT call ``open()`` itself: ``open()`` clears the manager's unflushed set, and a pass run
against a manager with appends still waiting for a barrier would drop them from that set. Opening
the log is the caller's step; repairing it is this one's.

The second half of the same dependency: **``scan_all`` must answer from the device, not from an
index cached before another participant wrote.** The whole plan of a pass -- where the good work
ended, what is discarded, what is replayed -- is derived from that one walk, so a stale answer is
a stale decision about what to destroy. C4 is re-deriving these doors as part of CF-6, and this
component is written against that: it takes the walk once, at the start of the pass, and it never
caches anything across passes of its own. Every re-run re-derives, which is also what makes a
re-run converge.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from typing import Protocol, cast, runtime_checkable

from okto_grafx.domain.control_record import ControlRecordReader
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxPortNotConfigured,
    GrafxRecoveryRefused,
    GrafxSchemaVersionMismatch,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
)
from okto_grafx.domain.ledger.classification import classify_failure, classify_record
from okto_grafx.domain.ledger.entry import LedgerOriginClass, LedgerReason
from okto_grafx.domain.ledger.payload import LedgerPayload
from okto_grafx.domain.page.file_header import FileHeaderPage
from okto_grafx.domain.page.layout import is_unwritten_image
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.recovery.decision import (
    CommittedReplay,
    DiscardedRange,
    DiscardedRecord,
    RecoveryPlan,
    committed_replay,
    plan_recovery,
)
from okto_grafx.domain.recovery.report import (
    OUTCOME_CLEAN,
    OUTCOME_QUARANTINED,
    OUTCOME_REFUSED,
    OUTCOME_TRUNCATED,
    POLICY_REFUSE,
    POLICY_REPLAY,
    RECOVERY_POLICIES,
    FindingKind,
    RecoveryFinding,
    RecoveryReport,
    stronger_outcome,
)
from okto_grafx.domain.recovery.retry import RETRYABLE_KEY, is_retryable
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FORMAT_VERSION, CommitState
from okto_grafx.domain.txn.records import decode_page_write_location
from okto_grafx.domain.wal.record import WalRecordType
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.commit_state_store import CommitStateStore
from okto_grafx.engine.coordination import COMMIT_SECTION
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.ledger_store import LedgerStore
from okto_grafx.engine.metrics_catalog import metric
from okto_grafx.engine.quarantine import QuarantineStore, is_protected

_CANONICAL_REPLAY_FLOOR = IndexManager.check_replay_floor
_CANONICAL_WATERMARK_PHOTO = IndexManager.table_watermark_photo

__all__ = [
    "MAX_LEDGER_BODY_BYTES",
    "META_FILE",
    "RECOVERIES_TOTAL",
    "RECOVERY_DISCARDED_RECORDS_TOTAL",
    "RECOVERY_METRICS",
    "RECOVERY_REPLAYS_TOTAL",
    "STORAGE_PORT_METHODS",
    "TRUNCATION_ATTEMPTS",
    "ControlRecordProbe",
    "RecoveryManager",
]

META_FILE: str = "grafx.meta"
"""The identity page of a database (CONTRACT.md section 6.1)."""

TRUNCATION_ATTEMPTS: int = 3
"""How many times a truncation is retried when the platform defers releasing a segment."""

MAX_LEDGER_BODY_BYTES: int = 1 << 20
"""Largest damaged range copied INTO a ledger entry as well as into quarantine.

A damaged range can be a whole segment, and an entry that carried one would put megabytes into an
append-only file every reader of the ledger has to walk. Past this size the entry keeps its
provenance and names the quarantine entry that holds the bytes, which is where the evidence lives
in either case -- the copy in the ledger is a convenience for small ranges, never the record of
truth. ``LedgerStore.export`` refuses such an entry by naming the quarantine entry rather than
returning a partial range, because half of a damaged range is worse evidence than none.
"""

RECOVERY_REPLAYS_TOTAL: str = "oktografx_recovery_replays_total"
"""Counter of records replayed by recovery (CONTRACT.md section 9)."""

RECOVERY_DISCARDED_RECORDS_TOTAL: str = "oktografx_recovery_discarded_records_total"
"""Counter of records discarded by recovery, by ledger origin class (section 9)."""

RECOVERIES_TOTAL: str = "oktografx_recoveries_total"
"""Counter of recovery runs completed, by outcome (section 9)."""

RECOVERY_METRICS: tuple[MetricDescriptor, ...] = (
    metric(RECOVERY_REPLAYS_TOTAL),
    metric(RECOVERY_DISCARDED_RECORDS_TOTAL),
    metric(RECOVERIES_TOTAL),
)
"""Every metric this manager emits, taken from the frozen catalogue by name and never invented."""

STORAGE_PORT_METHODS: tuple[str, ...] = (
    "exists",
    "read_page",
    "page_count",
    "log_size",
    "read_log",
    "recycle",
    "remove",
)
"""The storage doors this manager opens directly. Everything else goes through the other stores."""

_ORIGIN_LABELS: dict[LedgerOriginClass, dict[str, str]] = {
    LedgerOriginClass.REAPPLICABLE: {"origin_class": "reapplicable"},
    LedgerOriginClass.FORENSIC: {"origin_class": "forensic"},
}

_CONTROL_PREFIX: str = "control/"

_LEASE_RECORD: str = "control/writer.lease"
"""The writer lease, exactly where the coordination adapter keeps it."""

_MAX_CONTROL_RECORD_BYTES: int = 4096
"""Maximum legacy record size; format 2 admits only its exact three-page lease envelope.

The strict shape check in :func:`_control_record_size_allowed` prevents an arbitrary oversized
or sparse file from demanding an unbounded allocation before the probe types the corruption.
It is pinned both ways by the retirement battery: gigantic and merely-off-shape files refuse
with zero reads, while real legacy and format-2 leases are accepted in full.
"""

_LEASE_SECTION: str = "writer.lease"
"""The section every cooperating publisher of the lease serialises on.

This mirrors ``coordination_local.LEASE_SECTION`` -- the engine may not import the adapter
(G2), so the name is restated here and pinned equal by the retirement battery. Holding it
around the last look and the removal is what turns the cooperative guarantee for the lease
from narrative into exclusion."""

_READERS_PREFIX: str = "control/readers/"
_READER_SUFFIX: str = ".reader"
"""Canonical reader records: ``control/readers/<reader_id>.reader`` and nothing else."""


_MAX_READER_ID_LENGTH: int = 96
"""The identifier bound the coordination adapter enforces (its ``_MAX_IDENTIFIER_LENGTH``)."""

_READER_ID_EXTRA_CHARACTERS: frozenset[str] = frozenset(".-_")
"""Beyond lower-case letters and digits, exactly what the adapter's validator accepts."""


def _is_canonical_reader_id(value: str) -> bool:
    """True when the id is byte-for-byte one the coordination adapter could have written.

    This mirrors the adapter's ``_validate_identifier`` -- non-empty, at most 96 characters,
    ASCII, never ``.``/``..`` nor containing ``..``, and drawn from lower-case letters, digits,
    ``.``, ``-`` and ``_`` only (file identity must not depend on case, CONTRACT.md section 11
    item 9). The engine may not import the adapter (G2), so the rule is restated here and
    pinned against near-canonical names by the retirement battery; a slash can never appear
    because the id is taken between the canonical directory and the suffix.
    """
    if not value or len(value) > _MAX_READER_ID_LENGTH:
        return False
    if value in {".", ".."} or ".." in value or not value.isascii():
        return False
    return all(
        character.isdigit()
        or (character.isalpha() and character.islower())
        or character in _READER_ID_EXTRA_CHARACTERS
        for character in value
    )


def _is_canonical_retirement_target(name: str) -> bool:
    """True only for the writer lease and canonical reader records (M0C).

    The retirement door destroys a name, so the names it accepts are enumerated rather than
    pattern-matched broadly: the lease, and readers directly under their canonical directory
    whose ``<reader_id>`` the coordination adapter itself would accept. ``commit.state`` is
    already protected upstream and can never reach this.
    """
    if name == _LEASE_RECORD:
        return True
    if not name.startswith(_READERS_PREFIX) or not name.endswith(_READER_SUFFIX):
        return False
    reader_id = name[len(_READERS_PREFIX) : -len(_READER_SUFFIX)]
    return "/" not in reader_id and _is_canonical_reader_id(reader_id)


def _generation_detail(damage: str, digest: str) -> str:
    """One line naming the damage and the exact generation it was seen on."""
    return f"{damage} [generation sha256={digest}]"


class _CommitSection(Protocol):
    """What ``exclusive`` returns: a context manager holding the section for its body."""

    def __enter__(self) -> object:
        """Enter the section."""

    def __exit__(self, exc_type: object, exc: object, tb: object) -> object:
        """Leave the section."""


class _FencingCoordinator(Protocol):
    """The complete coordinator surface every fenced door demands (M0C)."""

    def exclusive(self, name: str, *, timeout: float) -> _CommitSection:
        """Grant the named cross-process section for the body of a ``with``."""

    def reader_horizon(self) -> object:
        """Answer the lowest LSN a live reader still needs, or None for no reader."""


_COORDINATOR_METHODS: tuple[str, ...] = ("exclusive", "reader_horizon")
"""What a COMPLETE coordinator must answer: the fence itself, and the reader horizon
C4 asks this manager to re-derive after a retirement. A coordinator missing either is
PARTIAL, and a partial fence is no fence."""

_PERMIT_SEAL: object = object()
"""The module-private seal a genuine recovery permit is minted with.

Python offers no unforgeable capability inside one interpreter; what the seal provides is the
COOPERATIVE guarantee the fencing contract asks for: a permit cannot be built by accident, by a
subclass, or by code outside this module without reaching into module privates by name -- the
same deliberate act as reading a mangled attribute. A door that demands one therefore proves it
was reached from :meth:`RecoveryManager.run` or its siblings, INSIDE the commit section, rather
than by a caller that skipped the fence.
"""


def _control_record_size_allowed(storage: StorageDevice, name: str, size: int) -> bool:
    """Accept legacy records, plus exactly one three-page format-2 writer lease.

    A size merely below the slot-file ceiling is not enough: that would let arbitrary oversized
    legacy bytes consume memory through the recovery door.  Format 2 has one exact physical
    length, while reader registrations remain legacy until CE-2.
    """
    return size <= _MAX_CONTROL_RECORD_BYTES or (
        name == _LEASE_RECORD and size == 3 * storage.page_size
    )


class _RecoveryPermit:
    """The capability one fenced pass holds exactly as long as its commit section.

    It is minted by the manager itself immediately after entering ``COMMIT_SECTION`` and revoked
    on the way out, so its liveness IS the section's: presenting it proves the holder is inside
    the exclusion writers share. It is bound to one manager instance; a permit from another
    manager -- even one over the same files -- says nothing about THIS pass and is refused.
    """

    __slots__ = ("_live", "_manager")

    def __init__(self, manager: RecoveryManager, seal: object) -> None:
        """Mint the permit for one manager; refuse any caller without the module seal."""
        if seal is not _PERMIT_SEAL:
            raise GrafxRecoveryRefused(
                "A recovery permit is minted only by the recovery manager itself, inside the "
                "commit section it shares with writers. This one was forged, so it proves "
                "nothing about the fence. Nothing was scanned or changed.",
                field="recovery_permit",
            )
        self._manager = manager
        self._live = True

    def revoke(self) -> None:
        """Kill the permit: the section it certified is being left."""
        self._live = False

    def require(self, manager: RecoveryManager) -> None:
        """Refuse unless this permit is live and belongs to exactly that manager."""
        if self._manager is not manager:
            raise GrafxRecoveryRefused(
                "The recovery permit presented belongs to another manager, so it does not "
                "prove that THIS pass is inside the commit section. Nothing was scanned or "
                "changed.",
                field="recovery_permit",
            )
        if not self._live:
            raise GrafxRecoveryRefused(
                "The recovery permit presented has been revoked: the commit section it "
                "certified was left, and with it every right to scan or mutate. Nothing was "
                "scanned or changed.",
                field="recovery_permit",
            )


@runtime_checkable
class ControlRecordProbe(Protocol):
    """Whoever can tell this component that a control-plane record is damaged.

    Carried finding CF-1 makes retirement of a damaged reader or lease record C6's act. C6 cannot
    decide damage for itself: those records belong to the coordination adapter, whose decoders the
    engine may not import (G2 keeps the layers apart). So the evidence is INJECTED, and a manager
    with no probe refuses to retire anything at all -- which is the fail-closed direction, and the
    one that cannot be talked into deleting a healthy lease.
    """

    def read_control_record(self, file: str) -> None:
        """Read one control record, raising when it is damaged and returning when it is not."""
        ...


class RecoveryManager:
    """Runs the frozen recovery algorithm of CONTRACT.md section 8.6 over one database."""

    __slots__ = (
        "_storage",
        "_wal",
        "_ledger",
        "_quarantine",
        "_pool",
        "_catalog",
        "_index_manager",
        "_index_sync",
        "_redo_engine",
        "_state_store",
        "_metrics",
        "_policy",
        "_probe",
        "_coordinator",
        "_commit_lock_timeout",
        "_meta_file",
    )

    def __init__(
        self,
        storage: StorageDevice,
        wal: object,
        ledger: LedgerStore,
        quarantine: QuarantineStore,
        pool: BufferPool,
        metrics: MetricsSink,
        *,
        attribute_probe: Callable[[object, str], bool],
        catalog: object = None,
        index_manager: object = None,
        index_sync: Callable[[], object] | None = None,
        commit_state_store: CommitStateStore | None = None,
        recovery_policy: str | None = None,
        control_probe: ControlRecordProbe | None = None,
        coordinator: object = None,
        commit_lock_timeout: float = 30.0,
        meta_file: str = META_FILE,
        policy: str | None = None,
        database_uuid: bytes | None = None,
        control_format_version: int = 1,
        control_file_nonce: int = 0,
        control_read_if_exists: ControlRecordReader | None = None,
    ) -> None:
        """Build the manager over the stores and ports one recovery pass needs.

        ``attribute_probe`` is supplied by the composition root: it observes
        declared members without evaluating descriptors, with dynamic lookup
        only for absent declarations. Host introspection is not an engine
        dependency. The engine still owns the required shape and refusal.

        ``catalog`` and ``control_probe`` are optional because a database can be recovered
        without them: a catalog store is needed only to complete the CF-4 route, and a probe
        only to retire a damaged control record. Each absent one disables exactly its own step
        and says so in the report rather than guessing. ``coordinator`` may be absent only AT
        CONSTRUCTION, for wiring order: since M0C every public door of this manager refuses,
        typed and before its first read, unless a COMPLETE coordinator can fence the pass
        inside the commit section writers share. There is no unfenced mode.

        **The policy is spelled ``recovery_policy``**, the name ``DatabaseConfig`` and section 5
        give it, so the composition root passes one word through rather than translating it.
        ``policy`` is still accepted, because the assembly already written against it belongs to
        another component and a silent rename would break it -- but the two may not DISAGREE.
        Naming the same setting twice with two different values has no correct reading, so it is
        refused rather than resolved by an ordering rule nobody can see from the call site.
        """
        if not callable(attribute_probe):
            raise GrafxConfigurationError(
                "Recovery attribute_probe must be callable.",
                field="attribute_probe",
                value=type(attribute_probe).__name__,
            )
        _require_port("storage", storage, STORAGE_PORT_METHODS, attribute_probe)
        _require_port(
            "wal",
            wal,
            (
                "scan_all",
                "truncate_after",
                "open",
                "damage",
                "segments",
                "barrier",
                "force_barrier_range",
            ),
            attribute_probe,
        )
        _require_port(
            "metrics", metrics, ("enabled", "register", "increment"), attribute_probe
        )
        if not isinstance(ledger, LedgerStore):
            raise GrafxConfigurationError(
                f"Recovery needs a LedgerStore; got {type(ledger).__name__}.",
                field="ledger",
                value=type(ledger).__name__,
            )
        if not isinstance(quarantine, QuarantineStore):
            raise GrafxConfigurationError(
                f"Recovery needs a QuarantineStore; got {type(quarantine).__name__}.",
                field="quarantine",
                value=type(quarantine).__name__,
            )
        if not isinstance(pool, BufferPool):
            raise GrafxConfigurationError(
                f"Recovery needs a BufferPool to replay pages through; got "
                f"{type(pool).__name__}.",
                field="pool",
                value=type(pool).__name__,
            )
        self._storage: StorageDevice = storage
        self._wal = wal
        self._ledger: LedgerStore = ledger
        self._quarantine: QuarantineStore = quarantine
        self._pool: BufferPool = pool
        self._catalog = catalog
        if index_sync is not None and not callable(index_sync):
            raise GrafxConfigurationError(
                "Recovery index_sync must be callable when configured.",
                field="index_sync",
                value=type(index_sync).__name__,
            )
        self._index_manager = index_manager
        self._index_sync = index_sync
        self._metrics: MetricsSink = metrics
        self._policy: str = _validate_policy(_one_policy(recovery_policy, policy))
        self._probe: ControlRecordProbe | None = control_probe
        self._coordinator = coordinator
        self._commit_lock_timeout: float = _require_timeout(
            "commit_lock_timeout", commit_lock_timeout
        )
        self._meta_file: str = _require_text("meta_file", meta_file)
        owner = "recovery"
        owner_reader = getattr(coordinator, "owner_id", None)
        if callable(owner_reader):
            owner = str(owner_reader())
        self._state_store = (
            commit_state_store
            if commit_state_store is not None
            else CommitStateStore(
                storage,
                owner_id=owner,
                database_uuid=database_uuid,
                control_format_version=control_format_version,
                file_nonce=control_file_nonce,
                control_read_if_exists=control_read_if_exists,
            )
        )
        self._redo_engine = CommitRedo(pool, index_manager)  # type: ignore[arg-type]
        if self._metrics.enabled:
            for declared in RECOVERY_METRICS:
                self._metrics.register(declared)

    @property
    def recovery_policy(self) -> str:
        """Return the recovery policy this manager runs under: replay or refuse."""
        return self._policy

    @property
    def policy(self) -> str:
        """Return the recovery policy, under the shorter name. Reading cannot be ambiguous."""
        return self._policy

    # --- the pass ----------------------------------------------------------------------------

    def _require_complete_coordinator(self, purpose: str) -> _FencingCoordinator:
        """Return the coordinator the fence needs, or refuse typed BEFORE any scan or mutation.

        M0C removed the unfenced fallback. Without the cross-process commit section there is no
        stable WAL picture: a scan could classify an append a live writer was still moving, and
        a truncation could cut it. A manager without a COMPLETE coordinator therefore may not
        scan, prove or mutate anything; the refusal happens before the first read of state or
        log, and it names what is missing instead of inventing a degraded mode.
        """
        coordinator = self._coordinator
        if coordinator is None:
            raise GrafxPortNotConfigured(
                f"Recovery cannot {purpose}: no process coordinator is wired, and the commit "
                "section it provides is the only thing that isolates the WAL picture from live "
                "writers. There is no unfenced fallback; configure coordination and retry.",
                missing=["coordinator"],
            )
        absent = [
            method
            for method in _COORDINATOR_METHODS
            if not callable(getattr(coordinator, method, None))
        ]
        if absent:
            raise GrafxPortNotConfigured(
                f"Recovery cannot {purpose}: the coordinator is missing "
                f"{', '.join(repr(name) for name in absent)}, so it cannot fence the pass or "
                "re-derive the reader horizon. Nothing was scanned or changed.",
                missing=absent,
            )
        return cast("_FencingCoordinator", coordinator)

    def _require_permit(self, permit: object) -> None:
        """Refuse unless the permit is this manager's own live, section-scoped capability."""
        if not isinstance(permit, _RecoveryPermit):
            raise GrafxRecoveryRefused(
                "This step demands the recovery permit minted inside the commit section, and "
                f"was handed {type(permit).__name__} instead: a forged or absent capability. "
                "Nothing was scanned or changed.",
                field="recovery_permit",
            )
        permit.require(self)

    def run(self) -> RecoveryReport:
        """Recover the database and return what was done (CONTRACT.md section 8.6).

        The pass exists only fenced (M0C): a complete coordinator is demanded first, and its
        timeout or refusal happens BEFORE the first scan or mutation. The permit minted on
        entering the section is the capability every destructive step demands, and it dies with
        the section, so no step can outlive or precede the exclusion writers share.
        """
        coordinator = self._require_complete_coordinator("run")
        # The whole destructive decision uses the exact section commits use: scan, forensic
        # preservation, truncation, redo and publication must all see one stable WAL picture.
        # Acquiring only around truncate would still let the scan classify an in-flight append.
        with coordinator.exclusive(COMMIT_SECTION, timeout=self._commit_lock_timeout):
            permit = _RecoveryPermit(self, _PERMIT_SEAL)
            try:
                return self._run_fail_closed(permit)
            finally:
                permit.revoke()

    def _run_fail_closed(self, permit: _RecoveryPermit) -> RecoveryReport:
        """Run one fenced pass and poison this handle's indexes if it cannot finish.

        A redo can fail after installing only a prefix of heap pages and before the first logical
        index effect. If this live handle then commits unrelated work, that commit may flush the
        recovered heap prefix and advance an untouched index past entries it never received.
        Excluding every registered index in memory prevents that false certification. Nothing is
        persisted here: a refusal that happened before mutation remains byte-identical, and the
        retained WAL lets a later open retry the pass.
        """
        try:
            return self._run_fenced(permit)
        except BaseException as failure:
            manager = self._index_manager
            exclude = getattr(manager, "mark_all_stale", None)
            if callable(exclude):
                try:
                    failure_name = (
                        failure.code
                        if isinstance(failure, GrafxError)
                        else type(failure).__name__
                    )
                    exclude(
                        f"Recovery did not complete ({failure_name}); this handle cannot prove "
                        "any index is complete.",
                        persist=False,
                    )
                except BaseException:  # noqa: BLE001 - never replace the recovery failure
                    pass
            raise

    def require_read_only_consistent(self) -> None:
        """Prove that pages need no WAL completion, without changing any persisted byte.

        ``commit.state`` is a publication record, not the durability authority. Restoring an
        older copy of it must not make a complete COMMIT still present in the WAL disappear from
        the startup decision. A read-only handle cannot replay that commit, so it may open only
        when both sources prove that the checkpoint already covers every complete commit.

        The tolerant scan is deliberate: damage is reported as a reason to refuse, never
        repaired here. Unsupported intact records retain their schema-mismatch classification.
        The state and WAL must be one atomic observation against commit, so this door takes the
        same ``COMMIT_SECTION`` as :meth:`run`. Taking an advisory lock changes no database byte;
        the proof itself calls no persistence door and is byte-identical even when it refuses.

        M0C makes the fence mandatory here too: without a COMPLETE coordinator this door
        refuses, typed, before its first read of state or log -- an unfenced proof could bless
        a picture a live writer was still moving.
        """
        coordinator = self._require_complete_coordinator("prove read-only consistency")
        with coordinator.exclusive(COMMIT_SECTION, timeout=self._commit_lock_timeout):
            permit = _RecoveryPermit(self, _PERMIT_SEAL)
            try:
                self._require_read_only_consistent_fenced(permit)
            finally:
                permit.revoke()

    def _require_read_only_consistent_fenced(self, permit: _RecoveryPermit) -> None:
        """Run the read-only proof; the permit proves the commit section is held around it."""
        self._require_permit(permit)
        self._check_meta()
        state = self._state_store.read()
        self._state_store.redundancy_needs_repair(state)
        plan = plan_recovery(self._wal.scan_all(), floor_lsn=state.checkpoint_lsn)
        if plan.unsupported is not None:
            raise GrafxSchemaVersionMismatch(
                f"The log holds a record this build cannot read at byte "
                f"{plan.unsupported.offset} of {plan.unsupported.segment!r}; a read-only open "
                "cannot prove that the checkpoint covers it.",
                file=plan.unsupported.segment,
                offset=plan.unsupported.offset,
                field="format_version",
            )
        replay = committed_replay(plan.replayable)
        expected_first = state.checkpoint_lsn + 1
        observed_first = plan.replayable[0].lsn if plan.replayable else None
        state_invalid = state.checkpoint_lsn > state.last_committed_lsn
        state_needs_replay = state.last_committed_lsn > state.checkpoint_lsn
        wal_needs_replay = replay.last_committed_lsn > state.checkpoint_lsn
        wal_has_ambiguous_effects = bool(replay.incomplete_effects)
        lineage_missing = (
            observed_first is not None and observed_first != expected_first
        )
        if not (
            state_invalid
            or state_needs_replay
            or wal_needs_replay
            or wal_has_ambiguous_effects
            or lineage_missing
            or plan.damaged
        ):
            return
        raise GrafxUnsupportedOperation(
            "A read-only open cannot prove a checkpoint-complete database from commit.state "
            "and the WAL. Open writable, run recovery and checkpoint, then reopen read-only.",
            field="read_only_consistency",
            last_committed_lsn=state.last_committed_lsn,
            checkpoint_lsn=state.checkpoint_lsn,
            wal_committed_lsn=replay.last_committed_lsn,
            incomplete_effect_lsns=tuple(
                record.lsn for record in replay.incomplete_effects
            ),
            expected_first_lsn=expected_first,
            observed_first_lsn=observed_first,
            wal_damaged=plan.damaged,
        )

    def _run_fenced(self, permit: _RecoveryPermit) -> RecoveryReport:
        """Execute one pass; the permit proves the commit section is held around all of it."""
        self._require_permit(permit)
        findings: list[RecoveryFinding] = []
        self._check_meta()
        state, state_was_damaged = self._read_recovery_state()
        if not state_was_damaged:
            self._state_store.redundancy_needs_repair(state)
        manager = self._index_manager
        plan = self._scan(findings, floor_lsn=state.checkpoint_lsn)
        if plan.unsupported is not None:
            raise GrafxSchemaVersionMismatch(
                f"The log holds a record this build cannot read at byte {plan.unsupported.offset} "
                f"of {plan.unsupported.segment!r}: {plan.unsupported.detail} Nothing was "
                "discarded, because those bytes are intact and belong to a newer build.",
                file=plan.unsupported.segment,
                offset=plan.unsupported.offset,
                field="format_version",
            )
        replay = committed_replay(plan.replayable)
        self._validate_publication_lineage(state, state_was_damaged, plan, replay)
        # This is the last read-only gate before recovery can preserve/cut WAL bytes or persist
        # a conservative stale bit into an index header. A bad effect late in the committed plan
        # must not leave either an applied page prefix or unrelated control/index publication
        # behind merely because static validation used to live inside the later redo step.
        preflighted, preflight_touched_catalog = self._preflight_committed_replay(
            replay, permit, checkpoint_lsn=state.checkpoint_lsn
        )
        replay_floor_watermarks = None
        if manager is not None and not state_was_damaged:
            # The checkpoint is the replay floor. An index already behind it cannot be completed
            # from the retained WAL suffix and must be marked stale BEFORE replay; otherwise the
            # final mark_built_through would certify a permanently missing historical entry.
            # Even the in-memory verdict follows preflight so a byte-identical refusal has no
            # state transition to unwind.
            if (
                type(self) is RecoveryManager
                and type(manager) is IndexManager
                and IndexManager.check_replay_floor is _CANONICAL_REPLAY_FLOOR
                and IndexManager.table_watermark_photo is _CANONICAL_WATERMARK_PHOTO
                and RecoveryManager._repair_ledger is _CANONICAL_LEDGER_REPAIR
                and type(self._ledger) is LedgerStore
                and self._ledger.damage is None
                and not plan.damaged
                and self._policy != POLICY_REFUSE
            ):
                # The two pre-redo floor checks share one heap picture only in this closed
                # clean-log/healthy-ledger interval. No page replay, catalog adoption or
                # external commit can intervene under this permit. Both index-header checks
                # still run; redo and post-section open take their own fresh photographs.
                replay_floor_watermarks = manager.table_watermark_photo()
                manager.check_replay_floor(
                    state.checkpoint_lsn, persist_stale=False,
                    watermarks=replay_floor_watermarks,
                )
            else:
                manager.check_replay_floor(state.checkpoint_lsn, persist_stale=False)
        outcome = OUTCOME_CLEAN
        entries_created = 0
        if plan.damaged:
            if self._policy == POLICY_REFUSE:
                self._count_outcome(OUTCOME_REFUSED)
                raise GrafxRecoveryRefused(
                    f"The log is damaged and the recovery policy is {POLICY_REFUSE!r}, so nothing "
                    "was quarantined, discarded or replayed. Reopen with the replay policy to "
                    "recover to the last intact record.",
                    field="recovery_policy",
                    last_good_lsn=plan.last_good_lsn,
                    discards=plan.discards,
                )
            self._repair_ledger(findings, permit)
            entries_created = self._preserve(plan, findings, permit)
            self._truncate(plan, findings, permit)
            outcome = stronger_outcome(outcome, OUTCOME_TRUNCATED)
        else:
            # A clean log still owes the ledger its repair: an append interrupted by the previous
            # crash would otherwise leave the ledger unwritable for good. Under the refuse policy
            # it is skipped with everything else, because step 7 says that policy leaves the disk
            # untouched and an operator picks it precisely for that -- and a CLEAN log is the
            # only shape in which this branch is the thing being tested, because the damaged-log
            # branch above raises before it can be reached. Pinned by
            # ``test_a_clean_log_under_refuse_leaves_a_damaged_ledger_exactly_as_it_was``.
            if self._policy != POLICY_REFUSE:
                self._repair_ledger(findings, permit)
        if (
            manager is not None
            and not state_was_damaged
            and self._policy != POLICY_REFUSE
        ):
            # Only after the policy has accepted mutation may the conservative verdict become a
            # durable stale bit. The refuse policy promises byte-for-byte non-interference.
            if replay_floor_watermarks is None:
                manager.check_replay_floor(state.checkpoint_lsn, persist_stale=True)
            else:
                manager.check_replay_floor(
                    state.checkpoint_lsn, persist_stale=True,
                    watermarks=replay_floor_watermarks,
                )
        replayed = self._redo(
            plan,
            findings,
            replay=replay,
            state=state,
            state_was_damaged=state_was_damaged,
            permit=permit,
            preflighted=preflighted,
            preflight_touched_catalog=preflight_touched_catalog,
        )
        self._count_outcome(outcome)
        report = RecoveryReport(
            outcome=outcome,
            records_replayed=replayed,
            records_discarded=plan.discards,
            ledger_entries_created=entries_created,
            last_good_lsn=plan.last_good_lsn,
            findings=tuple(findings),
        )
        return report

    def retire_control_record(self, file: str) -> RecoveryReport:
        """Retire a damaged control-plane record, in the order carried finding CF-1 fixes.

        Quarantine the record with a manifest, write the forensic ledger entry, retire the name,
        report it. Never as a side effect of a hot path: this is a door an operator or the open
        sequence calls deliberately, and it refuses in three ways rather than guessing.

        * With no ``control_probe`` configured it refuses outright. Retiring a control record
          needs EVIDENCE that it is damaged, and this component cannot produce that evidence
          itself -- the records belong to the coordination adapter and the engine may not import
          it (G2). A manager that would retire on request alone is a delete button.
        * A record the probe reads successfully is refused. Failing closed on a damaged reader
          blocks reclamation and failing closed on a damaged lease blocks writing; neither is a
          reason to remove a HEALTHY record, and C4's position is explicit that a writer must
          never delete the file that is refusing it.
        * A file outside ``control/`` is refused, so this door can never be aimed at the log, the
          heap or anything else.
        * **Only a non-retryable ``GrafxCorruptionDetected`` from the probe counts as evidence.**
          Section 2 freezes exactly one damage class and this is it. A retryable failure is an
          ACCESS failure -- a sharing violation from an antivirus or an indexer -- and says
          nothing about the bytes; ``stale_epoch`` and ``lease_stolen`` are what the coordination
          adapter raises about a HEALTHY lease when the READER is the stale one, so retiring on
          them is the stale writer deleting the live writer's lease; ``port_not_configured`` is
          the probe failing to run at all; ``schema_version_mismatch`` is intact bytes from a
          newer build, which ``run()`` already refuses to call damage. All of them refuse, and
          the retryable case declares itself retryable so the caller knows to try again (A47,
          A11-revised, CF-1). Proven by
          ``test_a_retryable_access_failure_is_never_evidence_of_damage``,
          ``test_a_probe_that_fails_with_a_foreign_exception_retires_nothing`` and
          ``test_only_corruption_detected_is_evidence_a_control_record_is_damaged``.

        C4 asks for the reader horizon to be re-derived after any retirement, because it
        caches none across a pass; that happens here, through the coordinator every fenced door
        now demands, and the result is reported.

        M0C adds two more locks on this door:

        * It is FENCED: probe, capture, ledger entry, last look and removal all happen inside
          the commit section writers share, holding the same permit every other fenced step
          demands -- and it refuses typed, before reading anything, when no complete
          coordinator can grant that section.
        * It retires ONE GENERATION, not a name: the exact bytes found first -- their length
          and SHA-256 recorded in both quarantine and ledger -- re-read and compared under the
          section immediately before the removal. A record that changed in ANY window refuses,
          retryably, naming the evidence already kept; a healthy or different replacement
          survives byte for byte. The only LIVE target is the writer lease, taken under
          the lease section nested inside the commit section; canonical reader records are
          recognised but FAIL-CLOSED until a safe compare-and-remove primitive exists, and
          ``commit.state`` is protected and can never leave through this door.

        For the writer lease the exclusion of cooperating participants is BY SECTION:
        every cooperating publisher of ``writer.lease`` -- renew, acquire, takeover --
        serialises on the lease section this door holds around the last look and the removal,
        nested inside the commit section (lock order audited: the adapter only ever takes the
        lease section alone, so commit-then-lease cannot deadlock). What remains outside every
        guarantee is a NON-cooperating process, because the storage port offers no atomic
        compare-and-remove; the last look narrows that residual window to what the platform
        allows. Reader records have no shared section in this contract, which is exactly why
        their retirement is FAIL-CLOSED above rather than window-narrowed.
        """
        name = _require_text("file", file)
        if self._probe is None:
            raise GrafxRecoveryRefused(
                "Retiring a control record needs a probe that can prove the record is damaged, "
                "and none is configured; nothing was touched.",
                field="control_probe",
                file=name,
            )
        if not name.startswith(_CONTROL_PREFIX) or is_protected(name):
            raise GrafxRecoveryRefused(
                f"{name!r} is not a control-plane record, so it is not this door's to retire.",
                field="file",
                file=name,
            )
        if not _is_canonical_retirement_target(name):
            raise GrafxRecoveryRefused(
                f"{name!r} is not a canonical retirement target: this door recognises exactly "
                f"{_LEASE_RECORD!r} and reader records under {_READERS_PREFIX!r}, and nothing "
                "else.",
                field="file",
                file=name,
            )
        if name != _LEASE_RECORD:
            raise GrafxRecoveryRefused(
                f"Retiring the reader record {name!r} is FAIL-CLOSED in this contract: reader "
                "registrations are published under no section this door could share, and the "
                "storage port offers no atomic compare-and-remove, so a retirement here could "
                "destroy a generation a live reader had just republished. A stale reader "
                "already leaves the horizon by TTL; this door opens for readers only when a "
                "safe compare-and-remove primitive exists. Nothing was read or touched.",
                field="file",
                file=name,
            )
        coordinator = self._require_complete_coordinator("retire a control record")
        # Lock order, audited: COMMIT_SECTION first, LEASE_SECTION nested inside it. The
        # coordination adapter only ever takes the lease section ALONE (renew, acquire,
        # takeover, horizon) and never opens the commit section while holding it, so this
        # nesting cannot deadlock -- and it is what closes the race the integrator's review
        # named: every cooperating publisher of writer.lease serialises on the lease section,
        # so none can land between the last look and the removal.
        with coordinator.exclusive(COMMIT_SECTION, timeout=self._commit_lock_timeout):
            with coordinator.exclusive(
                _LEASE_SECTION, timeout=self._commit_lock_timeout
            ):
                permit = _RecoveryPermit(self, _PERMIT_SEAL)
                try:
                    return self._retire_fenced(name, permit)
                finally:
                    permit.revoke()

    def _read_generation(self, name: str) -> bytes:
        """Read the whole record as it is right now, capped exactly like the first read.

        The confirmation re-read and the last look are reads too: a non-cooperating
        replacement (or fresh damage) between observations could otherwise declare an absurd
        size and demand an arbitrary allocation at a point the entry cap no longer guards.
        A size past the cap already answers the only question a re-read asks -- the inspected
        generation is gone -- so it refuses, retryably, without reading a byte. Whatever
        evidence this pass had already persisted stays where it is; a retry meets the entry
        cap, which tells the same truth non-retryably.
        """
        size = self._storage.log_size(name)
        if not _control_record_size_allowed(self._storage, name, size):
            raise GrafxRecoveryRefused(
                f"The control record {name!r} now claims {size} bytes, past the "
                f"{_MAX_CONTROL_RECORD_BYTES} cap: the generation under inspection is gone, "
                "and what replaced it is not worth reading blind. Whatever evidence this pass "
                "had already persisted remains; nothing was read or removed.",
                retryable=True,
                field="length",
                file=name,
                length=size,
            )
        return self._storage.read_log(name, 0, size)

    def _retire_fenced(self, name: str, permit: _RecoveryPermit) -> RecoveryReport:
        """Retire one damaged, canonical control record as a single inspected generation."""
        self._require_permit(permit)
        if not self._storage.exists(name):
            raise GrafxRecoveryRefused(
                f"There is no control record {name!r} to retire.",
                field="file",
                file=name,
            )
        # ONE generation: the exact bytes, their length and their SHA-256, read once. Every
        # decision below is about THESE bytes, and the door refuses rather than act on any
        # other generation it happens to find later.
        size = self._storage.log_size(name)
        if not _control_record_size_allowed(self._storage, name, size):
            raise GrafxRecoveryRefused(
                f"The control record {name!r} claims {size} bytes, past the "
                f"{_MAX_CONTROL_RECORD_BYTES} any record the coordination adapter writes can "
                "occupy. That is either damage this door does not need to READ to prove, or "
                "not a control record at all; nothing was read, quarantined or retired.",
                field="length",
                file=name,
                length=size,
            )
        body = self._storage.read_log(name, 0, size)
        digest = hashlib.sha256(body).hexdigest()
        damage = self._probe_damage(name)
        if damage is None:
            raise GrafxRecoveryRefused(
                f"The control record {name!r} reads cleanly, so retiring it would remove a "
                "healthy registration and the participant it belongs to would lose its place.",
                field="file",
                file=name,
            )
        if self._read_generation(name) != body:
            raise GrafxRecoveryRefused(
                f"The control record {name!r} changed while it was being inspected, so the "
                "probe's verdict belongs to another generation. Nothing was quarantined or "
                "retired; retry against the record that is there now.",
                retryable=True,
                field="generation",
                file=name,
                expected_sha256=digest,
            )
        findings: list[RecoveryFinding] = []
        entry = self._quarantine.capture(
            origin=name,
            offset=0,
            length=size,
            reason=LedgerReason.QUARANTINED_SEGMENT.name.lower(),
            detail=_generation_detail(damage, digest),
            payload=body,
        )
        kept = self._quarantine.read(entry.name)
        if kept != body:
            raise GrafxRecoveryRefused(
                f"Quarantine entry {entry.name!r} does not hold the inspected generation of "
                f"{name!r}: an earlier capture of the same range kept different bytes, and "
                "overwriting evidence is not what this door does. The earlier copy is "
                "preserved; nothing was retired.",
                field="quarantine",
                file=name,
                quarantine=entry.name,
                expected_sha256=digest,
            )
        findings.append(
            RecoveryFinding(
                kind=FindingKind.QUARANTINED_RANGE,
                detail=f"The control record {name!r} was copied into quarantine before retiring.",
                file=name,
                length=size,
                quarantine=entry.name,
            )
        )
        # Counted rather than assumed. ``record_retirement`` is idempotent on the damage
        # identity, so a retry after an interrupted retirement returns the identifier of the
        # entry that already exists and creates NOTHING -- and a report that says one entry was
        # created either way is a report that cannot be added up across retries (G8/BR-3 asks for
        # exactly one entry per discard, and this number is how a caller checks that).
        entries_before = len(self._ledger.entries())
        entry_id = self._ledger.record_retirement(
            payload=LedgerPayload(
                origin=name,
                offset=0,
                length=size,
                failure=LedgerReason.QUARANTINED_SEGMENT.name.lower(),
                detail=_generation_detail(damage, digest),
                quarantine=entry.name,
                body=kept,
            )
        )
        entries_created = len(self._ledger.entries()) - entries_before
        # The last look, under the same section: the name may go only while it still holds THE
        # INSPECTED GENERATION. A name that vanished on its own is already the outcome this door
        # was asked for -- the evidence is kept, nothing else is touched. A name that holds
        # DIFFERENT bytes is a replacement, and a replacement survives byte for byte.
        vanished = not self._storage.exists(name)
        if not vanished and self._read_generation(name) != body:
            raise GrafxRecoveryRefused(
                f"A replacement landed in {name!r} between the evidence and the removal. The "
                f"damaged generation is preserved byte-for-byte in quarantine {entry.name!r} "
                f"and ledger entry {entry_id}, and the replacement survives untouched. Retire "
                "again only if the record that is there now is itself damaged.",
                retryable=True,
                field="generation",
                file=name,
                quarantine=entry.name,
                ledger_entry=entry_id,
                expected_sha256=digest,
            )
        # The name goes last, and only after the copy and the entry are durable. A platform that
        # defers the release is reported rather than retried into a loop: the evidence is already
        # kept, so a name that lingers is bounded and visible, which is the honest outcome C4 asks
        # for when it says an unknown horizon is not a horizon.
        lingering = ""
        if not vanished:
            released = self._storage.recycle(name)
            if not released and self._storage.exists(name):
                try:
                    self._storage.remove(name)
                except GrafxError as failure:
                    lingering = str(failure)
        if self._storage.exists(name):
            detail = (
                f"The damaged control record {name!r} was quarantined as {entry.name!r} and "
                f"recorded in ledger entry {entry_id}, and the platform has not released its "
                f"name yet. {lingering} Retry the retirement; nothing was lost."
            )
        elif vanished:
            detail = (
                f"The damaged control record {name!r} was already gone at the last look, after "
                f"its generation was quarantined as {entry.name!r} and recorded in ledger "
                f"entry {entry_id}; the evidence is kept and nothing else was touched. The "
                "reader horizon must be re-derived before the log is recycled again."
            )
        else:
            detail = (
                f"The damaged control record {name!r} was retired as the exact generation "
                f"quarantined in {entry.name!r} and recorded in ledger entry {entry_id}. The "
                "reader horizon must be re-derived before the log is recycled again."
            )
        findings.append(
            RecoveryFinding(
                kind=FindingKind.CONTROL_RECORD_RETIRED,
                detail=detail,
                file=name,
                entry_id=entry_id,
                quarantine=entry.name,
            )
        )
        findings.append(self._rederive_horizon())
        self._count_outcome(OUTCOME_QUARANTINED)
        return RecoveryReport(
            outcome=OUTCOME_QUARANTINED,
            records_replayed=0,
            records_discarded=1,
            ledger_entries_created=entries_created,
            last_good_lsn=NO_LSN,
            findings=tuple(findings),
        )

    # --- steps -------------------------------------------------------------------------------

    def _check_meta(self) -> None:
        """Step 1: read the identity page and refuse a format this build cannot read.

        A database with no meta page yet is not a mismatch -- it is a database being created, and
        FR-1 puts recovery in the reopen path. Neither is a page 0 of nothing but zeros: C1's
        ``is_unwritten_image`` establishes that an all-zero image is an allocated page nobody has
        written, which is what a crash between creating the file and stamping it leaves. A meta
        page whose format is NEWER than this build does stop the run, before anything is scanned,
        because every later step would be deciding what to discard using rules that do not apply
        to it.
        """
        if not self._storage.exists(self._meta_file):
            return
        if self._storage.page_count(self._meta_file) < 1:
            return
        raw = self._storage.read_page(self._meta_file, 0)
        if is_unwritten_image(raw, self._pool.page_size):
            return
        # FileHeaderPage.read is the whole check. It refuses a page that is not of type META,
        # a page with no header record, a file that is not this engine's, and a format newer
        # than this build reads -- with the same class and the same ``field`` a second check
        # here would use. A mutation battery proved that second check untestable: deleting it
        # left the suite green because C1's answered in its place (A62, A67a). One mechanism,
        # independently observable, beats two that alibi each other.
        page = self._pool.codec.decode_page(raw, verify=True)
        FileHeaderPage.read(page)

    def _repair_ledger(
        self, findings: list[RecoveryFinding], permit: _RecoveryPermit
    ) -> None:
        """Make the ledger writable again after a crash during one of its own appends (TR-5).

        The interrupted bytes are quarantined before they are cut, exactly like a damaged log
        tail, so even the ledger's own repair leaves evidence. Without this the whole pass would
        fail on its first append, and G8 would be unsatisfiable for the run that most needs it.

        The preservation is the ledger store's own, not a step performed here: preserve and
        destroy are one door there, so this component cannot skip the first half by editing the
        second. What is left here is the reporting.
        """
        self._require_permit(permit)
        damage = self._ledger.damage
        if damage is None:
            return
        discard = self._ledger.discard_damaged_tail()
        findings.append(
            RecoveryFinding(
                kind=FindingKind.QUARANTINED_RANGE,
                detail=(
                    f"An interrupted ledger append left {discard.removed_bytes} unreadable bytes "
                    f"at byte {discard.offset}; they were quarantined as {discard.quarantine!r} "
                    "and then cut so the ledger could take this pass's entries."
                ),
                file=self._ledger.file,
                offset=discard.offset,
                length=discard.removed_bytes,
                quarantine=discard.quarantine,
            )
        )

    def _scan(
        self, findings: list[RecoveryFinding], *, floor_lsn: Lsn = NO_LSN
    ) -> RecoveryPlan:
        """Step 2: walk the log and decide where the good work ended."""
        plan = plan_recovery(self._wal.scan_all(), floor_lsn=floor_lsn)
        for discontinuity in plan.discontinuities:
            findings.append(
                RecoveryFinding(
                    kind=FindingKind.LSN_DISCONTINUITY,
                    detail=(
                        f"The log expected sequence number {discontinuity.expected_lsn} at byte "
                        f"{discontinuity.offset} of {discontinuity.segment!r} and found a record "
                        "carrying another, so everything from there on is discarded."
                    ),
                    file=discontinuity.segment,
                    offset=discontinuity.offset,
                    lsn=discontinuity.expected_lsn,
                )
            )
        return plan

    def _preserve(
        self,
        plan: RecoveryPlan,
        findings: list[RecoveryFinding],
        permit: _RecoveryPermit,
    ) -> int:
        """Steps 3 and 4: quarantine every discarded range, then write one ledger entry each.

        The order inside one item is copy, then record, then (later) cut. Across items it is all
        copies and all records before any cut, so an interruption at any point leaves a log that
        still holds the damage and a quarantine that already holds the evidence -- and a re-run
        recognises both instead of duplicating either.

        The count returned is the number of entries this pass ENSURED EXIST, one per discarded
        item, and it stays one per item on a re-run that finds the entry an interrupted pass
        already wrote. That is deliberate: ``records_discarded == ledger_entries_created`` is the
        equality G8 and BR-3 want a caller to be able to assert, and it would be false exactly
        when recovery was interrupted -- the case where the trace matters most.
        """
        self._require_permit(permit)
        created = 0
        for damaged in plan.ranges:
            created += self._record_range(damaged, findings)
        for discarded in plan.records:
            created += self._record_record(discarded, findings)
        return created

    def _record_range(
        self, damaged: DiscardedRange, findings: list[RecoveryFinding]
    ) -> int:
        """Quarantine one undecodable range and write its forensic ledger entry."""
        origin_class, reason = classify_failure(damaged.reason)
        entry = self._quarantine.capture(
            origin=damaged.segment,
            offset=damaged.offset,
            length=damaged.length,
            reason=reason.name.lower(),
            detail=damaged.detail,
            expected_lsn=damaged.expected_lsn,
        )
        body, detail = _carried_body(
            self._quarantine.read(entry.name), entry.name, damaged.detail
        )
        entry_id = self._ledger.record_discard(
            origin_class=origin_class,
            reason=reason,
            lsn_start=damaged.expected_lsn,
            lsn_end=damaged.expected_lsn,
            payload=LedgerPayload(
                origin=damaged.segment,
                offset=damaged.offset,
                length=damaged.length,
                expected_lsn=damaged.expected_lsn,
                failure=str(damaged.reason.value),
                detail=detail,
                quarantine=entry.name,
                body=body,
            ),
        )
        findings.append(
            RecoveryFinding(
                kind=FindingKind.DAMAGED_RANGE,
                detail=(
                    f"{damaged.length} bytes at byte {damaged.offset} of "
                    f"{damaged.segment!r} are not a record ({damaged.reason.value}); they were "
                    f"quarantined as {entry.name!r} and recorded as forensic ledger entry "
                    f"{entry_id}."
                ),
                file=damaged.segment,
                offset=damaged.offset,
                length=damaged.length,
                lsn=damaged.expected_lsn,
                entry_id=entry_id,
                quarantine=entry.name,
            )
        )
        self._count_discard(origin_class)
        return 1

    def _record_record(
        self, discarded: DiscardedRecord, findings: list[RecoveryFinding]
    ) -> int:
        """Quarantine one decodable record that sits above the cut, and record it as reapplicable.

        The bytes are copied as well as decoded. A reapplicable entry carries the encoded record
        so an applier can be handed the operation itself, and quarantining the same range means
        the range is recoverable even if this build's decoder is the thing that turns out to be
        wrong.
        """
        segment = discarded.segment
        offset = discarded.offset
        record = discarded.record
        length = record.encoded_length()
        origin_class, reason = classify_record()
        entry = self._quarantine.capture(
            origin=segment,
            offset=offset,
            length=length,
            reason=reason.name.lower(),
            detail=(
                f"A record with sequence number {record.lsn} decoded cleanly and sits above the "
                "last intact record, so it cannot be applied."
            ),
            expected_lsn=record.lsn,
        )
        body, detail = _carried_body(
            self._quarantine.read(entry.name),
            entry.name,
            (
                f"Record {record.lsn} of transaction {record.txn_id} in epoch "
                f"{record.epoch} was discarded with the truncated tail."
            ),
        )
        entry_id = self._ledger.record_discard(
            origin_class=origin_class,
            reason=reason,
            lsn_start=record.lsn,
            lsn_end=record.lsn,
            epoch=record.epoch,
            payload=LedgerPayload(
                origin=segment,
                offset=offset,
                length=length,
                expected_lsn=record.lsn,
                record_type=record.record_type,
                failure=reason.name.lower(),
                detail=detail,
                quarantine=entry.name,
                body=body,
            ),
        )
        findings.append(
            RecoveryFinding(
                kind=FindingKind.DISCARDED_RECORD,
                detail=(
                    f"Record {record.lsn} at byte {offset} of {segment!r} decoded cleanly and sat "
                    f"above the cut; it was quarantined as {entry.name!r} and recorded as "
                    f"reapplicable ledger entry {entry_id}."
                ),
                file=segment,
                offset=offset,
                length=length,
                lsn=record.lsn,
                entry_id=entry_id,
                quarantine=entry.name,
            )
        )
        self._count_discard(origin_class)
        return 1

    def _truncate(
        self,
        plan: RecoveryPlan,
        findings: list[RecoveryFinding],
        permit: _RecoveryPermit,
    ) -> None:
        """Step 3, second half: cut the log back to the last intact record.

        C4 leaves a log LONGER than asked when the platform will not release a segment, which is
        a valid log rather than one with a hole in it, so an incomplete truncation is retried
        inside a small budget and then reported. Reporting it is not a formality: until the cut
        completes the log still refuses appends, and the operator needs to know why.
        """
        self._require_permit(permit)
        attempt = 1
        while True:
            report = self._wal.truncate_after(plan.last_good_lsn)
            if report.completed or attempt >= TRUNCATION_ATTEMPTS:
                break
            attempt += 1
        if report.completed:
            kind = FindingKind.LOG_TRUNCATED
            detail = (
                f"The log was cut back to sequence number {plan.last_good_lsn}: "
                f"{report.removed_records} records and {report.removed_bytes} bytes went."
            )
        else:
            kind = FindingKind.TRUNCATION_DEFERRED
            detail = (
                f"The log could not be cut back to sequence number {plan.last_good_lsn}: the "
                f"platform still holds {len(report.deferred_segments)} segments after "
                f"{attempt} attempts. The bytes are quarantined and recorded; retry the open."
            )
        findings.append(
            RecoveryFinding(
                kind=kind,
                detail=detail,
                lsn=plan.last_good_lsn,
                length=report.removed_bytes,
            )
        )
        if not report.completed:
            raise GrafxRecoveryRefused(
                detail,
                retryable=True,
                field="wal_truncation",
                last_good_lsn=plan.last_good_lsn,
                attempts=attempt,
                deferred_segments=tuple(report.deferred_segments),
            )

    def _preflight_committed_replay(
        self, replay: CommittedReplay, permit: _RecoveryPermit, *, checkpoint_lsn: Lsn
    ) -> tuple[object, bool]:
        """Validate every committed effect before recovery performs its first mutation.

        Startup intentionally delays catalog interpretation until recovery owns the commit
        section. If this replay does not replace catalog pages, the existing catalog can safely
        populate the registry before strict validation. If it does replace them, only lookups of
        names introduced by those pages are deferred; payload/image validation still runs now,
        and the index-only subplan is preflighted strictly after adoption inside :meth:`_redo`.
        """
        self._require_permit(permit)
        touched_catalog = any(
            decode_page_write_location(record.payload).file == _CATALOG_FILE
            for record in replay.effects
            if record.record_type == int(WalRecordType.WRITE_PAGE)
        )
        if not touched_catalog and self._index_sync is not None:
            self._index_sync()
        proof = self._redo_engine.preflight(
            replay,
            allow_unregistered_indexes=touched_catalog,
            _passage=permit,
            _checkpoint_lsn=checkpoint_lsn,
        )
        return proof, touched_catalog

    def _redo(
        self,
        plan: RecoveryPlan,
        findings: list[RecoveryFinding],
        *,
        replay: CommittedReplay,
        state: CommitState,
        state_was_damaged: bool,
        permit: _RecoveryPermit,
        preflighted: object | None = None,
        preflight_touched_catalog: bool | None = None,
    ) -> int:
        """Complete committed WAL work and publish it as one fail-closed unit.

        Page effects land first so catalog pages can be adopted and the index registry can be
        synchronised before logical index records are dispatched.  No effect refusal is turned
        into a successful report: publishing a watermark after skipping one mandatory effect
        would make a partial commit visible permanently.
        """
        self._require_permit(permit)
        del (
            plan
        )  # the committed replay is the only part of the scan this stage consumes
        page_records = tuple(
            record
            for record in replay.effects
            if record.record_type == int(WalRecordType.WRITE_PAGE)
        )
        index_records = tuple(
            record
            for record in replay.effects
            if record.record_type
            in (int(WalRecordType.INDEX_WRITE), int(WalRecordType.INDEX_RECONCILE))
        )
        if preflight_touched_catalog is None:
            preflight_touched_catalog = any(
                decode_page_write_location(record.payload).file == _CATALOG_FILE
                for record in page_records
            )

        page_replay = CommittedReplay(
            effects=page_records, last_committed_lsn=replay.last_committed_lsn,
            commit_records=replay.commit_records,
        )
        index_replay = CommittedReplay(
            effects=index_records, last_committed_lsn=replay.last_committed_lsn,
            commit_records=replay.commit_records,
        )
        manager = self._index_manager
        if not preflight_touched_catalog and self._index_sync is not None:
            # Startup deliberately postpones interpreting catalog bytes until recovery owns the
            # commit section. When this WAL range does not replace catalog pages, the existing
            # catalog is already the authority, so adopt its existing indexes now. This turns
            # an absent mandatory index into a preflight refusal before heap pages move.
            self._index_sync()
        # Decode and statically validate the whole committed plan before the first real page
        # changes. A catalog effect may be the authority that introduces an index to this
        # participant, so only that registry lookup is deferred; after catalog adoption the
        # index-only apply below performs its ordinary strict preflight before dispatch.
        full_preflight = self._redo_engine._ensure_preflight(
            replay,
            preflighted,
            allow_unregistered_indexes=preflight_touched_catalog,
            passage=permit,
            checkpoint_lsn=state.checkpoint_lsn,
        )
        touched_catalog = preflight_touched_catalog
        page_preflight = self._redo_engine._project_page_preflight(
            replay,
            page_replay,
            full_preflight,
            allow_unregistered_indexes=touched_catalog,
            passage=permit,
            checkpoint_lsn=state.checkpoint_lsn,
        )
        if page_preflight is None:
            raise GrafxRecoveryRefused(
                "The recovery page subplan no longer matches its complete preflight.",
                field="preflighted_replay",
            )
        target = max(state.last_committed_lsn, replay.last_committed_lsn)
        # The scan proves that the bytes form complete records; it does not prove that the process
        # which appended them forced them to stable storage. Recovery must establish that proof
        # before the first data-page apply and, especially, before publishing commit.state. Use
        # the explicit range door rather than WalManager's pending cache: an earlier barrier may
        # have emptied that cache without proving these foreign bytes for this publication.
        if target > state.checkpoint_lsn:
            first = NO_LSN + 1 if state_was_damaged else state.checkpoint_lsn + 1
            self._wal.force_barrier_range(first, target)
        page_result = self._redo_engine.apply(
            page_replay,
            _preflighted=page_preflight,
            _passage=permit,
            _checkpoint_lsn=state.checkpoint_lsn,
        )
        if touched_catalog:
            self._adopt_catalog(findings)
        if touched_catalog and self._index_sync is not None:
            self._index_sync()
        watermarks = None
        if manager is not None and not state_was_damaged:
            # Startup could not register the persistent indexes until catalog page redo made
            # their definitions readable. Check those newly adopted files against the retained
            # WAL floor before replay can certify them through the final target. Page redo and
            # adoption are DONE, index replay moves index files and never the heap, and this
            # process still holds the section -- so this one photo answers both this pass and
            # the completion mark below (ST-7).
            watermarks = manager.table_watermark_photo()
            manager.check_replay_floor(
                state.checkpoint_lsn,
                persist_stale=self._policy != POLICY_REFUSE,
                watermarks=watermarks,
            )
        index_result = self._redo_engine.apply(index_replay, _checkpoint_lsn=state.checkpoint_lsn)

        # Make every replayed heap/catalog/index effect visible to the device before certifying
        # derived indexes. If a data flush fails, no fresh header may get ahead of the data it
        # claims to cover.
        self._redo_engine.flush(page_result)
        self._redo_engine.flush(index_result)
        if manager is not None and target > NO_LSN:
            marker = getattr(manager, "mark_built_through", None)
            if not callable(marker):
                raise GrafxRecoveryRefused(
                    "Recovery has an index manager that cannot certify completed replay.",
                    field="index_manager",
                )
            if watermarks is not None:
                marker(target, watermarks=watermarks)
            else:
                marker(target)

        # WAL remains the durability authority; publication below is the last visible act. A
        # replayed catalog-v2 activation promotes the same-size mixed-fleet fence, including the
        # crash window after catalog apply and before the original publisher reached commit.state.
        format_version = self._commit_state_format_for_catalog(state.format_version)
        needs_publication = (
            target > state.last_committed_lsn
            or state_was_damaged
            or format_version != state.format_version
        )
        published_state = (
            CommitState(
                last_committed_lsn=target,
                last_csn=target,
                checkpoint_lsn=state.checkpoint_lsn,
                format_version=format_version,
            )
            if needs_publication
            else state
        )
        if needs_publication:
            # Recovery publication must not outrun the physical files it certifies.  Flush only
            # makes dirty frames visible to the device; establish per-file durability after the
            # index completion marker (which may dirty headers without a logical index record)
            # and before commit.state becomes the visible authority.  A deterministic, explicit
            # file set avoids the broader and adapter-dependent ``barrier(None)`` door.
            durable_files = set(page_result.touched_files)
            durable_files.update(index_result.touched_files)
            if manager is not None:
                durable_files.update(index.file for index in manager.active_indexes())
            for file in sorted(durable_files):
                self._pool.durability_barrier(file)
            self._state_store.publish(
                published_state,
                previous=state,
                previous_was_damaged=state_was_damaged,
            )
        self._state_store.repair_redundancy(published_state)

        replayed = page_result.effects_replayed + index_result.effects_replayed
        if replayed and self._metrics.enabled:
            self._metrics.increment(RECOVERY_REPLAYS_TOTAL, float(replayed))
        return replayed

    def _adopt_catalog(self, findings: list[RecoveryFinding]) -> None:
        """Complete the CF-4 route after catalog pages were replayed: read, then adopt.

        Never ``load()``. C1 measured what that costs -- a store holding ``['Person','Second']``
        came back holding ``['Person']`` -- and the guard exists to prevent exactly that loss.
        ``read_from_pages()`` says what the replayed pages hold, ``adopt()`` states that the store
        now describes those pages, and the store's next ``save()`` is then accepted instead of
        being refused for an epoch that moved under it.
        """
        if self._catalog is None:
            findings.append(
                RecoveryFinding(
                    kind=FindingKind.CATALOG_UNREADABLE,
                    detail=(
                        "Catalog pages were replayed and no catalog store is wired to this "
                        "recovery, so commit completion cannot safely publish them."
                    ),
                    file=_CATALOG_FILE,
                )
            )
            raise GrafxRecoveryRefused(
                "Recovery replayed catalog pages but has no catalog store to adopt them; "
                "commit-state publication was refused.",
                field="catalog",
                file=_CATALOG_FILE,
            )
        try:
            adopted = self._catalog.adopt(self._catalog.read_from_pages())
        except GrafxError as failure:
            findings.append(
                RecoveryFinding(
                    kind=FindingKind.CATALOG_UNREADABLE,
                    detail=(
                        "Catalog pages were replayed and the catalog could not be read back from "
                        f"them: {failure}"
                    ),
                    file=_CATALOG_FILE,
                )
            )
            raise
        findings.append(
            RecoveryFinding(
                kind=FindingKind.CATALOG_ADOPTED,
                detail=(
                    f"The catalog was re-derived from the replayed pages and adopted, holding "
                    f"{len(adopted.tables())} tables; the store is not left refusing its next "
                    "save."
                ),
                file=_CATALOG_FILE,
            )
        )

    # --- helpers -----------------------------------------------------------------------------

    def _read_recovery_state(self) -> tuple[CommitState, bool]:
        """Return the strict published state and whether damaged bytes had to be rebuilt.

        Corruption is recoverable only from a complete WAL lineage and is checked before any
        destructive step by :meth:`_validate_publication_lineage`. Intact bytes from a newer
        format and transient device failures propagate unchanged; neither is evidence that the
        state may be replaced.
        """
        try:
            return self._state_store.read(), False
        except GrafxCorruptionDetected as failure:
            if failure.details.get("commit_state_reconstructible", False) is not True:
                raise
            minimum_format = failure.details.get(
                "commit_state_minimum_format_version", 1
            )
            return (
                CommitState(
                    format_version=self._commit_state_format_for_catalog(
                        minimum_format, tolerate_unreadable=True
                    )
                ),
                True,
            )

    def _commit_state_format_for_catalog(
        self, fallback: int, *, tolerate_unreadable: bool = False
    ) -> int:
        """Derive the non-downgradable control fence from durable catalog pages.

        Recovery runs before normal startup adopts the catalog, so it reads through the
        non-destructive ``read_from_pages`` door. During the initial damaged-state probe a torn
        catalog may still be repairable by retained WAL and is tolerated; before publication the
        same uncertainty is a refusal, never permission to emit version 1 over catalog v2.
        """

        reader = getattr(self._catalog, "read_from_pages", None)
        if not callable(reader):
            return fallback
        bootstrap_probe = getattr(self._catalog, "is_bootstrapped", None)
        if callable(bootstrap_probe) and not bootstrap_probe():
            if fallback == COMMIT_STATE_FORMAT_VERSION:
                raise GrafxRecoveryRefused(
                    "Commit-state format 2 requires a bootstrapped catalog format 2; recovery "
                    "will not infer or lower that fleet fence.",
                    field="format_version",
                    commit_state_format=fallback,
                    catalog_bootstrapped=False,
                )
            return fallback
        try:
            catalog = reader()
        except GrafxCorruptionDetected:
            if tolerate_unreadable:
                return fallback
            raise
        observed = getattr(catalog, "format_version", None)
        if observed == CATALOG_FORMAT_VERSION:
            return COMMIT_STATE_FORMAT_VERSION
        if observed != CATALOG_LEGACY_FORMAT_VERSION:
            raise GrafxRecoveryRefused(
                f"Recovery read unsupported catalog format {observed!r} before publication.",
                field="format_version",
                value=observed,
                supported=CATALOG_FORMAT_VERSION,
            )
        if fallback == COMMIT_STATE_FORMAT_VERSION:
            raise GrafxRecoveryRefused(
                "Commit-state format 2 requires catalog format 2, but durable catalog pages "
                "still decode as version 1; recovery will not downgrade the fleet fence.",
                field="format_version",
                commit_state_format=fallback,
                catalog_format=observed,
            )
        return fallback

    def _validate_publication_lineage(
        self,
        state: CommitState,
        state_was_damaged: bool,
        plan: RecoveryPlan,
        replay: CommittedReplay,
    ) -> None:
        """Refuse to invent or regress a published commit watermark from an incomplete WAL."""
        if state.checkpoint_lsn > state.last_committed_lsn:
            raise GrafxRecoveryRefused(
                "The published checkpoint is ahead of the last committed LSN; recovery cannot "
                "derive a safe replay floor.",
                field="checkpoint_lsn",
                checkpoint_lsn=state.checkpoint_lsn,
                last_committed_lsn=state.last_committed_lsn,
            )
        if plan.last_good_lsn < state.checkpoint_lsn:
            # Recycling deliberately retains the segment that reaches the published checkpoint.
            # Falling below it therefore means the WAL no longer carries the sequence anchor
            # that keeps future appends above the page replay floor. Truncating there would let
            # WalManager reuse LSNs at or below the checkpoint; a later recovery would filter
            # their page images out and could publish their COMMIT as an empty transaction.
            raise GrafxRecoveryRefused(
                "The trustworthy WAL prefix ends below the published checkpoint; recovery "
                "will not truncate into the replay floor or allow its sequence numbers to be "
                "reused.",
                field="wal_lineage",
                checkpoint_lsn=state.checkpoint_lsn,
                last_good_lsn=plan.last_good_lsn,
            )
        damage_positions = tuple((item.segment, item.offset) for item in plan.ranges)
        effect_types = {
            int(WalRecordType.WRITE_PAGE),
            int(WalRecordType.INDEX_WRITE),
            int(WalRecordType.INDEX_RECONCILE),
        }
        ambiguous_tail = tuple(
            discarded.record
            for discarded in plan.records
            if discarded.record.lsn > state.last_committed_lsn
            and (
                discarded.record.record_type == int(WalRecordType.COMMIT)
                or (
                    discarded.record.record_type in effect_types
                    and any(
                        position > (discarded.segment, discarded.offset)
                        for position in damage_positions
                    )
                )
            )
        )
        if ambiguous_tail:
            # A decoder failure cuts the trustworthy prefix, but later bytes may still decode.
            # Those records cannot be treated as an unapplied tail: the old live path barriers
            # the complete batch, applies its effects, and only then publishes commit.state. If
            # corruption later destroys the FIRST effect, the surviving COMMIT sits above the
            # cut and replay sees neither it nor an incomplete effect; truncating would leave the
            # already-applied page/index state behind as a ghost. With no UNDO or batch digest,
            # a surviving COMMIT above the already-published watermark (or a later damaged range
            # that may have held one) makes physical application ambiguous. Duplicate records at
            # or below commit.state can be left by a crash during segment replacement and are
            # already proven/applied; effects that reach the end with no COMMIT are likewise
            # provably unapplied by the live protocol and remain safe reapplicable discards.
            transactions = tuple(
                sorted({(record.epoch, record.txn_id) for record in ambiguous_tail})
            )
            record_lsns = tuple(record.lsn for record in ambiguous_tail)
            raise GrafxRecoveryRefused(
                "The WAL contains transactional records after its first damaged byte range. "
                "Those records may belong to a batch whose effects already reached the data "
                "files, so truncating the batch cannot prove atomicity without UNDO.",
                field="wal_lineage",
                incomplete_effect_lsns=record_lsns,
                incomplete_transactions=transactions,
                discarded_record_lsns=record_lsns,
                last_good_lsn=plan.last_good_lsn,
            )
        if replay.incomplete_effects:
            # A successful WAL barrier is followed by page/index apply and only then by
            # commit.state publication. Damage can therefore erase a COMMIT record after its
            # effects reached the device. Treating those effects as merely uncommitted leaves
            # future MVCC watermarks free to make an orphaned row visible, and logical index
            # effects provide no general physical test that could prove they were not applied.
            # With no UNDO record, preserving the ambiguous tail is the only safe answer.
            pending = replay.incomplete_effects
            transactions = tuple(
                sorted({(record.epoch, record.txn_id) for record in pending})
            )
            raise GrafxRecoveryRefused(
                "The retained WAL ends with durable effects whose COMMIT or ABORT outcome "
                "cannot be proved. Continuing or truncating could leave applied effects that "
                "a later watermark would legitimize.",
                field="wal_lineage",
                incomplete_effect_lsns=tuple(record.lsn for record in pending),
                incomplete_transactions=transactions,
                last_good_lsn=plan.last_good_lsn,
            )
        records = plan.replayable
        expected_first = NO_LSN + 1 if state_was_damaged else state.checkpoint_lsn + 1
        needs_lineage = (
            state_was_damaged or state.last_committed_lsn > state.checkpoint_lsn
        )
        if records and records[0].lsn != expected_first:
            raise GrafxRecoveryRefused(
                f"Recovery expected WAL record {expected_first} after its replay floor, but the "
                f"first retained record is {records[0].lsn}; a recycled prefix prevents safe "
                "commit-state reconstruction.",
                field="wal_lineage",
                expected_lsn=expected_first,
                observed_lsn=records[0].lsn,
            )
        if needs_lineage and not records:
            raise GrafxRecoveryRefused(
                "The published state needs WAL completion, but no retained record proves the "
                "lineage after its checkpoint.",
                field="wal_lineage",
                checkpoint_lsn=state.checkpoint_lsn,
                last_committed_lsn=state.last_committed_lsn,
            )
        if (
            state.last_committed_lsn > state.checkpoint_lsn
            and replay.last_committed_lsn < state.last_committed_lsn
        ):
            raise GrafxRecoveryRefused(
                "The WAL no longer proves the commit already named by commit.state; recovery "
                "will not lower a published snapshot.",
                field="last_committed_lsn",
                published_lsn=state.last_committed_lsn,
                recovered_lsn=replay.last_committed_lsn,
            )
        if state_was_damaged and replay.last_committed_lsn == NO_LSN:
            raise GrafxRecoveryRefused(
                "commit.state is damaged and the retained WAL proves no complete commit; "
                "recovery cannot invent a published state.",
                field="commit_state",
            )

    def _checkpoint_lsn(self) -> Lsn:
        """Return the published checkpoint, or zero when there is none to read.

        A commit state that will not parse is not a reason to refuse: it is a control-plane hint
        about how much of the log is already on the pages, and treating it as absent only makes
        recovery redo more, which is idempotent. Refusing here would wedge the database on a file
        that no data depends on.
        """
        return self._state_store.checkpoint_hint()

    def _probe_damage(self, file: str) -> str | None:
        """Return why the probe says a control record is damaged, or None when it reads cleanly.

        **The taxonomy of section 2 freezes exactly ONE damage class, and this door accepts
        exactly that one.** ``GrafxCorruptionDetected`` is the class A11-revised reserves for
        conditions attributable to DAMAGED BYTES, and it is the class FR-8/FR-10 turn into
        truncation, quarantine and a forensic ledger entry. Every other class says something
        about the reader, the caller or the build -- never about the bytes:

        * ``stale_epoch`` and ``lease_stolen`` are what the coordination adapter raises about a
          perfectly HEALTHY lease when the reader is the stale one. Retiring on those is a stale
          writer deleting the live writer's lease and filing it as forensic damage, which is
          carried finding CF-1 point 4 inverted: *never for letting a writer delete the file that
          is refusing it*.
        * ``port_not_configured`` is G5 saying the probe could not run at all -- the same "it
          said nothing about the bytes" the foreign-exception branch below refuses on.
        * ``schema_version_mismatch`` is a record from a NEWER build, and ``run()`` already
          states the opposite position for the log: *nothing was discarded, because those bytes
          are intact and belong to a newer build*. Two doors of one component may not disagree
          about whether intact bytes are damage.
        * a retryable failure is an ACCESS failure -- a sharing violation from an antivirus or an
          indexer -- and it declares itself retryable so the caller knows to try again (A47).

        Everything that is not the one damage class therefore refuses in the same direction the
        healthy case and the foreign-exception case already refuse, and says which class it saw.
        The probe is host-supplied code and is called with nothing of this component in flight
        (A91).
        """
        probe = self._probe
        if probe is None:  # pragma: no cover - the caller refuses before reaching here
            return None
        try:
            probe.read_control_record(file)
        except GrafxCorruptionDetected as damaged:
            if is_retryable(damaged):
                # A corruption class that declares itself retryable is not the settled evidence
                # this door destroys a name on; it is a reader asking to be called again.
                raise self._busy_refusal(file, damaged) from damaged
            return damaged.message
        except GrafxError as failure:
            if is_retryable(failure):
                # A47 and A11-revised, on the one door of this component that destroys a name.
                # A sharing violation from an antivirus or an indexer is an ACCESS failure: it
                # says the record could not be read now, and nothing whatever about the bytes.
                # Treating it as damage quarantines and retires a HEALTHY lease -- which is C4's
                # position inverted, and it is the exact condition A11-revised was written about.
                # Retirement needs evidence of DAMAGE, so this refuses and says to try again.
                raise self._busy_refusal(file, failure) from failure
            raise GrafxRecoveryRefused(
                f"The probe for control record {file!r} refused with {failure.code!r}, which is "
                f"not evidence that its bytes are damaged -- only "
                f"{GrafxCorruptionDetected.code!r} is: {failure.message} Nothing was quarantined "
                "or retired.",
                file=file,
                field="control_probe",
                cause=failure.code,
                probe_error=type(failure).__name__,
            ) from failure
        except Exception as failure:  # noqa: BLE001 - only Grafx errors leave a public door
            # An unclassifiable failure is evidence of nothing either. The probe broke; whether
            # the RECORD is damaged is exactly what it failed to say, and guessing in the
            # destructive direction is the wrong way to be wrong.
            raise GrafxRecoveryRefused(
                f"The probe for control record {file!r} failed with "
                f"{type(failure).__name__}: {failure} That says nothing about the bytes, so "
                "nothing was quarantined or retired.",
                file=file,
                field="control_probe",
                probe_error=type(failure).__name__,
            ) from failure
        return None

    @staticmethod
    def _busy_refusal(file: str, failure: GrafxError) -> GrafxRecoveryRefused:
        """Return the retryable refusal for a probe that says the file is busy, not damaged."""
        refusal = GrafxRecoveryRefused(
            f"The control record {file!r} could not be read, and the failure declares "
            f"itself retryable, so it is evidence that the file is busy rather than "
            f"damaged: {failure.message} Nothing was quarantined or retired; retry.",
            retryable=True,
            file=file,
            field="control_probe",
            cause=failure.code,
        )
        refusal.details[RETRYABLE_KEY] = True
        return refusal

    def _rederive_horizon(self) -> RecoveryFinding:
        """Ask the coordinator for the reader horizon again, as C4 requires after a retirement."""
        if self._coordinator is None:
            return RecoveryFinding(
                kind=FindingKind.READER_HORIZON_REDERIVED,
                detail=(
                    "No coordinator is wired to this recovery, so the caller must re-derive the "
                    "reader horizon before the log is recycled again; the log manager caches "
                    "none across a pass."
                ),
            )
        try:
            horizon = self._coordinator.reader_horizon()
        except GrafxError as failure:
            return RecoveryFinding(
                kind=FindingKind.READER_HORIZON_REDERIVED,
                detail=(
                    f"The reader horizon still cannot be derived after the retirement: {failure} "
                    "Nothing may be recycled until it can."
                ),
            )
        return RecoveryFinding(
            kind=FindingKind.READER_HORIZON_REDERIVED,
            detail=(
                "The reader horizon was re-derived after the retirement and is "
                f"{'unset, so no live reader holds the log back' if horizon is None else horizon}."
            ),
            lsn=NO_LSN if horizon is None else horizon,
        )

    def _count_discard(self, origin_class: LedgerOriginClass) -> None:
        """Count one discarded item under its origin class (CONTRACT.md section 9)."""
        if self._metrics.enabled:
            self._metrics.increment(
                RECOVERY_DISCARDED_RECORDS_TOTAL, 1.0, _ORIGIN_LABELS[origin_class]
            )

    def _count_outcome(self, outcome: str) -> None:
        """Count one completed recovery run under its outcome (CONTRACT.md section 9)."""
        if self._metrics.enabled:
            self._metrics.increment(RECOVERIES_TOTAL, 1.0, {"outcome": outcome})

    def __repr__(self) -> str:
        """Return a short description naming the policy this manager runs under."""
        return f"RecoveryManager(recovery_policy={self._policy!r})"


_CATALOG_FILE: str = "catalog.dat"


_CANONICAL_LEDGER_REPAIR = RecoveryManager._repair_ledger


def _carried_body(body: bytes, entry_name: str, detail: str) -> tuple[bytes, str]:
    """Return the bytes a ledger entry may carry, and the detail that explains what it carries.

    Past ``MAX_LEDGER_BODY_BYTES`` the entry carries none and says where they are instead. Half a
    damaged range in the ledger and the whole of it in quarantine would be two answers to one
    question, and the smaller one wears the same digest as if it were complete.
    """
    if len(body) <= MAX_LEDGER_BODY_BYTES:
        return body, detail
    return b"", (
        f"{detail} The range is {len(body)} bytes, past the {MAX_LEDGER_BODY_BYTES} an entry "
        f"carries, so the bytes are preserved in quarantine entry {entry_name!r}."
    )


def _require_port(
    slot: str,
    instance: object,
    methods: Sequence[str],
    attribute_probe: Callable[[object, str], bool],
) -> None:
    """Refuse a port or collaborator that cannot answer the doors recovery opens (G5).

    Inspect declared attributes without invoking descriptors.  In particular, ``damage`` is
    scan-backed on a freshly opened native WAL (and may be on alternative implementations), so
    ``hasattr`` would perform a full pass merely to validate the port shape.  A dynamic fallback
    keeps transparent ``__getattr__`` wrappers compatible; as with ``hasattr``, only
    ``AttributeError`` means that a door is absent and every other exception remains visible.
    """
    missing: list[str] = []
    for name in methods:
        present = attribute_probe(instance, name)
        if type(present) is not bool:
            raise GrafxConfigurationError(
                "Recovery attribute_probe must return an exact bool.",
                field="attribute_probe",
                slot=slot,
                member=name,
                value=type(present).__name__,
            )
        if not present:
            missing.append(name)
    if missing:
        raise GrafxPortNotConfigured(
            f"The {slot} port of recovery is missing {', '.join(missing)}.",
            slot=slot,
            missing=tuple(missing),
        )


def _one_policy(recovery_policy: object, policy: object) -> object:
    """Return the single policy the caller named, refusing two spellings that disagree.

    Section 5 and ``DatabaseConfig`` call this setting ``recovery_policy``; the assembly written
    against this manager calls it ``policy``. Accepting both keeps that assembly working through
    the rename, and refusing a DISAGREEMENT is what stops the alias from becoming a place where a
    caller's stated policy is silently discarded -- which on the ``refuse`` policy would mean
    quarantining and truncating a log the operator asked nobody to touch.
    """
    if recovery_policy is not None and policy is not None and recovery_policy != policy:
        raise GrafxConfigurationError(
            f"The recovery policy was given twice and the two disagree: recovery_policy="
            f"{recovery_policy!r} and policy={policy!r}. Name it once.",
            field="recovery_policy",
            value=repr((recovery_policy, policy)),
        )
    chosen = recovery_policy if recovery_policy is not None else policy
    return POLICY_REPLAY if chosen is None else chosen


def _validate_policy(policy: object) -> str:
    """Return the recovery policy, refusing a word that is not one of the two FR-8 defines."""
    if policy not in RECOVERY_POLICIES:
        raise GrafxConfigurationError(
            f"A recovery policy is one of {RECOVERY_POLICIES}; got {policy!r}.",
            field="recovery_policy",
            value=repr(policy),
        )
    return str(policy)


def _require_timeout(field: str, value: object) -> float:
    """Return a finite positive timeout for the shared recovery section."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrafxConfigurationError(
            f"The {field} must be a positive number; got {value!r}.",
            field=field,
            value=repr(value),
        )
    timeout = float(value)
    if not timeout > 0.0 or timeout != timeout or timeout == float("inf"):
        raise GrafxConfigurationError(
            f"The {field} must be finite and greater than zero; got {value!r}.",
            field=field,
            value=repr(value),
        )
    return timeout


def _require_text(field: str, value: object) -> str:
    """Return a non-empty string argument, refusing anything else."""
    if not isinstance(value, str) or not value:
        raise GrafxConfigurationError(
            f"The {field} of a recovery operation must be a non-empty string; got {value!r}.",
            field=field,
            value=repr(value),
        )
    return value
