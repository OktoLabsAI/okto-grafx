"""Signature-based CALL expansion, handle isolation and unchanged callback authority."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxConfigurationError, GrafxPlanError
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure
from okto_grafx.domain.query import parse
from okto_grafx.domain.query.ast import Literal, ReturnClause, ReturnItem
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.planner import build_plan
from okto_grafx.domain.query.procedure_resolution import resolve_procedure_calls


def registration(seen, **options):
    """Declare names explicitly instead of inspecting a Python callback signature."""
    def callback(value):
        seen.append(value)
        return ((value, "first"), (value + 1, "second"))
    return TabularProcedure("app.values", ("INT64",), (("value", "INT64"), ("name", "STRING")),
                             callback, **options)


@pytest.mark.parametrize("query, parameters", (
    ("CALL app.values(4)", None),
    ("CALL app.values", {"input": 4}),
    ("CALL app.values(4) YIELD *", None),
    ("CALL app.values YIELD *", {"input": 4}),
))
def test_standalone_columns_and_implicit_arguments_use_the_declared_signature(query, parameters):
    seen = []
    proc = registration(seen, argument_names=("input",))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        plan = db.explain(query)
        assert seen == []
        result = db.execute(query, parameters)
        assert result.columns == ("value", "name")
        assert result.rows == ((4, "first"), (5, "second"))
        assert result.plan == plan
        assert seen == [4]
        with db.query(query, parameters).cursor(batch_size=1) as cursor:
            assert list(cursor) == [(4, "first"), (5, "second")]
        assert seen == [4, 4]
        assert db.transactions.open_transactions == 0


@pytest.mark.parametrize("query, expected", (
    ("CALL app.values(1) YIELD * WHERE value > 1", ((2, "second"),)),
    ("CALL app.values(1) YIELD name AS text, value AS n", (("first", 1), ("second", 2))),
    ("UNWIND [1, 3] AS n CALL app.values(n) YIELD value RETURN value ORDER BY value", ((1,), (2,), (3,), (4,))),
    ("CALL { CALL app.values(1) YIELD value RETURN value } RETURN value", ((1,), (2,))),
    ("CALL app.values(1) YIELD value RETURN value UNION CALL app.values(2) YIELD value RETURN value ORDER BY value",
     ((1,), (2,), (3,))),
))
def test_output_expansion_composes_without_dropping_filters_or_aliases(query, expected):
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(registration([]),))) as db:
        assert db.execute(query).rows == expected


@pytest.mark.parametrize("query", (
    "CALL app.values YIELD value RETURN value",
    "UNWIND [] AS x CALL app.values RETURN x",
    "CALL { CALL app.values YIELD value RETURN value } RETURN value",
))
def test_implicit_parameter_passing_is_only_for_outer_standalone_calls(query):
    seen = []
    proc = registration(seen, argument_names=("input",))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(query, {"input": 1})
        assert failure.value.details["reason"] == "procedure_argument_mode"
        assert failure.value.details["query_phase"] == "planning"
        assert seen == []


@pytest.mark.parametrize("query", (
    "WITH 1 AS value CALL app.values(1) YIELD value RETURN value",
    "CALL app.values(1) YIELD value AS name, name RETURN name",
))
def test_expanded_and_named_yields_never_shadow_existing_bindings(query):
    seen = []
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(registration(seen),))) as db:
        with pytest.raises(GrafxPlanError):
            db.execute(query)
        assert seen == []


@pytest.mark.parametrize("names", (["input"], (), ("a", "b"), (True,), ("",)))
def test_invalid_argument_name_declarations_are_rejected(names):
    with pytest.raises(GrafxConfigurationError) as failure:
        registration([], argument_names=names)
    assert failure.value.details["field"] == "argument_names"


def test_duplicate_argument_names_are_rejected():
    with pytest.raises(GrafxConfigurationError):
        TabularProcedure("app.unit", ("INT64", "INT64"), (), lambda *args: None,
                         argument_names=("same", "same"))


def test_unnamed_signature_remains_explicit_only():
    seen = []
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(registration(seen),))) as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute("CALL app.values", {"value": 1})
        assert failure.value.details["reason"] == "procedure_argument_names"
        assert seen == []
        assert db.execute("CALL app.values(1)").rows == ((1, "first"), (2, "second"))


def test_zero_argument_unit_has_an_unambiguous_implicit_call():
    seen = []
    proc = TabularProcedure("app.unit", (), (), lambda: seen.append(True))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        assert db.execute("CALL app.unit").rows == ()
        assert seen == [True]
        with pytest.raises(GrafxPlanError):
            db.execute("CALL app.unit() YIELD *")
        assert seen == [True]


def test_resolution_is_handle_local_and_does_not_mutate_the_original_tree(tmp_path):
    original = parse("CALL app.unit()")
    description = original.describe()
    for name in ("first", "second"):
        proc = TabularProcedure("app.unit", (), ((name, "STRING"),), lambda: (("ok",),))
        with connect(tmp_path / name, extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
            assert db.execute("CALL app.unit()").columns == (name,)
            normalized = resolve_procedure_calls(original, {proc.name: proc})
            assert normalized.return_clause.column_names() == (name,)
            assert original.describe() == description
            plan = build_plan(original, catalog=Catalog(), procedures={proc.name: proc},
                              analysis=analyze(parse("RETURN 1 AS wrong")))
            assert plan.root.columns == (name,)


def test_permission_denial_precedes_output_and_argument_expansion():
    seen = []
    proc = registration(seen, argument_names=("input",), required_permissions=frozenset({"use"}))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute("CALL app.values", {"input": 1})
        assert failure.value.details["reason"] == "procedure_not_found"
        assert seen == []


def test_forged_invocation_flags_are_refused_before_semantic_traversal():
    query = parse("CALL app.values(1) YIELD value")
    clause = query.ordered_clauses()[0]
    malformed = replace(query, clause_pipeline=(replace(clause, yield_all=1),))
    with pytest.raises(GrafxPlanError) as failure:
        resolve_procedure_calls(malformed, {})
    assert failure.value.details["reason"] == "invalid_ast_structure"


@pytest.mark.parametrize("query, parameters", (
    ("CALL app.values", {}),
    ("CALL app.values($input)", {}),
    ("UNWIND [] AS x CALL app.values($input) YIELD value RETURN value", {}),
))
def test_missing_parameter_names_are_refused_before_any_callback(query, parameters):
    seen = []
    proc = registration(seen, argument_names=("input",))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(query, parameters)
        assert failure.value.details == {"field": "parameter", "value": "input",
                                         "reason": "missing_parameter", "query_phase": "planning"}
        assert seen == []
        assert db.transactions.open_transactions == 0


def test_implicit_arguments_have_the_same_value_type_checks():
    seen = []
    proc = registration(seen, argument_names=("input",))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(proc,))) as db:
        with pytest.raises(GrafxPlanError):
            db.execute("CALL app.values", {"input": True})
        assert seen == []


@pytest.mark.parametrize("query", (
    "CALL app.values(1) YIELD * RETURN value",
    "WITH 1 AS n CALL app.values(n) YIELD * RETURN value",
    "CALL { CALL app.values(1) YIELD * RETURN value } RETURN value",
))
def test_wildcard_yield_is_only_available_on_standalone_calls(query):
    seen = []
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(registration(seen),))) as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(query)
        assert failure.value.details["reason"] == "procedure_yield_mode"
        assert seen == []


def test_resolution_does_not_compare_or_render_literal_payloads():
    class HostValue:
        def __eq__(self, other):
            raise AssertionError("host equality executed")
        def __repr__(self):
            raise AssertionError("host formatting executed")
        __str__ = __repr__

    proc = registration([])
    query = parse("CALL app.values(1) YIELD value")
    clause = query.ordered_clauses()[0]
    literal = Literal(HostValue())
    inputs = replace(query, clause_pipeline=(replace(clause, arguments=(literal,)),))
    result = resolve_procedure_calls(inputs, {proc.name: proc})
    assert result.ordered_clauses()[0].arguments[0] is literal
    invalid_yield = replace(query, clause_pipeline=(replace(clause, yields=(ReturnItem(literal),)),))
    with pytest.raises(GrafxPlanError):
        resolve_procedure_calls(invalid_yield, {proc.name: proc})


def test_forged_standalone_flag_cannot_discard_an_explicit_return_expression():
    proc = registration([])
    query = parse("CALL app.values(1)")
    malformed = replace(query, return_clause=ReturnClause(items=(ReturnItem(Literal(99)),)))
    with pytest.raises(GrafxPlanError) as failure:
        resolve_procedure_calls(malformed, {proc.name: proc})
    assert failure.value.details["reason"] == "invalid_ast_structure"
