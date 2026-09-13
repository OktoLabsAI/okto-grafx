"""Narrow, evidence-based reference mappings; unmapped errors never become matches.

Only called at a proven native lexer/parser/analysis boundary. Message signatures
are paired with structured fields; changes require a focused mapper regression.
No expected scenario error is consulted while mapping an actual exception.
"""

from okto_grafx.errors import GrafxParseError, GrafxPlanError, GrafxQueryError

if __package__:
    from tools.tck_stateful import ObservedError
else:
    from tck_stateful import ObservedError


def compile_error(error) -> ObservedError:
    field, value = error.details.get("field"), error.details.get("value")
    message = error.message
    detail = error.code
    if isinstance(error, GrafxParseError):
        if (field == "number_literal" and isinstance(value, str)
                and error.details.get("reason") == "invalid_numeric_literal"
                and error.details.get("query_phase") == "planning"):
            return ObservedError("SyntaxError", "compile time", "InvalidNumberLiteral")
        if (field == "character" and isinstance(value, str) and len(value) == 1 and value.isascii()
                and error.details.get("reason") == "unsupported_query_character"
                and error.details.get("query_phase") == "planning"):
            return ObservedError("SyntaxError", "compile time", "UnexpectedSyntax")
        if (field == "hops" and error.details.get("reason") == "invalid_relationship_pattern"
                and error.details.get("query_phase") == "planning"):
            detail = "InvalidRelationshipPattern"
        elif (field == "pattern_properties" and isinstance(value, str)
                and error.details.get("reason") == "invalid_parameter_use"
                and error.details.get("query_phase") == "planning"):
            detail = "InvalidParameterUse"
        elif (error.details.get("reason") == "unexpected_syntax"
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
        if error.details.get("query_phase") == "planning" and (
            (field, error.details.get("reason")) in {
                ("procedure", "procedure_not_found"), ("procedure_arity", "procedure_arity"),
                ("procedure_arguments", "procedure_argument_mode"),
                ("procedure_type", "procedure_argument_type"), ("parameter", "missing_parameter"),
                ("yield", "variable_already_bound"),
                ("yield", "procedure_yield_mode"),
            }
        ):
            return native_error(error)
        if error.details.get("query_phase") == "planning":
            pattern_detail = {("types","no_single_relationship_type"):"NoSingleRelationshipType",
                              ("hops","creating_variable_length"):"CreatingVarLength",
                              ("direction","requires_directed_relationship"):"RequiresDirectedRelationship"}.get(
                                  (field,error.details.get("reason")))
            if pattern_detail is not None:
                return ObservedError("SyntaxError","compile time",pattern_detail)
        if (field == "target" and error.details.get("query_phase") == "planning"
                and error.details.get("reason") in {"delete_argument_type", "invalid_delete"}):
            return native_error(error)
        if (field in {"alias", "column", "item"} and isinstance(value, str)
                and error.details.get("reason") == "column_name_conflict"
                and error.details.get("query_phase") == "planning"):
            return ObservedError("SyntaxError", "compile time", "ColumnNameConflict")
        if (field == "item" and isinstance(value, str)
                and error.details.get("reason") == "no_expression_alias"
                and error.details.get("query_phase") == "planning"):
            return ObservedError("SyntaxError", "compile time", "NoExpressionAlias")
        if (field == "function" and value == "SIZE"
                and error.details.get("reason") == "size_path_argument_type"
                and error.details.get("query_phase") == "planning"):
            return ObservedError("SyntaxError", "compile time", "InvalidArgumentType")
        if (field == "expression" and error.details.get("reason") == "arithmetic_operand_type"
                and error.details.get("query_phase") == "planning"):
            return native_error(error)
        if (field == "aggregation" and error.details.get("reason") == "non_deterministic_aggregate_argument"
                and error.details.get("query_phase") == "planning"):
            return ObservedError("SyntaxError", "compile time", "NonConstantExpression")
        if (field == "function" and error.details.get("reason") == "nested_aggregation"
                and error.details.get("query_phase") == "planning"):
            return ObservedError("SyntaxError", "compile time", "NestedAggregation")
        if (field == "aggregation" and error.details.get("reason") == "ambiguous_aggregation_expression"
                and error.details.get("query_phase") == "planning"):
            return ObservedError("SyntaxError", "compile time", "AmbiguousAggregationExpression")
        if (field in {"skip", "limit"} and error.details.get("query_phase") == "planning"
                and error.details.get("reason") in {"window_argument_type", "negative_window", "non_constant_window"}):
            return native_error(error)
        if (field == "return_star" and error.details.get("reason") == "no_variables_in_scope"
                and error.details.get("query_phase") == "planning"):
            detail = "NoVariablesInScope"
        elif (field == "union" and value == "columns"
                and error.details.get("reason") == "different_columns_in_union"
                and error.details.get("query_phase") == "planning"):
            detail = "DifferentColumnsInUnion"
        elif (field == "union" and value == "composition"
                and error.details.get("reason") == "mixed_union_composition"
                and error.details.get("query_phase") == "planning"):
            detail = "InvalidClauseComposition"
        elif (field == "subquery" and error.details.get("reason") in {"existential_write", "existential_return_mismatch"}
                and error.details.get("query_phase") == "planning"):
            detail = "InvalidClauseComposition"
        elif (field == "variable" and error.details.get("reason") == "undefined_variable"
                and error.details.get("query_phase") == "planning"):
            detail = "UndefinedVariable"
        elif (field == "variable" and error.details.get("reason") == "variable_type_conflict"
                and error.details.get("query_phase") == "planning"):
            detail = "VariableTypeConflict"
        elif (field == "variable" and isinstance(value, str)
                and error.details.get("reason") == "relationship_uniqueness_violation"
                and error.details.get("query_phase") == "planning"):
            detail = "RelationshipUniquenessViolation"
        elif (field in {"variable", "pattern"} and error.details.get("reason") == "variable_already_bound"
                and error.details.get("query_phase") == "planning"):
            detail = "VariableAlreadyBound"
        elif (field in {"expression", "predicate", "sort_item", "iteration"} and error.details.get("reason") == "invalid_aggregation_context"
                and error.details.get("query_phase") == "planning"):
            detail = "InvalidAggregation"
        elif field == "function" and message.startswith("There is no function named "):
            detail = "UnknownFunction"
        elif (field == "predicate" and error.details.get("reason") == "predicate_argument_type"
                and error.details.get("query_phase") == "planning"):
            detail = "InvalidArgumentType"
    return ObservedError("SyntaxError", "compile time", detail)


def native_error(error) -> ObservedError:
    """Map only explicitly typed native refusals with raise-site phase evidence.

    A message, error class, or the fact that execute raised is not phase proof.
    Missing parameter names are pre-execution admission errors; dynamic parameter
    value errors remain runtime. Schema/static type validation is compile time.
    """
    phase = {"planning": "compile time", "execution": "runtime"}.get(error.details.get("query_phase"))
    reason = error.details.get("reason")
    field = error.details.get("field")
    if (isinstance(error, GrafxQueryError) and phase == "runtime"
            and reason == "connected_node_delete" and type(error.details.get("table")) is str
            and type(error.details.get("relationship_table")) is str):
        return ObservedError("ConstraintVerificationFailed", phase, "DeleteConnectedNode")
    if isinstance(error, GrafxPlanError) and phase is not None:
        if phase == "compile time":
            procedure_error = {
                ("procedure", "procedure_not_found"): ("ProcedureError", "ProcedureNotFound"),
                ("procedure_arity", "procedure_arity"): ("SyntaxError", "InvalidNumberOfArguments"),
                ("procedure_arguments", "procedure_argument_mode"): ("SyntaxError", "InvalidArgumentPassingMode"),
                ("procedure_type", "procedure_argument_type"): ("SyntaxError", "InvalidArgumentType"),
                ("parameter", "missing_parameter"): ("ParameterMissing", "MissingParameter"),
                ("yield", "variable_already_bound"): ("SyntaxError", "VariableAlreadyBound"),
                ("yield", "procedure_yield_mode"): ("SyntaxError", "UnexpectedSyntax"),
            }.get((field, reason))
            if procedure_error is not None:
                return ObservedError(procedure_error[0], phase, procedure_error[1])
        if field == "properties" and reason == "merge_null_property" and phase == "runtime":
            return ObservedError("SemanticError",phase,"MergeReadOwnWrites")
        if field == "target" and reason == "invalid_delete" and phase == "compile time":
            return ObservedError("SyntaxError", phase, "InvalidDelete")
        if field == "target" and reason == "delete_argument_type":
            return ObservedError("SyntaxError" if phase == "compile time" else "TypeError", phase, "InvalidArgumentType")
        if (field == "function" and error.details.get("value") == "SIZE"
                and reason == "size_path_argument_type" and phase == "compile time"):
            return ObservedError("SyntaxError", phase, "InvalidArgumentType")
        if (reason == "arithmetic_operand_type"
                and (field == "operator" and phase == "runtime" and error.details.get("value") in {"+", "-", "*", "/", "%", "^"}
                     or field == "expression" and phase == "compile time" and error.details.get("operator") in {"+", "-", "*", "/", "%", "^"})):
            return ObservedError("SyntaxError" if phase == "compile time" else "TypeError", phase, "InvalidArgumentType")
        if (field == "function" and phase == "runtime"
                and error.details.get("value") in {"TOINTEGER", "TOFLOAT", "TOBOOLEAN", "TOSTRING"}
                and reason == "conversion_argument_type"):
            return ObservedError("TypeError", phase, "InvalidArgumentValue")
        if field == "percentile" and phase == "runtime":
            if reason == "percentile_argument_bounds":
                return ObservedError("ArgumentError", phase, "NumberOutOfRange")
            if reason in {"percentile_argument_type", "percentile_sample_type"}:
                return ObservedError("TypeError", phase, "InvalidArgumentType")
        if field in {"skip", "limit"}:
            window_detail = {"window_argument_type":"InvalidArgumentType", "negative_window":"NegativeIntegerArgument"}.get(reason)
            if window_detail is not None:
                return ObservedError("SyntaxError", phase, window_detail)
            if reason == "non_constant_window" and phase == "compile time":
                return ObservedError("SyntaxError", phase, "NonConstantExpression")
        if field == "entity" and reason == "deleted_entity_access" and phase == "runtime":
            return ObservedError("EntityNotFound", phase, "DeletedEntityAccess")
        if field == "property" and reason == "path_property_type" and phase == "compile time" and isinstance(error.details.get("value"), str):
            return ObservedError("SyntaxError", phase, "InvalidArgumentType")
        if field == "function" and error.details.get("value") in {"PROPERTIES", "KEYS", "LABELS", "TYPE"} and reason == "entity_function_argument_type":
            return ObservedError("SyntaxError" if phase == "compile time" else "TypeError", phase,
                                 "InvalidArgumentType" if phase == "compile time" else "InvalidArgumentValue")
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
