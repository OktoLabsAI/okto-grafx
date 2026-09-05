"""Short cross-process critical sections: exclusion, re-entry, release on exception, timeout.

The re-entrancy decision under test is the documented one: nesting the same section name from the
same thread of the same coordinator is allowed and counted, and the operating-system lock is held
until the outermost block exits. Anything else -- another instance, another thread, another
process -- contends for real.
"""

from __future__ import annotations

import threading

import pytest

from conftest import CoordinatorFactory
from coordination_support import ManualClock
from okto_grafx.adapters import coordination_local
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxLeaseTimeout


def test_two_coordinators_over_one_directory_exclude_each_other(
    make_coordinator: CoordinatorFactory
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    with first.exclusive("commit", timeout=1.0):
        with pytest.raises(GrafxLeaseTimeout) as failure:
            with second.exclusive("commit", timeout=0.5):
                pytest.fail("the section was granted twice at once")
        assert failure.value.retryable is True
        assert failure.value.details["section"] == "commit"
    # Once the first participant leaves, the second gets in immediately.
    with second.exclusive("commit", timeout=0.5):
        pass


def test_a_section_is_released_when_the_body_raises(
    make_coordinator: CoordinatorFactory
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    with pytest.raises(RuntimeError):
        with first.exclusive("commit", timeout=1.0):
            raise RuntimeError("the body failed")
    with second.exclusive("commit", timeout=0.5):
        pass
    with first.exclusive("commit", timeout=0.5):
        pass


def test_the_same_section_nests_and_only_the_outermost_exit_releases_it(
    make_coordinator: CoordinatorFactory
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    with first.exclusive("commit", timeout=1.0):
        with first.exclusive("commit", timeout=1.0):
            with first.exclusive("commit", timeout=1.0):
                pass
            # Two frames were left; the section is still held by the outermost one.
            with pytest.raises(GrafxLeaseTimeout):
                with second.exclusive("commit", timeout=0.2):
                    pass
        with pytest.raises(GrafxLeaseTimeout):
            with second.exclusive("commit", timeout=0.2):
                pass
    with second.exclusive("commit", timeout=0.2):
        pass


def test_a_nested_section_survives_an_exception_in_the_inner_frame(
    make_coordinator: CoordinatorFactory
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    with first.exclusive("commit", timeout=1.0):
        with pytest.raises(RuntimeError):
            with first.exclusive("commit", timeout=1.0):
                raise RuntimeError("the inner body failed")
        with pytest.raises(GrafxLeaseTimeout):
            with second.exclusive("commit", timeout=0.2):
                pass
    with second.exclusive("commit", timeout=0.2):
        pass


def test_different_sections_do_not_block_each_other(
    make_coordinator: CoordinatorFactory
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    with first.exclusive("commit", timeout=1.0):
        with second.exclusive("writer.lease", timeout=0.5):
            with second.exclusive("checkpoint", timeout=0.5):
                pass


def test_a_section_name_is_case_insensitive(make_coordinator: CoordinatorFactory) -> None:
    # File identity must not depend on case (CONTRACT.md section 11 item 9), so two spellings of
    # one name must not become two sections that both look free.
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    with first.exclusive("Commit", timeout=1.0):
        with pytest.raises(GrafxLeaseTimeout):
            with second.exclusive("commit", timeout=0.2):
                pass
        with first.exclusive("COMMIT", timeout=0.2):
            pass


@pytest.mark.parametrize("name", ["", "with space", "with/slash", "..\\escape", 7])
def test_an_unusable_section_name_is_refused(
    make_coordinator: CoordinatorFactory, name: object
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    with pytest.raises(GrafxConfigurationError):
        coordinator.exclusive(name, timeout=1.0)  # type: ignore[arg-type]


def test_a_negative_timeout_is_refused(make_coordinator: CoordinatorFactory) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    with pytest.raises(GrafxConfigurationError):
        with coordinator.exclusive("commit", timeout=-0.5):
            pass


def test_a_zero_timeout_makes_exactly_one_attempt(make_coordinator: CoordinatorFactory) -> None:
    clock = ManualClock(monotonic=1_000.0)
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", clock=clock)
    with second.exclusive("commit", timeout=0.0):
        pass
    with first.exclusive("commit", timeout=1.0):
        clock.slept.clear()
        with pytest.raises(GrafxLeaseTimeout):
            with second.exclusive("commit", timeout=0.0):
                pass
        assert clock.slept == []


def test_the_timeout_is_measured_on_the_injected_clock(
    make_coordinator: CoordinatorFactory
) -> None:
    clock = ManualClock(monotonic=1_000.0)
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", clock=clock, poll_interval=0.25)
    with first.exclusive("commit", timeout=1.0):
        with pytest.raises(GrafxLeaseTimeout):
            with second.exclusive("commit", timeout=1.0):
                pass
    assert clock.slept == [0.25, 0.25, 0.25, 0.25]
    assert clock.monotonic() == pytest.approx(1_001.0)


def test_another_thread_of_the_same_coordinator_contends_for_real(
    make_coordinator: CoordinatorFactory
) -> None:
    # Re-entry is per thread. A second thread of the same instance is a genuine contender, which
    # is what keeps the counter from turning a shared section into a shared illusion.
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, poll_interval=0.5)
    outcome: list[str] = []
    started = threading.Event()

    def contend() -> None:
        started.set()
        try:
            with coordinator.exclusive("commit", timeout=2.0):
                outcome.append("granted")
        except GrafxLeaseTimeout:
            outcome.append("timeout")

    with coordinator.exclusive("commit", timeout=1.0):
        worker = threading.Thread(target=contend, name="contender")
        worker.start()
        started.wait(timeout=5.0)
        worker.join(timeout=5.0)
    assert worker.is_alive() is False
    assert outcome == ["timeout"]


def test_an_idle_reusable_descriptor_does_not_exclude_another_thread(
    make_coordinator: CoordinatorFactory,
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    outcome: list[str] = []

    with coordinator.reuse_unlocked_section_descriptor("commit"):
        with coordinator.exclusive("commit", timeout=1.0):
            pass

        def acquire() -> None:
            with coordinator.exclusive("commit", timeout=1.0):
                outcome.append("granted")

        worker = threading.Thread(target=acquire, name="descriptor-contender")
        worker.start()
        worker.join(timeout=5.0)
        assert worker.is_alive() is False
        assert outcome == ["granted"]

    assert coordinator._descriptor_scopes == {}


def test_failed_acquire_and_unlock_both_evict_a_reusable_descriptor(
    make_coordinator: CoordinatorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)
    real_release = coordination_local._release_os_lock
    release_attempts = 0

    def fail_first_release(descriptor: int) -> None:
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            raise OSError("injected unlock failure")
        real_release(descriptor)

    with first.reuse_unlocked_section_descriptor("commit"):
        with monkeypatch.context() as patch:
            patch.setattr(coordination_local, "_release_os_lock", fail_first_release)
            with first.exclusive("commit", timeout=1.0):
                pass
        key = (threading.get_ident(), "commit")
        assert first._descriptor_scopes[key].descriptor is None

        with second.exclusive("commit", timeout=0.5):
            with pytest.raises(GrafxLeaseTimeout):
                with first.exclusive("commit", timeout=0.2):
                    pass
        assert first._descriptor_scopes[key].descriptor is None

        with first.exclusive("commit", timeout=0.5):
            pass

    assert first._descriptor_scopes == {}


def test_base_exception_closes_an_idle_reusable_descriptor(
    make_coordinator: CoordinatorFactory,
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)

    with pytest.raises(KeyboardInterrupt):
        with first.reuse_unlocked_section_descriptor("commit"):
            with first.exclusive("commit", timeout=1.0):
                pass
            raise KeyboardInterrupt

    assert first._descriptor_scopes == {}
    with second.exclusive("commit", timeout=0.5):
        pass


def test_base_exception_after_os_acquire_closes_the_locked_descriptor(
    make_coordinator: CoordinatorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even failure to wrap an acquired descriptor must release cross-process authority."""
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=50.0)

    def fail_to_wrap(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        with first.reuse_unlocked_section_descriptor("commit"):
            with monkeypatch.context() as patch:
                patch.setattr(coordination_local, "_FileSectionHandle", fail_to_wrap)
                with first.exclusive("commit", timeout=1.0):
                    pass

    assert first._descriptor_scopes == {}
    with second.exclusive("commit", timeout=0.5):
        pass


def test_a_coordinator_without_a_lock_directory_still_serialises_itself(
    make_coordinator: CoordinatorFactory
) -> None:
    # The in-memory device is never shared between processes, so its sections are process-local
    # by construction. Inside one coordinator the semantics are identical: re-entry counts, a
    # second thread contends, and the section is released on the way out.
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(
        owner_id="p1-aaaa", clock=clock, use_lock_directory=False, poll_interval=0.5
    )
    with coordinator.exclusive("commit", timeout=1.0):
        with coordinator.exclusive("commit", timeout=1.0):
            pass
        outcome: list[str] = []

        def contend() -> None:
            try:
                with coordinator.exclusive("commit", timeout=2.0):
                    outcome.append("granted")
            except GrafxLeaseTimeout:
                outcome.append("timeout")

        worker = threading.Thread(target=contend, name="contender")
        worker.start()
        worker.join(timeout=5.0)
        assert outcome == ["timeout"]
    with coordinator.exclusive("commit", timeout=1.0):
        pass


def test_the_lock_file_is_created_next_to_the_control_files(
    make_coordinator: CoordinatorFactory, lock_directory: str
) -> None:
    from pathlib import Path

    coordinator = make_coordinator(owner_id="p1-aaaa")
    with coordinator.exclusive("commit", timeout=1.0):
        pass
    assert (Path(lock_directory) / "commit.lock").exists()
    # The lock file stays: removing it while another process holds it open is how a lock stops
    # being one.
    with coordinator.exclusive("commit", timeout=1.0):
        pass
    assert (Path(lock_directory) / "commit.lock").exists()


def test_a_busy_lock_file_is_retried_before_it_is_reported(
    make_coordinator: CoordinatorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An anti-virus scanner or an indexer holds a freshly created file open for a few
    # milliseconds and answers the next open with a sharing violation (TR-3). The section must
    # ride that out inside the budget it was given rather than call the setup broken.
    import os

    real_open = os.open
    refusals = {"left": 3}

    def flaky(path, flags, mode=0o777, *, dir_fd=None):
        if refusals["left"] > 0 and str(path).endswith(".lock"):
            refusals["left"] -= 1
            raise PermissionError(32, "The process cannot access the file.")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, poll_interval=0.01)
    monkeypatch.setattr(os, "open", flaky)
    with coordinator.exclusive("commit", timeout=1.0):
        pass
    monkeypatch.undo()
    assert refusals["left"] == 0
    assert clock.slept == [0.01, 0.01, 0.01]


def test_a_lock_file_that_never_opens_is_not_reported_as_a_busy_section(
    make_coordinator: CoordinatorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nobody holds the section: the file itself cannot be opened. The typed answer for that is a
    # device failure, and the classification of the two cases lives in the regression module.
    import os

    from okto_grafx.domain.errors import GrafxStorageError

    real_open = os.open

    def refuse(path, flags, mode=0o777, *, dir_fd=None):
        if str(path).endswith(".lock"):
            raise PermissionError(13, "The process cannot access the file.")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, poll_interval=0.25)
    monkeypatch.setattr(os, "open", refuse)
    with pytest.raises(GrafxStorageError) as failure:
        with coordinator.exclusive("commit", timeout=1.0):
            pass
    monkeypatch.undo()
    assert not isinstance(failure.value, GrafxLeaseTimeout)
    assert "cannot access" in str(failure.value.details["detail"])
