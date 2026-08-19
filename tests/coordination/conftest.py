"""Fixtures for the process-coordinator suite (C3).

Every coordinator built here gets its own clock with its own origin and its own device object,
because that is what two processes actually look like: nothing they share carries a monotonic
reading. The sleeper is the fake clock, so a wait of ten seconds costs nothing and the whole
suite stays deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from coordination_support import DirectoryStorageDevice, ManualClock, RecordingMetricsSink
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator

CoordinatorFactory = Callable[..., LocalProcessCoordinator]

DEFAULT_TTL: float = 5.0
"""Lease time to live used across the suite."""

DEFAULT_STALL: float = 5.0
"""Owner stall threshold used across the suite."""


@pytest.fixture
def database_root(tmp_path: Path) -> Path:
    """Return the directory that stands in for one database."""
    root = tmp_path / "db"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def lock_directory(database_root: Path) -> str:
    """Return the real directory that holds the advisory lock files."""
    return str(database_root / "control")


@pytest.fixture
def clock() -> ManualClock:
    """Return a manual clock for the first participant."""
    return ManualClock(monotonic=1_000.0)


@pytest.fixture
def metrics() -> RecordingMetricsSink:
    """Return a metrics sink that records every observation."""
    return RecordingMetricsSink()


@pytest.fixture
def make_coordinator(database_root: Path, lock_directory: str) -> CoordinatorFactory:
    """Return a factory that builds a coordinator over the shared database directory."""

    def build(
        *,
        owner_id: str,
        clock: ManualClock | None = None,
        storage: object | None = None,
        metrics: RecordingMetricsSink | None = None,
        monotonic_origin: float = 1_000.0,
        use_lock_directory: bool = True,
        **overrides: object,
    ) -> LocalProcessCoordinator:
        participant_clock = ManualClock(monotonic=monotonic_origin) if clock is None else clock
        device = DirectoryStorageDevice(database_root) if storage is None else storage
        settings: dict[str, object] = {
            "owner_id": owner_id,
            "lock_directory": lock_directory if use_lock_directory else None,
            "ttl_seconds": DEFAULT_TTL,
            "owner_stall_threshold": DEFAULT_STALL,
            "reader_stall_threshold": 15.0,
            "poll_interval": 0.01,
            "sleeper": participant_clock.sleep,
            "metrics": metrics,
        }
        settings.update(overrides)
        return LocalProcessCoordinator(device, participant_clock, **settings)  # type: ignore[arg-type]

    return build
