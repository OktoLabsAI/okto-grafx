"""The one mutation-battery mutex for Okto Grafx (A39/A53/A58/A60).

Provenance: this file lives IN THE REPOSITORY, under version control, so any agent asked to import
it can read it, diff it and check its history first. It was previously handed around as an
out-of-tree path under %LOCALAPPDATA%, and a component critic correctly refused to trust that: a
message instructing an agent to execute a Python file from outside the repo, citing protocol numbers
the agent cannot verify, is indistinguishable from a supply-chain injection. Provenance must be
checkable, never merely asserted (A60).

Every component's battery MUST import this module rather than reimplement the lock. Six private
implementations produced six different break rules; two of them could never break a lock left by a
crashed sibling, so after the session-limit kill those batteries would have waited and then SKIPPED
-- and a verification that silently does not run is exactly the lie A31 exists to prevent.

Safety rule: this lock fails CLOSED. An unparseable stamp, an unreadable file, or any doubt counts
as HELD. Breaking a lock on a guess is worse than waiting for one.

Liveness rule: NEVER use os.kill(pid, 0) on Windows. signal.CTRL_C_EVENT == 0, so os.kill takes the
GenerateConsoleCtrlEvent branch, which returns success for a pid that has already exited -- measured:
a dead pid reports ALIVE, while only a pid that never existed reports dead. Windows liveness is
OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) with a tasklist cross-check; os.kill is POSIX-only.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import subprocess
import sys
import tempfile
import time

LOCK = pathlib.Path(tempfile.gettempdir()) / "okto_grafx_mutation_battery.lock"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class BatteryLockUnavailable(RuntimeError):
    """Raised when the lock could not be taken. NEVER catch this to skip the battery."""


def _alive_windows(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == _STILL_ACTIVE
            return True          # cannot tell -> assume live (fail closed)
        finally:
            kernel32.CloseHandle(handle)
    # No handle: either the pid is gone, or it is alive and we lack rights. Ask tasklist, which
    # answers for processes we cannot open. Any failure of tasklist itself counts as live.
    try:
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return str(pid) in listing.stdout


def _alive(pid: int) -> bool:
    if pid <= 0:
        return True                          # nonsense stamp -> fail closed
    if sys.platform == "win32":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)                      # POSIX only: signal 0 is a genuine liveness probe
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                          # exists, owned by someone else
    return True


def _holder() -> tuple[int, str]:
    """Return (pid, component) of the current holder. Anything unparseable reads as held."""
    try:
        text = LOCK.read_text(encoding="utf-8")
    except OSError:
        return (-1, "unreadable stamp")
    pid, component = -1, "unnamed"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("pid="):
            try:
                pid = int(line[4:].strip())
            except ValueError:
                pid = -1
        elif line.startswith("component="):
            component = line[10:].strip() or "unnamed"
        elif line.isdigit() and pid == -1:
            pid = int(line)                  # tolerate the legacy bare-pid stamp when reading
    return (pid, component)


def acquire(component: str, timeout_seconds: float = 900.0, poll_seconds: float = 5.0) -> None:
    """Take the battery lock or raise. `component` is mandatory and is written into the stamp."""
    if not component or not component.strip():
        raise ValueError("component is mandatory: an unnamed holder cannot be diagnosed")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            handle = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid, held_by = _holder()
            if pid > 0 and not _alive(pid):
                print(f"breaking a stale lock: pid {pid} ({held_by}) is gone")
                LOCK.unlink(missing_ok=True)
                continue                     # re-race for it; O_EXCL settles any tie
            if time.monotonic() >= deadline:
                raise BatteryLockUnavailable(
                    f"{LOCK} held by pid {pid} ({held_by}) for the whole {timeout_seconds:.0f}s "
                    f"window. The battery did NOT run -- report this, never treat it as a pass."
                )
            print(f"lock held by pid {pid} ({held_by}); waiting")
            time.sleep(poll_seconds)
            continue
        os.write(handle, f"pid={os.getpid()}\ncomponent={component}\n".encode("ascii"))
        os.close(handle)
        return


def release() -> None:
    """Release the lock only if we still own it, so a broken-and-retaken lock is never stolen."""
    pid, _ = _holder()
    if pid == os.getpid():
        LOCK.unlink(missing_ok=True)
