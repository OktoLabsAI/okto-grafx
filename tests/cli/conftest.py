"""Fixtures for the command-line suite (C12).

Nothing here is a double. Every fixture builds a real database through ``connect`` and drives the
real command line through :func:`okto_grafx.cli.entry.main`, because the whole subject of this
component is what an operator sees when the delivered stack answers -- and LESSONS L12 records
three defects in this build that only appeared once somebody drove the real adapter.

The one thing that is stood in for is the terminal itself: ``main`` takes the streams to write
to, so a test captures output without touching ``sys.stdout`` and without a monkeypatch that
another test could inherit.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from okto_grafx import connect
from okto_grafx.cli.entry import main

TABLE_STATEMENT: str = "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
"""The one statement this build can commit, used wherever a test needs a database with content."""

WAL_SEGMENT: str = "wal/000000000001.wal"
"""The first log segment, the file the damage fixtures append garbage to."""


@dataclass(frozen=True, slots=True)
class Run:
    """One command-line run: what it returned, and what it wrote to each stream."""

    code: int
    out: str
    err: str

    @property
    def document(self) -> dict[str, object]:
        """Return the machine-readable document this run printed, refusing anything else."""
        parsed = json.loads(self.out)
        assert isinstance(parsed, dict), f"expected one JSON object, got {type(parsed).__name__}"
        return parsed

    @property
    def text(self) -> str:
        """Return everything this run wrote, on either stream."""
        return self.out + self.err


CliRunner = Callable[..., Run]
"""What the ``cli`` fixture hands back: a callable taking argv tokens and returning a Run."""


@pytest.fixture
def cli() -> CliRunner:
    """Return a callable that runs one command line and captures both streams."""

    def run(*argv: str) -> Run:
        out = io.StringIO()
        err = io.StringIO()
        code = main(list(argv), stdout=out, stderr=err)
        return Run(code=code, out=out.getvalue(), err=err.getvalue())

    return run


@pytest.fixture
def database_path(tmp_path: Path) -> str:
    """Return the path of a real database holding one committed table."""
    path = tmp_path / "db"
    with connect(str(path)) as database:
        with database.begin("write") as txn:
            txn.execute(TABLE_STATEMENT)
    return str(path)


@pytest.fixture
def empty_database_path(tmp_path: Path) -> str:
    """Return the path of a real database that was created and never written to."""
    path = tmp_path / "fresh"
    with connect(str(path)):
        pass
    return str(path)


def damage_log_tail(database_path: str, *, length: int = 4096) -> int:
    """Append a run of zeros past the end of the log and return how many bytes were added.

    This is the interior-zeros signature SPEC-M1 TS-5 describes, applied at the tail: the records
    before it stay intact, so recovery truncates, quarantines the range and writes a forensic
    ledger entry rather than refusing the whole database.
    """
    segment = Path(database_path) / WAL_SEGMENT
    assert segment.is_file(), f"no log segment at {segment}"
    before = segment.stat().st_size
    with open(segment, "ab") as handle:
        handle.write(b"\x00" * length)
    after = segment.stat().st_size
    assert after == before + length, "the fixture did not extend the log"
    return length


def damage_page(database_path: str, file: str, *, offset: int, length: int = 16) -> None:
    """Overwrite bytes inside an existing page so its checksum can no longer hold.

    The write stays inside the file, because extending a paged file by a partial page is a
    different failure -- an unaligned file, refused before any page is read -- and a test that
    meant to seed a checksum failure would then be measuring something else (A72).
    """
    target = Path(database_path) / file
    size = target.stat().st_size
    assert offset + length <= size, f"{file} holds {size} bytes; cannot damage {offset}+{length}"
    with open(target, "r+b") as handle:
        handle.seek(offset)
        handle.write(b"\xde\xad\xbe\xef" * (length // 4))
    assert target.stat().st_size == size, "damaging a page must not resize the file"


def append_blank_page(database_path: str, file: str, *, page_size: int = 8192) -> None:
    """Append one page of zeros to a paged file, which no checksum can claim.

    Used where a test needs a database that still OPENS while carrying a page verification must
    report: the extent chain never reaches the new page, so nothing on the open path reads it.
    """
    target = Path(database_path) / file
    before = target.stat().st_size
    with open(target, "ab") as handle:
        handle.write(b"\x00" * page_size)
    assert target.stat().st_size == before + page_size, "the fixture did not extend the file"


def source_root() -> str:
    """Return the ``src`` directory, for pinning how a subprocess resolves the package (A94)."""
    return str(Path(__file__).resolve().parents[2] / "src")


def child_environment() -> dict[str, str]:
    """Return an environment whose PYTHONPATH names this checkout and nothing else (A94).

    A subprocess inherits an editable install, which can point at a different tree than the one
    under test. Pinning it is the difference between measuring this code and measuring whatever
    happens to be installed.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = source_root()
    return environment
