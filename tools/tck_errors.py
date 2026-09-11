"""Narrow, evidence-based reference mappings; unmapped errors never become matches.

Only called at a proven native lexer/parser/analysis boundary. Message signatures
are paired with structured fields; changes require a focused mapper regression.
No expected scenario error is consulted while mapping an actual exception.
"""

from okto_grafx.errors import GrafxParseError, GrafxPlanError

if __package__:
    from tools.tck_stateful import ObservedError
else:
    from tck_stateful import ObservedError


def compile_error(error) -> ObservedError:
    field, value = error.details.get("field"), error.details.get("value")
    message = error.message
    detail = error.code
    if isinstance(error, GrafxParseError):
        if field == "character" and isinstance(value, str) and len(value) == 1 and not value.isascii():
            detail = "InvalidUnicodeCharacter"
        elif field in {"number", "integer"} and "outside the range a 64-bit integer can hold" in message:
            detail = "IntegerOverflow"
        elif field == "number_literal" and message.startswith("Invalid based integer literal"):
            detail = "InvalidNumberLiteral"
        elif field == "number" and "outside the range a double can hold" in message:
            detail = "FloatingPointOverflow"
        elif field == "escape" and message.startswith("A numeric string escape needs "):
            detail = "InvalidUnicodeLiteral"
    elif isinstance(error, GrafxPlanError):
        if field == "function" and message.startswith("There is no function named "):
            detail = "UnknownFunction"
    return ObservedError("SyntaxError", "compile time", detail)
