"""Entry points for the children spawned by the multiprocess tests.

This module is imported by a fresh interpreter under the ``spawn`` start method, so it may not
assume anything about the parent beyond ``sys.path``, which multiprocessing does carry over. The
two paths it needs are inserted defensively anyway.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
for _entry in (str(HERE), str(PROJECT_ROOT / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

__all__ = [
    "acquire_and_die",
    "build_coordinator",
    "hold_section",
    "hold_the_lease_file_open",
    "race_for_the_lease",
]

POLL_SECONDS: float = 0.002
"""How often a child looks for the marker file that tells it to move on."""

SAFETY_BUDGET: float = 120.0
"""Last-resort bound on any wait, so a wedged child fails loudly instead of hanging a run.

It is three orders of magnitude above the time these steps take, so it never decides an outcome:
it exists only so that a genuine deadlock is reported rather than waited on for ever.
"""


def build_coordinator(root: str, owner_id: str, stall: float = 5.0) -> object:
    """Build a coordinator over the database directory, with the production clock and locks."""
    from coordination_support import DirectoryStorageDevice

    from okto_grafx.adapters.clock_system import SystemClock
    from okto_grafx.adapters.coordination_local import LocalProcessCoordinator

    return LocalProcessCoordinator(
        DirectoryStorageDevice(root),
        SystemClock(),
        owner_id=owner_id,
        lock_directory=str(Path(root) / "control"),
        poll_interval=POLL_SECONDS,
        ttl_seconds=stall,
        owner_stall_threshold=stall,
    )


def _wait_for_marker(marker: str, budget: float) -> bool:
    """Poll for a marker file, so every child leaves the gate at the same moment."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if Path(marker).exists():
            return True
        time.sleep(0.001)
    return False


def race_for_the_lease(
    root: str, owner_id: str, ready: str, go: str, result: str, stall: float = 0.05
) -> None:
    """Watch the dead owner stall, then take over the instant the gate opens, and report back.

    Every outcome here is decided by evidence, never by a stopwatch. A contender leaves the loop
    when it has taken the lease, when it has been told the lease moved, or when it can see that
    the epoch it was racing for is already gone. A loaded machine therefore makes this slower and
    never makes it report something different, which is the only way a test of AC-7 is worth
    anything: a flaky guard on the one invariant the engine is built around trains everybody to
    re-run instead of to look.
    """
    from okto_grafx.domain.errors import GrafxError

    coordinator = build_coordinator(root, owner_id, stall=stall)
    starting_epoch = coordinator.current_epoch()  # type: ignore[attr-defined]
    coordinator.detect_dead_owner(stall_threshold=stall)  # type: ignore[attr-defined]
    Path(ready).write_text("ready", encoding="ascii")
    _wait_for_marker(go, budget=SAFETY_BUDGET)

    lease = None
    outcome = ""
    attempts = 0
    guard = time.monotonic() + SAFETY_BUDGET
    while not outcome:
        attempts += 1
        if coordinator.current_epoch() > starting_epoch:  # type: ignore[attr-defined]
            # Somebody else already replaced the owner this contender was racing for. There is
            # nothing left to take over, and saying so needs no clock.
            outcome = "lost superseded epoch_moved"
            break
        if coordinator.detect_dead_owner(stall_threshold=stall) is None:  # type: ignore[attr-defined]
            continue
        try:
            lease = coordinator.takeover()  # type: ignore[attr-defined]
            outcome = f"won {lease.epoch} {lease.owner_id}"
        except GrafxError as failure:
            if failure.retryable:
                # A busy section or a sharing violation is not an answer about who won, and a
                # real caller retries it. Looping keeps the outcome decided by the protocol.
                if time.monotonic() > guard:
                    outcome = f"lost {type(failure).__name__} {failure.code}"
                continue
            outcome = f"lost {type(failure).__name__} {failure.code}"
    Path(result).write_text(f"{outcome} attempts={attempts}", encoding="ascii")
    if lease is None:
        return
    # The winner stays alive and keeps its heartbeat moving until the parent closes the race.
    # A winner that walked away would be legitimately dead within one stall threshold, and the
    # next contender would take over correctly -- a second winner at a LATER epoch, which says
    # nothing about the window this test exists to examine.
    done = f"{go}.done"
    deadline = time.monotonic() + SAFETY_BUDGET
    while not Path(done).exists() and time.monotonic() < deadline:
        try:
            lease = coordinator.renew_lease(lease)  # type: ignore[attr-defined]
        except GrafxError:
            # The race is already recorded; a failure to keep the heartbeat moving afterwards is
            # not this test subject and must not turn into a non-zero exit code.
            return
        time.sleep(stall / 8.0)


def hold_the_lease_file_open(root: str, ready: str, release: str) -> None:
    """Open the lease file the way any ordinary reader does, and hold it until told to let go.

    On Windows a handle opened without FILE_SHARE_DELETE makes os.replace over that name fail
    with a sharing violation, which is exactly the transient condition the publish path has to
    ride out. On POSIX the same code is harmless, and the contract is the same on both.

    The handle is held until the parent says otherwise rather than for a fixed interval: a timed
    hold makes the overlap a hope, and on a loaded machine the parent finishes its renewals after
    the file is already closed, so the test asserts a race that did not happen.
    """
    target = Path(root) / "control" / "writer.lease"
    with target.open("rb") as handle:
        handle.read()
        Path(ready).write_text("open", encoding="ascii")
        _wait_for_marker(release, budget=SAFETY_BUDGET)


def hold_section(root: str, ready: str, release: str, budget: float = 20.0) -> None:
    """Take the commit section, announce it, and hold it until the parent says to let go."""
    coordinator = build_coordinator(root, "child-holder")
    with coordinator.exclusive("commit", timeout=5.0):  # type: ignore[attr-defined]
        Path(ready).write_text("held", encoding="ascii")
        deadline = time.monotonic() + budget
        while not Path(release).exists() and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)


def acquire_and_die(root: str, done: str) -> None:
    """Take the writer lease and leave the process without releasing anything, as a kill would."""
    coordinator = build_coordinator(root, "child-zombie")
    lease = coordinator.acquire_writer_lease(timeout=5.0)  # type: ignore[attr-defined]
    Path(done).write_text(str(lease.epoch), encoding="ascii")
    sys.stdout.flush()
    os._exit(0)
