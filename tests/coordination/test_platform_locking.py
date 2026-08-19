"""Both operating-system families are present, and each is exercised on its own (G4, TR-3).

Every family-specific assertion below is marked ``platform_specific`` and has a counterpart for
the other family, so the pair states the same contract twice and the CI matrix covers both. The
family-agnostic tests at the end read the source itself, so the branch that cannot run here is
still checked for the mistakes that would make it unsound.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from okto_grafx.adapters import coordination_local
from okto_grafx.adapters.coordination_local import LOCK_MECHANISM, PLATFORM_FAMILY
from okto_grafx.domain.errors import GrafxLeaseTimeout

SOURCE: str = Path(coordination_local.__file__).read_text(encoding="ascii")

WINDOWS: bool = os.name == "nt"


@pytest.mark.platform_specific
@pytest.mark.skipif(not WINDOWS, reason="The Windows branch runs on Windows; POSIX has a counterpart.")
def test_the_windows_branch_locks_with_msvcrt() -> None:
    assert PLATFORM_FAMILY == "windows"
    assert LOCK_MECHANISM == "msvcrt.locking"
    assert "msvcrt" in sys.modules
    assert coordination_local._IS_WINDOWS is True


@pytest.mark.platform_specific
@pytest.mark.skipif(WINDOWS, reason="The POSIX branch runs on POSIX; Windows has a counterpart.")
def test_the_posix_branch_locks_with_flock() -> None:
    assert PLATFORM_FAMILY == "posix"
    assert LOCK_MECHANISM == "fcntl.flock"
    assert "fcntl" in sys.modules
    assert coordination_local._IS_WINDOWS is False


@pytest.mark.platform_specific
@pytest.mark.skipif(not WINDOWS, reason="The Windows branch runs on Windows; POSIX has a counterpart.")
def test_a_second_windows_handle_of_this_process_is_excluded(
    make_coordinator: CoordinatorFactory
) -> None:
    # On Windows a byte-range lock is refused to a second handle even inside the locking process,
    # which is what makes two coordinators in one process contend exactly as two processes do.
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=5.0)
    with first.exclusive("commit", timeout=1.0):
        with pytest.raises(GrafxLeaseTimeout):
            with second.exclusive("commit", timeout=0.2):
                pass


@pytest.mark.platform_specific
@pytest.mark.skipif(WINDOWS, reason="The POSIX branch runs on POSIX; Windows has a counterpart.")
def test_a_second_posix_open_file_description_is_excluded(
    make_coordinator: CoordinatorFactory
) -> None:
    # flock is owned by the open file description, so two separate opens in one process contend.
    # fcntl.lockf would not: its locks belong to the process, and the second acquire would
    # silently succeed. That is why this module uses flock.
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=5.0)
    with first.exclusive("commit", timeout=1.0):
        with pytest.raises(GrafxLeaseTimeout):
            with second.exclusive("commit", timeout=0.2):
                pass


def test_exactly_one_family_is_selected() -> None:
    assert PLATFORM_FAMILY in {"windows", "posix"}
    assert (PLATFORM_FAMILY == "windows") is WINDOWS
    assert LOCK_MECHANISM == ("msvcrt.locking" if WINDOWS else "fcntl.flock")


def test_both_branches_exist_in_the_source() -> None:
    # The branch that cannot run on this machine still has to be there, and has to be the right
    # one: a family-specific test is only allowed with a counterpart (G4).
    assert "import msvcrt" in SOURCE
    assert "import fcntl" in SOURCE
    assert "msvcrt.locking(handle, msvcrt.LK_NBLCK" in SOURCE
    assert "fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)" in SOURCE
    assert "msvcrt.locking(handle, msvcrt.LK_UNLCK" in SOURCE
    assert "fcntl.flock(handle, fcntl.LOCK_UN)" in SOURCE


def test_the_posix_branch_never_reaches_for_the_per_process_lock() -> None:
    # fcntl.lockf and fcntl.fcntl(F_SETLK) place POSIX record locks, which belong to the process
    # rather than to the handle and are dropped by closing ANY descriptor of the same file. Using
    # them here would make two coordinators in one process invisible to each other.
    assert "lockf" not in SOURCE
    assert "F_SETLK" not in SOURCE


def test_the_windows_branch_never_blocks_the_host(make_coordinator: CoordinatorFactory) -> None:
    # LK_LOCK retries for a second per attempt inside the C runtime, which would put the wait
    # outside the injected clock and outside the timeout the caller asked for.
    assert "LK_LOCK" not in SOURCE
    assert "LK_NBLCK" in SOURCE
    coordinator = make_coordinator(owner_id="p1-aaaa")
    with coordinator.exclusive("commit", timeout=0.0):
        pass


def test_no_path_separator_is_assumed_in_the_storage_namespace() -> None:
    # Names inside the device namespace are slash-separated on both families; the only place a
    # native path is built is the advisory lock directory.
    assert "os.path.join" in SOURCE
    assert SOURCE.count("os.path.join") == 1
    assert "\\\\" not in SOURCE.replace("\\\\n", "")


def test_the_lock_handle_is_opened_in_binary_mode() -> None:
    # Omitting O_BINARY selects the text mode of the C runtime on Windows, whose commit truncates
    # a file at a trailing 0x1A byte.
    assert "_BINARY_FLAG" in SOURCE
    assert 'getattr(os, "O_BINARY", 0)' in SOURCE
    assert "os.O_RDWR | os.O_CREAT | _BINARY_FLAG" in SOURCE
