"""ContextVar-backed transport for engine-owned, explicitly revoked scope payloads."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class ContextLocalValue:
    """One independently owned execution-context slot; no module-global binding."""

    __slots__ = ("_value",)

    def __init__(self, name: str) -> None:
        self._value: ContextVar[object | None] = ContextVar(name, default=None)

    def get(self) -> object | None:
        """Return the value in this thread/task's current execution context."""
        return self._value.get()

    @contextmanager
    def bind(self, value: object) -> Iterator[None]:
        """Restore the previous token even when the body raises BaseException."""
        token = self._value.set(value)
        try:
            yield
        finally:
            self._value.reset(token)

__all__ = ["ContextLocalValue"]
