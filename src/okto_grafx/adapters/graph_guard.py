"""The condition guard injected into waitable engine state machines, built on ``threading``.

The vector and buffer protocols ask for a context manager that can wait and wake, plus the identity
of the calling thread -- the one thing a bare ``threading.Condition`` cannot say, and the one thing
that tells an operation's own re-entrant call apart from another thread's work. This is mechanism,
so it lives with the adapters and reaches the engine by construction (CF-13): the pure core imports
no ``threading``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

__all__ = ["ConditionGuard"]


class ConditionGuard:
    """A ``threading.Condition`` that also knows which thread is asking."""

    __slots__ = ("_condition",)

    def __init__(self) -> None:
        """Build the condition over its own lock."""
        self._condition = threading.Condition()

    def __enter__(self) -> ConditionGuard:
        """Take the lock behind the condition."""
        self._condition.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """Release the lock, whether or not the body raised."""
        self._condition.release()

    def wait_for(
        self, predicate: Callable[[], bool], timeout: float | None = None
    ) -> bool:
        """Release the lock while waiting for the predicate, up to the timeout; return its value."""
        return bool(self._condition.wait_for(predicate, timeout))

    def notify_all(self) -> None:
        """Wake every thread waiting on this guard."""
        self._condition.notify_all()

    def thread_token(self) -> int:
        """Return a token that identifies the calling thread for as long as it lives."""
        return threading.get_ident()
