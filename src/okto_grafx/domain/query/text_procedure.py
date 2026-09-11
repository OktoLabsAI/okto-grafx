"""Closed read-only procedure grammar; not an extensible CALL or arbitrary suffix parser."""

from __future__ import annotations

from collections.abc import Mapping

from okto_grafx.domain.errors import GrafxParseError
from okto_grafx.domain.query.lexer import tokenize
from okto_grafx.domain.query.tokens import TokenKind

__all__: list[str] = []


def text_call(text: str, parameters: Mapping[str, object]) -> tuple[object, ...] | None:
    """Parse CALL grafx.search_text(index, query, k [, record_ids_parameter]) completely."""
    prefix = text.lstrip()[:4].upper()
    if prefix != "CALL" and not text.lstrip().startswith(("//", "/*")):
        return None
    tokens = tokenize(text)
    if not tokens[0].reads_as("CALL"):
        return None
    if len(tokens) < 7 or tuple(t.text for t in tokens[1:5]) != (
        "grafx",
        ".",
        "search_text",
        "(",
    ):
        return None  # All other CALL forms belong to the composable query parser.
    position = 5
    arguments = []
    while position < len(tokens):
        token = tokens[position]
        if token.kind is TokenKind.PARAMETER:
            if token.text not in parameters:
                raise GrafxParseError(
                    "Missing procedure parameter.", field="parameter", value=token.text
                )
            value = parameters[token.text]
        elif token.kind in (TokenKind.STRING, TokenKind.INTEGER):
            value = token.value
        else:
            raise GrafxParseError(
                "Procedure arguments must be scalar literals or parameters.",
                offset=token.offset,
            )
        arguments.append(value)
        position += 1
        if position >= len(tokens) or len(arguments) > 4:
            raise GrafxParseError("Invalid procedure argument list.")
        if tokens[position].text == ")":
            position += 1
            break
        if tokens[position].text != ",":
            raise GrafxParseError("Expected a procedure comma or closing parenthesis.")
        position += 1
    if position < len(tokens) and tokens[position].text == ";":
        position += 1
    if (
        len(arguments) not in (3, 4)
        or position != len(tokens) - 1
        or tokens[position].kind is not TokenKind.END
    ):
        raise GrafxParseError(
            "Procedure takes 3 or 4 arguments and no trailing clauses."
        )
    return tuple(arguments)
