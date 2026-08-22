"""Round-three findings: who may validate, and which arm answers which failure.

Two themes. The first is A74: ``validate_epoch`` is the guard a committing writer passes
immediately before it writes, so the question it answers is "may I commit", not "is this number
current". A participant holding nothing must never get yes.

The second is A66 applied to the arms that classify a failure. Three answers are possible --
damaged bytes, a transport failure, a section somebody else holds -- and each arm has a sibling
that gets it right, which is exactly how each wrong one stayed invisible.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice, HookStorageDevice, ManualClock
from okto_grafx.adapters.coordination_local import (
    LocalProcessCoordinator,
    ReaderRecord,
    decode_lease_record,
    encode_lease_record,
    encode_reader_record,
)
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxLeaseStolen,
    GrafxLeaseTimeout,
    GrafxStaleEpoch,
    GrafxStorageError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ports.coordination import ReaderHandle

# --- A74: validation asks "may I commit", not "is this number current" -------------------------


def test_a_participant_that_holds_nothing_cannot_validate_the_current_epoch(
    make_coordinator: CoordinatorFactory
) -> None:
    """The epoch number being current says nothing about who is entitled to write under it.

    Sections 8.5 steps 2 and 3.1 make this the last thing a writer does before it writes, so a
    participant that never acquired anything getting yes is the two-writer window BR-7 and AC-6
    exist to close. The identity that decides is the lease this coordinator installed, which is a
    fact it holds locally -- not the owner name in the record, which an operator repair or a
    restored file can make match by accident.
    """
    holder = make_coordinator(owner_id="p1-holder")
    lease = holder.acquire_writer_lease(timeout=1.0)
    bystander = make_coordinator(owner_id="p2-bystander", monotonic_origin=40.0)

    assert bystander.current_epoch() == lease.epoch
    with pytest.raises(GrafxStaleEpoch) as refusal:
        bystander.validate_epoch(lease.epoch)
    assert refusal.value.retryable is False
    holder.validate_epoch(lease.epoch)


def test_a_participant_sharing_the_owner_name_still_cannot_validate(
    make_coordinator: CoordinatorFactory
) -> None:
    # The stronger form earns its keep here: the record's owner string matches, and the answer is
    # still no, because this participant installed nothing.
    holder = make_coordinator(owner_id="p1-same")
    lease = holder.acquire_writer_lease(timeout=1.0)
    twin = make_coordinator(owner_id="p1-same", monotonic_origin=40.0)
    with pytest.raises(GrafxStaleEpoch):
        twin.validate_epoch(lease.epoch)
    holder.validate_epoch(lease.epoch)


def test_validation_follows_the_lease_through_a_takeover(
    make_coordinator: CoordinatorFactory
) -> None:
    zombie = make_coordinator(owner_id="p0-dead", monotonic_origin=500.0)
    stale = zombie.acquire_writer_lease(timeout=1.0)
    clock = ManualClock(monotonic=1_000.0)
    survivor = make_coordinator(owner_id="p1-alive", clock=clock)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is not None
    taken = survivor.takeover()

    survivor.validate_epoch(taken.epoch)
    with pytest.raises(GrafxStaleEpoch):
        zombie.validate_epoch(stale.epoch)
    with pytest.raises(GrafxStaleEpoch):
        zombie.validate_epoch(taken.epoch)


def test_a_released_holder_can_no_longer_validate(make_coordinator: CoordinatorFactory) -> None:
    holder = make_coordinator(owner_id="p1-holder")
    lease = holder.acquire_writer_lease(timeout=1.0)
    holder.validate_epoch(lease.epoch)
    holder.release_lease(lease)
    with pytest.raises(GrafxStaleEpoch):
        holder.validate_epoch(lease.epoch)


# --- D1: a transport failure is never reported as damaged bytes -------------------------------


def test_a_straddled_read_followed_by_a_transport_failure_is_not_damage(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """The benign race must not be remembered as evidence against the bytes.

    A short read is the signature of another participant replacing the file between the size
    call and the bytes call -- this module's own docstring calls it a benign race, not damage,
    and says only a mismatch surviving EVERY attempt is corruption. Carrying that first straddle
    forward and raising it when a later attempt fails for a transport reason manufactures an
    integrity incident, and in this engine corruption means truncation, quarantine and forensic
    ledger entries (FR-8, FR-10).
    """
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    coordinator.acquire_writer_lease(timeout=1.0)

    def arm_transport_failure(_operation: str, _index: int) -> None:
        device.failure = PermissionError(errno.EACCES, "The process cannot access the file.")
        device.fail_next["read_log"] = 10_000

    device.reset()
    device.short_read_next = 1
    device.hook = arm_transport_failure
    device.hook_at = next(
        index for index, name in enumerate(("exists", "log_size", "read_log"), start=1)
        if name == "read_log"
    )
    with pytest.raises(GrafxStorageError) as failure:
        coordinator.current_epoch()
    assert not isinstance(failure.value, GrafxCorruptionDetected)
    assert failure.value.details["attempts"] >= 2

    # The bytes were never in question: with the transport restored the record reads back whole.
    device.fail_next["read_log"] = 0
    device.short_read_next = 0
    assert coordinator.current_epoch() == 1


def test_a_mismatch_that_survives_every_attempt_is_still_damage(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    coordinator.acquire_writer_lease(timeout=1.0)
    device.short_read_next = 10_000
    with pytest.raises(GrafxCorruptionDetected):
        coordinator.current_epoch()


# --- D2: a control file that exists but says nothing ------------------------------------------


def test_a_zero_length_lease_file_is_damage_rather_than_absence(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """Every other malformed shape of this file fails closed; zero bytes must not fail open.

    Read as "never published", an empty lease restarts the epoch lineage at 1 -- re-issuing a
    number a live participant may still hold, and handing WAL records two different meanings for
    the same epoch. The file existing is itself the evidence that something was published.
    """
    first = make_coordinator(owner_id="p1-first")
    first.acquire_writer_lease(timeout=1.0)
    lease_file = database_root / "control" / "writer.lease"
    assert lease_file.stat().st_size > 0
    lease_file.write_bytes(b"")

    later = make_coordinator(owner_id="p2-later", monotonic_origin=90.0)
    with pytest.raises(GrafxCorruptionDetected):
        later.current_epoch()
    with pytest.raises(GrafxCorruptionDetected):
        later.acquire_writer_lease(timeout=0.0)


def test_a_lease_file_that_was_never_created_is_absence(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator = make_coordinator(owner_id="p1-first")
    assert coordinator.current_epoch() == 0
    assert coordinator.acquire_writer_lease(timeout=0.0).epoch == 1


def test_a_zero_length_reader_registration_is_absence(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # The reader file has no lineage to restart: an empty one is an interrupted create, and the
    # sanctioned answer is to treat the registration as gone.
    coordinator = make_coordinator(owner_id="p1-aaaa")
    coordinator.register_reader(30)
    empty = database_root / "control" / "readers" / "p9-other-r0002.reader"
    empty.write_bytes(b"")
    assert coordinator.reader_horizon() == 30
    assert not empty.exists()


# --- D3: a lock primitive that cannot lock is not a busy section ------------------------------


def test_a_lock_primitive_that_will_never_work_is_a_storage_failure(
    make_coordinator: CoordinatorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contention is one errno among several, and only contention clears by waiting.

    A mount without advisory locking answers ENOLCK for ever. Reported as a lease timeout it
    blames a holder that does not exist and invites a caller to retry a path that cannot work --
    which is the rule the sibling that opens the same file already states.
    """
    from okto_grafx.adapters import coordination_local

    def refuse(handle: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available.")

    coordinator = make_coordinator(owner_id="p1-aaaa", poll_interval=0.25)
    monkeypatch.setattr(coordination_local, "_acquire_os_lock", refuse)
    with pytest.raises(GrafxStorageError) as failure:
        with coordinator.exclusive("commit", timeout=1.0):
            pass
    monkeypatch.undo()
    assert not isinstance(failure.value, GrafxLeaseTimeout)
    assert failure.value.details["errno"] == errno.ENOLCK
    assert failure.value.retryable is False
    assert failure.value.details["retryable"] is False


def test_real_contention_is_still_a_lease_timeout(make_coordinator: CoordinatorFactory) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    with first.exclusive("commit", timeout=1.0):
        with pytest.raises(GrafxLeaseTimeout) as failure:
            with second.exclusive("commit", timeout=0.2):
                pass
    assert failure.value.retryable is True
    assert failure.value.details["section"] == "commit"


# --- D4: the one hand-built storage error carries its classification too ----------------------


def test_a_lock_directory_failure_carries_its_classification_in_the_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    def refuse(path: object, mode: int = 0o777, exist_ok: bool = False) -> None:
        raise PermissionError(errno.EACCES, "Access is denied.")

    monkeypatch.setattr(os, "makedirs", refuse)
    with pytest.raises(GrafxStorageError) as failure:
        LocalProcessCoordinator(
            DirectoryStorageDevice(tmp_path / "db"),
            ManualClock(),
            owner_id="p1-aaaa",
            lock_directory=str(tmp_path / "control"),
        )
    monkeypatch.undo()
    assert failure.value.details["retryable"] is failure.value.retryable
    assert failure.value.details["errno"] == errno.EACCES
    assert failure.value.details["attempts"] >= 1


# --- E24: the sweep may only ever release a temporary ------------------------------------------


def test_the_sweep_only_ever_releases_temporary_files(
    make_coordinator: CoordinatorFactory, database_root: Path, lock_directory: str
) -> None:
    """The lock file lives beside the lease, and releasing it is a mutual-exclusion failure.

    On Windows an open handle makes the release fail and the file survive, so a suite that only
    checks for the file afterwards is blind on that family. What is asserted here instead is the
    intent: every name this participant releases while sweeping ends in the temporary suffix.
    """
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    control = database_root / "control"
    control.mkdir(parents=True, exist_ok=True)
    (control / "writer.lease.p9-crashed.tmp").write_bytes(b"half a record")

    device.reset()
    coordinator.acquire_writer_lease(timeout=1.0)
    released = [name for operation, name in device.touched if operation == "recycle"]
    assert released, "the sweep did run"
    for name in released:
        assert name.endswith(".tmp"), f"the sweep released {name!r}, which is not a temporary"
    assert (Path(lock_directory) / "writer.lease.lock").exists()


# --- C04 and C08: the errno table decides when there is no winerror ---------------------------


def test_an_errno_only_transient_failure_is_ridden_out(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # Every other transient test carries a winerror, so on Windows the errno table is never the
    # term that decides. A POSIX device answers with an errno and nothing else.
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    device.failure = OSError(errno.EAGAIN, "Resource temporarily unavailable.")
    assert getattr(device.failure, "winerror", None) is None
    device.fail_next["atomic_replace"] = 2
    assert coordinator.acquire_writer_lease(timeout=1.0).epoch == 1


def test_an_errno_only_permanent_failure_is_not_retried(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    device.failure = OSError(errno.EROFS, "Read-only file system.")
    device.fail_next["atomic_replace"] = 10_000
    with pytest.raises(GrafxStorageError) as failure:
        coordinator.acquire_writer_lease(timeout=1.0)
    assert device.fail_next["atomic_replace"] == 9_999, "a permanent errno was retried"
    assert failure.value.retryable is False
    assert failure.value.details["retryable"] is False


# --- E25: the instance nonce is the term under test --------------------------------------------


def test_two_coordinators_with_one_owner_name_refuse_each_other_registrations(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """The nonce exists for exactly this case and was never the deciding term.

    Same owner name, same counter: the prefix differs only by the per-instance nonce, so nothing
    else can tell these two registrations apart. What stands on it is a live reader's snapshot
    pin (BR-10).
    """
    device = DirectoryStorageDevice(database_root)
    first = make_coordinator(owner_id="p1-same", storage=device)
    second = make_coordinator(owner_id="p1-same", storage=device, monotonic_origin=60.0)
    theirs = first.register_reader(5)
    mine = second.register_reader(9_000)
    assert theirs.reader_id != mine.reader_id
    assert theirs.reader_id.endswith("-r0001") and mine.reader_id.endswith("-r0001")

    with pytest.raises(GrafxUnsupportedOperation):
        second.unregister_reader(theirs)
    with pytest.raises(GrafxUnsupportedOperation):
        second.refresh_reader(ReaderHandle(reader_id=theirs.reader_id, snapshot_lsn=1))
    assert first.reader_horizon() == 5


# --- E33: the idempotent acquisition compares the epoch too ------------------------------------


def test_a_superseded_lease_is_never_handed_back_as_granted(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """The fast path answers "you already hold this" and must mean the epoch, not just the name.

    Without the epoch term a participant is handed its own stale Lease as a fresh grant, and it
    then believes it may write under an epoch the control plane has moved past.
    """
    coordinator = make_coordinator(owner_id="p1-aaaa")
    held = coordinator.acquire_writer_lease(timeout=1.0)
    lease_file = database_root / "control" / "writer.lease"
    published = decode_lease_record(lease_file.read_bytes())
    from okto_grafx.adapters.coordination_local import LeaseRecord

    lease_file.write_bytes(
        encode_lease_record(
            LeaseRecord(
                owner_id=published.owner_id,
                epoch=published.epoch + 4,
                heartbeat_seq=published.heartbeat_seq,
                ttl_seconds=published.ttl_seconds,
                wall_stamp=published.wall_stamp,
                held=True,
                superseded_epoch=published.epoch,
            )
        )
    )
    with pytest.raises(GrafxLeaseTimeout):
        coordinator.acquire_writer_lease(timeout=0.0)
    assert held.epoch == 1


# --- F09: the active flag is an invariant, not dead code ---------------------------------------


def test_a_registration_published_as_inactive_is_not_a_live_pin(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """Nothing in this build publishes an inactive registration, and the branch still matters.

    The flag is part of a record format C6 and C13 read, and a future writer or an older one may
    set it. Honouring an inactive registration as a live pin would hold the horizon for a reader
    that has declared itself finished, so the decoder branch is tested rather than removed.
    """
    coordinator = make_coordinator(owner_id="p1-aaaa")
    live = coordinator.register_reader(500)
    readers = database_root / "control" / "readers"
    retired = readers / "p9-other-r0001.reader"
    retired.write_bytes(
        encode_reader_record(
            ReaderRecord(
                reader_id="p9-other-r0001",
                snapshot_lsn=7,
                heartbeat_seq=1,
                wall_stamp=0.0,
                active=False,
            )
        )
    )
    assert coordinator.reader_horizon() == 500, "an inactive registration pinned the horizon"
    assert not retired.exists()
    assert live.snapshot_lsn == 500


# --- F15: a section that times out leaves no descriptor behind ---------------------------------


def test_a_section_timeout_closes_the_handle_it_opened(
    make_coordinator: CoordinatorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open handle is what keeps a file undeletable on Windows and what holds the lock.

    Leaking one per timed-out acquisition is invisible until a long-running writer runs out of
    descriptors, which is why the guard exists and why it needs a test of its own.
    """
    import os

    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def watched_open(path: object, flags: int, mode: int = 0o777, *, dir_fd: object = None) -> int:
        handle = real_open(path, flags, mode, dir_fd=dir_fd)
        if str(path).endswith(".lock"):
            opened.append(handle)
        return handle

    def watched_close(handle: int) -> None:
        closed.append(handle)
        real_close(handle)

    holder = make_coordinator(owner_id="p1-aaaa")
    contender = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0, poll_interval=0.25)
    with holder.exclusive("commit", timeout=1.0):
        monkeypatch.setattr(os, "open", watched_open)
        monkeypatch.setattr(os, "close", watched_close)
        try:
            with pytest.raises(GrafxLeaseTimeout):
                with contender.exclusive("commit", timeout=0.5):
                    pass
        finally:
            monkeypatch.undo()
    assert opened, "the contender did open the lock file"
    assert set(opened) <= set(closed), f"leaked handles: {set(opened) - set(closed)}"


def test_a_configuration_refusal_is_still_a_configuration_error(
    make_coordinator: CoordinatorFactory
) -> None:
    # The classification arms must not swallow the constructor's own refusals.
    with pytest.raises(GrafxConfigurationError):
        make_coordinator(owner_id="p1-aaaa", control_directory="..")


# --- the terms A74 masked: only a repaired or restored record can exercise them ----------------


def _republish(database_root: Path, **changes: object) -> None:
    """Rewrite the lease record with some fields changed, as a repair or a restore would."""
    from okto_grafx.adapters.coordination_local import LeaseRecord

    lease_file = database_root / "control" / "writer.lease"
    current = decode_lease_record(lease_file.read_bytes())
    fields: dict[str, object] = {
        "owner_id": current.owner_id,
        "epoch": current.epoch,
        "heartbeat_seq": current.heartbeat_seq,
        "ttl_seconds": current.ttl_seconds,
        "wall_stamp": current.wall_stamp,
        "held": current.held,
        "superseded_epoch": current.superseded_epoch,
    }
    fields.update(changes)
    lease_file.write_bytes(encode_lease_record(LeaseRecord(**fields)))  # type: ignore[arg-type]


def test_a_vacated_record_refuses_the_participant_that_still_thinks_it_holds(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """Holding a lease locally is not enough: the record has to still say the lease is held.

    Asking whether this participant holds the lease answers most of these cases, and it masked
    this one. Only the record can say that the lease was given up out of band -- an operator
    repair, or a restore of a file captured while it was vacant -- and a writer that trusted its
    own memory would commit under an epoch nobody holds.
    """
    coordinator = make_coordinator(owner_id="p1-holder")
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    coordinator.validate_epoch(lease.epoch)

    _republish(database_root, held=False)
    with pytest.raises(GrafxStaleEpoch) as refusal:
        coordinator.validate_epoch(lease.epoch)
    assert refusal.value.details["published_epoch"] == lease.epoch


def test_a_record_naming_another_participant_refuses_us_at_our_own_epoch(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """The epoch matching is not the same as the lease being ours.

    Ownership normally changes only with the epoch, which is what hid this term. A repaired or
    restored record can keep the epoch and change the name, and then two participants each hold
    a local belief about the same number -- the situation BR-7 exists to make impossible.
    """
    coordinator = make_coordinator(owner_id="p1-holder")
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    coordinator.validate_epoch(lease.epoch)

    _republish(database_root, owner_id="p9-repair")
    with pytest.raises(GrafxStaleEpoch) as refusal:
        coordinator.validate_epoch(lease.epoch)
    assert refusal.value.details["published_owner"] == "p9-repair"


# --- round four: the identity that reaches the disk, and the guard behind the holder check -----


def test_two_coordinators_sharing_an_owner_name_are_never_both_admitted(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """A configured owner name is a caller's string; the identity on disk must not be.

    The reader registry was hardened against a shared name because a collision there costs a
    snapshot pin. The lease is where the harm is BR-7 itself: with one name on both records, each
    participant answers to the other's, and an out-of-band deletion lets both install the same
    epoch and both be told yes. The instance nonce is what makes the record say which of them.
    """
    lease_file = database_root / "control" / "writer.lease"
    first = make_coordinator(owner_id="p1-same")
    second = make_coordinator(owner_id="p1-same", monotonic_origin=70.0)
    assert second.current_epoch() == 0  # B observes the absence, so its basis is "no record"

    early = first.acquire_writer_lease(timeout=1.0)
    first.validate_epoch(early.epoch)
    lease_file.unlink()  # an operator, a scanner, or a restore removes it

    late = second.acquire_writer_lease(timeout=1.0)
    assert late.epoch == early.epoch, "the demonstration needs both to land on one epoch"
    assert late.owner_id != early.owner_id, "the identities must still differ"

    admitted = []
    for label, coordinator, lease in (("first", first, early), ("second", second, late)):
        try:
            coordinator.validate_epoch(lease.epoch)
            admitted.append(label)
        except GrafxStaleEpoch:
            pass
    assert admitted == ["second"], f"both participants were authorised: {admitted}"


def test_a_restarted_process_cannot_renew_a_lease_it_did_not_install(
    make_coordinator: CoordinatorFactory
) -> None:
    # The same property from the other side: a fresh coordinator with the SAME configured name is
    # a different participant, and the lease it finds on disk is not its own to renew or release.
    first = make_coordinator(owner_id="p1-same")
    lease = first.acquire_writer_lease(timeout=1.0)
    restarted = make_coordinator(owner_id="p1-same", monotonic_origin=70.0)
    for act in (restarted.renew_lease, restarted.release_lease):
        with pytest.raises(GrafxLeaseStolen):
            act(lease)
    first.validate_epoch(lease.epoch)


def test_a_deleted_lease_file_refuses_the_participant_that_still_holds_one(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """Holding a lease locally is not the same as an epoch being published.

    A74 gave the validation a holder check, and that check answers first in every scenario the
    suite had -- which left the guard behind it, the one that refuses when nothing is published
    at all, with no scenario of its own. Without it a participant whose lease file was removed
    out of band is still told it may write.
    """
    coordinator = make_coordinator(owner_id="p1-holder")
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    coordinator.validate_epoch(lease.epoch)

    (database_root / "control" / "writer.lease").unlink()
    with pytest.raises(GrafxStaleEpoch) as refusal:
        coordinator.validate_epoch(lease.epoch)
    assert refusal.value.retryable is False
    assert coordinator.current_epoch() == 0


# --- the classification tables, both pinned on both families ----------------------------------


def _with_winerror(number: int, code: int = errno.EACCES) -> OSError:
    """Return an OSError carrying a Windows error number, buildable on either family.

    Setting the attribute rather than using the four-argument constructor is what lets the
    Windows table be pinned from POSIX too: the classifier reads ``winerror`` when it is there,
    so the branch is reachable wherever the tests run (D9, A30).
    """
    failure = OSError(code, "injected")
    failure.winerror = number  # type: ignore[attr-defined]
    return failure


@pytest.mark.parametrize(
    ("failure", "expected", "why"),
    [
        (_with_winerror(5), True, "access denied is what a scanner produces"),
        (_with_winerror(32), True, "sharing violation"),
        (_with_winerror(33), True, "lock violation"),
        (_with_winerror(2), False, "the file is not there and will not appear by waiting"),
        (_with_winerror(3), False, "the path is not there"),
        (_with_winerror(19), False, "write protected"),
        (_with_winerror(87), False, "an argument the platform refuses"),
        (OSError(errno.EACCES, "injected"), True, "errno side, transient"),
        (OSError(errno.EAGAIN, "injected"), True, "errno side, transient"),
        (OSError(errno.EROFS, "injected"), False, "errno side, permanent"),
        (OSError(errno.ENOENT, "injected"), False, "errno side, permanent"),
    ],
)
def test_the_transience_tables_are_pinned_on_both_sides(
    failure: OSError, expected: bool, why: str
) -> None:
    """Whichever term decides, it is the one this row is about.

    On Windows the classifier answers from the winerror whenever one is present, so the errno
    table is never the deciding term there and was pinned only by the POSIX rows. Both tables are
    now stated directly, and widening either one turns these rows red on either family.
    """
    from okto_grafx.adapters.coordination_local import _is_transient

    assert _is_transient(failure) is expected, why


def test_a_lock_file_that_the_platform_refuses_by_winerror_is_permanent(
    make_coordinator: CoordinatorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The counterpart to the errno-only case: the same refusal carried the way this family
    # carries it. The previous test of this branch built its error with two arguments, so it had
    # no winerror at all and measured the other table.
    import os

    real_open = os.open

    def refuse(path: object, flags: int, mode: int = 0o777, *, dir_fd: object = None) -> int:
        if str(path).endswith(".lock"):
            raise _with_winerror(3, errno.ENOENT)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    coordinator = make_coordinator(owner_id="p1-aaaa")
    monkeypatch.setattr(os, "open", refuse)
    with pytest.raises(GrafxStorageError) as failure:
        with coordinator.exclusive("commit", timeout=0.1):
            pass
    monkeypatch.undo()
    assert failure.value.retryable is False
    assert failure.value.details["winerror"] == 3


# --- the identifier suffix guard keeps a raw ValueError out of the public surface --------------


@pytest.mark.parametrize("suffix", ["abc", "", "12a", "-1"])
def test_a_reader_handle_with_a_non_numeric_counter_is_refused_typed(
    make_coordinator: CoordinatorFactory, suffix: str
) -> None:
    """Only Grafx errors leave this component (section 11 item 5).

    The identifier validator accepts letters, so a handle whose counter is not a number reaches
    the ownership check and would otherwise be handed to int().
    """
    coordinator = make_coordinator(owner_id="p1-aaaa")
    real = coordinator.register_reader(5)
    prefix = real.reader_id[: -len("0001")]
    forged = ReaderHandle(reader_id=f"{prefix}{suffix}" or prefix, snapshot_lsn=5)
    for act in (coordinator.unregister_reader, coordinator.refresh_reader):
        with pytest.raises(GrafxUnsupportedOperation):
            act(forged)
    assert coordinator.reader_horizon() == 5


def test_a_short_read_names_the_straddle_it_saw(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """The short-read check reaches the same verdict as the envelope check, and says more.

    Disabling it changes no outcome -- the decoder refuses the same bytes for the same reason --
    so it is kept for its message rather than for its verdict, and the message is what is pinned
    here. A person reading a race report needs the two lengths, which the envelope check cannot
    give: it only knows what the record claims, not what the device returned.
    """
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = make_coordinator(owner_id="p1-aaaa", storage=device)
    coordinator.acquire_writer_lease(timeout=1.0)
    device.short_read_next = 10_000
    with pytest.raises(GrafxCorruptionDetected) as failure:
        coordinator.current_epoch()
    assert "fewer bytes" in failure.value.message
    assert failure.value.details["declared"] == failure.value.details["actual"] + 1
    assert failure.value.details["file"].endswith("writer.lease")


# --- what the composed identity masked: the record can name us at an epoch we never installed --


def _advance_the_record(database_root: Path, coordinator: object, epoch: int) -> "object":
    """Rewrite the lease record to a different epoch, still naming this participant.

    A restore from an older or newer capture does exactly this, and so does an operator repair.
    It is the only shape in which the record agrees with us about WHO and disagrees about WHICH
    -- which is what makes it the only shape that can reach the guards behind the identity check.
    """
    from okto_grafx.adapters.coordination_local import LeaseRecord
    from okto_grafx.domain.ports.coordination import Lease

    lease_file = database_root / "control" / "writer.lease"
    current = decode_lease_record(lease_file.read_bytes())
    lease_file.write_bytes(
        encode_lease_record(
            LeaseRecord(
                owner_id=current.owner_id,
                epoch=epoch,
                heartbeat_seq=current.heartbeat_seq,
                ttl_seconds=current.ttl_seconds,
                wall_stamp=current.wall_stamp,
                held=True,
                superseded_epoch=current.epoch,
            )
        )
    )
    return Lease(
        owner_id=coordinator.owner_id(),  # type: ignore[attr-defined]
        epoch=epoch,
        acquired_monotonic=0.0,
        heartbeat_seq=current.heartbeat_seq,
        ttl_seconds=current.ttl_seconds,
    )


def test_validation_refuses_an_epoch_this_participant_never_installed(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """The record naming us is not the same as us having installed this epoch.

    Composing the identity with an instance nonce answers most of these cases, and it masked
    this one: here the name on the record IS ours. Only the local record of what this coordinator
    installed can say that the epoch moved without us.
    """
    coordinator = make_coordinator(owner_id="p1-holder")
    held = coordinator.acquire_writer_lease(timeout=1.0)
    coordinator.validate_epoch(held.epoch)

    _advance_the_record(database_root, coordinator, held.epoch + 1)
    with pytest.raises(GrafxStaleEpoch) as refusal:
        coordinator.validate_epoch(held.epoch + 1)
    assert refusal.value.details["held_epoch"] == held.epoch


def test_renewal_refuses_a_lease_this_participant_never_installed(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # Without this the participant forges a heartbeat for an epoch it never took, which is the
    # forgery FR-7 and AC-7 rest on: one such renewal resets every observer's stall baseline.
    coordinator = make_coordinator(owner_id="p1-holder")
    held = coordinator.acquire_writer_lease(timeout=1.0)
    fabricated = _advance_the_record(database_root, coordinator, held.epoch + 1)

    with pytest.raises(GrafxLeaseStolen) as refusal:
        coordinator.renew_lease(fabricated)  # type: ignore[arg-type]
    assert refusal.value.details["held_epoch"] == held.epoch
    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert published.heartbeat_seq == held.heartbeat_seq, "a heartbeat was forged"


def test_release_refuses_a_lease_this_participant_never_installed(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # And without this it vacates an epoch it never took, handing the writer role to whoever
    # asks next with no stall to wait out.
    coordinator = make_coordinator(owner_id="p1-holder")
    held = coordinator.acquire_writer_lease(timeout=1.0)
    fabricated = _advance_the_record(database_root, coordinator, held.epoch + 1)

    with pytest.raises(GrafxLeaseStolen) as refusal:
        coordinator.release_lease(fabricated)  # type: ignore[arg-type]
    assert refusal.value.details["last_installed_epoch"] == held.epoch
    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert published.held is True, "an epoch this participant never took was vacated"


def test_a_stolen_lease_stays_stolen_even_if_the_record_comes_back(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """Learning that the lease was taken is remembered, not re-derived from the record.

    A renewal that finds the lease gone clears what this participant believes it holds. Every
    other refusal on that path comes from the published record, so the two are indistinguishable
    until the record itself comes back -- a restore from a capture taken before the takeover.
    Then only the memory of having been stolen from can refuse, and a participant that resumed on
    a reverted record would be writing under an epoch another participant had already replaced.
    """
    lease_file = database_root / "control" / "writer.lease"
    first = make_coordinator(owner_id="p1-first")
    held = first.acquire_writer_lease(timeout=1.0)
    backup = lease_file.read_bytes()

    clock = ManualClock(monotonic=4_000.0)
    second = make_coordinator(owner_id="p2-second", clock=clock)
    assert second.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert second.detect_dead_owner(stall_threshold=5.0) is not None
    second.takeover()

    with pytest.raises(GrafxLeaseStolen):
        first.renew_lease(held)

    lease_file.write_bytes(backup)  # the control plane is restored to before the takeover
    with pytest.raises(GrafxStaleEpoch) as refusal:
        first.validate_epoch(held.epoch)
    assert refusal.value.details["held_epoch"] is None
