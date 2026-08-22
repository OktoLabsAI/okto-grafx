"""The pieces that put text on a terminal, tested on their own (C12).

These are small and boring, and they are also the only reason the rest of the component cannot be
ended by a value it did not expect. A table cell holding a newline silently rewrites the report
below it; a value whose ``__str__`` raises turns a verification into a crash; a structure that
nests deeper than the interpreter's recursion limit does the same in the machine-readable path.
Each of those is a real shape to meet when reading back a damaged device.
"""

from __future__ import annotations

import io
import json
import math

import pytest

from okto_grafx.cli.output import (
    MAX_CELL_WIDTH,
    MAX_JSON_DEPTH,
    TRUNCATION_MARK,
    Writer,
    describe,
    jsonable,
    render_json,
    render_table,
    sanitize_cell,
)


class _Hostile:
    """An object whose description raises, as anything read off a damaged device might."""

    def __str__(self) -> str:
        """Fail the way a broken value fails."""
        raise ValueError("this value refuses to describe itself")

    def __repr__(self) -> str:
        """Fail the same way, so nothing falls back to a working spelling."""
        raise ValueError("this value refuses to describe itself")


def test_a_value_that_refuses_to_describe_itself_still_prints() -> None:
    text = describe(_Hostile())
    assert "_Hostile" in text
    assert "cannot be described" in text


def test_an_ordinary_value_describes_as_itself() -> None:
    # A34: the guard above is only meaningful if the ordinary path is unchanged.
    assert describe(42) == "42"
    assert describe("plain") == "plain"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("two\nlines", "two\\nlines"),
        ("tab\there", "tab\\there"),
        ("carriage\rreturn", "carriage\\rreturn"),
        ("bell\x07", "bell\\x07"),
        ("delete\x7f", "delete\\x7f"),
    ],
)
def test_a_control_character_cannot_rewrite_a_table(raw: str, expected: str) -> None:
    assert sanitize_cell(raw) == expected


def test_a_very_long_value_is_shortened_and_says_so() -> None:
    cell = sanitize_cell("x" * (MAX_CELL_WIDTH * 4))
    assert len(cell) == MAX_CELL_WIDTH
    assert cell.endswith(TRUNCATION_MARK)


def test_a_value_that_fits_is_not_touched() -> None:
    value = "y" * MAX_CELL_WIDTH
    assert sanitize_cell(value) == value


def test_a_table_lines_up_its_columns() -> None:
    lines = render_table(("id", "name"), [("1", "Ada"), ("22", "Grace")])
    assert len(lines) == 4
    assert lines[1].startswith("--")
    assert lines[2].startswith("1 ")
    assert lines[3].startswith("22")
    assert all(not line.endswith(" ") for line in lines)


def test_a_table_with_no_rows_still_shows_its_columns() -> None:
    lines = render_table(("only",), [])
    assert lines[0] == "only"
    assert lines[1] == "----"


def test_a_row_wider_than_its_header_does_not_lose_cells() -> None:
    lines = render_table(("a",), [("one", "two")])
    assert "two" in lines[-1]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (7, 7),
        (1.5, 1.5),
        ("text", "text"),
        (b"\x00\xff", "0x00ff"),
        (bytearray(b"\x01"), "0x01"),
        ((1, 2), [1, 2]),
        ({"b": 1, "a": 2}, {"b": 1, "a": 2}),
        (frozenset({"b", "a"}), ["a", "b"]),
        (float("nan"), "NaN"),
        (float("inf"), "Infinity"),
        (float("-inf"), "-Infinity"),
    ],
)
def test_a_value_becomes_something_the_document_can_carry(value: object, expected: object) -> None:
    assert jsonable(value) == expected


def test_a_structure_nested_past_the_limit_is_marked_rather_than_fatal() -> None:
    deep: object = "bottom"
    for _ in range(MAX_JSON_DEPTH * 3):
        deep = [deep]
    rendered = jsonable(deep)
    text = json.dumps(rendered)
    assert "nested deeper" in text
    assert "bottom" not in text


def test_a_document_is_a_function_of_its_content_alone() -> None:
    # Two runs over the same state must produce the same bytes, or a diff of two reports is
    # noise rather than a difference.
    first = render_json({"b": 1, "a": {"z": 1, "y": 2}})
    second = render_json({"a": {"y": 2, "z": 1}, "b": 1})
    assert first == second
    assert first.isascii(), "a document must be printable on any console this tool can reach"


def test_a_document_carries_a_value_it_cannot_hold_as_a_description() -> None:
    text = render_json({"broken": _Hostile()})
    assert "cannot be described" in text
    assert json.loads(text)["broken"]


def test_a_document_with_a_non_finite_number_is_still_valid_json() -> None:
    text = render_json({"ratio": math.nan})
    assert json.loads(text)["ratio"] == "NaN"


class _RefusingStream(io.StringIO):
    """A stream that cannot encode anything past ASCII, like a legacy console."""

    encoding = "cp1252"

    def write(self, text: str) -> int:
        """Refuse text a cp1252 console could not encode."""
        text.encode("cp1252")
        return super().write(text)


def test_a_writer_degrades_characters_rather_than_the_run() -> None:
    stream = _RefusingStream()
    writer = Writer(stream)
    writer.line("path " + chr(0x1F600))
    assert writer.broken is False
    written = stream.getvalue()
    assert "path " in written
    assert chr(0x1F600) not in written
    assert "\\U0001f600" in written


def test_a_writer_over_an_ordinary_stream_changes_nothing() -> None:
    stream = io.StringIO()
    writer = Writer(stream)
    writer.line("unchanged " + chr(0x00E9))
    assert stream.getvalue() == "unchanged " + chr(0x00E9) + "\n"
    assert writer.broken is False


def test_a_writer_reports_what_it_is() -> None:
    assert "broken=False" in repr(Writer(io.StringIO()))


def test_a_writer_over_nothing_at_all_does_not_raise() -> None:
    writer = Writer(None)  # type: ignore[arg-type]
    writer.line("into the void")
    writer.flush()
    assert writer.broken is True
