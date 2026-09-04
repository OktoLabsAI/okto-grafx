"""D-26 measurements of the write-commit exclusive windows.

The assertions pin three properties rather than wall-clock speed: the phase observations cover
the commit-section hold exactly, all sink callbacks happen after coordination is released, and
the default disabled path does not even construct a trace collector.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from threading import Event, Thread

import pytest

import okto_grafx.engine.txn_manager as txn_manager_module
from okto_grafx.engine.txn_manager import (
    COMMIT_SECTION,
    COMMIT_FLUSHES_TOTAL,
    COMMIT_FOREIGN_COMMITS_TOTAL,
    COMMIT_FRAMES_EXAMINED_TOTAL,
    COMMIT_PAGES_LOGGED_TOTAL,
    COMMIT_PHASE_DURATION_SECONDS,
    COMMIT_RETARGETS_TOTAL,
    COMMIT_WAL_BYTES_TOTAL,
    COMMIT_WINDOW_DURATION_SECONDS,
    TransactionManager,
)
from okto_grafx.domain.errors import GrafxLeaseTimeout
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.catalog import (
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.txn import WalRecord, WalRecordType
from okto_grafx.domain.wal.commit import CommitPayload
from okto_grafx.engine.buffer_pool import BufferPool, _BufferWorkProbe
from okto_grafx.engine.index_manager import HashIndex, IndexManager
from okto_grafx.engine.wal_manager import WalManager
from txn_support import (
    DEFAULT_PAGE_SIZE,
    ManualClock,
    RecordingMetricsSink,
    TracingCoordinator,
    build_stack,
    make_page_image,
)

HEAP = "heap.dat"
SEGMENT_BYTES = 4096


def _real_wal(device: object, clock: object, metrics: object) -> WalManager:
    """Build the production segmented WAL so byte metrics include implicit headers."""
    manager = WalManager(
        device,  # type: ignore[arg-type]
        clock,  # type: ignore[arg-type]
        metrics,  # type: ignore[arg-type]
        segment_bytes=SEGMENT_BYTES,
        descriptor="hash-v1;partitions_per_table=8",
    )
    manager.open()
    return manager


class StepClock(ManualClock):
    """Advance one millisecond on every observation for deterministic duration assertions."""

    def monotonic(self) -> float:
        self.advance(0.001)
        return super().monotonic()


class CountingClock(ManualClock):
    """Expose whether enabling D-26 adds calls to the host Clock port."""

    def __init__(self) -> None:
        super().__init__()
        self.monotonic_calls = 0
        self.wall_calls = 0

    def monotonic(self) -> float:
        self.monotonic_calls += 1
        return super().monotonic()

    def wall(self) -> float:
        self.wall_calls += 1
        return super().wall()

    def reset_calls(self) -> None:
        """Start a fresh observation window after stack assembly."""
        self.monotonic_calls = 0
        self.wall_calls = 0


class OrderedMetrics(RecordingMetricsSink):
    """Record where commit telemetry occurs in the coordinator trail."""

    def __init__(self, trail: list[str]) -> None:
        super().__init__()
        self._trail = trail

    def increment(
        self,
        name: str,
        value: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if name.startswith("oktografx_commit_"):
            self._trail.append(f"metric:{name}")
        super().increment(name, value, labels)

    def observe(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if name.startswith("oktografx_commit_"):
            self._trail.append(f"metric:{name}")
        super().observe(name, value, labels)


class HostileCommitMetrics(RecordingMetricsSink):
    """Raise from every new commit-metric callback and remember each attempt."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[str] = []

    def increment(
        self,
        name: str,
        value: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if name in {
            COMMIT_FLUSHES_TOTAL,
            COMMIT_FOREIGN_COMMITS_TOTAL,
            COMMIT_FRAMES_EXAMINED_TOTAL,
            COMMIT_PAGES_LOGGED_TOTAL,
            COMMIT_RETARGETS_TOTAL,
            COMMIT_WAL_BYTES_TOTAL,
        }:
            self.attempts.append(name)
            raise KeyboardInterrupt("hostile commit counter")
        super().increment(name, value, labels)

    def observe(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if name in {COMMIT_WINDOW_DURATION_SECONDS, COMMIT_PHASE_DURATION_SECONDS}:
            self.attempts.append(name)
            raise KeyboardInterrupt("hostile commit histogram")
        super().observe(name, value, labels)


class BlockingFirstCommitMetric(RecordingMetricsSink):
    """Hold only the first D-26 emission so another commit can enter concurrently."""

    def __init__(self, entered: Event, release: Event) -> None:
        super().__init__()
        self._entered = entered
        self._release = release
        self._blocked = False

    def observe(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if name == COMMIT_WINDOW_DURATION_SECONDS and not self._blocked:
            self._blocked = True
            self._entered.set()
            if not self._release.wait(5.0):
                raise AssertionError(
                    "the test did not release the first metric callback"
                )
        super().observe(name, value, labels)


class _ExitBeforeRelease:
    """Raise before delegating exit, retaining the exact boundary for test cleanup."""

    def __init__(self, boundary: object, failure: BaseException) -> None:
        self._boundary = boundary
        self._failure = failure
        self.held = False

    def __enter__(self) -> object:
        entered = self._boundary.__enter__()  # type: ignore[attr-defined]
        self.held = True
        return entered

    def __exit__(self, kind: object, value: object, trace: object) -> None:
        del kind, value, trace
        raise self._failure

    def release(self) -> None:
        self._boundary.__exit__(None, None, None)  # type: ignore[attr-defined]
        self.held = False


class _CommitExitFailureCoordinator:
    """Delegate all coordination except the first commit-section release."""

    def __init__(self, inner: object, failure: BaseException) -> None:
        self._inner = inner
        self._failure = failure
        self.boundary: _ExitBeforeRelease | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def exclusive(self, name: str, *, timeout: float) -> object:
        boundary = self._inner.exclusive(name, timeout=timeout)  # type: ignore[attr-defined]
        if name == COMMIT_SECTION and self.boundary is None:
            self.boundary = _ExitBeforeRelease(boundary, self._failure)
            return self.boundary
        return boundary


class _CommitEnterFailureCoordinator:
    """Return one selected acquisition failure only for the commit section."""

    def __init__(self, inner: object, failure: BaseException) -> None:
        self._inner = inner
        self._failure = failure

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def exclusive(self, name: str, *, timeout: float) -> object:
        if name != COMMIT_SECTION:
            return self._inner.exclusive(name, timeout=timeout)  # type: ignore[attr-defined]
        failure = self._failure

        class _TimedOut:
            def __enter__(self) -> None:
                raise failure

            def __exit__(self, kind: object, value: object, trace: object) -> None:
                del kind, value, trace

        return _TimedOut()


class _LeaseReleaseFailureCoordinator:
    """Keep the physical lease held while reporting a release failure once."""

    def __init__(self, inner: object, failure: BaseException) -> None:
        self._inner = inner
        self._failure = failure
        self.lease: object = None
        self.held = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def acquire_writer_lease(self, *, timeout: float) -> object:
        lease = self._inner.acquire_writer_lease(timeout=timeout)  # type: ignore[attr-defined]
        self.lease = lease
        self.held = True
        return lease

    def release_lease(self, lease: object) -> None:
        assert lease is self.lease
        raise self._failure

    def release(self) -> None:
        assert self.lease is not None
        self._inner.release_lease(self.lease)  # type: ignore[attr-defined]
        self.held = False


def _stage_page(manager: TransactionManager, codec: object) -> object:
    txn = manager.begin("write")
    txn.owner._stage_page_image(
        txn,
        HEAP,
        3,
        make_page_image(codec, [b"measured"], page_index=3),  # type: ignore[arg-type]
    )
    txn.note_write(manager.partition_of(1, b"measured"))
    return txn


def test_commit_metrics_cover_the_section_and_emit_after_release(
    tmp_path: Path,
) -> None:
    trail: list[str] = []
    metrics = OrderedMetrics(trail)
    clock = StepClock()
    stack = build_stack(
        tmp_path,
        metrics=metrics,
        clock=clock,
        wal_factory=_real_wal,
    )
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        TracingCoordinator(stack.coordinator, trail),
        clock,
        metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )

    trail.clear()
    wal_bytes_before = stack.wal.total_bytes()
    manager.commit(_stage_page(manager, stack.codec))
    wal_bytes_after = stack.wal.total_bytes()

    first_metric = next(
        index for index, item in enumerate(trail) if item.startswith("metric:")
    )
    assert first_metric > trail.index("leave:commit")
    assert first_metric > trail.index("release_lease")

    window_samples = [
        (labels["window"], labels["interval"], seconds)
        for name, seconds, labels in metrics.observations
        if name == COMMIT_WINDOW_DURATION_SECONDS
    ]
    assert len(window_samples) == 4
    assert len({(window, interval) for window, interval, _ in window_samples}) == 4
    windows = {
        (labels["window"], labels["interval"]): seconds
        for name, seconds, labels in metrics.observations
        if name == COMMIT_WINDOW_DURATION_SECONDS
    }
    assert set(windows) == {
        ("writer_lease", "wait"),
        ("writer_lease", "hold"),
        ("commit_section", "wait"),
        ("commit_section", "hold"),
    }
    phase_samples = [
        (labels["phase"], seconds)
        for name, seconds, labels in metrics.observations
        if name == COMMIT_PHASE_DURATION_SECONDS
    ]
    assert len(phase_samples) == 10
    assert len({phase for phase, _ in phase_samples}) == 10
    phases = {
        labels["phase"]: seconds
        for name, seconds, labels in metrics.observations
        if name == COMMIT_PHASE_DURATION_SECONDS
    }
    assert set(phases) == {
        "other",
        "occ",
        "materialize",
        "build_records",
        "append",
        "barrier",
        "apply",
        "flush",
        "index",
        "publish",
    }
    assert sum(phases.values()) == pytest.approx(windows[("commit_section", "hold")])

    totals = metrics.snapshot()
    assert totals[COMMIT_PAGES_LOGGED_TOTAL] == 1.0
    assert totals[COMMIT_WAL_BYTES_TOTAL] == wal_bytes_after - wal_bytes_before
    assert totals[COMMIT_WAL_BYTES_TOTAL] > DEFAULT_PAGE_SIZE
    assert totals[COMMIT_FRAMES_EXAMINED_TOTAL] > 0.0
    assert totals[COMMIT_FLUSHES_TOTAL] == 1.0
    assert totals[COMMIT_RETARGETS_TOTAL] == 1.0


def test_disabled_commit_metrics_construct_no_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = RecordingMetricsSink(enabled=False)
    stack = build_stack(tmp_path, metrics=metrics)

    def trace_must_not_be_built(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "the disabled metrics path must not allocate a commit trace"
        )

    monkeypatch.setattr(txn_manager_module, "_CommitTrace", trace_must_not_be_built)
    report = stack.manager.commit(_stage_page(stack.manager, stack.codec))

    assert report.durable is True
    assert metrics.observations == []
    assert metrics.counters == []


def test_enabled_commit_metrics_add_no_host_clock_calls(tmp_path: Path) -> None:
    def commit_with(metrics: RecordingMetricsSink, root: Path) -> tuple[int, int]:
        clock = CountingClock()
        stack = build_stack(root, metrics=metrics, clock=clock)
        clock.reset_calls()
        stack.manager.commit(_stage_page(stack.manager, stack.codec))
        return clock.monotonic_calls, clock.wall_calls

    disabled = commit_with(
        RecordingMetricsSink(enabled=False),
        tmp_path / "disabled",
    )
    enabled = commit_with(RecordingMetricsSink(), tmp_path / "enabled")

    assert enabled == disabled


def test_failed_diagnostic_timer_drops_durations_without_changing_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics)
    txn = _stage_page(stack.manager, stack.codec)

    readings = iter((0, 1_000_000))

    def fail_after_partial_sample() -> int:
        try:
            return next(readings)
        except StopIteration:
            raise KeyboardInterrupt("diagnostic timer failed") from None

    monkeypatch.setattr(
        txn_manager_module, "perf_counter_ns", fail_after_partial_sample
    )
    report = stack.manager.commit(txn)

    assert report.durable is True
    assert not [
        item
        for item in metrics.observations
        if item[0] in {COMMIT_WINDOW_DURATION_SECONDS, COMMIT_PHASE_DURATION_SECONDS}
    ]
    assert metrics.total(COMMIT_PAGES_LOGGED_TOTAL) == 1.0


def test_failed_writer_lease_wait_is_closed_before_unwind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics)
    txn = _stage_page(stack.manager, stack.codec)
    readings = iter((0, 1_000_000, 999_000_000))
    failure = GrafxLeaseTimeout("lease acquisition timed out")

    monkeypatch.setattr(txn_manager_module, "perf_counter_ns", lambda: next(readings))

    def fail_lease(_manager: TransactionManager) -> object:
        raise failure

    monkeypatch.setattr(TransactionManager, "_hold_lease", fail_lease)
    with pytest.raises(GrafxLeaseTimeout) as escaped:
        stack.manager.commit(txn)

    assert escaped.value is failure
    waits = [
        seconds
        for name, seconds, labels in metrics.observations
        if name == COMMIT_WINDOW_DURATION_SECONDS
        and labels == {"window": "writer_lease", "interval": "wait"}
    ]
    assert waits == [pytest.approx(0.001)]


def test_unknown_writer_lease_acquisition_failure_suppresses_the_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics)
    txn = _stage_page(stack.manager, stack.codec)
    failure = RuntimeError("the port did not prove whether it granted the lease")

    def fail_lease(_manager: TransactionManager) -> object:
        raise failure

    monkeypatch.setattr(TransactionManager, "_hold_lease", fail_lease)
    with pytest.raises(RuntimeError) as escaped:
        stack.manager.commit(txn)

    assert escaped.value is failure
    emitted = {
        name
        for name, _value, _labels in (*metrics.observations, *metrics.counters)
        if name.startswith("oktografx_commit_")
    }
    assert emitted == set()


def test_commit_section_timeout_closes_its_wait_before_unwind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics)
    failure = GrafxLeaseTimeout("commit section timed out")
    coordinator = _CommitEnterFailureCoordinator(stack.coordinator, failure)
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        coordinator,  # type: ignore[arg-type]
        stack.clock,
        metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = _stage_page(manager, stack.codec)
    readings = iter(range(0, 20_000_000, 1_000_000))
    monkeypatch.setattr(txn_manager_module, "perf_counter_ns", lambda: next(readings))

    with pytest.raises(GrafxLeaseTimeout) as escaped:
        manager.commit(txn)

    assert escaped.value is failure
    waits = [
        seconds
        for name, seconds, labels in metrics.observations
        if name == COMMIT_WINDOW_DURATION_SECONDS
        and labels == {"window": "commit_section", "interval": "wait"}
    ]
    assert waits == [pytest.approx(0.001)]


def test_unknown_commit_section_acquisition_failure_suppresses_the_trace(
    tmp_path: Path,
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics)
    failure = RuntimeError(
        "the section port did not prove whether it acquired the lock"
    )
    coordinator = _CommitEnterFailureCoordinator(stack.coordinator, failure)
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        coordinator,  # type: ignore[arg-type]
        stack.clock,
        metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = _stage_page(manager, stack.codec)
    metrics.counters.clear()
    metrics.observations.clear()

    with pytest.raises(RuntimeError) as escaped:
        manager.commit(txn)

    assert escaped.value is failure
    assert not [
        name
        for name, _value, _labels in (*metrics.observations, *metrics.counters)
        if name.startswith("oktografx_commit_")
    ]


def test_failed_retarget_is_not_counted_as_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics, wal_factory=_real_wal)
    txn = _stage_page(stack.manager, stack.codec)
    failure = RuntimeError("retarget failed before changing the batch")

    def fail_retarget(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(TransactionManager, "_retarget_commit_batch", fail_retarget)
    with pytest.raises(RuntimeError) as escaped:
        stack.manager.commit(txn)

    assert escaped.value is failure
    assert metrics.total("oktografx_commit_retargets_total") == 0.0
    assert stack.wal.last_lsn == 0


def test_retained_lease_mode_emits_no_commit_trace_under_the_live_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics, retain_lease=True)

    def trace_must_not_be_built(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "there is no safe sink boundary while the lease remains retained"
        )

    monkeypatch.setattr(txn_manager_module, "_CommitTrace", trace_must_not_be_built)
    report = stack.manager.commit(_stage_page(stack.manager, stack.codec))

    assert report.durable is True
    assert stack.manager._lease_guard is not None  # noqa: SLF001 - proves the unsafe boundary
    assert stack.manager._lease_guard.released is False  # noqa: SLF001
    emitted = {
        name
        for name, _value, _labels in (*metrics.observations, *metrics.counters)
        if name.startswith("oktografx_commit_")
    }
    assert emitted == set()
    stack.manager.close()


def test_next_commit_can_measure_while_the_previous_sink_is_still_emitting(
    tmp_path: Path,
) -> None:
    entered = Event()
    release = Event()
    metrics = BlockingFirstCommitMetric(entered, release)
    stack = build_stack(tmp_path, metrics=metrics)
    outcomes: list[object] = []
    failures: list[BaseException] = []

    def commit() -> None:
        try:
            outcomes.append(
                stack.manager.commit(_stage_page(stack.manager, stack.codec))
            )
        except BaseException as failure:  # noqa: BLE001 - the test reports thread escapes
            failures.append(failure)

    first = Thread(target=commit, daemon=True)
    first.start()
    assert entered.wait(5.0), "the first commit never reached its post-release sink"

    second = Thread(target=commit, daemon=True)
    second.start()
    second.join(5.0)
    assert not second.is_alive(), (
        "the previous trace kept the participant/probe occupied"
    )

    release.set()
    first.join(5.0)
    assert not first.is_alive()
    assert failures == []
    assert len(outcomes) == 2
    assert metrics.total(COMMIT_FLUSHES_TOTAL) == 2.0


def test_uncertain_participant_release_suppresses_all_commit_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics)
    txn = _stage_page(stack.manager, stack.codec)
    failure = RuntimeError("participant exit failed before releasing")
    original = TransactionManager._participant_section
    boundaries: list[_ExitBeforeRelease] = []

    def fail_before_release(manager: TransactionManager) -> _ExitBeforeRelease:
        boundary = _ExitBeforeRelease(original(manager), failure)
        boundaries.append(boundary)
        return boundary

    monkeypatch.setattr(TransactionManager, "_participant_section", fail_before_release)
    metrics.counters.clear()
    metrics.observations.clear()
    try:
        with pytest.raises(RuntimeError) as escaped:
            stack.manager.commit(txn)
        assert escaped.value is failure
        assert len(boundaries) == 1 and boundaries[0].held
        assert txn.state.value == "committed"
        assert not [
            name
            for name, _value, _labels in (*metrics.observations, *metrics.counters)
            if name.startswith("oktografx_commit_")
        ]
    finally:
        if boundaries and boundaries[0].held:
            boundaries[0].release()


def test_uncertain_commit_section_release_suppresses_all_commit_callbacks(
    tmp_path: Path,
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics)
    failure = RuntimeError("commit exit failed before releasing")
    coordinator = _CommitExitFailureCoordinator(stack.coordinator, failure)
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        coordinator,  # type: ignore[arg-type]
        stack.clock,
        metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = _stage_page(manager, stack.codec)
    metrics.counters.clear()
    metrics.observations.clear()
    try:
        with pytest.raises(RuntimeError) as escaped:
            manager.commit(txn)
        assert escaped.value is failure
        assert coordinator.boundary is not None and coordinator.boundary.held
        assert txn.state.value == "committed"
        assert not [
            name
            for name, _value, _labels in (*metrics.observations, *metrics.counters)
            if name.startswith("oktografx_commit_")
        ]
    finally:
        if coordinator.boundary is not None and coordinator.boundary.held:
            coordinator.boundary.release()


def test_uncertain_writer_lease_release_suppresses_all_commit_callbacks(
    tmp_path: Path,
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics)
    failure = RuntimeError("lease release failed before reaching the coordinator")
    coordinator = _LeaseReleaseFailureCoordinator(stack.coordinator, failure)
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        coordinator,  # type: ignore[arg-type]
        stack.clock,
        metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = _stage_page(manager, stack.codec)
    metrics.counters.clear()
    metrics.observations.clear()
    try:
        report = manager.commit(txn)
        assert report.durable and report.wrote
        assert coordinator.held
        assert not [
            name
            for name, _value, _labels in (*metrics.observations, *metrics.counters)
            if name.startswith("oktografx_commit_")
        ]
    finally:
        if coordinator.held:
            coordinator.release()


def test_hostile_commit_sink_cannot_change_a_durable_outcome(tmp_path: Path) -> None:
    metrics = HostileCommitMetrics()
    stack = build_stack(
        tmp_path,
        metrics=metrics,
        clock=StepClock(),
        wal_factory=_real_wal,
    )

    report = stack.manager.commit(_stage_page(stack.manager, stack.codec))

    assert report.durable is True
    assert report.wrote is True
    assert COMMIT_WINDOW_DURATION_SECONDS in metrics.attempts
    assert COMMIT_PHASE_DURATION_SECONDS in metrics.attempts
    assert COMMIT_WAL_BYTES_TOTAL in metrics.attempts
    assert COMMIT_RETARGETS_TOTAL in metrics.attempts


def test_foreign_commit_completion_is_counted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics, clock=StepClock())
    observed_phases: list[str | None] = []
    original = TransactionManager._complete_committed_gap

    def observe_gap_phase(manager: TransactionManager):
        trace = manager._active_commit_trace
        observed_phases.append(None if trace is None else trace._phase)
        return original(manager)

    monkeypatch.setattr(
        TransactionManager, "_complete_committed_gap", observe_gap_phase
    )
    stack.wal.append(
        WalRecord(
            record_type=int(WalRecordType.COMMIT),
            payload=CommitPayload(snapshot_lsn=0).encode(),
            descriptor="hash-v1;partitions_per_table=8",
            epoch=1,
            txn_id=91,
        )
    )

    report = stack.manager.commit(_stage_page(stack.manager, stack.codec))

    assert report.durable is True
    assert metrics.total(COMMIT_FOREIGN_COMMITS_TOTAL) == 1.0
    assert observed_phases[0] == "other"


def test_real_index_commit_reports_the_actual_dirty_candidate_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = RecordingMetricsSink()
    stack = build_stack(tmp_path, metrics=metrics, clock=StepClock())
    indexes = IndexManager(stack.pool, stack.heap, metrics)
    generation = IndexGenerationDescriptor(
        artifact_nonce=0xD10,
        bucket_count=64,
        state=IndexGenerationState.ACTIVE,
    )
    stack.catalog.catalog.add_table(
        TableDef(
            table_id=1,
            name="person",
            kind="node",
            columns=(ColumnDef("value", ValueType.STRING, False),),
        )
    )
    logical = CatalogIndexDefinition(
        name="by_name",
        table_id=1,
        table_name="person",
        positions=(0,),
        visibility=IndexVisibility.EXACT,
        generations=(generation,),
    )
    stack.catalog.catalog.upgrade_index_catalog((logical,))
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)
    index = indexes.register(
        HashIndex(logical.runtime_definition(generation), stack.pool, metrics)
    )
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        stack.coordinator,
        stack.clock,
        metrics,
        indexes,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = _stage_page(manager, stack.codec)
    index.stage_insert(txn, b"ada", RecordRef(page=3, slot=1), 0)

    actual_flushes = 0
    actual_candidates = 0
    original_flush = BufferPool.flush
    original_modified = BufferPool.modified_pages

    def count_flush(pool: BufferPool, file: str | None = None) -> int:
        nonlocal actual_flushes, actual_candidates
        if pool is stack.pool:
            actual_flushes += 1
            actual_candidates += len(pool._dirty_candidate_keys(file))  # noqa: SLF001
        return original_flush(pool, file)

    def count_modified(
        pool: BufferPool, file: str | None = None
    ) -> frozenset[tuple[str, int]]:
        nonlocal actual_candidates
        if pool is stack.pool:
            actual_candidates += len(  # noqa: SLF001
                pool._dirty_candidate_keys(file)  # noqa: SLF001
            )
        return original_modified(pool, file)

    monkeypatch.setattr(BufferPool, "flush", count_flush)
    monkeypatch.setattr(BufferPool, "modified_pages", count_modified)
    metrics.counters.clear()
    manager.commit(txn)

    assert actual_flushes == 3
    assert metrics.total(COMMIT_FLUSHES_TOTAL) == float(actual_flushes)
    assert metrics.total(COMMIT_FRAMES_EXAMINED_TOTAL) == float(actual_candidates)


def test_dirty_page_probe_counts_early_exit_and_retired_pinned_frames(
    tmp_path: Path,
) -> None:
    stack = build_stack(tmp_path)
    held = stack.pool.pin(HEAP, 0)
    assert not held.dirty
    assert stack.pool.discard_clean_page(HEAP, 0)
    probe = _BufferWorkProbe()
    assert stack.pool._attach_work_probe(probe)  # noqa: SLF001 - D-26 probe contract
    try:
        held.dirty = True
        before_match = probe.frames_examined
        expected_match = 1
        assert stack.pool.has_dirty_pages(HEAP)
        assert probe.frames_examined - before_match == expected_match

        before_miss = probe.frames_examined
        expected_miss = 0
        assert not stack.pool.has_dirty_pages("another.dat")
        assert probe.frames_examined - before_miss == expected_miss
    finally:
        held.dirty = False
        stack.pool._detach_work_probe(probe)  # noqa: SLF001 - D-26 probe contract
        stack.pool.unpin(HEAP, 0, page=held)
