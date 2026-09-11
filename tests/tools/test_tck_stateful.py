"""Adversarial oracle tests plus an unchanged typed-fixture native/reopen scenario."""

from dataclasses import replace

import pytest

from tests.tools.test_tck_ledger import report
from tools.tck_stateful import GraphState, ObservedError, QueryObservation, run_stateful_case
from tools.tck_native import NativeScenarioBackend


def state(*ids):
    return GraphState(nodes=tuple(ids), labels=("N",) if ids else (),
                      properties=tuple((i, "id", ("int", i)) for i in ids))


class FakeBackend:
    adaptations = ()

    def __init__(self, *, fault=None):
        self.current = state()
        self.fault = fault
        self.calls = []

    def admit(self, case):
        self.calls.append("admit")

    def setup(self, query, parameters):
        self.calls.append(query)
        self.current = state(1)
        return QueryObservation()

    def execute(self, query, parameters, *, control):
        self.calls.append(query)
        if control:
            return QueryObservation(("id",), ((1,), (999 if self.fault == "rows" else 2,)))
        self.current = state(1, 2, 3) if self.fault == "extra" else (
            state(1) if self.fault == "missing" else state(1, 2))
        return QueryObservation()

    def snapshot(self):
        return self.current

    def reopen(self):
        self.calls.append("reopen")
        if self.fault == "durability":
            self.current = state(1)


def test_sequential_setup_write_control_and_durable_reopen():
    backend = FakeBackend()
    result = run_stateful_case(report()["cases"][0], backend)
    assert result["conformance"] == "passed"
    assert backend.calls == ["admit", "CREATE (:N {id: 1})", "CREATE (:N {id: 2})",
                             "MATCH (n:N) RETURN n.id AS id ORDER BY id", "reopen"]


@pytest.mark.parametrize("fault", ["extra", "missing", "rows", "durability"])
def test_missing_extra_effects_wrong_rows_and_lost_durability_fail(fault):
    result = run_stateful_case(report()["cases"][0], FakeBackend(fault=fault))
    assert result["conformance"] == "failed", result


def test_unknown_late_step_refuses_before_any_fixture_execution():
    case = report()["cases"][0]
    case["steps"].append({"text": "an unknown fixture"})
    backend = FakeBackend()
    assert run_stateful_case(case, backend)["conformance"] == "not_run"
    assert not backend.calls


@pytest.mark.parametrize("phase,detail,leak,expected", [
    ("runtime", "InvalidArgumentValue", False, "passed"),
    ("compile time", "InvalidArgumentValue", False, "failed"),
    ("unknown", "InvalidArgumentValue", False, "failed"),
    ("runtime", "WrongDetail", False, "failed"),
    ("runtime", "InvalidArgumentValue", True, "failed"),
])
def test_error_taxonomy_phase_and_actual_rollback_are_independent(phase, detail, leak, expected):
    class ErrorBackend(FakeBackend):
        def execute(self, query, parameters, *, control):
            if leak:
                self.current = state(1)
            return QueryObservation(error=ObservedError("TypeError", phase, detail))

    case = {"steps": [
        {"text": "an empty graph"},
        {"text": "executing query:", "argument": {"docString": {"content": "RETURN 1 / 0"}}},
        {"text": "a TypeError should be raised at runtime: InvalidArgumentValue"},
        {"text": "no side effects"},
    ]}
    assert run_stateful_case(case, ErrorBackend())["conformance"] == expected


def test_changed_property_has_removal_and_addition_not_zero_net_effect():
    before = state(1)
    after = replace(before, properties=((1, "id", ("int", 2)),))
    delta = after.counts_since(before)
    assert delta["+properties"] == delta["-properties"] == 1
    assert delta["+nodes"] == delta["-nodes"] == 0


def test_schema_adaptation_never_counts_as_upstream_pass():
    backend = FakeBackend()
    backend.adaptations = ("explicit typed schema",)
    result = run_stateful_case(report()["cases"][0], backend)
    assert result["conformance"] == "adapted_passed"
    assert result["adaptations"] == ["explicit typed schema"]


@pytest.mark.parametrize("expectation,rows,status", [
    ("the result should be, in order:", ((2,), (1,)), "failed"),
    ("the result should be, in any order:", ((2,), (1,)), "passed"),
    ("the result should be, in any order:", ((1,), (1,), (2,)), "failed"),
    ("the result should be, in any order:", ((True,), (2,)), "failed"),
])
def test_order_multiplicity_and_boolean_integer_identity_are_not_weakened(expectation, rows, status):
    class ReadBackend(FakeBackend):
        def execute(self, query, parameters, *, control):
            return QueryObservation(("n",), rows)

    case = {"steps": [
        {"text": "an empty graph"},
        {"text": "executing query:", "argument": {"docString": {"content": "UNWIND [1,2] AS n RETURN n"}}},
        {"text": expectation, "argument": {"dataTable": {"rows": [
            {"cells": [{"value": "n"}]}, {"cells": [{"value": "1"}]},
            {"cells": [{"value": "2"}]},
        ]}}},
        {"text": "no side effects"},
    ]}
    assert run_stateful_case(case, ReadBackend())["conformance"] == status


def test_unsupported_case_does_not_even_create_a_native_database():
    case = report()["cases"][0]
    case["steps"].append({"text": "unknown late step"})
    backend = NativeScenarioBackend()
    try:
        assert run_stateful_case(case, backend)["conformance"] == "not_run"
        assert backend.temporary is None
        assert backend.database is None
    finally:
        backend.close()


def test_native_unchanged_queries_commit_observe_and_reopen():
    backend = NativeScenarioBackend(schema=("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))",))
    try:
        result = run_stateful_case(report()["cases"][0], backend)
        assert result["conformance"] == "adapted_passed", result
        assert len(backend.snapshot().nodes) == 2
        assert len(backend.snapshot().properties) == 2
    finally:
        backend.close()


def test_native_late_write_failure_is_observed_through_independent_scan():
    backend = NativeScenarioBackend(schema=("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))",))
    try:
        backend.admit({})
        before = backend.snapshot()
        result = backend.execute("CREATE (:N {id: 1}) WITH 1 AS x RETURN x / 0", {}, control=False)
        assert result.error is not None
        assert result.error.phase == "unknown"  # Do not invent TCK runtime equivalence.
        assert backend.snapshot().equivalent(before)
        backend.reopen()
        assert backend.snapshot().equivalent(before)
    finally:
        backend.close()


def test_native_relationship_endpoint_columns_are_not_counted_as_properties():
    backend = NativeScenarioBackend(schema=(
        "CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))",
        "CREATE REL TABLE R(FROM N TO N, weight INT64)",
    ))
    try:
        backend.admit({})
        assert backend.execute("CREATE (:N {id: 1}), (:N {id: 2})", {}, control=False).error is None
        before = backend.snapshot()
        assert backend.execute("MATCH (a:N {id:1}), (b:N {id:2}) CREATE (a)-[:R {weight: 5}]->(b)",
                               {}, control=False).error is None
        after = backend.snapshot()
        effects = after.counts_since(before)
        assert effects["+relationships"] == 1
        assert effects["+properties"] == 1
        assert effects["+labels"] == 0
        backend.reopen()
        assert backend.snapshot().equivalent(after)
    finally:
        backend.close()
