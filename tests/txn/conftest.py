"""Fixtures for the transaction-manager suite (C5).

Every stack is built over its own directory, because the properties under test are cross-process
properties and a memory device shared by two managers would prove nothing about a second
process. The clock is manual so lease and reader scheduling cost no real time and the suite stays
deterministic.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:  # a spawned child inherits sys.path and imports the helper by name
    sys.path.insert(0, HERE)

from txn_support import ManualClock, RecordingMetricsSink, Stack, build_stack  # noqa: E402

StackFactory = Callable[..., Stack]


@pytest.fixture
def database_root(tmp_path: Path) -> Path:
    """Return the directory that stands in for one database."""
    root = tmp_path / "db"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def make_stack(database_root: Path) -> StackFactory:
    """Return a factory that builds another participant over the shared database directory."""
    built: list[Stack] = []

    def build(**overrides: object) -> Stack:
        overrides.setdefault("owner_id", f"participant-{len(built) + 1}")
        stack = build_stack(database_root, **overrides)  # type: ignore[arg-type]
        built.append(stack)
        return stack

    return build


@pytest.fixture
def stack(make_stack: StackFactory) -> Stack:
    """Return one participant over a fresh database."""
    return make_stack()


@pytest.fixture
def clock() -> ManualClock:
    """Return a manual clock a test can move by hand."""
    return ManualClock()


@pytest.fixture
def metrics() -> RecordingMetricsSink:
    """Return a metrics sink that records every emission."""
    return RecordingMetricsSink()
