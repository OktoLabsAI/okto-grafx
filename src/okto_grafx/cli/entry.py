"""The one boundary between an operator's terminal and the rest of Okto Grafx (C12).

Everything below this module answers with a value. This module is where a value becomes a process
status, and it is therefore the only place where a failure can still turn into something an
operator should never see.

**No traceback leaves here, for any input.** A malformed argument, a missing path, a database
another process is writing, a database whose bytes are damaged, an interrupt at the wrong moment
-- each one comes back as an en-US line and a documented exit code. That is not politeness: this
tool is what someone reaches for at three in the morning when a database is already in trouble,
and a stack trace at that moment costs the one thing they do not have.

**The exit code and the printed word come from the same decision.** A command builds one
:class:`~okto_grafx.cli.commands.Report`, and the machine-readable ``result`` and ``exit_code``
fields are derived here from that report's code rather than written beside it, so the document a
script parses and the number its shell reads cannot say different things about one run.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import IO

from okto_grafx.cli.commands import Report, run
from okto_grafx.cli.exits import (
    INTERNAL,
    INTERRUPTED,
    USAGE,
    result_word,
)
from okto_grafx.cli.output import Writer, describe, render_json
from okto_grafx.cli.parser import Invocation, ParseOutcome, parse, wants_machine_output
from okto_grafx.domain.errors import GrafxConfigurationError

__all__ = ["main"]

_INTERRUPT_MESSAGE: str = (
    "Interrupted by the operator. Nothing was left half-written: every command of this tool "
    "either finished its work or did none of it."
)
"""What an interrupt reports. Accurate because no command here writes to a database."""

_INTERNAL_PREFIX: str = (
    "An unexpected failure was contained here so that no traceback reaches this terminal"
)
"""How a contained defect introduces itself, before naming what it was."""


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run one ``oktografx`` command line and return its exit code.

    ``argv`` is the arguments after the program name, defaulting to this process's own. ``stdout``
    and ``stderr`` are the streams to write to, defaulting to this process's own, so a caller --
    a test, or a host embedding this tool -- can capture the output without touching ``sys``.

    This function never raises and never exits the process. Every documented exit code of
    :mod:`okto_grafx.cli.exits` can come out of it, and nothing else can.
    """
    out = Writer(stdout if stdout is not None else sys.stdout)
    err = Writer(stderr if stderr is not None else sys.stderr)
    try:
        arguments = _arguments(argv)
    except GrafxConfigurationError as failure:
        return _finish(out, err, False, USAGE, failure.message)
    machine = wants_machine_output(arguments)
    try:
        return _dispatch(arguments, out, err, machine)
    except KeyboardInterrupt:
        return _finish(out, err, machine, INTERRUPTED, _INTERRUPT_MESSAGE)
    except SystemExit as request:
        # Nothing in this package raises SystemExit. If a stream or an adapter ever does, an
        # operator still gets a sentence rather than an unexplained status.
        code = request.code
        return _finish(
            out,
            err,
            machine,
            code if isinstance(code, int) and not isinstance(code, bool) else INTERNAL,
            "The command was stopped by an exit request from inside the tool.",
        )
    except BaseException as failure:  # noqa: BLE001 - the whole purpose of this boundary
        return _finish(
            out,
            err,
            machine,
            INTERNAL,
            f"{_INTERNAL_PREFIX}: {type(failure).__name__}: {describe(failure)}. This is a "
            "defect in Okto Grafx; please report it together with the command line you ran.",
        )
    finally:
        out.flush()
        err.flush()


def _arguments(argv: Sequence[str] | None) -> list[str]:
    """Return the argument list as strings, refusing anything that is not a sequence of them."""
    if argv is None:
        return list(sys.argv[1:])
    if isinstance(argv, str):
        # A string IS a sequence of strings, so this would otherwise be read one character at a
        # time and refused as an unknown one-letter command -- a true answer to a question the
        # caller did not ask.
        raise GrafxConfigurationError(
            "The argument list is a sequence of separate arguments, not one string. "
            f"Got {argv!r}.",
            field="argv",
            value="str",
        )
    try:
        arguments = list(argv)
    except TypeError as failure:
        raise GrafxConfigurationError(
            f"The argument list must be a sequence of strings; a "
            f"{type(argv).__name__} cannot be read as one.",
            field="argv",
            value=type(argv).__name__,
        ) from failure
    for position, token in enumerate(arguments):
        if not isinstance(token, str):
            raise GrafxConfigurationError(
                f"Argument {position + 1} is a {type(token).__name__}, not a string.",
                field="argv",
                value=type(token).__name__,
            )
    return arguments


def _dispatch(arguments: Sequence[str], out: Writer, err: Writer, machine: bool) -> int:
    """Parse the command line, run what it asks for, and write the answer."""
    outcome = parse(arguments)
    if outcome.invocation is None:
        return _emit_message(outcome, out, err, machine)
    return _emit_report(outcome.invocation, run(outcome.invocation), out, err, machine)


def _emit_message(outcome: ParseOutcome, out: Writer, err: Writer, machine: bool) -> int:
    """Write help, a version banner or a usage refusal, in the form the caller asked for."""
    if machine:
        out.line(
            render_json(
                {
                    "command": "",
                    "message": outcome.message,
                    "exit_code": outcome.exit_code,
                    "result": result_word(outcome.exit_code),
                }
            )
        )
        return outcome.exit_code
    (err if outcome.to_stderr else out).line(outcome.message)
    return outcome.exit_code


def _emit_report(
    invocation: Invocation, report: Report, out: Writer, err: Writer, machine: bool
) -> int:
    """Write one command's report, in the form the caller asked for."""
    code = report.exit_code
    if machine:
        out.line(render_json(_document(invocation, report)))
        return code
    out.lines(report.lines)
    err.lines(report.problems)
    return code


def _document(invocation: Invocation, report: Report) -> Mapping[str, object]:
    """Return the machine-readable document of one report.

    ``exit_code`` and ``result`` are derived here from the report's own code, never carried in
    the payload a command built. A command therefore has no way to publish a verdict that
    disagrees with the status its caller's shell will see.
    """
    payload = dict(report.payload)
    payload.setdefault("command", invocation.spec.label)
    payload.setdefault("path", invocation.path)
    payload["exit_code"] = report.exit_code
    payload["result"] = result_word(report.exit_code)
    return payload


def _finish(out: Writer, err: Writer, machine: bool, code: int, message: str) -> int:
    """Write a single contained message and return its exit code."""
    if machine:
        out.line(
            render_json(
                {
                    "command": "",
                    "message": message,
                    "exit_code": code,
                    "result": result_word(code),
                }
            )
        )
    else:
        err.line(message)
    return code
