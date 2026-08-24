"""Retirement destroys one inspected GENERATION, fenced, or it destroys nothing.

M0C's second lock on the door. What these tests pin, window by window:

* the door is FENCED: no coordinator (or a held section in another PROCESS) refuses typed
  before the probe is even called, and the record keeps every byte;
* targets are CANONICAL: exactly the writer lease and ``control/readers/<id>.reader`` --
  a flat reader name, ``commit.state`` and arbitrary control files all refuse;
* a record that changes DURING inspection refuses retryably with nothing captured;
* a record replaced AFTER the evidence was persisted refuses retryably, names the quarantine
  entry and ledger entry that hold the damaged generation, and the replacement -- healthy or
  not -- survives byte for byte;
* a record that VANISHES before the last look is the outcome already: evidence kept, nothing
  else touched;
* a second retirement of a retired name refuses (there is nothing to retire), and a retirement
  the platform interrupted resumes idempotently: same quarantine entry, no second ledger entry.

The guarantee is COOPERATIVE and says so: the storage port has no atomic compare-and-remove,
so the last look under the commit section is exactly as wide as the platform allows -- these
tests exercise every window that exists ABOVE the port.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxLeaseTimeout,
    GrafxPortNotConfigured,
    GrafxRecoveryRefused,
)
from okto_grafx.domain.recovery.report import FindingKind
from okto_grafx.engine.recovery_manager import RecoveryManager

from .conftest import Stack

LEASE = "control/writer.lease"
TARGET = LEASE
READER = "control/readers/reader-a.reader"
DAMAGED = b"AAAA-damaged"
REPLACED = b"BBBB-healthy"


def _seed(stack: Stack, name: str, body: bytes = DAMAGED) -> str:
    """Create one control record holding exactly ``body``."""
    stack.storage.create(name, exclusive=False)  # type: ignore[attr-defined]
    stack.storage.append_log(name, body)  # type: ignore[attr-defined]
    return name


def _replace(stack: Stack, name: str, body: bytes) -> None:
    """Swap the record's bytes underneath whoever is inspecting it."""
    stack.storage.remove(name)  # type: ignore[attr-defined]
    stack.storage.create(name, exclusive=False)  # type: ignore[attr-defined]
    stack.storage.append_log(name, body)  # type: ignore[attr-defined]


def _quarantine_files(stack: Stack) -> list[str]:
    """Snapshot the quarantine directory, so a refusal can prove it captured nothing."""
    return sorted(
        name
        for name in stack.storage.list_files()  # type: ignore[attr-defined]
        if name.startswith("quarantine/")
    )


def _bytes_of(stack: Stack, name: str) -> bytes:
    """Read the record exactly as the device holds it."""
    return stack.storage.read_log(name, 0, stack.storage.log_size(name))  # type: ignore[attr-defined]


class _DamageProbe:
    """Evidence of damage on every read, counting how often it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    def read_control_record(self, file: str) -> None:
        """Refuse with the one class that counts as damage."""
        self.calls += 1
        raise GrafxCorruptionDetected(f"The bytes of {file!r} are unreadable.")


class _SwappingProbe(_DamageProbe):
    """A probe whose read coincides with the record being replaced (the inspection window)."""

    def __init__(self, stack: Stack, name: str, body: bytes) -> None:
        super().__init__()
        self._stack = stack
        self._name = name
        self._body = body

    def read_control_record(self, file: str) -> None:
        """Replace the record, then report the damage that was seen."""
        _replace(self._stack, self._name, self._body)
        super().read_control_record(file)


class _WindowStorage:
    """Delegate to the device, striking one arranged blow at an exact retirement window."""

    def __init__(
        self,
        inner: object,
        name: str,
        *,
        at_read: int = 0,
        at_exists: int = 0,
        strike: Any = None,
    ) -> None:
        self._inner = inner
        self._name = name
        self._at_read = at_read
        self._at_exists = at_exists
        self._strike = strike
        self.reads = 0
        self.exists_calls = 0

    def __getattr__(self, attr: str) -> Any:
        """Everything not struck here is answered by the real device."""
        return getattr(self._inner, attr)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Count reads of the target and strike exactly once, at the arranged one."""
        if file == self._name:
            self.reads += 1
            if self.reads == self._at_read and self._strike is not None:
                self._strike()
                length = self._inner.log_size(file)  # type: ignore[attr-defined]
        return self._inner.read_log(file, offset, length)  # type: ignore[attr-defined]

    def exists(self, file: str) -> bool:
        """Count existence checks of the target and strike at the arranged one."""
        if file == self._name:
            self.exists_calls += 1
            if self.exists_calls == self._at_exists and self._strike is not None:
                self._strike()
        return self._inner.exists(file)  # type: ignore[attr-defined]


class _DeferringStorage:
    """A platform that will not release the name: recycle refuses, remove is swallowed."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, attr: str) -> Any:
        """Everything else is answered by the real device."""
        return getattr(self._inner, attr)

    def recycle(self, file: str) -> bool:
        """Hold the name."""
        return False

    def remove(self, file: str) -> None:
        """Hold the name, silently, as an indexer or antivirus does."""


def _manager(
    stack: Stack, *, storage: object | None = None, **overrides: object
) -> RecoveryManager:
    """Build a manager whose positional storage can be replaced by a window wrapper."""
    settings: dict[str, object] = {"catalog": stack.catalog}
    settings.setdefault("control_probe", _DamageProbe())
    settings.update(overrides)
    from .conftest import StackCoordinator

    settings.setdefault("coordinator", StackCoordinator())
    return RecoveryManager(
        storage if storage is not None else stack.storage,  # type: ignore[arg-type]
        stack.wal,
        stack.ledger,
        stack.quarantine,
        stack.pool,
        stack.metrics,  # type: ignore[arg-type]
        **settings,  # type: ignore[arg-type]
    )


# --- the fence on this door -------------------------------------------------------------------


def test_retirement_without_a_coordinator_refuses_before_the_probe_is_called(
    stack: Stack,
) -> None:
    """No fence, no evidence-gathering: the probe stays uncalled and every byte stays."""
    _seed(stack, LEASE)
    probe = _DamageProbe()
    with pytest.raises(GrafxPortNotConfigured, match="no process coordinator is wired"):
        stack.recovery(control_probe=probe, coordinator=None).retire_control_record(
            LEASE
        )
    assert probe.calls == 0
    assert _bytes_of(stack, LEASE) == DAMAGED
    assert len(stack.ledger.entries()) == 0


def test_a_live_holder_of_the_section_keeps_retirement_out_until_it_lets_go(
    stack: Stack, tmp_path: Path
) -> None:
    """Cross-process: a held commit section refuses the retirement typed, with the probe
    uncalled and the record intact; the release lets the same manager retire it."""
    _seed(stack, LEASE)
    root = tmp_path / "fence"
    root.mkdir()
    ready = tmp_path / "ready.marker"
    release = tmp_path / "release.marker"
    coordinator = LocalProcessCoordinator(
        LocalStorageDevice(str(root)),
        SystemClock(),
        owner_id="retire-parent",
        lock_directory=str(root / "control"),
        poll_interval=0.002,
    )
    probe = _DamageProbe()
    manager = _manager(
        stack, control_probe=probe, coordinator=coordinator, commit_lock_timeout=0.4
    )
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
        with pytest.raises(GrafxLeaseTimeout):
            manager.retire_control_record(LEASE)
        assert probe.calls == 0
        assert _bytes_of(stack, LEASE) == DAMAGED
    finally:
        release.write_text("go", encoding="ascii")
        child.wait(timeout=30.0)
    report = manager.retire_control_record(LEASE)
    assert report.records_discarded == 1
    assert not stack.storage.exists(LEASE)  # type: ignore[attr-defined]


# --- canonical targets ------------------------------------------------------------------------


def test_only_the_lease_and_canonical_reader_records_pass_the_target_gate(
    stack: Stack,
) -> None:
    """A flat reader, an arbitrary control file and an empty reader id are nobody's target."""
    manager = _manager(stack)
    for name in (
        "control/reader-a.reader",
        "control/other.bin",
        "control/readers/.reader",
        "control/readers/nested.lease",
        "control/readers/UPPER.reader",
        "control/readers/a..b.reader",
    ):
        _seed(stack, name)
        with pytest.raises(GrafxRecoveryRefused, match="canonical retirement target"):
            manager.retire_control_record(name)
        assert _bytes_of(stack, name) == DAMAGED, name


def test_a_near_canonical_reader_id_refuses_before_any_byte_is_read(
    stack: Stack,
) -> None:
    """Ids the coordination adapter could never have written are refused at the gate:
    slashes, traversal, over-length and non-ASCII never reach the device or the probe."""
    probe = _DamageProbe()
    manager = _manager(stack, control_probe=probe)
    for name in (
        "control/readers/a/b.reader",
        "control/readers/" + "x" * 97 + ".reader",
        "control/readers/n\u00e3o.reader",
        "control/readers/..reader",
    ):
        with pytest.raises(GrafxRecoveryRefused, match="canonical retirement target"):
            manager.retire_control_record(name)
    assert probe.calls == 0


def test_commit_state_can_never_leave_through_the_retirement_door(stack: Stack) -> None:
    """The publication record is protected upstream of everything else this door does."""
    _seed(stack, "control/commit.state", b"published")
    manager = _manager(stack)
    with pytest.raises(GrafxRecoveryRefused, match="not a control-plane record"):
        manager.retire_control_record("control/commit.state")
    assert _bytes_of(stack, "control/commit.state") == b"published"


# --- one generation, window by window ---------------------------------------------------------


def test_a_record_that_changes_during_inspection_refuses_with_nothing_captured(
    stack: Stack,
) -> None:
    """The probe's verdict belongs to the OLD bytes, so nothing is quarantined or retired."""
    _seed(stack, TARGET)
    probe = _SwappingProbe(stack, TARGET, REPLACED)
    with pytest.raises(
        GrafxRecoveryRefused, match="changed while it was being inspected"
    ) as caught:
        _manager(stack, control_probe=probe).retire_control_record(TARGET)
    assert caught.value.details.get("retryable") or "retry" in str(caught.value)
    assert _bytes_of(stack, TARGET) == REPLACED
    assert len(stack.ledger.entries()) == 0


def test_a_replacement_after_the_evidence_survives_byte_for_byte(stack: Stack) -> None:
    """A healthy record that lands between the ledger entry and the removal is untouchable:
    the refusal names the evidence kept, and the replacement keeps every byte."""
    _seed(stack, TARGET)
    window = _WindowStorage(
        stack.storage,
        TARGET,
        at_read=3,  # 1: the generation; 2: the confirm; 3: the last look
        strike=lambda: _replace(stack, TARGET, REPLACED),
    )
    with pytest.raises(GrafxRecoveryRefused, match="replacement landed") as caught:
        _manager(stack, storage=window).retire_control_record(TARGET)
    details = caught.value.details
    assert details["expected_sha256"] == hashlib.sha256(DAMAGED).hexdigest()
    assert _bytes_of(stack, TARGET) == REPLACED
    kept = stack.quarantine.read(str(details["quarantine"]))
    assert kept == DAMAGED, (
        "quarantine must hold the inspected generation, byte for byte"
    )
    assert len(stack.ledger.entries()) == 1, "the forensic entry legitimately stays"


def test_a_record_that_vanishes_before_the_last_look_is_the_outcome_already(
    stack: Stack,
) -> None:
    """Someone else removed the name: the evidence is kept and nothing else is touched."""
    _seed(stack, TARGET)
    window = _WindowStorage(
        stack.storage,
        TARGET,
        at_exists=2,  # 1: the door's entry check; 2: the last look
        strike=lambda: stack.storage.remove(TARGET),  # type: ignore[attr-defined]
    )
    report = _manager(stack, storage=window).retire_control_record(TARGET)
    assert report.records_discarded == 1
    retired = report.findings_of(FindingKind.CONTROL_RECORD_RETIRED)
    assert retired and "already gone" in retired[0].detail
    assert not stack.storage.exists(TARGET)  # type: ignore[attr-defined]
    assert stack.quarantine.read(retired[0].quarantine) == DAMAGED


def test_a_completed_retirement_keeps_the_exact_generation_in_quarantine_and_ledger(
    stack: Stack,
) -> None:
    """The happy path is still a generation: sha256 in the details, bytes in the evidence."""
    _seed(stack, LEASE)
    report = _manager(stack).retire_control_record(LEASE)
    assert report.records_discarded == 1
    assert report.ledger_entries_created == 1
    retired = report.findings_of(FindingKind.CONTROL_RECORD_RETIRED)
    assert retired and "exact generation" in retired[0].detail
    assert stack.quarantine.read(retired[0].quarantine) == DAMAGED
    digest = hashlib.sha256(DAMAGED).hexdigest()
    assert stack.quarantine.inspect(retired[0].quarantine).manifest.digest == digest
    assert not stack.storage.exists(LEASE)  # type: ignore[attr-defined]


def test_a_second_retirement_of_a_retired_name_refuses(stack: Stack) -> None:
    """The name is gone; a door that destroys names refuses to destroy their absence."""
    _seed(stack, LEASE)
    manager = _manager(stack)
    manager.retire_control_record(LEASE)
    with pytest.raises(GrafxRecoveryRefused, match="There is no control record"):
        manager.retire_control_record(LEASE)
    assert len(stack.ledger.entries()) == 1


def test_an_interrupted_retirement_resumes_idempotently(stack: Stack) -> None:
    """A platform that held the name leaves evidence behind; the retry retires the SAME
    generation without a second quarantine entry or a second ledger entry."""
    _seed(stack, TARGET)
    held = _manager(
        stack, storage=_DeferringStorage(stack.storage)
    ).retire_control_record(TARGET)
    assert held.records_discarded == 1
    assert held.ledger_entries_created == 1
    lingering = held.findings_of(FindingKind.CONTROL_RECORD_RETIRED)
    assert lingering and "not released its name" in lingering[0].detail
    assert stack.storage.exists(TARGET)  # type: ignore[attr-defined]
    retry = _manager(stack).retire_control_record(TARGET)
    assert retry.records_discarded == 1
    assert retry.ledger_entries_created == 0, (
        "the retry recognises the entry it already wrote"
    )
    assert not stack.storage.exists(TARGET)  # type: ignore[attr-defined]
    assert len(stack.ledger.entries()) == 1
    retried = retry.findings_of(FindingKind.CONTROL_RECORD_RETIRED)
    assert retried and stack.quarantine.read(retried[0].quarantine) == DAMAGED


def test_the_engine_lease_section_mirrors_the_adapters(stack: Stack) -> None:
    """G2 forbids the import, so the mirrored constant is pinned equal here instead."""
    from okto_grafx.adapters.coordination_local import LEASE_SECTION
    from okto_grafx.engine.recovery_manager import _LEASE_SECTION  # noqa: PLC2701

    assert _LEASE_SECTION == LEASE_SECTION


def test_a_canonical_reader_record_is_fail_closed_with_nothing_read(
    stack: Stack,
) -> None:
    """No shared section, no atomic compare-and-remove: reader retirement refuses typed,
    before the probe runs, and the record keeps every byte."""
    _seed(stack, READER)
    probe = _DamageProbe()
    with pytest.raises(GrafxRecoveryRefused, match="FAIL-CLOSED"):
        _manager(stack, control_probe=probe).retire_control_record(READER)
    assert probe.calls == 0
    assert _bytes_of(stack, READER) == DAMAGED
    assert len(stack.ledger.entries()) == 0


def test_the_last_look_and_removal_happen_inside_the_lease_section(
    stack: Stack,
) -> None:
    """The deterministic pin on the integrator's race: at the exact post-compare window the
    manager holds commit AND lease sections, nested in that audited order, so a cooperating
    lease publisher (which serialises on the lease section) cannot land there."""
    from .conftest import StackCoordinator

    coordinator = StackCoordinator()
    _seed(stack, LEASE)
    observed: list[tuple[str, ...]] = []

    def strike() -> None:
        held = [name for kind, name, _ in coordinator.sections if kind == "enter"]
        left = [name for kind, name, _ in coordinator.sections if kind == "exit"]
        for name in left:
            held.remove(name)
        observed.append(tuple(held))

    window = _WindowStorage(stack.storage, LEASE, at_read=3, strike=strike)
    report = _manager(
        stack, storage=window, coordinator=coordinator
    ).retire_control_record(LEASE)
    assert report.records_discarded == 1
    assert observed == [("commit", "writer.lease")], (
        "the last look must run holding commit THEN lease, and nothing else"
    )
    entered = [name for kind, name, _ in coordinator.sections if kind == "enter"]
    assert entered == ["commit", "writer.lease"], "lock order is commit before lease"


def test_a_live_holder_of_the_lease_section_keeps_retirement_out(
    stack: Stack, tmp_path: Path
) -> None:
    """Cross-process: the REAL lease section held elsewhere refuses the lease retirement
    typed, with the record intact and no removal; the release lets it complete."""
    _seed(stack, LEASE)
    root = tmp_path / "fence"
    root.mkdir()
    ready = tmp_path / "ready.marker"
    release = tmp_path / "release.marker"
    coordinator = LocalProcessCoordinator(
        LocalStorageDevice(str(root)),
        SystemClock(),
        owner_id="lease-retire-parent",
        lock_directory=str(root / "control"),
        poll_interval=0.002,
    )
    probe = _DamageProbe()
    quarantine_before = _quarantine_files(stack)
    manager = _manager(
        stack, control_probe=probe, coordinator=coordinator, commit_lock_timeout=0.4
    )
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("fence_child.py")),
            str(root),
            str(ready),
            str(release),
            "writer.lease",
        ],
    )
    try:
        deadline = time.monotonic() + 30.0
        while not ready.exists() and time.monotonic() < deadline:
            assert child.poll() is None, (
                "the section-holding child died before announcing"
            )
            time.sleep(0.005)
        assert ready.exists(), "the child never announced the held lease section"
        with pytest.raises(GrafxLeaseTimeout):
            manager.retire_control_record(LEASE)
        assert probe.calls == 0, "the lease-section timeout must precede the probe"
        assert _bytes_of(stack, LEASE) == DAMAGED
        assert len(stack.ledger.entries()) == 0
        assert _quarantine_files(stack) == quarantine_before
    finally:
        release.write_text("go", encoding="ascii")
        child.wait(timeout=30.0)
    report = manager.retire_control_record(LEASE)
    assert report.records_discarded == 1
    assert not stack.storage.exists(LEASE)  # type: ignore[attr-defined]


def test_no_adapter_path_ever_opens_the_commit_section() -> None:
    """The audited lock order (commit, then lease) holds because the coordination adapter
    never opens the commit section at all: it takes the lease section ALONE. Pinned against
    the adapter's source, so a future lease->commit nesting fails here before it deadlocks."""
    import inspect

    from okto_grafx.adapters import coordination_local

    source = Path(inspect.getsourcefile(coordination_local)).read_text(encoding="utf-8")  # type: ignore[arg-type]
    assert "COMMIT_SECTION" not in source
    assert 'exclusive("commit"' not in source
    assert "exclusive('commit'" not in source


class _GiganticSizeStorage:
    """A device that declares an absurd size for the target and refuses to be read from it."""

    def __init__(self, inner: object, name: str) -> None:
        self._inner = inner
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        """Everything else is answered by the real device."""
        return getattr(self._inner, attr)

    def log_size(self, file: str) -> int:
        """Claim a gigantic size for the target."""
        if file == self._name:
            return 10**9
        return self._inner.log_size(file)  # type: ignore[attr-defined]

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Refuse loudly if the door tries to read the oversized target anyway."""
        assert file != self._name, "an oversized control record must never be read"
        return self._inner.read_log(file, offset, length)  # type: ignore[attr-defined]


def test_an_oversized_control_record_refuses_before_the_first_read(
    stack: Stack,
) -> None:
    """A size past the cap is evidence enough: zero reads, zero evidence, zero removal."""
    _seed(stack, LEASE)
    quarantine_before = _quarantine_files(stack)
    probe = _DamageProbe()
    huge = _GiganticSizeStorage(stack.storage, LEASE)
    with pytest.raises(GrafxRecoveryRefused, match="claims") as caught:
        _manager(stack, storage=huge, control_probe=probe).retire_control_record(LEASE)
    assert caught.value.details["length"] == 10**9
    assert probe.calls == 0
    assert len(stack.ledger.entries()) == 0
    assert _quarantine_files(stack) == quarantine_before
    assert _bytes_of(stack, LEASE) == DAMAGED


def test_a_real_lease_written_by_the_adapter_fits_the_cap(tmp_path: Path) -> None:
    """The cap can never refuse a legitimate lease: the production adapter's own record,
    written end to end, stays a fraction of one page."""
    from okto_grafx.engine.recovery_manager import _MAX_CONTROL_RECORD_BYTES  # noqa: PLC2701

    root = tmp_path / "capdb"
    root.mkdir()
    coordinator = LocalProcessCoordinator(
        LocalStorageDevice(str(root)),
        SystemClock(),
        owner_id="cap-check",
        lock_directory=str(root / "control"),
        poll_interval=0.002,
    )
    coordinator.acquire_writer_lease(timeout=5.0)
    lease_file = root / "control" / "writer.lease"
    assert lease_file.exists()
    assert lease_file.stat().st_size <= _MAX_CONTROL_RECORD_BYTES // 4


class _SizeSequenceStorage:
    """A device whose target size mutates at an exact observation, never to be read again."""

    def __init__(self, inner: object, name: str, huge_at: int) -> None:
        self._inner = inner
        self._name = name
        self._huge_at = huge_at
        self.size_calls = 0
        self.reads = 0

    def __getattr__(self, attr: str) -> Any:
        """Everything else is answered by the real device."""
        return getattr(self._inner, attr)

    def log_size(self, file: str) -> int:
        """Answer the real size until the arranged observation, then claim a gigantic one."""
        if file == self._name:
            self.size_calls += 1
            if self.size_calls >= self._huge_at:
                return 10**9
        return self._inner.log_size(file)  # type: ignore[attr-defined]

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Count reads of the target and refuse loudly past the cap."""
        if file == self._name:
            self.reads += 1
            assert length <= 10**6, "an oversized generation must never be read"
        return self._inner.read_log(file, offset, length)  # type: ignore[attr-defined]


def test_an_oversized_replacement_at_the_confirmation_refuses_with_nothing_captured(
    stack: Stack,
) -> None:
    """Size observation 2 is the confirmation re-read: a gigantic claim there refuses
    retryably with exactly one read ever issued and no evidence persisted."""
    _seed(stack, LEASE)
    quarantine_before = _quarantine_files(stack)
    swap = _SizeSequenceStorage(stack.storage, LEASE, huge_at=2)
    with pytest.raises(GrafxRecoveryRefused, match="now claims") as caught:
        _manager(stack, storage=swap).retire_control_record(LEASE)
    assert caught.value.details["length"] == 10**9
    assert swap.reads == 1, "the confirmation must refuse on size, before its read"
    assert len(stack.ledger.entries()) == 0
    assert _quarantine_files(stack) == quarantine_before
    assert _bytes_of(stack, LEASE) == DAMAGED


def test_an_oversized_replacement_at_the_last_look_keeps_the_evidence_and_removes_nothing(
    stack: Stack,
) -> None:
    """Size observation 3 is the last look: a gigantic claim there refuses retryably with
    no third read, the quarantine and ledger entries intact, and the name untouched."""
    _seed(stack, LEASE)
    swap = _SizeSequenceStorage(stack.storage, LEASE, huge_at=3)
    with pytest.raises(GrafxRecoveryRefused, match="now claims"):
        _manager(stack, storage=swap).retire_control_record(LEASE)
    assert swap.reads == 2, "the last look must refuse on size, before its read"
    assert len(stack.ledger.entries()) == 1, "the forensic evidence legitimately stays"
    assert stack.storage.exists(LEASE)  # type: ignore[attr-defined]
    assert _bytes_of(stack, LEASE) == DAMAGED
    quarantined = _quarantine_files(stack)
    assert quarantined, "the inspected generation stays preserved in quarantine"
