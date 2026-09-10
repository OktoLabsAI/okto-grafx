"""Cooperative, read-only execution control; never a durable-commit interruption."""

from __future__ import annotations

import math

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxQueryCancelled,
    GrafxQueryDeadlineExceeded,
)
from okto_grafx.domain.ports.clock import Clock

__all__ = ["CancellationToken"]


class CancellationToken:
    """A one-way cancellation signal, shareable by threads but not across processes.

    Calling cancel does not close a cursor from the calling thread. The consuming thread
    observes the signal at a cooperative check and releases its own resources.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation idempotently; a cancelled token cannot be reset."""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._cancelled


class _ReadControl:
    """One execution deadline, shared by all operators of that read only."""

    __slots__ = ("_clock", "_deadline", "_token", "_steps")

    def __init__(
        self,
        clock: Clock,
        timeout_seconds: float | None,
        cancellation: CancellationToken | None,
    ) -> None:
        if cancellation is not None and type(cancellation) is not CancellationToken:
            raise GrafxConfigurationError(
                "cancellation must be an exact CancellationToken.", field="cancellation"
            )
        if timeout_seconds is not None and (
            type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds < float("inf")
        ):
            raise GrafxConfigurationError(
                "timeout_seconds must be finite and positive.", field="timeout_seconds"
            )
        self._clock = clock
        self._token = cancellation
        try:
            self._deadline = (
                None if timeout_seconds is None else clock.monotonic() + timeout_seconds
            )
        except OverflowError as failure:
            raise GrafxConfigurationError(
                "The query deadline is outside the clock's numeric range.",
                field="timeout_seconds",
            ) from failure
        if self._deadline is not None and not math.isfinite(self._deadline):
            raise GrafxConfigurationError(
                "The query deadline is not finite.", field="timeout_seconds"
            )
        self._steps = 0

    def check(self) -> None:
        """Refuse a cancelled or expired read at a safe execution boundary."""
        if self._token is not None and self._token._cancelled:
            raise GrafxQueryCancelled("Read execution was cancelled.")
        if self._deadline is not None and self._clock.monotonic() >= self._deadline:
            raise GrafxQueryDeadlineExceeded("Read execution exceeded its deadline.")

    def step(self) -> None:
        """Amortize clock checks across at most 64 operator work observations."""
        self._steps += 1
        if self._steps >= 64:
            self._steps = 0
            self.check()


def _read_control(
    clock: Clock, timeout_seconds: float | None, cancellation: CancellationToken | None
) -> _ReadControl | None:
    """Avoid a control allocation and clock reads on the default execution path."""
    if timeout_seconds is None and cancellation is None:
        return None
    control = _ReadControl(clock, timeout_seconds, cancellation)
    control.check()
    return control
