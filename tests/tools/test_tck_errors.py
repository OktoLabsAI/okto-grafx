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
