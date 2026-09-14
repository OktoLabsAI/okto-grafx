"""Sections proved private to one coordinator instance (W-01).

``_declare_private_section`` is the optional, private-performance capability that lets a section
whose name carries this instance's own ``owner_id`` digest be serialised with a process lock
instead of an operating-system file lock. The claim it rests on is narrow and is tested here as
three separate obligations:

* the declaration is REFUSED for every name that is not EXACTLY this instance's participant
  name, so no cross-process section can be downgraded by a caller that merely asks nicely -- and
  not by one that pads a spelling until it ends in the right eight hex characters either;
* a declared section still serialises the THREADS of its coordinator, which is the entire reason
  the participant section exists (txn_manager ``_participant_section``);
* a declaration and an entry that race are ordered: the mechanism behind a live section never
  changes underneath its holder, which is a fence rather than a check;
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

_RACE_ROUNDS: int = 16
"""How many times the declaration/entry race is replayed.

The race is staged rather than sampled, so one round already discriminates; the repetition is
there because a scheduler that parks a thread at the wrong moment must not be able to turn a
real defect into an intermittent green.
"""

_RACE_TIMEOUT: float = 30.0
"""Ceiling on every wait of the race, so a wedged round fails the suite instead of hanging it."""


def _digest(coordinator: object) -> str:
    """Return the eight hex characters of this coordinator's own identity digest."""
    owner: str = coordinator.owner_id()  # type: ignore[attr-defined]
    return f"{crc32c(owner.encode('utf-8')):08x}"


def _participant_name(coordinator: object) -> str:
    """Return the section name the engine would build for this coordinator."""
    return f"{PARTICIPANT_PREFIX}{_digest(coordinator)}"


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
    """Every cross-process section name is refused, whoever asks and however it is spelled.

    The refusal list is anchored deliberately. A guard written as ``name.endswith(digest)``
    passes every case that ends in the right eight hex characters, and ``page0-<digest>`` is
    exactly that shape (``api/assembly.py`` spells page-0 sections ``page0-<crc32c(file):08x>``),
    so the cases below are the ones that separate a full-name comparison from a tail comparison.
    """
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    declare = type(first).__dict__["_declare_private_section"]
    digest = _digest(first)

    assert declare(first, _participant_name(first)) is True
    for refused in (
        # Named cross-process sections, as they are actually spelled.
        "commit",
        "lease",
        "writer.lease",
        "first-open",
        "page0-1dec8160",
        # A different instance's participant name, and a digest that belongs to nobody.
        _participant_name(second),
        f"{PARTICIPANT_PREFIX}00000000",
        # The tail is this instance's digest, the name is not. Every one of these passes an
        # endswith guard.
        digest,
        f"page0-{digest}",
        f"commit-{digest}",
        f"writer.lease-{digest}",
        f"x-{PARTICIPANT_PREFIX}{digest}",
        f"zzz{digest}",
        # The digest as a PREFIX, and the right name padded on the right.
        f"{digest}-{PARTICIPANT_PREFIX.rstrip('-')}",
        f"{digest}0000",
        f"{PARTICIPANT_PREFIX}{digest}-1",
        f"{PARTICIPANT_PREFIX}{digest}0",
        f"{PARTICIPANT_PREFIX}{digest}.lock",
    ):
        assert declare(first, refused) is False, refused
    assert set(first._private_section_locks) == {_participant_name(first)}


def test_the_accepted_name_is_the_one_the_engine_builds(
    make_coordinator: CoordinatorFactory,
) -> None:
    """The adapter's grammar and the engine's participant name are pinned to each other.

    The adapter re-declares the prefix instead of importing the engine, so this test is the fence
    against drift: if either side changes its spelling, the capability silently stops applying
    (or, worse, starts applying to something else) and this fails instead.
    """
    from okto_grafx.engine.txn_manager import PARTICIPANT_SECTION_PREFIX

    assert coordination_local._PRIVATE_SECTION_PREFIX == PARTICIPANT_SECTION_PREFIX
    assert PARTICIPANT_PREFIX == PARTICIPANT_SECTION_PREFIX

    coordinator = make_coordinator(owner_id="p1-aaaa")
    engine_name = (
        f"{PARTICIPANT_SECTION_PREFIX}"
        f"{crc32c(coordinator.owner_id().encode('utf-8')):08x}"
    )
    assert coordinator._private_section_name() == engine_name
    declare = type(coordinator).__dict__["_declare_private_section"]
    assert declare(coordinator, engine_name) is True
    # The same normalized name spelled in upper case is the same section, and exclusive()
    # normalizes identically, so it is accepted rather than treated as a foreign name.
    assert declare(coordinator, engine_name.upper()) is True
    assert set(coordinator._private_section_locks) == {engine_name}


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
    """The mechanism behind a live section never changes underneath its holder.

    The sequentially ordered half of the obligation: the holder is already registered when the
    declaration asks. The racing half -- a holder that is still on its way IN -- is the test
    below, and only that one discriminates the fence from the check.
    """
    coordinator = make_coordinator(owner_id="p1-aaaa")
    name = _participant_name(coordinator)
    declare = type(coordinator).__dict__["_declare_private_section"]
    with coordinator.exclusive(name, timeout=1.0):
        assert declare(coordinator, name) is False
    assert declare(coordinator, name) is True


def test_a_declaration_racing_an_entry_never_changes_the_mechanism_in_flight(
    database_root: Path, lock_directory: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declaration and an entry that overlap are ORDERED, not merely checked.

    THE SECOND DISCRIMINATING TEST FOR W-01, and the one that separates a fence from a
    time-of-check window. ``_section`` resolves the mechanism of an entry and registers that
    entry in ``self._sections`` at two different moments. Between them the entry is in flight:
    it is committed to the operating-system lock and it is invisible to anything that only reads
    ``self._sections``. A declaration that installs the process lock inside that window leaves a
    section whose holder is excluding on the FILE while every later thread excludes on a fresh,
    unheld process lock -- so two threads are inside at once.

    The window is the shipped code's; the test only decides WHEN it is entered instead of
    sampling it, by stalling the holder inside ``_open_lock_file`` (the first thing the entry
    does after it stops being able to see the declaration). Both threads meet at a barrier first
    and the round is repeated, because a scheduler that happened to order them the other way
    would otherwise turn a real defect into a flake.

    Pre-fix this fails on the first round: the declaration returns True, the intruder reports
    INSIDE, and ``overlaps`` is 1.
    """
    real_open = LocalProcessCoordinator._open_lock_file
    stage: dict[str, object] = {"section": None, "holder": None}

    def staged_open(
        self: LocalProcessCoordinator, path: str, section: str, deadline: float
    ) -> int:
        if section == stage["section"] and threading.get_ident() == stage["holder"]:
            entry_in_flight: threading.Event = stage["in_flight"]  # type: ignore[assignment]
            decided: threading.Event = stage["decided"]  # type: ignore[assignment]
            entry_in_flight.set()
            decided.wait(timeout=_RACE_TIMEOUT)
        return real_open(self, path, section, deadline)

    monkeypatch.setattr(LocalProcessCoordinator, "_open_lock_file", staged_open)

    for attempt in range(_RACE_ROUNDS):
        coordinator = LocalProcessCoordinator(
            DirectoryStorageDevice(database_root),
            SystemClock(),
            owner_id=f"race{attempt:03d}",
            lock_directory=lock_directory,
            poll_interval=0.001,
            sleeper=time.sleep,
        )
        name = _participant_name(coordinator)
        declare = type(coordinator).__dict__["_declare_private_section"]

        stage["section"] = name
        stage["holder"] = None
        stage["in_flight"] = in_flight = threading.Event()
        stage["decided"] = decided = threading.Event()

        gate = threading.Barrier(2, timeout=_RACE_TIMEOUT)
        entered = threading.Event()
        may_release = threading.Event()
        guard = threading.Lock()
        declared: list[object] = []
        intruder_saw: list[str] = []
        inside = 0
        overlaps = 0
        failures: list[BaseException] = []

        def holder() -> None:
            nonlocal inside, overlaps
            try:
                stage["holder"] = threading.get_ident()
                gate.wait()
                with coordinator.exclusive(name, timeout=_RACE_TIMEOUT):
                    with guard:
                        inside += 1
                        if inside > 1:
                            overlaps += 1
                    entered.set()
                    may_release.wait(timeout=_RACE_TIMEOUT)
                    with guard:
                        inside -= 1
            except BaseException as raised:  # noqa: BLE001 - asserted after the join
                failures.append(raised)
                entered.set()

        def declarer() -> None:
            try:
                gate.wait()
                assert in_flight.wait(timeout=_RACE_TIMEOUT)
                declared.append(declare(coordinator, name))
            except BaseException as raised:  # noqa: BLE001 - asserted after the join
                failures.append(raised)
            finally:
                decided.set()

        def intruder() -> None:
            nonlocal inside, overlaps
            try:
                with coordinator.exclusive(name, timeout=0.1):
                    with guard:
                        inside += 1
                        if inside > 1:
                            overlaps += 1
                        intruder_saw.append("INSIDE")
                        inside -= 1
            except GrafxLeaseTimeout:
                intruder_saw.append("GrafxLeaseTimeout")

        threads = [
            threading.Thread(target=holder, name="holder"),
            threading.Thread(target=declarer, name="declarer"),
        ]
        for thread in threads:
            thread.start()
        assert entered.wait(timeout=_RACE_TIMEOUT), attempt
        third = threading.Thread(target=intruder, name="intruder")
        third.start()
        third.join(timeout=_RACE_TIMEOUT)
        may_release.set()
        for thread in (*threads, third):
            thread.join(timeout=_RACE_TIMEOUT)

        assert failures == [], attempt
        # The harm first, because that is what the invariant is FOR: nobody joins the holder
        # inside the section, whatever the declaration did.
        assert overlaps == 0, attempt
        assert intruder_saw == ["GrafxLeaseTimeout"], (attempt, intruder_saw)
        assert inside == 0, attempt
        # Then the mechanism that prevents it: the declaration is refused while an entry is in
        # flight, exactly as it is refused while one is held.
        assert declared == [False], (attempt, declared)
        # Once nothing is live the mechanism may change, and does.
        assert declare(coordinator, name) is True, attempt
        assert set(coordinator._private_section_locks) == {name}, attempt


def test_an_entry_that_fails_leaves_nothing_pending_behind(
    make_coordinator: CoordinatorFactory,
) -> None:
    """The fence must not wedge its own door.

    A declaration is refused while a name is PENDING, so an entry that ends in a timeout, a
    device failure or a host interrupt has to stop being pending on the way out. If it did not,
    one lost race would make the capability permanently undeclarable for the life of the
    coordinator -- silently, because the declaration just answers False.
    """
    coordinator = make_coordinator(owner_id="p1-aaaa", poll_interval=0.001)
    name = _participant_name(coordinator)
    declare = type(coordinator).__dict__["_declare_private_section"]
    failure: list[BaseException] = []

    def loser() -> None:
        try:
            with coordinator.exclusive(name, timeout=0.05):
                pytest.fail("the section was granted twice at once")
        except BaseException as raised:  # noqa: BLE001 - recorded and asserted below
            failure.append(raised)

    # The section is NOT declared here, so the loser fails inside the file-lock path.
    with coordinator.exclusive(name, timeout=1.0):
        thread = threading.Thread(target=loser)
        thread.start()
        thread.join(timeout=30.0)
        assert coordinator._pending_sections == {}

    assert len(failure) == 1
    assert isinstance(failure[0], GrafxLeaseTimeout)
    assert coordinator._pending_sections == {}
    assert coordinator._sections == {}
    # And the door still opens.
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
