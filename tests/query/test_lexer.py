"""The lexer: what it reads, and what it refuses rather than reading (CONTRACT.md section 8.9)."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxParseError
from okto_grafx.domain.query.lexer import INTEGER_MAGNITUDE_LIMIT, tokenize
from okto_grafx.domain.query.limits import (
    MAX_NAME_CHARACTERS,
    MAX_NUMBER_CHARACTERS,
    MAX_QUERY_CHARACTERS,
    MAX_STRING_CHARACTERS,
    MAX_TOKENS,
)
from okto_grafx.domain.query.tokens import SYMBOLS, TokenKind


def kinds(text: str) -> tuple[str, ...]:
    """Return the kind of every token of a query, including the end marker."""
    return tuple(token.kind.value for token in tokenize(text))


def texts(text: str) -> tuple[str, ...]:
    """Return the text of every token of a query except the end marker."""
    return tuple(token.text for token in tokenize(text)[:-1])


def values(text: str) -> tuple[object, ...]:
    """Return the decoded value of every token of a query except the end marker."""
    return tuple(token.value for token in tokenize(text)[:-1])


# --- what it reads --------------------------------------------------------------------------


def test_a_query_always_ends_with_exactly_one_end_token() -> None:
    tokens = tokenize("RETURN 1")
    assert tokens[-1].kind is TokenKind.END
    assert [token.kind for token in tokens].count(TokenKind.END) == 1


def test_an_empty_query_is_one_end_token() -> None:
    assert kinds("") == ("end",)


def test_whitespace_and_both_comment_forms_carry_no_tokens() -> None:
    assert kinds("  // a line\n /* a block */ \t\n") == ("end",)


def test_a_line_comment_ends_at_the_newline_and_not_before() -> None:
    assert texts("// RETURN 1\nRETURN 2") == ("RETURN", "2")


def test_the_symbol_table_is_ordered_longest_first() -> None:
    # Order decides the cut: a one-character symbol tried before its two-character extension
    # would split "<=" into "<" and "=" and the query would parse as something else.
    lengths = [len(symbol) for symbol in SYMBOLS]
    assert lengths == sorted(lengths, reverse=True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<=", "<="),
        (">=", ">="),
        ("<>", "<>"),
        ("!=", "!="),
        ("=>", "=>"),
        ("..", ".."),
    ],
)
def test_a_two_character_symbol_is_cut_whole(text: str, expected: str) -> None:
    assert texts(text) == (expected,)


def test_a_hop_range_does_not_swallow_its_dots_into_the_number() -> None:
    assert texts("*1..3") == ("*", "1", "..", "3")


def test_an_integer_and_a_double_are_told_apart() -> None:
    assert kinds("1 1.5 1e3 1.5e-3") == ("integer", "double", "double", "double", "end")


def test_a_leading_dot_reads_as_a_double() -> None:
    assert values(".5") == (0.5,)


def test_an_exponent_without_digits_is_not_part_of_the_number() -> None:
    assert texts("1e") == ("1", "e")


def test_a_string_resolves_its_escapes() -> None:
    assert values(r"'a\nb\tc\\dA'") == ("a\nb\tc\\dA",)


def test_both_quote_characters_open_a_string() -> None:
    assert values("'a' \"b\"") == ("a", "b")


def test_a_back_quoted_name_may_hold_anything_and_is_never_a_keyword() -> None:
    tokens = tokenize("`MATCH one`")
    assert tokens[0].kind is TokenKind.NAME
    assert tokens[0].text == "MATCH one"
    assert tokens[0].quoted is True
    assert tokens[0].reads_as("MATCH") is False


def test_a_doubled_back_quote_is_one_back_quote() -> None:
    assert values("`a``b`") == ("a`b",)


def test_a_parameter_carries_its_name_without_the_marker() -> None:
    tokens = tokenize("$limit")
    assert tokens[0].kind is TokenKind.PARAMETER
    assert tokens[0].text == "limit"


def test_a_token_knows_where_it_started() -> None:
    tokens = tokenize("RETURN\n  1")
    assert (tokens[1].line, tokens[1].column) == (2, 3)


def test_a_string_may_hold_characters_outside_ascii() -> None:
    accented = "caf" + chr(0xE9)
    assert values("'" + accented + "'") == (accented,)


# --- what it refuses ------------------------------------------------------------------------


def test_a_query_that_is_not_text_is_refused_in_the_taxonomy() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize(b"RETURN 1")  # type: ignore[arg-type]
    assert failure.value.details["field"] == "text"


def test_a_query_longer_than_the_bound_is_refused_before_it_is_cut() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize(" " * (MAX_QUERY_CHARACTERS + 1))
    assert failure.value.details["value"] == MAX_QUERY_CHARACTERS + 1


def test_more_tokens_than_the_bound_are_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("+" * (MAX_TOKENS + 1))
    assert failure.value.details["field"] == "tokens"


def test_an_unterminated_string_is_refused_at_its_opening_quote() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("RETURN 'never closed")
    assert failure.value.details["field"] == "string"
    assert failure.value.details["column"] == 8


def test_an_unterminated_block_comment_is_refused_at_its_opening() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("RETURN 1 /* never closed")
    assert failure.value.details["field"] == "comment"
    assert failure.value.details["column"] == 10


def test_an_unterminated_back_quoted_name_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("RETURN `never closed")
    assert failure.value.details["field"] == "name"


def test_a_numeric_literal_longer_than_the_bound_is_refused_before_conversion() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("1" * (MAX_NUMBER_CHARACTERS + 1))
    assert failure.value.details["field"] == "number"


def test_an_enormous_digit_run_never_reaches_the_integer_constructor() -> None:
    # CPython refuses to build an integer from a very long digit string and raises ValueError
    # while doing it, which is not a Grafx type. The bound has to fire first.
    with pytest.raises(GrafxParseError):
        tokenize("9" * 100000)


def test_a_magnitude_one_past_the_positive_range_is_still_read() -> None:
    # It is the magnitude of the most negative 64-bit integer, so the lexer admits it and the
    # parser decides once the sign is known.
    assert values(str(INTEGER_MAGNITUDE_LIMIT)) == (INTEGER_MAGNITUDE_LIMIT,)


def test_a_magnitude_beyond_that_is_refused_by_the_lexer() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize(str(INTEGER_MAGNITUDE_LIMIT + 1))
    assert failure.value.details["field"] == "number"


def test_a_double_literal_that_is_not_finite_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("1e999")
    assert failure.value.details["value"] == "1e999"


def test_a_string_longer_than_the_bound_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("'" + "a" * (MAX_STRING_CHARACTERS + 1) + "'")
    assert failure.value.details["field"] == "string"


def test_a_string_of_exactly_the_bound_is_accepted() -> None:
    body = "a" * MAX_STRING_CHARACTERS
    assert values("'" + body + "'") == (body,)


def test_an_unknown_escape_is_refused_rather_than_passed_through() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize(r"'\q'")
    assert failure.value.details["field"] == "escape"


def test_a_numeric_escape_without_enough_digits_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize(r"'\u12'")
    assert failure.value.details["field"] == "escape"


def test_an_escaped_lone_surrogate_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize(r"'\ud800'")
    assert failure.value.details["field"] == "string"
    assert failure.value.details["value"] == 0xD800


def test_a_pasted_lone_surrogate_is_refused_by_the_same_rule() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("'" + chr(0xD800) + "'")
    assert failure.value.details["field"] == "string"
    assert failure.value.details["value"] == 0xD800


def test_a_code_point_beyond_the_last_character_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize(r"'\U00110000'")
    assert failure.value.details["field"] == "escape"
    assert failure.value.details["value"] == 0x110000


def test_a_name_longer_than_the_bound_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("a" * (MAX_NAME_CHARACTERS + 1))
    assert failure.value.details["field"] == "name"


def test_a_back_quoted_name_longer_than_the_bound_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("`" + "a" * (MAX_NAME_CHARACTERS + 1) + "`")
    assert failure.value.details["field"] == "name"


def test_an_empty_quoted_token_retains_its_extent_for_contextual_admission() -> None:
    token = tokenize("``")[0]
    assert token.text == ""
    assert token.quoted
    assert token.offset == 0
    assert token.end_offset == 2


def test_a_name_outside_ascii_is_refused_and_names_the_remedy() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("caf" + chr(0xE9))
    assert failure.value.details["field"] == "name"


def test_a_dollar_sign_with_no_name_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("$ ")
    assert failure.value.details["field"] == "parameter"


def test_a_digit_leading_alphanumeric_parameter_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("$1name")
    assert failure.value.details["field"] == "parameter"


@pytest.mark.parametrize("name", ["0", "1", "001", "12345678901234567890"])
def test_decimal_parameter_names_preserve_exact_spelling(name):
    token = tokenize("$" + name)[0]
    assert token.kind is TokenKind.PARAMETER
    assert token.value == name


def test_a_character_with_no_meaning_is_refused_with_its_position() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("RETURN #")
    assert failure.value.details["field"] == "character"
    assert failure.value.details["column"] == 8


def test_every_lexical_refusal_carries_the_parse_error_code() -> None:
    with pytest.raises(GrafxParseError) as failure:
        tokenize("RETURN #")
    assert failure.value.code == "parse_error"
    assert failure.value.retryable is False
