"""Published parity-module annotations must resolve, not only exist as strings."""

import importlib
import inspect
import ast
from decimal import localcontext, ROUND_CEILING, ROUND_FLOOR
from typing import get_type_hints

import pytest


MODULES = (
    "collection_json",
    "adapters.temporal_clock", "adapters.temporal_tzif", "adapters.temporal_zoneinfo",
    "domain.model.relationship_type", "domain.model.table_selection", "domain.model.node_labels",
    "domain.model.temporal_codec", "domain.model.temporal_interchange", "domain.model.temporal_values",
    "domain.model.stored_types",
    "domain.ports.temporal_clock", "domain.ports.temporal_zone",
    "domain.query.binding_inference", "domain.query.effects", "domain.query.entity_identity",
    "domain.query.entity_scalars", "domain.query.entity_values", "domain.query.existential",
    "domain.query.percentiles", "domain.query.projection_scope", "domain.query.structure",
    "domain.query.procedure_resolution",
    "domain.query.subquery_scope", "domain.query.temporal_functions",
    "domain.temporal_access", "domain.temporal_admission", "domain.temporal_arithmetic",
    "domain.temporal_between", "domain.temporal_comparison", "domain.temporal_components",
    "domain.temporal_runtime", "domain.temporal_text", "domain.temporal_truncation",
    "engine.copy_execution",
)


@pytest.mark.parametrize("name", MODULES)
def test_exports_and_runtime_type_hints_resolve(name):
    module = importlib.import_module("okto_grafx." + name)
    names = module.__all__
    assert len(names) == len(set(names))
    for symbol in names:
        assert type(symbol) is str and not symbol.startswith("_")
        value = getattr(module, symbol)
        if inspect.isfunction(value) or inspect.isclass(value):
            get_type_hints(value)


def test_temporal_union_remains_the_same_under_both_import_paths():
    from okto_grafx.domain.model.temporal_values import TemporalValue as model_type
    from okto_grafx.domain.temporal_arithmetic import TemporalValue as arithmetic_type

    assert model_type is arithmetic_type


@pytest.mark.parametrize("name", ["domain.temporal_text", "domain.model.temporal_interchange"])
def test_temporal_regex_patterns_are_fixed_ascii_grammar(name):
    module = importlib.import_module("okto_grafx." + name)
    calls = [node for node in ast.walk(ast.parse(inspect.getsource(module)))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id in {"_compile_pattern", "_fullmatch", "_search", "_split"}]
    assert calls
    for node in calls:
        assert isinstance(node.args[0], ast.Constant)
        assert type(node.args[0].value) is str and node.args[0].value.isascii()


def test_temporal_rational_arithmetic_ignores_decimal_context():
    from okto_grafx.domain.temporal_arithmetic import scale_duration
    from okto_grafx.domain.temporal_components import build_duration
    from okto_grafx.domain.temporal_text import parse_duration

    outputs = []
    for precision, rounding in ((1, ROUND_CEILING), (50, ROUND_FLOOR)):
        with localcontext() as context:
            context.prec = precision
            context.rounding = rounding
            parsed = parse_duration("P0.5M")
            built = build_duration({"months": 0.5, "seconds": 0.125})
            outputs.append((parsed, built, scale_duration(built, 0.25)))
    assert outputs[0] == outputs[1]
