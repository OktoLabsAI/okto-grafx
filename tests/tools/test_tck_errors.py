"""Observed compile errors are matched without looking at scenario expectations."""

import pytest

from okto_grafx.errors import GrafxParseError
from tools.tck_errors import compile_error
from tools.tck_native import NativeScenarioBackend
from tools.tck_stateful import run_stateful_case


@pytest.mark.parametrize("query,detail", [
    ("RETURN 42 — 41", "InvalidUnicodeCharacter"),
    ("RETURN 9223372036854775809", "IntegerOverflow"),
    ("RETURN 1e999", "FloatingPointOverflow"),
    (r"RETURN '\u000X'", "InvalidUnicodeLiteral"),
    ("RETURN this_function_is_not_registered(1)", "UnknownFunction"),
    ("RETURN size(()--())", "UnexpectedSyntax"),
    ("RETURN [1,]", "UnexpectedSyntax"),
    ("RETURN (", "UnexpectedSyntax"),
    ("RETURN missing", "UndefinedVariable"),
    ("MATCH(n) WHERE (n) RETURN n", "InvalidArgumentType"),
    ("WITH 1 AS x WITH 2 AS y RETURN x", "UndefinedVariable"),
    ("WITH 1 AS x, x + 1 AS y RETURN y", "UndefinedVariable"),
    ("UNWIND count(*) AS x RETURN x", "InvalidAggregation"),
    ("MATCH (n) RETURN n.num1 ORDER BY max(n.num2)", "InvalidAggregation"),
    ("MATCH (n) WITH n.num1 AS foo ORDER BY count(n) RETURN foo", "InvalidAggregation"),
    ("MATCH (n) WITH n.num1 AS foo ORDER BY n.name, max(n.num2) RETURN foo", "InvalidAggregation"),
])
def test_native_compile_phase_and_exact_reference_detail(query, detail):
    case = {"steps": [
        {"text": "an empty graph"},
        {"text": "executing query:", "argument": {"docString": {"content": query}}},
        {"text": f"a SyntaxError should be raised at compile time: {detail}"},
        {"text": "no side effects"},
    ]}
    backend = NativeScenarioBackend()
    try:
        outcome = run_stateful_case(case, backend)
        assert outcome["conformance"] == "passed", outcome
        assert not backend.snapshot().nodes
    finally:
        backend.close()


def test_same_error_class_without_supported_evidence_remains_unmapped():
    error = GrafxParseError("unrecognized parse refusal", field="character", value="?")
    assert compile_error(error).detail == "parse_error"
    error = GrafxParseError("unrecognized parse refusal", field="name", value="—")
    assert compile_error(error).detail == "parse_error"


@pytest.mark.parametrize("details", [
    {"expected": "an expression", "found": ")"},
    {"reason": "unexpected_syntax", "query_phase": "planning"},
    {"reason": "unexpected_syntax", "query_phase": "execution", "expected": "x", "found": ")"},
])
def test_syntax_mapping_requires_matching_native_evidence(details):
    error = GrafxParseError("Expected an expression", **details)
    assert compile_error(error).detail == "parse_error"


@pytest.mark.parametrize("details", [
    {"field": "variable", "reason": "undefined_variable"},
    {"field": "sort", "reason": "undefined_variable", "query_phase": "planning"},
    {"field": "variable", "reason": "invalid_aggregation_context", "query_phase": "planning"},
    {"field": "predicate", "reason": "predicate_argument_type"},
    {"field": "predicate", "reason": "predicate_argument_type", "query_phase": "execution"},
    {"field": "variable", "reason": "predicate_argument_type", "query_phase": "planning"},
])
def test_scope_mapping_requires_matching_native_evidence(details):
    from okto_grafx.errors import GrafxPlanError

    assert compile_error(GrafxPlanError("A semantic refusal", **details)).detail == "plan_error"


@pytest.mark.parametrize("query,detail", [
    ("RETURN range(1, 2, 0)", "NumberOutOfRange"),
    ("RETURN range(true, 2)", "InvalidArgumentType"),
    ("RETURN range(1, 2, {})", "InvalidArgumentType"),
])
def test_range_mapper_uses_actual_native_runtime_error(query, detail):
    backend = NativeScenarioBackend()
    try:
        backend.admit({"steps": []})
        observed = backend.execute(query, {}, control=False)
        assert observed.error.type == "ArgumentError"
        assert observed.error.phase == "runtime"
        assert observed.error.detail == detail
    finally:
        backend.close()


@pytest.mark.parametrize("overrides", [
    {"query_phase": "planning"}, {"query_phase": None},
    {"value": "OTHER"}, {"field": "operator"}, {"reason": "other"},
])
def test_range_mapping_requires_complete_matching_evidence(overrides):
    from okto_grafx.errors import GrafxPlanError
    from tools.tck_errors import native_error

    details = {"field": "function", "value": "RANGE", "reason": "range_argument_type", "query_phase": "execution"}
    assert native_error(GrafxPlanError("Range refusal", **(details | overrides))).phase == "unknown"


@pytest.mark.parametrize("details", [
    {"field": "operator", "value": "IN", "reason": "membership_operand_type"},
    {"field": "function", "value": "IN", "reason": "membership_operand_type", "query_phase": "planning"},
    {"field": "operator", "value": "OTHER", "reason": "membership_operand_type", "query_phase": "execution"},
])
def test_membership_mapping_requires_full_evidence(details):
    from okto_grafx.errors import GrafxPlanError
    from tools.tck_errors import native_error

    assert native_error(GrafxPlanError("IN refusal", **details)).phase == "unknown"


@pytest.mark.parametrize("function", ["length", "nodes", "relationships"])
def test_path_mapper_uses_actual_native_static_type_error(function):
    backend = NativeScenarioBackend()
    try:
        backend.admit({"steps": []})
        observed = backend.execute(f"RETURN {function}(1)", {}, control=False)
        assert observed.error.type == "SyntaxError"
        assert observed.error.phase == "compile time"
        assert observed.error.detail == "InvalidArgumentType"
    finally:
        backend.close()


@pytest.mark.parametrize("overrides", [
    {"query_phase": None}, {"field": "operator"}, {"value": "SIZE"}, {"reason": "other"},
])
def test_path_mapping_requires_complete_native_evidence(overrides):
    from okto_grafx.errors import GrafxPlanError
    from tools.tck_errors import native_error

    details = {"field": "function", "value": "LENGTH", "reason": "path_argument_type", "query_phase": "planning"}
    assert native_error(GrafxPlanError("Path refusal", **(details | overrides))).phase == "unknown"


@pytest.mark.parametrize("function", ["properties", "labels", "type"])
@pytest.mark.parametrize("query,phase,error_type,detail", [
    ("RETURN {function}(1)", "compile time", "SyntaxError", "InvalidArgumentType"),
    ("UNWIND [null,1] AS value RETURN {function}(value)", "runtime", "TypeError", "InvalidArgumentValue"),
])
def test_entity_mapper_uses_native_phase_evidence(function, query, phase, error_type, detail):
    backend = NativeScenarioBackend()
    try:
        backend.admit({"steps": []})
        observed = backend.execute(query.format(function=function), {}, control=False)
        assert (observed.error.type, observed.error.phase, observed.error.detail) == (error_type, phase, detail)
    finally:
        backend.close()


@pytest.mark.parametrize("overrides", [
    {"query_phase": None}, {"field": "operator"}, {"value": "OTHER"}, {"reason": "other"},
])
def test_entity_mapping_requires_complete_native_evidence(overrides):
    from okto_grafx.errors import GrafxPlanError
    from tools.tck_errors import native_error

    details = {"field": "function", "value": "PROPERTIES", "reason": "entity_function_argument_type", "query_phase": "execution"}
    assert native_error(GrafxPlanError("Entity refusal", **(details | overrides))).phase == "unknown"


@pytest.mark.parametrize("query,detail", [
    ("MATCH(n) WHERE count(n)>0 RETURN n", "InvalidAggregation"),
    ("MATCH(n) MATCH p=(n)-[*]->() WHERE p.name='x' RETURN p", "InvalidArgumentType"),
])
def test_match_where_native_error_phase_and_category(query, detail):
    backend = NativeScenarioBackend()
    try:
        backend.admit({"steps":[]})
        observed = backend.execute(query, {}, control=False)
        assert (observed.error.type,observed.error.phase,observed.error.detail) == ('SyntaxError','compile time',detail)
    finally:
        backend.close()


@pytest.mark.parametrize("overrides", [
    {"query_phase":None}, {"query_phase":"execution"}, {"reason":"other"},
    {"field":"function"}, {"value":None},
])
def test_path_property_mapping_needs_complete_static_evidence(overrides):
    from okto_grafx.errors import GrafxPlanError
    from tools.tck_errors import native_error
    details = {"field":"property", "value":"name", "reason":"path_property_type", "query_phase":"planning"}
    assert native_error(GrafxPlanError("Path property refusal", **(details | overrides))).phase == 'unknown'


@pytest.mark.parametrize("query,detail", [
    ("MATCH()-[r]->() MATCH(r) RETURN r", "VariableTypeConflict"),
    ("MATCH(r) MATCH()-[r]->() RETURN r", "VariableTypeConflict"),
    ("MATCH p=()-->() MATCH(p) RETURN p", "VariableTypeConflict"),
    ("WITH 1 AS n MATCH(n) RETURN n", "VariableTypeConflict"),
    ("MATCH(p) MATCH p=()-->() RETURN p", "VariableAlreadyBound"),
    ("MATCH()-[p]->() MATCH p=()-->() RETURN p", "VariableAlreadyBound"),
    ("MATCH p=(p)-->() RETURN p", "VariableAlreadyBound"),
    ("MATCH p=()-[p]->() RETURN p", "VariableAlreadyBound"),
    ("WITH 1 AS p MATCH p=()-->() RETURN p", "VariableAlreadyBound"),
    ("MATCH(p)--(), p=()-->() RETURN p", "VariableAlreadyBound"),
    ("MATCH p=()-->(), (p)--() RETURN p", "VariableTypeConflict"),
    ("MATCH p=()-->(), ()-[p]->() RETURN p", "VariableTypeConflict"),
    ("MATCH()-[p]->(), p=()-->() RETURN p", "VariableAlreadyBound"),
    ("MATCH(a)-[r]->()-[r]->(a) RETURN r", "RelationshipUniquenessViolation"),
])
def test_scope_error_categories_follow_native_binding_state(query, detail):
    from okto_grafx.domain.query.parser import parse
    from okto_grafx.domain.query.analysis import analyze
    from okto_grafx.errors import GrafxPlanError
    with pytest.raises(GrafxPlanError) as error:
        analyze(parse(query))
    observed = compile_error(error.value)
    assert (observed.type,observed.phase,observed.detail) == ('SyntaxError','compile time',detail)


@pytest.mark.parametrize("reason,category", [('variable_type_conflict','VariableTypeConflict'),
                                            ('variable_already_bound','VariableAlreadyBound')])
@pytest.mark.parametrize("overrides", [{"field":"operator"},{"query_phase":"execution"},{"query_phase":None},{"reason":"unknown"}])
def test_scope_mapping_does_not_guess_missing_or_wrong_evidence(reason, category, overrides):
    from okto_grafx.errors import GrafxPlanError
    fields = {'field':'variable','value':'n','reason':reason,'query_phase':'planning'}
    assert compile_error(GrafxPlanError('Binding refusal', **(fields | overrides))).detail != category


@pytest.mark.parametrize("overrides", [{"field":"pattern"}, {"value":None},
                                      {"query_phase":None}, {"query_phase":"execution"}, {"reason":"other"}])
def test_relationship_uniqueness_mapping_requires_native_evidence(overrides):
    from okto_grafx.errors import GrafxPlanError
    fields = {"field":"variable", "value":"r", "query_phase":"planning",
              "reason":"relationship_uniqueness_violation"}
    assert compile_error(GrafxPlanError("Relationship refusal", **(fields | overrides))).detail != "RelationshipUniquenessViolation"


@pytest.mark.parametrize("query", ["MATCH(n $properties) RETURN n", "MATCH()-[r:R $properties]->() RETURN r"])
def test_bare_pattern_parameter_has_native_error_category(query):
    from okto_grafx.domain.query.parser import parse
    from okto_grafx.errors import GrafxParseError
    with pytest.raises(GrafxParseError) as error:
        parse(query)
    assert error.value.details["value"] == "properties"
    observed = compile_error(error.value)
    assert (observed.type, observed.phase, observed.detail) == ("SyntaxError", "compile time", "InvalidParameterUse")


@pytest.mark.parametrize("overrides", [{"field":"parameter"}, {"value":None}, {"reason":"other"},
                                      {"query_phase":None}, {"query_phase":"execution"}])
def test_pattern_parameter_mapping_needs_complete_evidence(overrides):
    from okto_grafx.errors import GrafxParseError
    details = {"field":"pattern_properties", "value":"p", "reason":"invalid_parameter_use", "query_phase":"planning"}
    assert compile_error(GrafxParseError("Parameter refusal", **(details | overrides))).detail != "InvalidParameterUse"


@pytest.mark.parametrize("suffix", ["..", "*-2", "*1..-2"])
def test_malformed_range_mapping_uses_native_parser_cause(suffix):
    from okto_grafx.domain.query.parser import parse
    from okto_grafx.errors import GrafxParseError
    with pytest.raises(GrafxParseError) as error:
        parse(f"MATCH()-[:R{suffix}]->() RETURN 1")
    assert compile_error(error.value).detail == "InvalidRelationshipPattern"


@pytest.mark.parametrize("overrides", [{"field":"pattern"}, {"reason":"other"}, {"query_phase":None}, {"query_phase":"execution"}])
def test_malformed_range_mapping_does_not_guess_missing_evidence(overrides):
    from okto_grafx.errors import GrafxParseError
    details = {"field":"hops", "reason":"invalid_relationship_pattern", "query_phase":"planning"}
    assert compile_error(GrafxParseError("Range refusal", **(details | overrides))).detail != "InvalidRelationshipPattern"


@pytest.mark.parametrize("overrides", [{}, {"field":"property"}, {"reason":"other"},
                                      {"query_phase":None}, {"query_phase":"planning"}])
def test_deleted_entity_mapping_requires_native_runtime_evidence(overrides):
    from okto_grafx.errors import GrafxPlanError
    from tools.tck_errors import native_error
    details = {"field":"entity", "reason":"deleted_entity_access", "query_phase":"execution"}
    observed = native_error(GrafxPlanError("Deleted entity content", **(details | overrides)))
    if not overrides:
        assert (observed.type, observed.phase, observed.detail) == ("EntityNotFound", "runtime", "DeletedEntityAccess")
    else:
        assert observed.detail != "DeletedEntityAccess"
