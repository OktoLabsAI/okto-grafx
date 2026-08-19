"""Two real processes over one database directory (FR-7, AC-7, TR-3).

Everything else in this suite drives interleavings inside one interpreter, which is the only way
to make them exhaustive and reproducible. These two tests answer the question that technique
cannot: does the mechanism actually reach across a process boundary. They use the ``spawn`` start
method on both families, so the child shares nothing but the file system.
"""

from __future__ import annotations

import multiprocessing
import sys
import time
from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import ManualClock
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
    assert report.owner_id == "child-zombie"
    assert report.observed_stall_seconds == pytest.approx(6.0)

    lease = survivor.takeover()
    assert lease.epoch == 2
    assert lease.owner_id == "parent-aaaa"
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
