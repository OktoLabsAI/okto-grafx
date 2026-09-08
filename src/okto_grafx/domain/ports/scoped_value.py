"""Execution-local state supplied by an outer composition, never process-global authority."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol


class ScopedValue(Protocol):
    """Isolate bindings by execution context and restore nested bindings on every exit.

    Scope payloads and their revocation belong to the engine. Implementations only
    transport a value; a copied context must keep the same payload identity so the
    engine's revocation is visible in delayed copies too.
    """

    def get(self) -> object | None:
        """Return the current binding or None outside a bound scope."""
        ...

    def bind(self, value: object) -> AbstractContextManager[None]:
        """Temporarily bind a value, restoring the exact previous binding on exit."""
        ...

__all__ = ["ScopedValue"]
