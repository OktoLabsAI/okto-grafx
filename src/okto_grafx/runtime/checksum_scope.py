"""Execution-local checksum selection; no lock is held during database operations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from okto_grafx.domain.page import checksum
from okto_grafx.runtime.scoped_value import ContextLocalValue

__all__ = ["capture_checksum", "checksum_scope"]

_scope = ContextLocalValue("okto_grafx_checksum")
checksum._execution_scope = _scope


def capture_checksum(install: Callable[[], object]) -> tuple[Callable[[bytes, int], int], str]:
    """Run the unchanged validating installer without changing the standalone default."""
    selection = checksum._ChecksumSelection()
    with _scope.bind(selection):
        install()
    return selection.state


@contextmanager
def checksum_scope(state: tuple[Callable[[bytes, int], int], str]) -> Iterator[None]:
    """Restore the previous database's provider after nested calls or any exception."""
    with _scope.bind(state):
        yield
