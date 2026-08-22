"""Two real processes over one database directory (FR-7, AC-7, TR-3).

Everything else in this suite drives interleavings inside one interpreter, which is the only way
to make them exhaustive and reproducible. These tests answer the question that technique cannot:
does the mechanism actually reach across a process boundary. They use the ``spawn`` start method
on both families, so a child shares nothing with the parent but the file system -- no memory, no
locks, and above all no clock.
"""

from __future__ import annotations

import multiprocessing
import sys
import time
from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import ManualClock, owned_by
from okto_grafx.adapters.coordination_local import decode_lease_record
from okto_grafx.domain.errors import GrafxLeaseTimeout, GrafxStaleEpoch

HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:  # the spawned child inherits sys.path and imports the helper by name
    sys.path.insert(0, HERE)

import coordination_child  # noqa: E402  - the path above has to exist before the import

CHILD_START_BUDGET: float = 30.0
"""How long the parent waits for a spawned interpreter to reach its marker."""

LOCK_WAIT: float = 0.03
"""How long the parent tries to take a section the child holds, measured on its injected clock.

The refusal is real -- the child holds a real operating-system lock in another process -- while
the waiting is not, so the test proves the exclusion without spending the time.
"""


def _wait_for(path: Path, budget: float = CHILD_START_BUDGET) -> bool:
    """Wait for a marker file to appear, polling rather than sleeping a fixed amount."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.001)
    return False


@pytest.mark.multiprocess
def test_a_section_held_by_another_process_is_not_granted_here(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    ready = database_root / "child.ready"
    release = database_root / "parent.release"
    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=coordination_child.hold_section,
        args=(str(database_root), str(ready), str(release)),
        name="grafx-section-holder",
    )
    child.start()
    try:
        assert _wait_for(ready), "the child never reported that it holds the section"
        parent = make_coordinator(owner_id="parent-aaaa", clock=None, poll_interval=0.005)
        with pytest.raises(GrafxLeaseTimeout):
            with parent.exclusive("commit", timeout=LOCK_WAIT):
                pytest.fail("the section was granted while another process held it")
    finally:
        release.write_text("go", encoding="ascii")
        child.join(timeout=CHILD_START_BUDGET)
    assert child.exitcode == 0
    # With the child gone, the kernel has dropped its lock and the section is free again.
    with parent.exclusive("commit", timeout=LOCK_WAIT):
        pass


@pytest.mark.multiprocess
def test_a_lease_left_by_a_dead_process_is_taken_over_at_the_next_epoch(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # The child takes the lease and leaves without any cleanup, which is what a kill -9 looks
    # like from here: a durable lease record whose heartbeat will never move again.
    done = database_root / "child.done"
    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=coordination_child.acquire_and_die,
        args=(str(database_root), str(done)),
        name="grafx-zombie",
    )
    child.start()
    assert _wait_for(done), "the child never reported that it holds the lease"
    child.join(timeout=CHILD_START_BUDGET)
    assert done.read_text(encoding="ascii") == "1"

    clock = ManualClock(monotonic=1_000.0)
    survivor = make_coordinator(owner_id="parent-aaaa", clock=clock)
    assert survivor.current_epoch() == 1
    with pytest.raises(GrafxStaleEpoch):
        survivor.validate_epoch(2)

    # Liveness is measured here, on this clock, without waiting for anything real.
    assert survivor.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    report = survivor.detect_dead_owner(stall_threshold=5.0)
    assert report is not None
    assert owned_by(report.owner_id, "child-zombie")
    assert report.observed_stall_seconds == pytest.approx(6.0)

    lease = survivor.takeover()
    assert lease.epoch == 2
    assert owned_by(lease.owner_id, "parent-aaaa")
    survivor.validate_epoch(2)
    with pytest.raises(GrafxStaleEpoch):
        survivor.validate_epoch(1)


@pytest.mark.multiprocess
def test_the_child_really_ran_in_another_interpreter(database_root: Path) -> None:
    # A multiprocess test that silently degraded to running in this process would prove nothing,
    # so the spawn start method and the separate process identity are asserted directly.
    context = multiprocessing.get_context("spawn")
    assert context.get_start_method() == "spawn"
    done = database_root / "child.done"
    child = context.Process(
        target=coordination_child.acquire_and_die,
        args=(str(database_root), str(done)),
        name="grafx-zombie",
    )
    child.start()
    child_pid = child.pid
    assert _wait_for(done)
    child.join(timeout=CHILD_START_BUDGET)
    assert child_pid is not None
    import os

    assert child_pid != os.getpid()
    lease_file = database_root / "control" / "writer.lease"
    assert lease_file.exists()
    assert b"child-zombie" in lease_file.read_bytes()


@pytest.mark.multiprocess
def test_four_real_processes_racing_over_a_dead_owner_produce_one_winner(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # The seeded sweep enumerates interleavings inside one interpreter; this answers the question
    # that technique cannot, with four operating-system schedulers deciding the order (AC-7).
    #
    # Nothing below lets a clock decide an outcome. Each child reports only what the protocol
    # told it, the parent opens the gate only once every child is at the line, and every
    # assertion carries the whole state of the race so a failure says which of the two possible
    # stories happened: two winners at one epoch (a real race) or a child that never ran.
    stall = 0.05
    zombie = make_coordinator(
        owner_id="p0-dead", owner_stall_threshold=stall, ttl_seconds=stall
    )
    zombie.acquire_writer_lease(timeout=1.0)

    context = multiprocessing.get_context("spawn")
    gate = database_root / "go.marker"
    names = [f"child-{index}" for index in range(4)]
    children = []
    for name in names:
        child = context.Process(
            target=coordination_child.race_for_the_lease,
            args=(
                str(database_root),
                name,
                str(database_root / f"{name}.ready"),
                str(gate),
                str(database_root / f"{name}.result"),
                stall,
            ),
            name=f"grafx-{name}",
        )
        child.start()
        children.append(child)

    def outcome_of(name: str) -> str:
        result = database_root / f"{name}.result"
        return result.read_text(encoding="ascii") if result.exists() else "<no result>"

    def state() -> str:
        record = "<absent>"
        lease_file = database_root / "control" / "writer.lease"
        if lease_file.exists() and lease_file.stat().st_size:
            record = repr(decode_lease_record(lease_file.read_bytes()))
        lines = [f"published: {record}"]
        for name, child in zip(names, children):
            lines.append(f"{name}: {outcome_of(name)!r} exitcode={child.exitcode}")
        return chr(10).join(lines)

    try:
        for name in names:
            assert _wait_for(
                database_root / f"{name}.ready"
            ), f"{name} never started: {state()}"
        gate.write_text("go", encoding="ascii")
        for name in names:
            assert _wait_for(
                database_root / f"{name}.result"
            ), f"{name} never reported an outcome: {state()}"
    finally:
        # The winner holds and renews until this marker appears, so the race stays one epoch
        # transition rather than a chain of correct handovers between dying processes.
        (database_root / "go.marker.done").write_text("done", encoding="ascii")
        for child in children:
            child.join(timeout=CHILD_START_BUDGET)

    report = state()
    outcomes = [outcome_of(name) for name in names]
    assert all(child.exitcode == 0 for child in children), report
    winners = [outcome for outcome in outcomes if outcome.startswith("won")]
    epochs = [outcome.split()[1] for outcome in winners]
    # Two winners are not one story but two, and the difference is the whole invariant. Sharing
    # an epoch means two holders were authorised at once, which is the AC-7 window and a defect
    # in the engine. Successive epochs mean a correct handover: the first winner stopped proving
    # it was alive and the next contender was right to replace it, which is a defect in a test
    # that let its winner walk away. The verdict is stated here so a failure never needs a guess.
    verdict = (
        "two holders shared an epoch (AC-7 window, defect in the coordinator)"
        if len(epochs) != len(set(epochs))
        else "successive epochs, a correct handover (defect in this harness)"
    )
    assert len(winners) == 1, f"the epoch was granted {len(winners)} times -- {verdict}: {report}"
    assert winners[0].split()[1] == "2", f"the epoch moved by more than one step: {report}"

    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert published.epoch == 2, report
    assert published.held is True, report
    assert published.owner_id == winners[0].split()[2], report
    assert published.superseded_epoch == 1, report
    assert sum(1 for outcome in outcomes if outcome.startswith("lost")) == 3, report


@pytest.mark.multiprocess
def test_a_concurrent_reader_of_the_lease_file_obstructs_and_then_does_not(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """A real reader in a real process, holding the handle until this test says otherwise.

    What this proves that an injected failure cannot: the obstruction is REAL on Windows, where
    a handle opened without FILE_SHARE_DELETE makes ``os.replace`` over that name fail. Both
    branches assert something, so neither family passes vacuously -- on POSIX a rename over an
    open file is unobstructed, and that is the finding there.

    Nothing here is timed. The reader holds until told, so the obstructed publish is certain
    rather than hoped for; that the retry rides out a TRANSIENT obstruction is proved
    deterministically elsewhere, with an injected one-shot failure and a fake clock.
    """
    import os

    from coordination_support import DirectoryStorageDevice, HookStorageDevice
    from okto_grafx.adapters.clock_system import SystemClock
    from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
    from okto_grafx.domain.errors import GrafxStorageError

    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    coordinator = LocalProcessCoordinator(
        device,
        SystemClock(),
        owner_id="parent-writer",
        lock_directory=str(database_root / "control"),
        poll_interval=0.005,
    )
    lease = coordinator.acquire_writer_lease(timeout=5.0)

    ready = database_root / "reader.open"
    release = database_root / "reader.release"
    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=coordination_child.hold_the_lease_file_open,
        args=(str(database_root), str(ready), str(release)),
        name="grafx-lease-reader",
    )
    child.start()
    obstruction: BaseException | None = None
    try:
        assert _wait_for(ready), "the child never opened the lease file"
        try:
            lease = coordinator.renew_lease(lease)
        except GrafxStorageError as failure:
            obstruction = failure
    finally:
        release.write_text("go", encoding="ascii")
        child.join(timeout=CHILD_START_BUDGET)
    assert child.exitcode == 0

    if os.name == "nt":
        assert obstruction is not None, "an open handle did not obstruct the replacement"
        assert obstruction.details["attempts"] >= 2, "the publish gave up without retrying"
        assert obstruction.retryable is True
        assert obstruction.details["winerror"] in {5, 32, 33}
    else:
        assert obstruction is None, "a rename over an open file was obstructed on POSIX"

    # With the reader gone the same publication goes through, on both families.
    renewed = coordinator.renew_lease(lease)
    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert owned_by(published.owner_id, "parent-writer")
    assert published.heartbeat_seq == renewed.heartbeat_seq
