"""``python -m okto_grafx.cli`` (C12).

This module exists so the operator surface is reachable from a plain interpreter, with no install
step and no console script: ``python -m okto_grafx.cli verify ./mydb`` works from a checkout.

It does one thing :func:`okto_grafx.cli.entry.main` deliberately does not, because it owns the
process and ``main`` does not: it makes sure the interpreter's own flush at shutdown cannot print
a ``BrokenPipeError`` after the command has already answered. Piping this tool into ``head`` is
an ordinary thing for an operator to do, and a command that reports its answer and then prints an
exception underneath it has not really answered.
"""

from __future__ import annotations

import os
import sys

from okto_grafx.cli.entry import main

__all__ = ["run"]


def run() -> int:
    """Run the command line of this process and return its exit code, absorbing a closed pipe."""
    code = main()
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        _silence_standard_output()
    return code


def _silence_standard_output() -> None:
    """Point standard output at the null device so the shutdown flush has nothing to fail on."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except (OSError, ValueError):
        return
    try:
        sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):
        os.close(devnull)
        return
    try:
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass
    finally:
        try:
            os.close(devnull)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(run())
