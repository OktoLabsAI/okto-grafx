"""Structural admission rejects hostile trees without narrowing valid composition."""

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query import ast, parse
from okto_grafx.domain.query.structure import validate_structure


def test_structural_cycle_is_typed_before_any_recursive_expression_walk():
    expression = ast.UnaryOperation(operator="NOT", operand=ast.Literal(True))
    object.__setattr__(expression, "operand", expression)
    statement = ast.Query(return_clause=ast.ReturnClause(items=(ast.ReturnItem(expression),)))
    with pytest.raises(GrafxPlanError, match="cycles"):
        validate_structure(statement)


def test_shared_dag_is_not_a_cycle_or_exponential_validation():
    expression = ast.Literal(1)
    for _ in range(24):
        expression = ast.BinaryOperation(operator="+", left=expression, right=expression)
    validate_structure(ast.Query(return_clause=ast.ReturnClause(items=(ast.ReturnItem(expression),))))


def test_shared_tuple_is_validated_against_each_field_type():
    # ReturnItem and SortItem are different contracts even if the tuple is shared.
    items = (ast.ReturnItem(ast.Literal(1)),)
    statement = ast.Query(return_clause=ast.ReturnClause(items=items, sort_items=items))
    with pytest.raises(GrafxPlanError, match="canonical"):
        validate_structure(statement)


@pytest.mark.parametrize("expression", [ast.Expression(), object.__new__(ast.Variable)])
def test_abstract_or_uninitialized_expression_is_typed(expression):
    statement = ast.Query(return_clause=ast.ReturnClause(items=(ast.ReturnItem(expression),)))
    with pytest.raises(GrafxPlanError) as raised:
        validate_structure(statement)
    assert raised.value.details["reason"] == "invalid_ast_structure"


def test_malformed_second_path_cannot_hide_behind_valid_first_path():
    query = parse("MATCH p=(a:A)-[:R]->(b:B), q=(c:A)-[:R]->(d:B) RETURN p,q")
    clause = query.match_clauses[0]
    bad = replace(clause.patterns[1], nodes=list(clause.patterns[1].nodes))
    query = replace(query, match_clauses=(replace(clause, patterns=(clause.patterns[0], bad)),))
    with pytest.raises(GrafxPlanError):
        validate_structure(query)


def test_foreign_metaclass_is_never_hashed_or_compared():
    class HostileMeta(type):
        def __hash__(cls):
            raise AssertionError("foreign metaclass hashed")

        def __eq__(cls, other):
            raise AssertionError("foreign metaclass compared")

    class Foreign(ast.Variable, metaclass=HostileMeta):
        pass

    query = ast.Query(return_clause=ast.ReturnClause(items=(ast.ReturnItem(Foreign("n")),)))
    with pytest.raises(GrafxPlanError):
        validate_structure(query)
