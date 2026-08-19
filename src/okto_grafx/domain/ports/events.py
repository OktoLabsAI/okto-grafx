"""The event port (CONTRACT.md section 4.7).

Structured facts the engine wants an operator to see, separated from metrics because an event
carries a payload while a metric carries a number.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

__all__ = ["EventSink"]


@runtime_checkable
class EventSink(Protocol):
    """Destination for structured, en-US operational events."""

    def emit(self, event: str, payload: Mapping[str, object]) -> None:
        """Publish one named event with its structured payload."""
        ...
