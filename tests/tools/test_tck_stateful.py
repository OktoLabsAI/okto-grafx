"""Adversarial oracle tests plus an unchanged typed-fixture native/reopen scenario."""

from dataclasses import replace

import pytest

from tests.tools.test_tck_ledger import report
from tools.tck_stateful import GraphState, ObservedError, QueryObservation, run_stateful_case
from tools.tck_native import NativeScenarioBackend


def test_native_snapshot_observes_membership_not_physical_table_names(monkeypatch):
    backend = NativeScenarioBackend()
    try:
        backend.admit({"steps": []})
        assert backend.setup("CREATE (:A:B {id:1}), (:B:A {id:2}), (n:C) REMOVE n:C", {}).error is None
        before = backend.snapshot()
        assert len(before.nodes) == 3
        assert before.labels == ("A", "B")
        assert backend.setup("MATCH (n:B) REMOVE n:A", {}).error is None
        expected = backend.snapshot()
        assert len(expected.nodes) == 3 and expected.labels == ("B",)
        backend.reopen()

        def no_queries(*args, **kwargs):
            raise AssertionError("The state oracle must use physical scans, not execute queries")

        monkeypatch.setattr(type(backend.database), "execute", no_queries)
        assert backend.snapshot() == expected
    finally:
        backend.close()


def test_native_entity_results_use_independent_reference_values_not_entity_equality():
    from okto_grafx import EntityIdentity, EntityProvenance, NodeValue, RelationshipValue
    from tools.tck_native import _reference_result_value
    from tools.tck_values import reference_key, reference_value

    identity = EntityIdentity(b"a" * 16, 1, "node", record_id=1)
    provenance = EntityProvenance(10, 1, 5)
    observed = NodeValue(identity, "N", {"id": 1, "missing": None}, provenance)
    wrong = NodeValue(identity, "N", {"id": 2}, provenance)
    assert observed == wrong  # Engine identity equality is NOT a property oracle.
    expected = reference_key(reference_value("(:N {id:1})"))
    assert reference_key(_reference_result_value(observed)) == expected
    assert reference_key(_reference_result_value(wrong)) != expected
    edge = RelationshipValue(EntityIdentity(b"a" * 16, 2, "relationship", record_id=1),
                             "R", identity, identity, {"weight": 7}, provenance)
    assert reference_key(_reference_result_value(edge)) == reference_key(reference_value("[:R {weight:7}]"))
    assert reference_key(_reference_result_value([observed, {"node": observed}])) == reference_key(
        reference_value("[(:N {id:1}), {node:(:N {id:1})}]"))


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


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("durable_leak", [False, True])
def test_zero_effect_write_attempt_still_checks_reopen(failed, durable_leak):
    class ZeroEffectBackend(FakeBackend):
        def execute(self, query, parameters, *, control):
            return QueryObservation(attempted_write=True, error=(
                ObservedError("TypeError", "runtime", "InvalidArgumentType") if failed else None))

        def reopen(self):
            self.calls.append("reopen")
            if durable_leak:
                self.current = state(99)

    case = {"steps": [
        {"text": "an empty graph"},
        {"text": "executing query:", "argument": {"docString": {"content": "MATCH (n:N) DELETE n"}}},
        {"text": ("a TypeError should be raised at runtime: InvalidArgumentType" if failed
                  else "the result should be empty")},
        {"text": "no side effects"},
    ]}
    backend = ZeroEffectBackend()
    result = run_stateful_case(case, backend)
    assert result["conformance"] == ("failed" if durable_leak else "passed"), result
    assert backend.calls == ["admit", "reopen"]
    if durable_leak:
        assert "Durable reopen changed graph state" in result["reason"]


@pytest.mark.parametrize("query,writes", [
    ("RETURN 1", False),
    ("RETURN 1 UNION RETURN 2", False),
    ("MATCH (n:N) DELETE n", True),
    ("CREATE (:N) RETURN 1 UNION RETURN 2", True),
    ("CALL () { CREATE (:N) RETURN 1 AS x } RETURN x", True),
    ("CREATE NODE TABLE N(id INT64)", True),
])
def test_native_write_attempt_classification_covers_composed_queries(query, writes):
    from okto_grafx.domain.query.parser import parse
    from tools.tck_native import _attempts_write

    assert _attempts_write(parse(query)) is writes


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
        assert result.attempted_write
        assert result.error.phase == "unknown"  # Do not invent TCK runtime equivalence.
        assert backend.snapshot().equivalent(before)
        backend.reopen()
        assert backend.snapshot().equivalent(before)
    finally:
        backend.close()


@pytest.mark.parametrize("query,expectation", [
    ("CREATE (:N {id:1}) RETURN noSuchFunction(1)",
     "a SyntaxError should be raised at compile time: UnknownFunction"),
    ("CREATE (:N {id:1}) WITH $m AS m RETURN m[1]",
     "a TypeError should be raised at runtime: MapElementAccessByNonString"),
    ("MATCH (n:N) DELETE n", "the result should be empty"),
])
def test_native_zero_effect_write_reopens_even_without_a_fixture(monkeypatch, query, expectation):
    backend = NativeScenarioBackend(schema=("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))",))
    reopened = []
    original = backend.reopen

    def record_reopen():
        original()
        reopened.append(True)

    monkeypatch.setattr(backend, "reopen", record_reopen)
    case = {"steps": [
        {"text": "an empty graph"},
        {"text": "parameters are:", "argument": {"dataTable": {"rows": [
            {"cells": [{"value": "m"}, {"value": "{a: 1}"}]},
        ]}}},
        {"text": "executing query:", "argument": {"docString": {"content": query}}},
        {"text": expectation},
        {"text": "no side effects"},
    ]}
    try:
        observed = run_stateful_case(case, backend)
        assert observed["conformance"] == "adapted_passed", observed
        assert reopened == [True]
        assert backend.snapshot() == GraphState()
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
