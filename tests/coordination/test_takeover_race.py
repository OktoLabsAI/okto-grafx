"""Takeover is atomic and the epoch increments exactly once (AC-7, TS-7, BR-7).

The scenario is the one the spec describes: an owner dies without cleanup and two participants
race to replace it. The race is driven deterministically rather than by threads: the device of
the first participant fires a hook at a chosen call, and the whole attempt of the second
participant runs at that exact point. Sweeping the hook over every call of the protocol, for
every seeded combination of contender behaviour, enumerates the interleavings that matter.

What must hold in every one of them: exactly one winner, exactly one epoch increment, the loser
told that the lease moved, and no moment at which two holders share an epoch.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice, HookStorageDevice, ManualClock
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator, decode_lease_record
from okto_grafx.domain.errors import GrafxLeaseStolen, GrafxLeaseTimeout, GrafxStaleEpoch

PERMUTATIONS: int = 200
"""How many seeded scenarios the sweep runs. Every one of them is reproducible from its seed."""

MAX_HOOK_POINT: int = 16
"""Upper bound on the device calls of one takeover; beyond it the hook simply never fires."""


class Witness:
    """Reads the published record straight from disk and keeps every distinct state it saw."""

    def __init__(self, root: Path) -> None:
        self._path = root / "control" / "writer.lease"
        self.history: list[tuple[str, int, bool]] = []

    def look(self) -> None:
        """Sample the published lease record, if there is one."""
        if not self._path.exists():
            return
        raw = self._path.read_bytes()
        if not raw:
            return
        record = decode_lease_record(raw)
        state = (record.owner_id, record.epoch, record.held)
        if not self.history or self.history[-1] != state:
            self.history.append(state)

    def holders_per_epoch(self) -> dict[int, set[str]]:
        """Return, for every epoch, the set of owners ever seen holding it."""
        holders: dict[int, set[str]] = {}
        for owner, epoch, held in self.history:
            if held:
                holders.setdefault(epoch, set()).add(owner)
        return holders


def _stalled_participant(
    make_coordinator: CoordinatorFactory,
    *,
    owner_id: str,
    origin: float,
    storage: object | None = None,
    observe: bool = True,
) -> tuple[LocalProcessCoordinator, ManualClock]:
    """Build a participant that has already watched the owner heartbeat stall on its own clock."""
    clock = ManualClock(monotonic=origin)
    coordinator = make_coordinator(owner_id=owner_id, clock=clock, storage=storage)
    if observe:
        assert coordinator.detect_dead_owner(stall_threshold=5.0) is None
        clock.advance(6.0)
        assert coordinator.detect_dead_owner(stall_threshold=5.0) is not None
    return coordinator, clock


@pytest.mark.parametrize("seed", range(PERMUTATIONS))
def test_two_participants_racing_over_a_dead_owner_produce_one_winner(
    seed: int, make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    choices = random.Random(seed)
    hook_at = choices.randrange(1, MAX_HOOK_POINT + 1)
    contender_takes_over = choices.random() < 0.5
    first_pre_observes = choices.random() < 0.75
    witness = Witness(database_root)

    zombie = make_coordinator(owner_id="p0-dead", monotonic_origin=500.0)
    zombie_lease = zombie.acquire_writer_lease(timeout=1.0)
    witness.look()

    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    first, _first_clock = _stalled_participant(
        make_coordinator,
        owner_id="p1-first",
        origin=1_000.0,
        storage=device,
        observe=first_pre_observes,
    )
    second, _second_clock = _stalled_participant(
        make_coordinator, owner_id="p2-second", origin=9_000_000.0
    )

    outcomes: dict[str, object] = {}

    def contend(_operation: str, _index: int) -> None:
        witness.look()
        try:
            if contender_takes_over:
                outcomes["second"] = second.takeover()
            else:
                outcomes["second"] = second.acquire_writer_lease(timeout=1.0)
        except (GrafxLeaseStolen, GrafxLeaseTimeout) as failure:
            outcomes["second"] = failure
        witness.look()

    device.reset()
    device.hook = contend
    device.hook_at = hook_at
    try:
        outcomes["first"] = first.takeover()
    except (GrafxLeaseStolen, GrafxLeaseTimeout) as failure:
        outcomes["first"] = failure
    witness.look()

    if "second" not in outcomes:
        # The hook point sat beyond the protocol; the contender still runs, just afterwards.
        contend("after", 0)
        witness.look()

    winners = {name for name, result in outcomes.items() if not isinstance(result, Exception)}
    assert len(winners) == 1, f"seed {seed}: winners were {winners}"
    winner_name = winners.pop()
    winner = outcomes[winner_name]
    loser = outcomes["first" if winner_name == "second" else "second"]

    assert winner.epoch == 2, f"seed {seed}: the epoch moved by more than one step"
    assert isinstance(loser, (GrafxLeaseStolen, GrafxLeaseTimeout))

    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert published.epoch == 2
    assert published.held is True
    assert published.owner_id == winner.owner_id
    assert published.superseded_epoch == 1

    holders = witness.holders_per_epoch()
    assert holders[1] == {"p0-dead"}, f"seed {seed}: epoch 1 had holders {holders[1]}"
    assert holders[2] == {winner.owner_id}, f"seed {seed}: epoch 2 had holders {holders[2]}"
    assert set(holders) == {1, 2}

    # The zombie, and the loser of the race, are refused before any byte of theirs can move.
    with pytest.raises(GrafxStaleEpoch):
        zombie.validate_epoch(zombie_lease.epoch)
    winner_coordinator = first if winner_name == "first" else second
    loser_coordinator = second if winner_name == "first" else first
    winner_coordinator.validate_epoch(2)
    with pytest.raises(GrafxStaleEpoch):
        loser_coordinator.validate_epoch(1)


def test_the_sweep_actually_reaches_inside_the_critical_section(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # A sweep whose hook never fires would pass for the wrong reason. This pins the fact that the
    # protocol really does perform a read-modify-write of several device calls.
    zombie = make_coordinator(owner_id="p0-dead", monotonic_origin=500.0)
    zombie.acquire_writer_lease(timeout=1.0)
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    first, _clock = _stalled_participant(
        make_coordinator, owner_id="p1-first", origin=1_000.0, storage=device
    )
    fired: list[str] = []
    device.reset()
    device.hook = lambda operation, _index: fired.append(operation)
    device.hook_at = 1
    first.takeover()
    assert fired == ["exists"]
    assert len(device.calls) >= 8
    assert "atomic_replace" in device.calls
    assert device.calls.index("read_log") < device.calls.index("atomic_replace")


def test_a_takeover_of_a_lease_that_already_moved_is_refused(
    make_coordinator: CoordinatorFactory
) -> None:
    zombie = make_coordinator(owner_id="p0-dead", monotonic_origin=500.0)
    zombie.acquire_writer_lease(timeout=1.0)
    first, _first_clock = _stalled_participant(
        make_coordinator, owner_id="p1-first", origin=1_000.0
    )
    second, _second_clock = _stalled_participant(
        make_coordinator, owner_id="p2-second", origin=2_000.0
    )
    assert first.takeover().epoch == 2
    with pytest.raises(GrafxLeaseStolen):
        second.takeover()
    assert second.current_epoch() == 2


def test_a_second_takeover_from_the_same_participant_needs_a_fresh_observation(
    make_coordinator: CoordinatorFactory
) -> None:
    # Taking over twice in a row from one stale observation would move the epoch without any new
    # evidence, which is exactly the double increment AC-7 forbids.
    zombie = make_coordinator(owner_id="p0-dead", monotonic_origin=500.0)
    zombie.acquire_writer_lease(timeout=1.0)
    first, clock = _stalled_participant(make_coordinator, owner_id="p1-first", origin=1_000.0)
    assert first.takeover().epoch == 2
    second, _clock = _stalled_participant(
        make_coordinator, owner_id="p2-second", origin=3_000.0, observe=False
    )
    assert second.detect_dead_owner(stall_threshold=5.0) is None
    with pytest.raises(GrafxLeaseTimeout) as refusal:
        second.takeover()
    assert refusal.value.retryable is True

    # Only after the new owner is itself observed to have stalled does a takeover become legal.
    _clock_of(second).advance(6.0)
    assert second.detect_dead_owner(stall_threshold=5.0) is not None
    assert second.takeover().epoch == 3
    with pytest.raises(GrafxStaleEpoch):
        first.validate_epoch(2)
    clock.advance(0.0)


def _clock_of(coordinator: LocalProcessCoordinator) -> ManualClock:
    """Return the fake clock injected into a coordinator, so a test can move it."""
    return coordinator._clock  # noqa: SLF001 - driving the injected fake is the point


def test_takeover_from_an_empty_directory_starts_at_epoch_one(
    make_coordinator: CoordinatorFactory
) -> None:
    coordinator = make_coordinator(owner_id="p1-first")
    lease = coordinator.takeover()
    assert lease.epoch == 1
    assert coordinator.current_epoch() == 1
    other = make_coordinator(owner_id="p2-second", monotonic_origin=10.0)
    other.detect_dead_owner(stall_threshold=5.0)
    with pytest.raises(GrafxLeaseTimeout):
        other.takeover()
