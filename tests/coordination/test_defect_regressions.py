"""Regressions for the defects the blind critic proved (M1, M2, M3 and the minors).

Every test here failed before its fix. They are kept together because they share one theme: a
guarantee this component states in a docstring must be true in every configuration it offers, not
only in the one its happy path exercises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice, HookStorageDevice, ManualClock, owned_by
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator, decode_lease_record
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxLeaseStolen,
    GrafxLeaseTimeout,
    GrafxStaleEpoch,
    GrafxStorageError,
)
from okto_grafx.domain.ports.coordination import Lease

# --- M1: the lockless mode must exclude across coordinator instances --------------------------


def _dead_owner(make_coordinator: CoordinatorFactory, *, locks: bool) -> None:
    """Publish a lease and stop renewing it, the way a killed process does."""
    zombie = make_coordinator(
        owner_id="p0-dead", monotonic_origin=500.0, use_lock_directory=locks
    )
    zombie.acquire_writer_lease(timeout=1.0)


def _observer(
    make_coordinator: CoordinatorFactory,
    *,
    owner_id: str,
    origin: float,
    locks: bool,
    storage: object | None = None,
) -> LocalProcessCoordinator:
    """Build a participant that has watched the owner heartbeat stall on its own clock."""
    clock = ManualClock(monotonic=origin)
    coordinator = make_coordinator(
        owner_id=owner_id, clock=clock, storage=storage, use_lock_directory=locks
    )
    assert coordinator.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert coordinator.detect_dead_owner(stall_threshold=5.0) is not None
    return coordinator


@pytest.mark.parametrize("hook_at", range(1, 13))
def test_the_lockless_mode_still_admits_exactly_one_winner(
    hook_at: int, make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # A13 selects lock_directory=None for the in-memory device. A section that is only local to
    # one coordinator object lets two of them publish the same epoch, which is the window AC-7
    # exists to close.
    _dead_owner(make_coordinator, locks=False)
    # One device object, because that is what "one in-memory database" means: the lockless mode
    # is only ever valid when the participants share the namespace inside one process.
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    first = _observer(
        make_coordinator, owner_id="p1-first", origin=1_000.0, locks=False, storage=device
    )
    second = _observer(
        make_coordinator, owner_id="p2-second", origin=9_000.0, locks=False, storage=device
    )

    outcomes: dict[str, object] = {}

    def contend(_operation: str, _index: int) -> None:
        try:
            outcomes["second"] = second.takeover()
        except (GrafxLeaseStolen, GrafxLeaseTimeout) as failure:
            outcomes["second"] = failure

    device.reset()
    device.hook = contend
    device.hook_at = hook_at
    try:
        outcomes["first"] = first.takeover()
    except (GrafxLeaseStolen, GrafxLeaseTimeout) as failure:
        outcomes["first"] = failure
    if "second" not in outcomes:
        contend("after", 0)

    winners = [name for name, result in outcomes.items() if isinstance(result, Lease)]
    assert len(winners) == 1, f"hook {hook_at}: winners were {winners}"
    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert published.epoch == 2
    winner = outcomes[winners[0]]
    assert isinstance(winner, Lease)
    assert published.owner_id == winner.owner_id
    holder = first if winners[0] == "first" else second
    loser = second if winners[0] == "first" else first
    holder.validate_epoch(2)
    # The loser holds nothing: it cannot renew the epoch it lost, and the epoch it started from
    # is refused before any byte of its own could move.
    with pytest.raises(GrafxLeaseStolen):
        loser.renew_lease(winner)
    with pytest.raises(GrafxStaleEpoch):
        loser.validate_epoch(1)


def test_two_lockless_coordinators_over_one_namespace_contend(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = DirectoryStorageDevice(database_root)
    first = make_coordinator(owner_id="p1-aaaa", storage=device, use_lock_directory=False)
    second = make_coordinator(
        owner_id="p2-bbbb", storage=device, use_lock_directory=False, monotonic_origin=50.0
    )
    with first.exclusive("commit", timeout=1.0):
        with pytest.raises(GrafxLeaseTimeout):
            with second.exclusive("commit", timeout=0.2):
                pytest.fail("the section was granted twice at once")
    with second.exclusive("commit", timeout=0.2):
        pass


def test_two_lockless_coordinators_over_different_namespaces_do_not_contend(
    make_coordinator: CoordinatorFactory, tmp_path: Path
) -> None:
    # Two in-memory databases are two databases: they must not serialise against each other.
    first = make_coordinator(
        owner_id="p1-aaaa",
        storage=DirectoryStorageDevice(tmp_path / "one"),
        use_lock_directory=False,
    )
    second = make_coordinator(
        owner_id="p2-bbbb",
        storage=DirectoryStorageDevice(tmp_path / "two"),
        use_lock_directory=False,
        monotonic_origin=50.0,
    )
    with first.exclusive("commit", timeout=1.0):
        with second.exclusive("commit", timeout=0.2):
            pass


# --- M2: renewal and release belong to the participant, not to the argument --------------------


def test_renewing_a_lease_this_participant_never_held_is_refused(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    owner = make_coordinator(owner_id="p1-owner")
    lease = owner.acquire_writer_lease(timeout=1.0)
    stranger = make_coordinator(owner_id="p2-stranger", monotonic_origin=40.0)
    with pytest.raises(GrafxLeaseStolen):
        stranger.renew_lease(lease)
    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert owned_by(published.owner_id, "p1-owner")
    assert published.heartbeat_seq == lease.heartbeat_seq, "the heartbeat was forged"


def test_a_stranger_cannot_keep_a_dead_owner_alive(make_coordinator: CoordinatorFactory) -> None:
    # A forged renewal resets the stall baseline of every observer, so a dead owner could be kept
    # alive forever by a participant that never held anything (FR-7, AC-7).
    owner = make_coordinator(owner_id="p1-owner")
    lease = owner.acquire_writer_lease(timeout=1.0)
    observer_clock = ManualClock(monotonic=3_000.0)
    observer = make_coordinator(owner_id="p3-observer", clock=observer_clock)
    stranger = make_coordinator(owner_id="p2-stranger", monotonic_origin=40.0)
    assert observer.detect_dead_owner(stall_threshold=5.0) is None
    observer_clock.advance(6.0)
    assert observer.detect_dead_owner(stall_threshold=5.0) is not None
    for _attempt in range(3):
        with pytest.raises(GrafxLeaseStolen):
            stranger.renew_lease(lease)
        observer_clock.advance(6.0)
        assert observer.detect_dead_owner(stall_threshold=5.0) is not None


def test_releasing_a_lease_of_another_participant_is_refused(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    owner = make_coordinator(owner_id="p1-owner")
    lease = owner.acquire_writer_lease(timeout=1.0)
    stranger = make_coordinator(owner_id="p2-stranger", monotonic_origin=40.0)
    with pytest.raises(GrafxLeaseStolen):
        stranger.release_lease(lease)
    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert published.held is True
    assert owned_by(published.owner_id, "p1-owner")


def test_renewing_after_this_participant_released_is_refused(
    make_coordinator: CoordinatorFactory
) -> None:
    owner = make_coordinator(owner_id="p1-owner")
    lease = owner.acquire_writer_lease(timeout=1.0)
    owner.release_lease(lease)
    with pytest.raises(GrafxLeaseStolen):
        owner.renew_lease(lease)


# --- M3: the lock directory error path is typed and reachable ---------------------------------


def test_a_lock_directory_that_is_a_file_is_refused_with_a_typed_error(tmp_path: Path) -> None:
    occupied = tmp_path / "control"
    occupied.write_text("not a directory", encoding="ascii")
    with pytest.raises(GrafxConfigurationError) as failure:
        LocalProcessCoordinator(
            DirectoryStorageDevice(tmp_path / "db"),
            ManualClock(),
            owner_id="p1-aaaa",
            lock_directory=str(occupied),
        )
    assert "control" in str(failure.value.details["lock_directory"])


def test_a_lock_directory_with_a_nul_byte_is_refused_with_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(GrafxConfigurationError):
        LocalProcessCoordinator(
            DirectoryStorageDevice(tmp_path / "db"),
            ManualClock(),
            owner_id="p1-aaaa",
            lock_directory=str(tmp_path / "co\x00ntrol"),
        )


def test_a_lock_directory_that_cannot_be_created_is_a_storage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    def refuse(path: object, mode: int = 0o777, exist_ok: bool = False) -> None:
        raise PermissionError(13, "Access is denied.")

    monkeypatch.setattr(os, "makedirs", refuse)
    with pytest.raises(GrafxStorageError) as failure:
        LocalProcessCoordinator(
            DirectoryStorageDevice(tmp_path / "db"),
            ManualClock(),
            owner_id="p1-aaaa",
            lock_directory=str(tmp_path / "control"),
        )
    monkeypatch.undo()
    assert failure.value.details["attempts"] >= 1
    assert failure.value.details["errno"] == 13


def test_a_control_directory_cannot_escape_the_database(
    make_coordinator: CoordinatorFactory
) -> None:
    for value in ("..", ".", "../elsewhere", "control/../.."):
        with pytest.raises(GrafxConfigurationError):
            make_coordinator(owner_id="p1-aaaa", control_directory=value)
