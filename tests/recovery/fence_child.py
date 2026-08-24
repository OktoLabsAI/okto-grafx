"""Child process that holds the commit section until told to let go (M0C fence battery).

Run as a script: ``python fence_child.py <root> <ready-file> <release-file>``. It builds the
PRODUCTION coordinator over the real directory, enters ``COMMIT_SECTION``, announces itself by
writing the ready file, and leaves only when the release file appears (or a safety budget ends,
so a wedged parent fails loudly instead of hanging the run).

The ``okto_grafx`` imports live INSIDE the function, after the ``src`` of THIS worktree is put
first on ``sys.path``: a bare interpreter would otherwise resolve the package from wherever an
editable install points, which is not necessarily the tree under test.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

SAFETY_BUDGET: float = 60.0
"""Last-resort bound so a genuine deadlock is reported rather than waited on for ever."""


def hold_commit_section(
    root: str, ready: str, release: str, section: str | None = None
) -> None:
    """Take a section over ``root`` (the commit section by default), announce it by
    writing the ready file, and hold it until the release file appears."""
    from okto_grafx.adapters.clock_system import SystemClock
    from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
    from okto_grafx.adapters.storage_local import LocalStorageDevice
    from okto_grafx.engine.coordination import COMMIT_SECTION

    coordinator = LocalProcessCoordinator(
        LocalStorageDevice(root),
        SystemClock(),
        owner_id="fence-child",
        lock_directory=str(Path(root) / "control"),
        poll_interval=0.002,
        ttl_seconds=SAFETY_BUDGET,
        owner_stall_threshold=SAFETY_BUDGET,
    )
    with coordinator.exclusive(section or COMMIT_SECTION, timeout=10.0):
        Path(ready).write_text("held", encoding="ascii")
        deadline = time.monotonic() + SAFETY_BUDGET
        while not Path(release).exists() and time.monotonic() < deadline:
            time.sleep(0.002)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    hold_commit_section(
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
        sys.argv[4] if len(sys.argv) > 4 else None,
    )
