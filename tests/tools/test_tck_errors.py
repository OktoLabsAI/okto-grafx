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
