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

__all__ = ["acquire_and_die", "build_coordinator", "hold_section"]

POLL_SECONDS: float = 0.002
"""How often a child looks for the marker file that tells it to move on."""


def build_coordinator(root: str, owner_id: str) -> object:
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
    )


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
