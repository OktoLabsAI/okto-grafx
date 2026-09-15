"""Sections proved private to one coordinator instance (W-01).

``_declare_private_section`` is the optional, private-performance capability that lets a section
whose name carries this instance's own ``owner_id`` digest be serialised with a process lock
instead of an operating-system file lock. The claim it rests on is narrow and is tested here as
five separate obligations:

* the declaration is REFUSED for every name that is not EXACTLY this instance's participant
  name, so no cross-process section can be downgraded by a caller that merely asks nicely -- and
  not by one that pads a spelling until it ends in the right eight hex characters either;
* a declared section still serialises the THREADS of its coordinator, which is the entire reason
  the participant section exists (txn_manager ``_participant_section``);
* a declared section keeps being HANDED OVER, not merely held by one thread at a time: every
  thread of the coordinator goes on getting it, and a wait for it is BOUNDED, so a waiter that
  cannot be woken is a failure here rather than a hang somewhere downstream;
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

_STARVATION_WORKERS: int = 3
"""Threads of one coordinator in the starvation test: two is a duel, three is a queue."""

_STARVATION_ENTRIES: int = 8
"""Back-to-back entries per round, the shape one statement of a participant really has."""

_STARVATION_WINDOW: float = 6.0
"""Seconds of contention. Long enough that a starved thread cannot be called unlucky."""

_STARVATION_SHARE: int = 10
"""How many times the busiest thread's rounds the quietest one is allowed to be below.

A share rather than a count, so a loaded machine that simply ran less still passes, while the
defect -- 794 rounds for one thread against none at all for the other two -- cannot come near
it at any factor.
"""

_STARVATION_FLOOR: int = 5
"""Rounds per worker, summed, below which the window proved nothing and the test skips (A75.2).

The floor is on the TOTAL across the workers, never on the smallest of them. Under the defect
one thread stays busy and turns in hundreds of rounds, so the total clears the floor easily and
the share assertion still fires; a floor on the minimum would skip exactly the starved shape the
test exists to catch. The accepted cost is the other direction: a CPU-bound thread inside the
same interpreter can hold every worker down to rounds like ``[0, 1, 1]`` with the fix and
``[0, 0, 2]`` without it, and both windows are below the floor and therefore UNMEASURED rather
than a regression (CONTRACT.md A75.2), which is the right answer for a window in which nothing
contended.
"""

_STARVATION_JOIN_SLACK: float = 20.0
"""Seconds past the contention window that ALL joins share, so no join can outlive the suite.

One deadline for the three joins rather than one timeout each: three sequential 36 s joins can
add up to 108 s, past the 60 s per-test timeout whose thread method ``os._exit()``s the whole
session and leaves no report. With a shared deadline the liveness assertion below is what
reports a wedged thread.
"""

_BOUND_BUDGET: float = 0.5
"""Timeout handed to the waiter of the bounded-wait test: small, so an unbounded park is obvious."""

_BOUND_SLACK: float = 5.0
"""Seconds past that budget the bounded waiter may take before the test calls it unbounded."""


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


@pytest.mark.slow
def test_no_thread_of_one_coordinator_is_starved_of_a_declared_section(
    database_root: Path, lock_directory: str
) -> None:
    """Every thread of one participant keeps GETTING the section, not only the one that has it.

    THE REGRESSION TEST FOR THE VECTOR-CONCURRENCY HANG. Serialising the threads is not the
    whole obligation: a wait that cannot see the release serialises them into one. A waiter that
    spends its interval beside the lock -- asleep, then sampling with a non-blocking acquire --
    is never enqueued, so nothing wakes it when the section is freed, and the thread that just
    released re-takes it within microseconds, long before the sleeper's next sample lands. A
    participant enters this section several times back to back for one statement, so the freed
    instants are exactly the ones a sleeping waiter is least likely to be looking at, and the
    thread already running wins every one of them.

    The shape below is that one: a round is a run of back-to-back entries whose body makes real
    system calls, which is where the interpreter hands over while the section is HELD. Forcing
    ``_parks_on_section_locks`` False restores exactly the wait ``0e8c13a`` shipped, and over
    six seconds this test then turns in rounds ``[0, 0, 794]`` with refusals ``[3, 3, 0]``: one
    thread completed 794 rounds while the other two completed none at all and were refused with
    the typed timeout three times each. With the parking wait the same window is shared -- 2 600,
    2 088 and 1 912 section entries across the three threads -- and all of them keep progressing.

    The assertion is a SHARE, so a loaded machine that simply ran less cannot fail it, and the
    volume floor above it is what stops a window that interleaved nothing from failing on a
    number that measured nothing (A75.2). The real clock and the default sleeper are used
    because this is a test about a wait that must really wait and must really be woken.
    """
    coordinator = LocalProcessCoordinator(
        DirectoryStorageDevice(database_root),
        SystemClock(),
        owner_id="p1-aaaa",
        lock_directory=lock_directory,
    )
    name = _participant_name(coordinator)
    assert type(coordinator).__dict__["_declare_private_section"](coordinator, name) is True
    # The section's own lock file: it exists because the declaration created it, and a stat of
    # it is the cheapest honest system call this test can make from inside the section.
    touched = str(Path(lock_directory) / f"{name}{LOCK_FILE_SUFFIX}")

    rounds = [0] * _STARVATION_WORKERS
    refusals = [0] * _STARVATION_WORKERS
    escaped: list[str] = []
    ready = threading.Barrier(_STARVATION_WORKERS)
    deadline = time.monotonic() + _STARVATION_WINDOW

    def worker(slot: int) -> None:
        try:
            ready.wait(timeout=_STARVATION_WINDOW)
            while time.monotonic() < deadline:
                try:
                    for _entry in range(_STARVATION_ENTRIES):
                        with coordinator.exclusive(name, timeout=2.0):
                            for _call in range(30):
                                os.stat(touched)
                except GrafxLeaseTimeout:
                    refusals[slot] += 1
                    continue
                rounds[slot] += 1
        except BaseException as failure:  # noqa: BLE001 - reported as a failure below
            escaped.append(f"{slot}:{type(failure).__name__}: {failure}")

    workers = [
        threading.Thread(target=worker, args=(slot,), name=f"starve-{slot}", daemon=True)
        for slot in range(_STARVATION_WORKERS)
    ]
    for thread in workers:
        thread.start()
    # ONE deadline shared by the three joins: see _STARVATION_JOIN_SLACK. The assertion below,
    # not a join that outlived the session, is what reports a thread that never came back.
    join_deadline = deadline + _STARVATION_JOIN_SLACK
    for thread in workers:
        thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
    assert [thread.name for thread in workers if thread.is_alive()] == []
    assert escaped == [], escaped

    evidence = (rounds, refusals)
    if sum(rounds) < _STARVATION_WORKERS * _STARVATION_FLOOR:
        pytest.skip(
            f"machine too loaded to prove anything: {sum(rounds)} rounds in total across the "
            f"workers ({rounds}) in {_STARVATION_WINDOW} s, below the "
            f"{_STARVATION_WORKERS * _STARVATION_FLOOR} this test needs to have exercised "
            "contention at all"
        )
    assert min(rounds) * _STARVATION_SHARE >= max(rounds), evidence


def test_a_wait_for_a_parked_declared_section_is_bounded_by_its_own_budget(
    database_root: Path, lock_directory: str
) -> None:
    """A waiter that parks IN the section lock still comes back, and still refuses in type.

    The companion of the starvation test and the guard of one term: the ``timeout=`` of the
    ``lock.acquire`` the parking wait performs. Parking is what makes a release wake a waiter,
    and an unbounded park would deliver that at the price of the refusal -- the thread would
    stay inside ``threading.Lock.acquire`` for as long as the holder wanted, never reach the
    deadline test above it, never raise :class:`GrafxLeaseTimeout`, and hand the caller a hang
    in place of an error. So the waiter below is given a budget far shorter than the holder's
    stay and must come back inside it, with the typed refusal the section contract promises.

    What this does NOT guard is the OTHER term of the same line, the ``deadline - now`` in
    ``min(self._poll, deadline - now)``: a wait that ignored it would overshoot by at most one
    poll interval, five milliseconds, which no assertion on a loaded machine can see. Only the
    bound of the acquire is observable from outside, and it is the one that matters, because it
    is the one whose absence turns a refusal into a wedge.

    A real :class:`SystemClock` and the default sleeper are used, the same construction as the
    starvation test, because those are exactly the conditions under which the wait parks at all.
    """
    coordinator = LocalProcessCoordinator(
        DirectoryStorageDevice(database_root),
        SystemClock(),
        owner_id="p1-aaaa",
        lock_directory=lock_directory,
    )
    name = _participant_name(coordinator)
    assert type(coordinator).__dict__["_declare_private_section"](coordinator, name) is True

    held = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    refusal: list[BaseException] = []

    def holder() -> None:
        with coordinator.exclusive(name, timeout=_RACE_TIMEOUT):
            held.set()
            release.wait(timeout=_RACE_TIMEOUT)

    def waiter() -> None:
        try:
            with coordinator.exclusive(name, timeout=_BOUND_BUDGET):
                pytest.fail("the section was granted while another thread held it")
        except BaseException as raised:  # noqa: BLE001 - recorded and asserted below
            refusal.append(raised)
        finally:
            returned.set()

    keeper = threading.Thread(target=holder, name="bound-holder")
    keeper.start()
    assert held.wait(timeout=_BOUND_SLACK), "the holder never entered the section"
    loser = threading.Thread(target=waiter, name="bound-waiter", daemon=True)
    loser.start()
    bounded = returned.wait(timeout=_BOUND_BUDGET + _BOUND_SLACK)
    release.set()
    join_deadline = time.monotonic() + _BOUND_SLACK
    for thread in (loser, keeper):
        thread.join(timeout=max(0.0, join_deadline - time.monotonic()))

    assert bounded, (
        f"the waiter was still inside its {_BOUND_BUDGET} s wait after "
        f"{_BOUND_BUDGET + _BOUND_SLACK} s: the park is not bounded by the budget"
    )
    assert [thread.name for thread in (loser, keeper) if thread.is_alive()] == []
    assert len(refusal) == 1, refusal
    assert isinstance(refusal[0], GrafxLeaseTimeout), refusal[0]
    assert refusal[0].retryable is True
    assert refusal[0].details["section"] == name


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
