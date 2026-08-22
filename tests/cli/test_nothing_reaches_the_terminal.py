"""No input produces a traceback, and no output stream can end a command (C12).

This tool is reached for when a database is already in trouble, so the failure modes that matter
are the ones that arrive together with the trouble: a path typed wrong in the dark, a database
whose bytes are damaged, a console that cannot spell the directory name, a pipe closed by
``head``, an interrupt at the wrong instant. Each of these is checked here to produce one of the
documented exit codes and nothing else.

The corpus deliberately leaves the ASCII plane. LESSONS L5 records that an entire defect class in
this build was unreachable because every test path was rooted at pytest's ASCII temporary
directory, so the tests agreed with the code about which inputs exist. A non-BMP character in a
database directory is exactly the input that opened cleanly and then killed the process on the
line that printed the path.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from okto_grafx.cli.exits import EXIT_CODE_MEANINGS, INTERNAL, INTERRUPTED, OK, USAGE
from okto_grafx.cli.entry import main
from okto_grafx.cli.output import Writer

from tests.cli.conftest import (
    TABLE_STATEMENT,
    CliRunner,
    Run,
    append_blank_page,
    damage_log_tail,
    damage_page,
)

HOSTILE_COMMAND_LINES: tuple[tuple[str, ...], ...] = (
    (),
    ("",),
    (" ",),
    ("\t",),
    ("\n",),
    ("-",),
    ("--",),
    ("--", "--"),
    ("---",),
    ("=",),
    ("--=",),
    ("--=1",),
    ("verify",),
    ("verify", ""),
    ("verify", "--"),
    ("verify", "--", ""),
    ("verify", ".", "--scope"),
    ("verify", ".", "--scope", ""),
    ("verify", ".", "--scope", "--json"),
    ("verify", ".", "--page-size", "-1"),
    ("verify", ".", "--page-size", "99999999999999999999999"),
    ("verify", ".", "--buffer-budget-bytes", "1"),
    ("verify", "/", ),
    ("verify", "\\"),
    ("verify", "."),
    ("verify", ".."),
    ("verify", "con"),
    ("verify", "nul"),
    ("verify", "*"),
    ("verify", "?"),
    ("verify", "a" * 4096),
    ("status", "-", "--json"),
    ("query", ".", ""),
    ("query", ".", "SELECT", "--parameter"),
    ("query", ".", "SELECT", "--parameter", "novalue"),
    ("query", ".", "SELECT", "--parameter", "=1"),
    ("query", ".", "SELECT", "--limit", "abc"),
    ("ledger",),
    ("ledger", "list"),
    ("ledger", "inspect", "."),
    ("ledger", "inspect", ".", "99999999999999999999"),
    ("ledger", "export", ".", "1"),
    ("ledger", "export", ".", "1", "--output"),
    ("quarantine",),
    ("quarantine", "read", ".", "x"),
    ("quarantine", "read", ".", "x", "--output", ""),
    ("metrics",),
    ("metrics", ".", "--metrics", "json"),
    ("metrics", ".", "--metrics", "json", "--metrics-destination", ""),
    ("metrics", ".", "--metrics-destination", "not-a-host-port"),
    ("recovery", ".", "--rerun", "--read-only"),
    ("help", "me"),
    ("version", "please"),
)
"""Command lines chosen because each one is a way a real operator or script gets it wrong."""


@pytest.mark.parametrize("argv", HOSTILE_COMMAND_LINES, ids=lambda item: "|".join(item) or "none")
def test_no_command_line_produces_a_traceback(argv: tuple[str, ...], cli: CliRunner) -> None:
    run = cli(*argv)
    assert run.code in EXIT_CODE_MEANINGS, f"{argv} returned an undocumented code {run.code}"
    assert run.code != INTERNAL, f"{argv} reached the containment boundary: {run.err}"
    assert "Traceback" not in run.text
    assert "File \"" not in run.text


def test_the_hostile_corpus_is_large_enough_to_mean_something() -> None:
    # A72: a parametrisation that collected three cases proves nothing about the claim above.
    assert len(HOSTILE_COMMAND_LINES) >= 45
    assert len(set(HOSTILE_COMMAND_LINES)) == len(HOSTILE_COMMAND_LINES)


NON_ASCII_NAMES: tuple[str, ...] = (
    "acentuacao-" + chr(0x00E7) + chr(0x00E3) + "o",
    "cyrillic-" + chr(0x0431) + chr(0x0434),
    "cjk-" + chr(0x6570) + chr(0x636E),
    "astral-" + chr(0x1F600) + chr(0x1F5C4),
    "combining-e" + chr(0x0301),
)
"""Directory names built from code points rather than literals, so this file stays ASCII itself.

The astral entry is the one that matters: a non-BMP character is what made a legacy console
encoder raise on a path the database had already opened without trouble.
"""


@pytest.mark.parametrize("name", NON_ASCII_NAMES, ids=lambda item: item.split("-")[0])
def test_a_database_whose_path_leaves_the_ascii_plane_still_reports(
    name: str, tmp_path: Path, cli: CliRunner
) -> None:
    target = tmp_path / name
    made = cli("query", str(target), TABLE_STATEMENT, "--write", "--create")
    assert made.code == OK, made.text
    run = cli("verify", str(target))
    assert run.code == OK, run.text
    assert "CLEAN" in run.out


class _AsciiOnlyStream(io.StringIO):
    """A stream that refuses anything an old console could not encode, like a legacy code page."""

    encoding = "ascii"

    def write(self, text: str) -> int:
        """Refuse text outside the ASCII plane exactly as a cp1252 console would."""
        text.encode("ascii")
        return super().write(text)


def test_a_console_that_cannot_encode_the_path_does_not_end_the_command(
    tmp_path: Path,
) -> None:
    target = tmp_path / ("console-" + chr(0x1F600))
    out = _AsciiOnlyStream()
    err = _AsciiOnlyStream()
    created = main(
        ["query", str(target), TABLE_STATEMENT, "--write", "--create"], stdout=out, stderr=err
    )
    assert created == OK, out.getvalue() + err.getvalue()
    out = _AsciiOnlyStream()
    err = _AsciiOnlyStream()
    code = main(["verify", str(target)], stdout=out, stderr=err)
    assert code == OK
    printed = out.getvalue()
    assert "CLEAN" in printed
    assert "\\U0001f600" in printed or "\\ud83d" in printed, printed
    assert printed.isascii(), "the degraded form must be printable on the stream that refused"


class _ClosedPipe(io.StringIO):
    """A stream that fails every write, the way a pipe closed by ``head`` does."""

    def write(self, text: str) -> int:
        """Fail exactly as a closed pipe fails."""
        raise BrokenPipeError(32, "The pipe is being closed")


def test_a_closed_pipe_does_not_change_the_answer(database_path: str) -> None:
    # `oktografx verify db | head -1` is an ordinary thing to type, and the exit code must still
    # be the verification's answer rather than a report about the pipe.
    code = main(["verify", database_path], stdout=_ClosedPipe(), stderr=_ClosedPipe())
    assert code == OK
    append_blank_page(database_path, "heap.dat")
    damaged = main(["verify", database_path], stdout=_ClosedPipe(), stderr=_ClosedPipe())
    assert damaged == 1


def test_a_writer_that_met_a_closed_stream_stops_trying() -> None:
    writer = Writer(_ClosedPipe())
    writer.line("first")
    assert writer.broken is True
    writer.line("second")
    writer.flush()
    assert writer.broken is True


def test_a_stream_that_is_not_a_stream_at_all_does_not_end_the_command(
    database_path: str,
) -> None:
    # A host embedding this tool can hand it anything. Nothing it hands over may turn a
    # verification into an exception.
    code = main(["verify", database_path], stdout=None, stderr=None)  # type: ignore[arg-type]
    assert code in EXIT_CODE_MEANINGS


class _InterruptingStream(io.StringIO):
    """A stream that interrupts the command the first time it is written to."""

    def __init__(self) -> None:
        """Start armed, so the first write raises and later ones behave."""
        super().__init__()
        self.armed = True

    def write(self, text: str) -> int:
        """Raise KeyboardInterrupt once, then write normally."""
        if self.armed:
            self.armed = False
            raise KeyboardInterrupt
        return super().write(text)


def test_an_interrupt_is_reported_as_an_interrupt(database_path: str) -> None:
    err = io.StringIO()
    code = main(["verify", database_path], stdout=_InterruptingStream(), stderr=err)
    assert code == INTERRUPTED
    assert "Interrupted" in err.getvalue()
    assert "Traceback" not in err.getvalue()


class _ExplodingStream(io.StringIO):
    """A stream whose write raises something no boundary could have anticipated."""

    def write(self, text: str) -> int:
        """Raise a failure that is neither an OSError nor a Grafx error."""
        raise ZeroDivisionError("a defect nobody planned for")


def test_an_unforeseen_failure_is_contained_rather_than_shown(database_path: str) -> None:
    # The Writer absorbs a failing stream, so an exploding stdout is survivable rather than
    # fatal: the command still answers, and no traceback is printed anywhere.
    err = io.StringIO()
    code = main(["verify", database_path], stdout=_ExplodingStream(), stderr=err)
    assert code in EXIT_CODE_MEANINGS
    assert "Traceback" not in err.getvalue()


def test_the_containment_boundary_reports_a_failure_it_cannot_prevent(
    monkeypatch: pytest.MonkeyPatch, database_path: str
) -> None:
    # A34: a guard that cannot fire is dead code wearing a test. This proves the last resort in
    # main() really does turn an arbitrary exception into a documented code and a sentence.
    import okto_grafx.cli.entry as module

    def explode(*_args: object, **_kwargs: object) -> int:
        raise ZeroDivisionError("planted inside the dispatcher")

    monkeypatch.setattr(module, "_dispatch", explode)
    out, err = io.StringIO(), io.StringIO()
    code = main(["verify", database_path], stdout=out, stderr=err)
    assert code == INTERNAL
    assert "ZeroDivisionError" in err.getvalue()
    assert "planted inside the dispatcher" in err.getvalue()
    assert "Traceback" not in err.getvalue()
    assert out.getvalue() == ""


def test_the_containment_boundary_answers_in_json_when_asked(
    monkeypatch: pytest.MonkeyPatch, database_path: str
) -> None:
    import json

    import okto_grafx.cli.entry as module

    def explode(*_args: object, **_kwargs: object) -> int:
        raise MemoryError("planted")

    monkeypatch.setattr(module, "_dispatch", explode)
    out, err = io.StringIO(), io.StringIO()
    code = main(["verify", database_path, "--json"], stdout=out, stderr=err)
    assert code == INTERNAL
    document = json.loads(out.getvalue())
    assert document["exit_code"] == INTERNAL
    assert document["result"] == "internal"


DAMAGE_SHAPES: tuple[str, ...] = ("blank_heap_page", "damaged_heap_page", "damaged_log_tail")
"""Every seeded damage this file drives every command against."""


def _apply(shape: str, path: str) -> None:
    """Seed one named damage on a database that already exists."""
    if shape == "blank_heap_page":
        append_blank_page(path, "heap.dat")
    elif shape == "damaged_heap_page":
        damage_page(path, "heap.dat", offset=4000)
    elif shape == "damaged_log_tail":
        damage_log_tail(path)
    else:  # pragma: no cover - the parametrisation is the closed set
        raise AssertionError(f"unknown damage shape {shape!r}")


COMMANDS_OVER_A_DAMAGED_DATABASE: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("status",), ()),
    (("verify",), ()),
    (("verify",), ("--scope", "records")),
    (("recovery",), ()),
    (("metrics",), ()),
    (("ledger", "list"), ()),
    (("quarantine", "list"), ()),
    (("query",), ("MATCH (p:Person) RETURN p.name",)),
)
"""Every command that takes only a path, as ``(leading tokens, trailing tokens)``."""


@pytest.mark.parametrize("shape", DAMAGE_SHAPES)
@pytest.mark.parametrize(
    ("leading", "trailing"),
    COMMANDS_OVER_A_DAMAGED_DATABASE,
    ids=lambda item: "-".join(item) or "plain",
)
def test_every_command_survives_every_damage(
    shape: str,
    leading: tuple[str, ...],
    trailing: tuple[str, ...],
    database_path: str,
    cli: CliRunner,
) -> None:
    _apply(shape, database_path)
    run: Run = cli(*leading, database_path, *trailing)
    assert run.code in EXIT_CODE_MEANINGS, run.text
    assert run.code != INTERNAL, run.text
    assert "Traceback" not in run.text
    assert run.code != USAGE, "a damaged database is not a command-line mistake"
