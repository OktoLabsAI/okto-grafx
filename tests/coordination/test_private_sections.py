"""Sections proved private to one coordinator instance (W-01).

``_declare_private_section`` is the optional, private-performance capability that lets a section
whose name carries this instance's own ``owner_id`` digest be serialised with a process lock
instead of an operating-system file lock. The claim it rests on is narrow and is tested here as
three separate obligations:

* the declaration is REFUSED for every name that is not this instance's digest, so no
  cross-process section can be downgraded by a caller that merely asks nicely;
* a declared section still serialises the THREADS of its coordinator, which is the entire reason
  the participant section exists (txn_manager ``_participant_section``);
* the control directory keeps the same lock file, because forensic and census tooling compares
  that inventory and a performance decision must not change what a directory looks like.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice
from okto_grafx.adapters import coordination_local
from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.coordination_local import (
    LOCK_FILE_SUFFIX,
    LocalProcessCoordinator,
)
from okto_grafx.domain.errors import GrafxLeaseTimeout
from okto_grafx.domain.page.checksum import crc32c

PARTICIPANT_PREFIX: str = "txn-"
"""The engine's participant prefix, repeated here so the test does not depend on the engine."""


def _participant_name(coordinator: object) -> str:
    """Return the section name the engine would build for this coordinator."""
    owner: str = coordinator.owner_id()  # type: ignore[attr-defined]
    return f"{PARTICIPANT_PREFIX}{crc32c(owner.encode('utf-8')):08x}"


def _count_os_locks(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count the platform lock primitive at the PORT, not at any call site."""
    counts = {"acquire": 0, "release": 0, "open_lock_file": 0}
    acquire = coordination_local._acquire_os_lock
    release = coordination_local._release_os_lock
    opener = coordination_local.LocalProcessCoordinator._open_lock_file

    def counted_acquire(handle: int) -> None:
        counts["acquire"] += 1
        acquire(handle)

    def counted_release(handle: int) -> None:
        counts["release"] += 1
        release(handle)

    def counted_open(self: object, path: str, name: str, deadline: float) -> int:
        counts["open_lock_file"] += 1
        return opener(self, path, name, deadline)

    monkeypatch.setattr(coordination_local, "_acquire_os_lock", counted_acquire)
    monkeypatch.setattr(coordination_local, "_release_os_lock", counted_release)
    monkeypatch.setattr(
        coordination_local.LocalProcessCoordinator, "_open_lock_file", counted_open
    )
    return counts


def test_only_this_instances_own_digest_can_be_declared_private(
    make_coordinator: CoordinatorFactory,
) -> None:
    """Every cross-process section name is refused, whoever asks and however it is spelled."""
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    declare = type(first).__dict__["_declare_private_section"]

    assert declare(first, _participant_name(first)) is True
    for refused in (
        "commit",
        "writer.lease",
        "first-open",
        "page0-1dec8160",
        _participant_name(second),
        f"{PARTICIPANT_PREFIX}00000000",
    ):
        assert declare(first, refused) is False, refused
    assert set(first._private_section_locks) == {_participant_name(first)}


def test_the_same_configured_owner_name_still_yields_two_private_sections(
    make_coordinator: CoordinatorFactory,
) -> None:
    """The instance nonce, not the configured name, is what the digest is taken over."""
    first = make_coordinator(owner_id="same-name")
    second = make_coordinator(owner_id="same-name", monotonic_origin=50.0)
    assert first.owner_id() != second.owner_id()
    assert _participant_name(first) != _participant_name(second)

    declare = type(first).__dict__["_declare_private_section"]
    assert declare(first, _participant_name(first)) is True
    assert declare(second, _participant_name(second)) is True
    # Neither may declare the other's, and holding one leaves the other free.
    assert declare(first, _participant_name(second)) is False
    with first.exclusive(_participant_name(first), timeout=1.0):
        with second.exclusive(_participant_name(second), timeout=0.2):
            pass


def test_a_declared_section_still_serialises_two_threads_of_one_coordinator(
    database_root: Path, lock_directory: str
) -> None:
    """The threads half of FR-3: overlap is impossible and the loser really waits.

    This is the discriminating test for W-01. Removing the in-process serialisation (returning a
    fresh lock per entry, or not locking at all) makes ``overlaps`` non-zero.

    The real clock and the real sleeper are used here on purpose: the suite's manual clock makes
    a waiter exhaust its budget at once, which is the right answer for a timeout test and the
    wrong one for a test about a waiter that must actually wait.
    """
    coordinator = LocalProcessCoordinator(
        DirectoryStorageDevice(database_root),
        SystemClock(),
        owner_id="p1-aaaa",
        lock_directory=lock_directory,
        poll_interval=0.001,
        sleeper=time.sleep,
    )
    name = _participant_name(coordinator)
    assert type(coordinator).__dict__["_declare_private_section"](coordinator, name) is True

    inside = 0
    overlaps = 0
    order: list[str] = []
    guard = threading.Lock()
    first_in = threading.Event()
    may_finish = threading.Event()

    def worker(label: str) -> None:
        nonlocal inside, overlaps
        with coordinator.exclusive(name, timeout=30.0):
            with guard:
                inside += 1
                if inside > 1:
                    overlaps += 1
                order.append(f"enter:{label}")
            if label == "a":
                first_in.set()
                may_finish.wait(timeout=30.0)
            with guard:
                order.append(f"exit:{label}")
                inside -= 1

    a = threading.Thread(target=worker, args=("a",))
    b = threading.Thread(target=worker, args=("b",))
    a.start()
    assert first_in.wait(timeout=30.0)
    b.start()
    # B is now blocked on the section A holds: it cannot have entered.
    b.join(timeout=0.2)
    assert b.is_alive() is True
    with guard:
        assert order == ["enter:a"]
    may_finish.set()
    a.join(timeout=30.0)
    b.join(timeout=30.0)

    assert overlaps == 0
    assert order == ["enter:a", "exit:a", "enter:b", "exit:b"]


def test_a_declared_section_stops_taking_operating_system_locks_but_keeps_its_lock_file(
    make_coordinator: CoordinatorFactory,
    lock_directory: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counters at the port: the file is created exactly once and never locked again."""
    coordinator = make_coordinator(owner_id="p1-aaaa")
    name = _participant_name(coordinator)
    counts = _count_os_locks(monkeypatch)

    assert type(coordinator).__dict__["_declare_private_section"](coordinator, name) is True
    assert counts == {"acquire": 0, "release": 0, "open_lock_file": 1}

    lock_file = Path(lock_directory) / f"{name}{LOCK_FILE_SUFFIX}"
    assert lock_file.is_file()
    assert lock_file.stat().st_size == 0

    for _ in range(5):
        with coordinator.exclusive(name, timeout=1.0):
            pass
    assert counts == {"acquire": 0, "release": 0, "open_lock_file": 1}

    # A section that was NOT declared keeps the operating-system lock, in the same coordinator.
    with coordinator.exclusive("commit", timeout=1.0):
        pass
    assert counts["acquire"] == 1
    assert counts["release"] == 1
    assert counts["open_lock_file"] == 2


def test_a_declared_section_keeps_the_timeout_and_re_entry_contract(
    make_coordinator: CoordinatorFactory,
) -> None:
    """Re-entry from the same thread is free; another thread is refused with the typed timeout."""
    coordinator = make_coordinator(owner_id="p1-aaaa", poll_interval=0.001)
    name = _participant_name(coordinator)
    assert type(coordinator).__dict__["_declare_private_section"](coordinator, name) is True

    failure: list[BaseException] = []

    def loser() -> None:
        try:
            with coordinator.exclusive(name, timeout=0.05):
                pytest.fail("the section was granted twice at once")
        except BaseException as raised:  # noqa: BLE001 - recorded and asserted below
            failure.append(raised)

    with coordinator.exclusive(name, timeout=1.0):
        with coordinator.exclusive(name, timeout=1.0):
            thread = threading.Thread(target=loser)
            thread.start()
            thread.join(timeout=30.0)
    assert len(failure) == 1
    assert isinstance(failure[0], GrafxLeaseTimeout)
    assert failure[0].retryable is True
    assert failure[0].details["section"] == name

    # Released on the way out, including for the thread that lost.
    with coordinator.exclusive(name, timeout=0.2):
        pass


def test_a_declaration_is_refused_while_the_section_is_held(
    make_coordinator: CoordinatorFactory,
) -> None:
    """The mechanism behind a live section never changes underneath its holder."""
    coordinator = make_coordinator(owner_id="p1-aaaa")
    name = _participant_name(coordinator)
    declare = type(coordinator).__dict__["_declare_private_section"]
    with coordinator.exclusive(name, timeout=1.0):
        assert declare(coordinator, name) is False
    assert declare(coordinator, name) is True


def test_a_coordinator_without_a_lock_directory_declares_nothing(
    make_coordinator: CoordinatorFactory,
) -> None:
    """The memory mode is already process-local; there is nothing to convert and no file."""
    coordinator = make_coordinator(owner_id="p1-aaaa", use_lock_directory=False)
    name = _participant_name(coordinator)
    assert type(coordinator).__dict__["_declare_private_section"](coordinator, name) is False
    assert coordinator._private_section_locks == {}
    with coordinator.exclusive(name, timeout=1.0):
        pass


def test_a_declared_section_parks_no_descriptor_scope(
    make_coordinator: CoordinatorFactory,
) -> None:
    """Nothing is opened per entry, so the TXN-1 reuse capability has nothing to offer."""
    coordinator = make_coordinator(owner_id="p1-aaaa")
    name = _participant_name(coordinator)
    revalidated = type(coordinator).__dict__[
        "_reuse_revalidated_unlocked_section_descriptor"
    ]
    assert revalidated(coordinator, name) is not None

    assert type(coordinator).__dict__["_declare_private_section"](coordinator, name) is True
    assert revalidated(coordinator, name) is None
    with coordinator.reuse_unlocked_section_descriptor(name):
        pass
    assert coordinator._descriptor_scopes == {}

    # A section that was not declared still offers the scope.
    assert revalidated(coordinator, "commit") is not None


def test_the_declared_lock_file_is_the_one_the_engine_would_have_created(
    make_coordinator: CoordinatorFactory, lock_directory: str
) -> None:
    """The inventory a census compares is byte-identical to the undeclared path's."""
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    undeclared = _participant_name(second)
    with second.exclusive(undeclared, timeout=1.0):
        pass
    undeclared_file = Path(lock_directory) / f"{undeclared}{LOCK_FILE_SUFFIX}"

    declared = _participant_name(first)
    assert type(first).__dict__["_declare_private_section"](first, declared) is True
    declared_file = Path(lock_directory) / f"{declared}{LOCK_FILE_SUFFIX}"

    assert declared_file.is_file()
    assert declared_file.stat().st_size == undeclared_file.stat().st_size == 0
    assert oct(declared_file.stat().st_mode) == oct(undeclared_file.stat().st_mode)
    assert os.path.islink(declared_file) is False
