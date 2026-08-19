"""The system clock adapter satisfies the Clock port and keeps its two contracts apart (TR-2)."""

from __future__ import annotations

import time

from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.domain.ports.clock import Clock


def test_the_system_clock_satisfies_the_port() -> None:
    assert isinstance(SystemClock(), Clock)


def test_the_monotonic_reading_never_goes_backwards() -> None:
    clock = SystemClock()
    readings = [clock.monotonic() for _ in range(64)]
    assert readings == sorted(readings)


def test_the_wall_reading_is_unix_epoch_seconds() -> None:
    clock = SystemClock()
    before = time.time()
    reading = clock.wall()
    after = time.time()
    assert before <= reading <= after


def test_the_two_readings_come_from_different_sources() -> None:
    # A monotonic reading is a local counter with an arbitrary origin; a wall reading is epoch
    # seconds. Confusing them is the bug FR-7 exists to prevent, so they must not be equal.
    clock = SystemClock()
    assert abs(clock.wall() - clock.monotonic()) > 1_000_000.0


def test_the_adapter_has_no_instance_state() -> None:
    clock = SystemClock()
    assert not hasattr(clock, "__dict__")
    assert repr(clock) == "SystemClock()"
