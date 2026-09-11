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
        if (error.details.get("reason") == "unexpected_syntax"
                and error.details.get("query_phase") == "planning"
                and isinstance(error.details.get("expected"), str)
                and isinstance(error.details.get("found"), str)):
            detail = "UnexpectedSyntax"
        elif field == "character" and isinstance(value, str) and len(value) == 1 and not value.isascii():
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
        if (field == "union" and value == "columns"
                and error.details.get("reason") == "different_columns_in_union"
                and error.details.get("query_phase") == "planning"):
            detail = "DifferentColumnsInUnion"
        elif (field == "union" and value == "composition"
                and error.details.get("reason") == "mixed_union_composition"
                and error.details.get("query_phase") == "planning"):
            detail = "InvalidClauseComposition"
        elif (field == "variable" and error.details.get("reason") == "undefined_variable"
                and error.details.get("query_phase") == "planning"):
            detail = "UndefinedVariable"
        elif (field in {"expression", "sort_item"} and error.details.get("reason") == "invalid_aggregation_context"
                and error.details.get("query_phase") == "planning"):
            detail = "InvalidAggregation"
        elif field == "function" and message.startswith("There is no function named "):
            detail = "UnknownFunction"
    return ObservedError("SyntaxError", "compile time", detail)


def native_error(error) -> ObservedError:
    """Map only explicitly typed native refusals with raise-site phase evidence.

    A message, error class, or the fact that execute raised is not phase proof.
    Parameter binding is runtime; schema/static type validation is compile time.
    """
    phase = {"planning": "compile time", "execution": "runtime"}.get(error.details.get("query_phase"))
    reason = error.details.get("reason")
    field = error.details.get("field")
    if isinstance(error, GrafxPlanError) and phase is not None:
        if field == "function" and error.details.get("value") == "RANGE" and phase == "runtime":
            if reason in {"range_argument_type", "range_argument_bounds"}:
                return ObservedError("ArgumentError", phase,
                                     "InvalidArgumentType" if reason == "range_argument_type" else "NumberOutOfRange")
        if field == "function" and error.details.get("value") in {"LENGTH", "NODES", "RELATIONSHIPS"} and reason == "path_argument_type":
            return ObservedError("SyntaxError" if phase == "compile time" else "TypeError", phase, "InvalidArgumentType")
        if field == "operator" and reason == "boolean_operand_type":
            return ObservedError("SyntaxError" if phase == "compile time" else "TypeError",
                                 phase, "InvalidArgumentType")
        if field == "operator" and error.details.get("value") == "IN" and reason == "membership_operand_type":
            return ObservedError("SyntaxError" if phase == "compile time" else "TypeError",
                                 phase, "InvalidArgumentType")
        if field == "property" and reason == "property_subject_type":
            return ObservedError("TypeError", phase, "InvalidArgumentType")
        if field == "subscript" and reason in {"subscript_subject_type", "map_key_type", "list_index_type"}:
            detail = ("MapElementAccessByNonString" if reason == "map_key_type" and phase == "runtime"
                      else "ListElementAccessByNonInteger" if reason == "list_index_type" and phase == "runtime"
                      else "InvalidArgumentType")
            return ObservedError("TypeError", phase, detail)
    return ObservedError(type(error).__name__, "unknown", error.code)
