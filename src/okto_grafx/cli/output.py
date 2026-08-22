"""Putting operator output on a terminal without ever ending the run (C12).

Three things break a command line that is otherwise correct, and all three happen at exactly the
moment an operator can least afford it -- against a damaged database, at the end of a pipe, on a
console that was configured years ago:

* **The console cannot encode what we are about to print.** A database directory whose name
  leaves the ASCII plane opens perfectly well and then kills the process on the ``print`` that
  reports it. Measured on this platform: a non-BMP character in the path raises
  ``UnicodeEncodeError`` from a legacy code page. :class:`Writer` degrades the characters instead
  of the run.
* **The reader went away.** ``oktografx verify db | head -1`` closes the pipe under us. A writer
  that raises there turns a successful verification into a crash report.
* **A value will not render.** Anything read back off a damaged device can be a shape nobody
  planned for, and ``str()`` on a hostile object can raise. :func:`describe` answers with a
  placeholder rather than propagating.

Nothing here decides anything about a database. It only decides how text reaches a terminal, so
that the part which does decide is never the part that fails.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import IO

__all__ = [
    "MAX_CELL_WIDTH",
    "MAX_JSON_DEPTH",
    "TRUNCATION_MARK",
    "Writer",
    "describe",
    "jsonable",
    "render_json",
    "render_table",
    "sanitize_cell",
]

MAX_CELL_WIDTH: int = 72
"""Longest value one table cell prints before it is shortened.

A single stored string can be megabytes long, and a table that pastes one into a terminal is not
a report. The shortened form is marked, and the machine-readable output carries the value whole,
so nothing is hidden -- it is moved to the door that can carry it.
"""

TRUNCATION_MARK: str = "..."
"""What a shortened cell ends with, so a reader can tell a short value from a shortened one."""

MAX_JSON_DEPTH: int = 32
"""How deep the machine-readable converter follows a nested value before it stops.

A value read back off a damaged device can nest further than the interpreter's own recursion
limit allows. Stopping at a stated depth turns that into a marked placeholder instead of a
``RecursionError`` in the middle of a report.
"""

_CONTROL_ESCAPES: Mapping[int, str] = {
    0x09: "\\t",
    0x0A: "\\n",
    0x0D: "\\r",
}
"""The three control characters that get a readable escape; the rest print as ``\\xNN``."""


def describe(value: object) -> str:
    """Return a printable description of any object, never raising.

    ``str()`` runs code the caller wrote, and this function is used while a report is being
    rendered, where a failure means an operator sees nothing at all.
    """
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - a description must never fail; see the docstring
        pass
    try:
        return f"<{type(value).__name__} that cannot be described>"
    except Exception:  # noqa: BLE001 - the type itself may misbehave; answer anyway
        return "<a value that cannot be described>"


def sanitize_cell(text: str) -> str:
    """Return one table cell with its control characters escaped and its length bounded.

    A stored string may hold a newline, and a newline inside a table cell silently rewrites the
    rest of the report. Escaping is a display decision only: the machine-readable output carries
    the original.
    """
    rendered: list[str] = []
    for character in text:
        code = ord(character)
        escape = _CONTROL_ESCAPES.get(code)
        if escape is not None:
            rendered.append(escape)
        elif code < 0x20 or code == 0x7F:
            rendered.append(f"\\x{code:02x}")
        else:
            rendered.append(character)
    joined = "".join(rendered)
    if len(joined) <= MAX_CELL_WIDTH:
        return joined
    return joined[: MAX_CELL_WIDTH - len(TRUNCATION_MARK)] + TRUNCATION_MARK


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> tuple[str, ...]:
    """Return a fixed-width table as lines, with a header rule under the column names.

    Column widths come from the sanitized cells, so a value that had to be shortened does not
    stretch the table to the width it would have had.
    """
    columns = [sanitize_cell(header) for header in headers]
    body = [[sanitize_cell(cell) for cell in row] for row in rows]
    widths = [len(header) for header in columns]
    for row in body:
        for position, cell in enumerate(row):
            if position < len(widths):
                widths[position] = max(widths[position], len(cell))
    lines = [_row_text(columns, widths), _row_text(["-" * width for width in widths], widths)]
    lines.extend(_row_text(row, widths) for row in body)
    return tuple(lines)


def _row_text(cells: Sequence[str], widths: Sequence[int]) -> str:
    """Return one table row, padded to the column widths and right-trimmed."""
    parts: list[str] = []
    for position, cell in enumerate(cells):
        width = widths[position] if position < len(widths) else len(cell)
        parts.append(cell.ljust(width))
    return "  ".join(parts).rstrip()


def jsonable(value: object, *, depth: int = 0) -> object:
    """Return a value the machine-readable renderer can carry, converting anything it cannot.

    Nothing is dropped silently: bytes become a hexadecimal string, a value that nests deeper
    than :data:`MAX_JSON_DEPTH` becomes a marked placeholder, a non-finite float becomes its
    en-US name, and any other object becomes its description. A consumer that finds a string
    where it expected a number is looking at a value the format could not hold, which is a
    statement worth making rather than an omission.
    """
    if depth > MAX_JSON_DEPTH:
        return "<nested deeper than this report renders>"
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, Mapping):
        return {describe(key): jsonable(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item, depth=depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(describe(item) for item in value)
    return describe(value)


def render_json(payload: Mapping[str, object]) -> str:
    """Return the machine-readable form of one report: sorted keys, ASCII only, one document.

    Sorted keys make the output a function of the report rather than of the order it was built
    in, so two runs over the same state produce the same bytes. ASCII escaping means the document
    can be written to any console this tool can reach.
    """
    prepared = {name: jsonable(value) for name, value in payload.items()}
    try:
        return json.dumps(prepared, sort_keys=True, ensure_ascii=True, indent=2)
    except (TypeError, ValueError):
        # Nothing jsonable() returns should reach this, and a report that cannot be rendered is
        # still a report that has to reach the operator, so it degrades to descriptions.
        degraded = {name: describe(value) for name, value in payload.items()}
        return json.dumps(degraded, sort_keys=True, ensure_ascii=True, indent=2)


class Writer:
    """One output stream that absorbs every way writing to a terminal can fail.

    A writer that has met a closed stream stops trying: :attr:`broken` says so, and every later
    call is a no-op. That is deliberate -- once the reader is gone, a second failed write adds
    nothing and a raised exception would replace the answer the operator asked for.
    """

    __slots__ = ("_stream", "_broken")

    def __init__(self, stream: IO[str]) -> None:
        """Bind this writer to an already-open text stream."""
        self._stream: IO[str] = stream
        self._broken: bool = False

    @property
    def broken(self) -> bool:
        """Return True once a write failed and this writer stopped trying."""
        return self._broken

    def write(self, text: str) -> None:
        """Write text, degrading characters the stream cannot encode rather than failing."""
        if self._broken:
            return
        try:
            self._stream.write(text)
            return
        except UnicodeEncodeError:
            pass
        except Exception:  # noqa: BLE001 - a closed pipe must not end the command
            self._broken = True
            return
        self._write_degraded(text)

    def line(self, text: str = "") -> None:
        """Write one line of text followed by a newline."""
        self.write(text + "\n")

    def lines(self, texts: Iterable[str]) -> None:
        """Write each item as its own line."""
        for text in texts:
            self.line(text)

    def flush(self) -> None:
        """Flush the stream, absorbing a failure the same way a write absorbs one."""
        if self._broken:
            return
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001 - flushing a closed pipe is not an error to report
            self._broken = True

    def _write_degraded(self, text: str) -> None:
        """Write text the stream refused, replacing unencodable characters with escapes."""
        encoding = getattr(self._stream, "encoding", None)
        for candidate in (encoding, "ascii"):
            if not isinstance(candidate, str) or not candidate:
                continue
            try:
                safe = text.encode(candidate, "backslashreplace").decode(candidate, "replace")
            except (LookupError, UnicodeError, ValueError):
                continue
            try:
                self._stream.write(safe)
                return
            except Exception:  # noqa: BLE001 - try the next spelling, then give up quietly
                continue
        self._broken = True

    def __repr__(self) -> str:
        """Return a representation naming whether this writer is still usable."""
        return f"Writer(broken={self._broken})"
