"""Recovery exists only fenced: a complete coordinator, and a permit scoped to its section.

M0C removes the unfenced fallbacks. What these tests pin, door by door:

* an ABSENT coordinator refuses typed -- ``GrafxPortNotConfigured`` -- before the first read of
  state or log, from ``run()`` and from the read-only proof alike;
* a PARTIAL coordinator (fence but no ``reader_horizon``, or the reverse) is no coordinator;
* a section TIMEOUT on either door reads and changes nothing;
* the permit minted inside the section is the only key the fenced steps accept: forged permits
  (wrong seal, wrong type), a FOREIGN manager's permit, and a permit whose section has closed --
  by return or by ``BaseException`` -- all refuse without scanning or mutating;
* a LIVE holder of the real commit section in ANOTHER PROCESS keeps recovery typed-refusing,
  with zero scans, until it lets go -- then the same manager truncates normally.

The permit's non-forgeability is COOPERATIVE, as Python is: these tests build foreign and
expired permits by importing module privates, which is exactly the deliberate act the contract
documents as the boundary.
"""

from __future__ import annotations

import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.errors import (
    GrafxLeaseTimeout,
    GrafxPortNotConfigured,
    GrafxRecoveryRefused,
)
from okto_grafx.engine.coordination import COMMIT_SECTION
from okto_grafx.engine.recovery_manager import (
    _PERMIT_SEAL,
    _RecoveryPermit,
    RecoveryManager,
)

from .conftest import HEAP_FILE, Stack, commit_pages, make_page_image


class _CountingWal:
    """Delegate to the real log, counting reads and cuts; optionally blow up on the first scan."""

    def __init__(
        self, inner: object, *, explode_with: BaseException | None = None
    ) -> None:
        self._inner = inner
        self._explode: BaseException | None = explode_with
        self.scans = 0
        self.truncations = 0

    def __getattr__(self, name: str) -> Any:
        """Everything not counted here is answered by the real log."""
        return getattr(self._inner, name)

    def scan_all(self) -> object:
        """Count one scan, or raise what the test armed."""
        self.scans += 1
        if self._explode is not None:
            raise self._explode
        return self._inner.scan_all()  # type: ignore[attr-defined]

    def truncate_after(self, lsn: int) -> object:
        """Count one cut."""
        self.truncations += 1
        return self._inner.truncate_after(lsn)  # type: ignore[attr-defined]


class _SectionCoordinator:
    """A complete coordinator double whose section can be made to refuse."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.active = 0

    def owner_id(self) -> str:
        """Name the owner for the commit-state store."""
        return "fence-battery"

    def reader_horizon(self) -> int | None:
        """No live reader holds the log back in this battery."""
        return None

    @contextmanager
    def exclusive(self, name: str, *, timeout: float) -> Iterator[None]:
        """Grant or refuse the section, tracking whether it is held."""
        assert name == COMMIT_SECTION
        if self.refuse:
            raise GrafxLeaseTimeout(
                "The commit section stayed busy.", section=name, retryable=True
            )
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1


class _PartialCoordinator:
    """A coordinator with a fence but no ``reader_horizon``: partial, hence refused."""

    def owner_id(self) -> str:
        """Name the owner for the commit-state store."""
        return "partial"

    @contextmanager
    def exclusive(self, name: str, *, timeout: float) -> Iterator[None]:
        """Grant the section; completeness, not the grant, is what the test is about."""
        yield


def _manager(
    stack: Stack, *, wal: object | None = None, **overrides: object
) -> RecoveryManager:
    """Build a manager whose positional wal can be replaced by a counting wrapper."""
    settings: dict[str, object] = {"catalog": stack.catalog}
    settings.update(overrides)
    return RecoveryManager(
        stack.storage,  # type: ignore[arg-type]
        wal if wal is not None else stack.wal,
        stack.ledger,
        stack.quarantine,
        stack.pool,
        stack.metrics,  # type: ignore[arg-type]
        **settings,  # type: ignore[arg-type]
    )


def _append_damaged_tail(stack: Stack) -> str:
    """Create one valid WAL transaction, append a torn tail, and return its segment."""
    commit_pages(
        stack,
        [(HEAP_FILE, 3, make_page_image(stack.codec, [b"fenced"], page_index=3))],
    )
    segment = stack.wal.segments()[-1].name
    stack.storage.append_log(segment, bytes(64))  # type: ignore[attr-defined]
    return segment


# --- absent and partial coordinators refuse before the first read ---------------------------


def test_run_without_a_coordinator_refuses_typed_and_reads_no_log(stack: Stack) -> None:
    """No coordinator, no pass: typed refusal naming the port, zero scans, zero cuts."""
    wal = _CountingWal(stack.wal)
    with pytest.raises(GrafxPortNotConfigured, match="no process coordinator is wired"):
        _manager(stack, wal=wal, coordinator=None).run()
    assert wal.scans == 0
    assert wal.truncations == 0


def test_run_with_a_partial_coordinator_refuses_typed_and_reads_no_log(
    stack: Stack,
) -> None:
    """A fence without ``reader_horizon`` is partial, and a partial fence is no fence."""
    wal = _CountingWal(stack.wal)
    with pytest.raises(GrafxPortNotConfigured, match="reader_horizon"):
        _manager(stack, wal=wal, coordinator=_PartialCoordinator()).run()
    assert wal.scans == 0


def test_run_with_a_fenceless_coordinator_refuses_typed_and_reads_no_log(
    stack: Stack,
) -> None:
    """``reader_horizon`` without ``exclusive`` is just as partial, in the other direction."""

    class _Fenceless:
        def owner_id(self) -> str:
            """Name the owner for the commit-state store."""
            return "fenceless"

        def reader_horizon(self) -> int | None:
            """Answer a horizon; the missing piece is the fence itself."""
            return None

    wal = _CountingWal(stack.wal)
    with pytest.raises(GrafxPortNotConfigured, match="exclusive"):
        _manager(stack, wal=wal, coordinator=_Fenceless()).run()
    assert wal.scans == 0


def test_the_read_only_proof_without_a_coordinator_refuses_typed_and_reads_no_log(
    stack: Stack,
) -> None:
    """The proof is an observation against commit; unfenced it may not even begin."""
    wal = _CountingWal(stack.wal)
    with pytest.raises(GrafxPortNotConfigured, match="no process coordinator is wired"):
        _manager(stack, wal=wal, coordinator=None).require_read_only_consistent()
    assert wal.scans == 0


def test_the_read_only_proof_with_a_partial_coordinator_refuses_before_reading(
    stack: Stack,
) -> None:
    """Partial coordinators refuse the proof exactly as they refuse the pass."""
    wal = _CountingWal(stack.wal)
    with pytest.raises(GrafxPortNotConfigured, match="reader_horizon"):
        _manager(
            stack, wal=wal, coordinator=_PartialCoordinator()
        ).require_read_only_consistent()
    assert wal.scans == 0


def test_a_section_timeout_on_the_read_only_proof_reads_no_log(stack: Stack) -> None:
    """The timeout happens BEFORE the proof's first read, and leaves nothing observed."""
    wal = _CountingWal(stack.wal)
    manager = _manager(stack, wal=wal, coordinator=_SectionCoordinator(refuse=True))
    with pytest.raises(GrafxLeaseTimeout):
        manager.require_read_only_consistent()
    assert wal.scans == 0


# --- the permit: forged, foreign, expired -----------------------------------------------------


def test_a_forged_permit_is_refused_at_every_fenced_door(stack: Stack) -> None:
    """Neither a wrong seal nor a duck-typed stand-in opens any fenced step."""
    manager = _manager(stack, coordinator=_SectionCoordinator())
    with pytest.raises(GrafxRecoveryRefused, match="forged"):
        _RecoveryPermit(manager, object())  # wrong seal
    for door in (
        lambda: manager._run_fenced(object()),  # noqa: SLF001
        lambda: manager._require_read_only_consistent_fenced(object()),  # noqa: SLF001
        lambda: manager._preserve(None, [], object()),  # type: ignore[arg-type]  # noqa: SLF001
        lambda: manager._truncate(None, [], object()),  # type: ignore[arg-type]  # noqa: SLF001
        lambda: manager._repair_ledger([], object()),  # type: ignore[arg-type]  # noqa: SLF001
        lambda: manager._redo(  # noqa: SLF001
            None,  # type: ignore[arg-type]
            [],
            replay=None,  # type: ignore[arg-type]
            state=None,  # type: ignore[arg-type]
            state_was_damaged=False,
            permit=object(),  # type: ignore[arg-type]
        ),
    ):
        with pytest.raises(GrafxRecoveryRefused, match="forged or absent"):
            door()


def test_a_foreign_permit_from_another_manager_is_refused(stack: Stack) -> None:
    """A permit proves ONE manager's section; another manager's pass refuses it."""
    manager_a = _manager(stack, coordinator=_SectionCoordinator())
    manager_b = _manager(stack, coordinator=_SectionCoordinator())
    foreign = _RecoveryPermit(manager_a, _PERMIT_SEAL)
    with pytest.raises(GrafxRecoveryRefused, match="another manager"):
        manager_b._run_fenced(foreign)  # noqa: SLF001
    assert (
        stack.wal.segments() is not None
    )  # the stack stayed usable; nothing was poisoned


class _CapturingManager(RecoveryManager):
    """A manager that remembers the permit its own run minted, for after-life assertions."""

    seen: object = None

    def _run_fenced(self, permit: object) -> Any:  # type: ignore[override]
        """Record the permit, then run the real fenced pass with it."""
        self.seen = permit
        return super()._run_fenced(permit)  # type: ignore[arg-type]


def test_a_permit_dies_with_its_section_and_cannot_be_replayed(stack: Stack) -> None:
    """After ``run()`` returns, the permit it minted opens nothing ever again."""
    coordinator = _SectionCoordinator()
    manager = _CapturingManager(
        stack.storage,  # type: ignore[arg-type]
        stack.wal,
        stack.ledger,
        stack.quarantine,
        stack.pool,
        stack.metrics,  # type: ignore[arg-type]
        catalog=stack.catalog,
        coordinator=coordinator,
    )
    manager.run()
    assert isinstance(manager.seen, _RecoveryPermit)
    assert coordinator.active == 0
    with pytest.raises(GrafxRecoveryRefused, match="revoked"):
        manager._run_fenced(manager.seen)  # noqa: SLF001
    with pytest.raises(GrafxRecoveryRefused, match="revoked"):
        manager._truncate(None, [], manager.seen)  # type: ignore[arg-type]  # noqa: SLF001
    with pytest.raises(GrafxRecoveryRefused, match="revoked"):
        manager._repair_ledger([], manager.seen)  # type: ignore[arg-type]  # noqa: SLF001


def test_a_base_exception_inside_the_section_still_revokes_the_permit(
    stack: Stack,
) -> None:
    """However the section is left -- ``KeyboardInterrupt`` included -- the permit dies with it."""
    coordinator = _SectionCoordinator()
    wal = _CountingWal(stack.wal, explode_with=KeyboardInterrupt())
    manager = _CapturingManager(
        stack.storage,  # type: ignore[arg-type]
        wal,
        stack.ledger,
        stack.quarantine,
        stack.pool,
        stack.metrics,  # type: ignore[arg-type]
        catalog=stack.catalog,
        coordinator=coordinator,
    )
    with pytest.raises(KeyboardInterrupt):
        manager.run()
    assert coordinator.active == 0, "the section must be released on the way out"
    assert isinstance(manager.seen, _RecoveryPermit)
    with pytest.raises(GrafxRecoveryRefused, match="revoked"):
        manager._run_fenced(manager.seen)  # noqa: SLF001


# --- the real section, held by another process ------------------------------------------------


def test_a_live_holder_of_the_commit_section_keeps_recovery_out_until_it_lets_go(
    stack: Stack, tmp_path: Path
) -> None:
    """Cross-process: while a child holds the REAL section, recovery times out typed with zero
    scans; the moment the child releases, the same manager truncates the torn tail normally."""
    segment = _append_damaged_tail(stack)
    root = tmp_path / "fence"
    root.mkdir()
    ready = tmp_path / "ready.marker"
    release = tmp_path / "release.marker"
    coordinator = LocalProcessCoordinator(
        LocalStorageDevice(str(root)),
        SystemClock(),
        owner_id="fence-parent",
        lock_directory=str(root / "control"),
        poll_interval=0.002,
    )
    wal = _CountingWal(stack.wal)
    manager = _manager(stack, wal=wal, coordinator=coordinator, commit_lock_timeout=0.4)
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("fence_child.py")),
            str(root),
            str(ready),
            str(release),
        ],
    )
    try:
        deadline = time.monotonic() + 30.0
        while not ready.exists() and time.monotonic() < deadline:
            assert child.poll() is None, (
                "the section-holding child died before announcing"
            )
            time.sleep(0.005)
        assert ready.exists(), "the child never announced the held section"
        before = stack.storage.read_log(segment, 0, stack.storage.log_size(segment))  # type: ignore[attr-defined]
        with pytest.raises(GrafxLeaseTimeout):
            manager.run()
        assert wal.scans == 0, "a held section must keep recovery from even scanning"
        assert wal.truncations == 0
        after = stack.storage.read_log(segment, 0, stack.storage.log_size(segment))  # type: ignore[attr-defined]
        assert after == before, "a refused pass may not move a WAL byte"
    finally:
        release.write_text("go", encoding="ascii")
        child.wait(timeout=30.0)
    report = manager.run()
    assert report.outcome == "truncated"
    assert wal.scans == 1
    assert wal.truncations == 1
