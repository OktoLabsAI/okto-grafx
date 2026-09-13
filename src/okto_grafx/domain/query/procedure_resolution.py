"""Resolve invocation sugar from trusted handle-local signatures, never callbacks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query import ast
from okto_grafx.domain.query.extensions import TabularProcedure
from okto_grafx.domain.query.structure import validate_structure

__all__ = ["resolve_procedure_calls"]


def _refuse(message: str, field: str, reason: str) -> GrafxPlanError:
    return GrafxPlanError(message, field=field, reason=reason, query_phase="planning")


def _synthetic_return(query: ast.Query, clause: ast.ProcedureCall) -> None:
    returned = query.return_clause
    if (returned is None or returned.distinct or returned.include_existing
            or returned.sort_items or returned.skip is not None or returned.limit is not None
            or len(returned.items) != len(clause.yields)
            or any(type(item.expression) is not ast.Variable or item.alias is not None
                   or item.expression.name != selected.name
                   for item, selected in zip(returned.items, clause.yields))):
        raise _refuse("Standalone CALL must retain its parser-generated output projection.",
                      "ast", "invalid_ast_structure")


def _query(query: ast.Query, procedures: Mapping[str, TabularProcedure], *, outermost: bool) -> ast.Query:
    clauses = query.ordered_clauses()
    resolved = []
    returned = query.return_clause
    changed = False
    for clause in clauses:
        if type(clause) is not ast.ProcedureCall:
            resolved.append(clause)
            continue
        procedure = procedures.get(clause.name)
        if procedure is None:
            raise _refuse("Procedure is not registered or its permissions were not granted.",
                          "procedure", "procedure_not_found")
        if any(type(item.expression) is not ast.Variable for item in clause.yields):
            raise _refuse("YIELD must name declared output columns.", "yield", "procedure_yield")
        if clause.standalone:
            _synthetic_return(query, clause)
        standalone = clause.standalone and outermost and len(clauses) == 1
        if clause.implicit_arguments and (not standalone or clause.arguments):
            raise _refuse("Implicit procedure arguments require a standalone CALL.",
                          "procedure_arguments", "procedure_argument_mode")
        if clause.yield_all and clause.yields:
            raise _refuse("YIELD * cannot carry additional named projections.", "yield", "procedure_yield")
        if clause.yield_all and not standalone:
            raise _refuse("YIELD * requires a standalone CALL; name outputs inside a query.",
                          "yield", "procedure_yield_mode")
        arguments = clause.arguments
        if clause.implicit_arguments:
            if procedure.argument_types and procedure.argument_names is None:
                raise _refuse("This procedure has no declared implicit argument names.",
                              "procedure_arguments", "procedure_argument_names")
            arguments = tuple(ast.Parameter(name) for name in (procedure.argument_names or ()))
        if len(arguments) != len(procedure.argument_types):
            raise _refuse("Procedure argument count mismatch.", "procedure_arity", "procedure_arity")
        yielded = clause.yields
        if clause.yield_all or (standalone and not yielded):
            if clause.yield_all and not procedure.columns:
                raise _refuse("A unit procedure has no columns to yield.", "yield", "procedure_yield")
            yielded = tuple(ast.ReturnItem(ast.Variable(name)) for name, _kind in procedure.columns)
        if standalone:
            returned = ast.ReturnClause(items=tuple(ast.ReturnItem(ast.Variable(item.name)) for item in yielded))
        replacement = clause
        if (arguments is not clause.arguments or yielded is not clause.yields
                or clause.implicit_arguments or clause.yield_all or clause.standalone
                or clause.writes != (procedure.mode == "write") or clause.result_types != procedure.columns
                or clause.deterministic != procedure.deterministic):
            replacement = replace(clause, arguments=arguments, yields=yielded,
                                  implicit_arguments=False, yield_all=False, standalone=False,
                                  writes=procedure.mode == "write", result_types=procedure.columns,
                                  deterministic=procedure.deterministic)
            changed = True
        resolved.append(replacement)
    return replace(query, clause_pipeline=tuple(resolved), return_clause=returned) if changed else query


def resolve_procedure_calls(statement: ast.Statement, procedures: Mapping[str, TabularProcedure]) -> ast.Statement:
    """Expand trusted signatures before analysis without evaluating callbacks or values.

    Only an outer standalone CALL may infer parameter names or omit tabular YIELD.
    Standalone YIELD * expands in declaration order; in-query calls name outputs.
    Iterative traversal preserves shared syntax, avoids Python recursion limits and
    never inspects literal value payloads.
    """
    validate_structure(statement)
    pending = [(statement, False)]
    memo: dict[int, object] = {}
    while pending:
        value, leaving = pending.pop()
        if id(value) in memo:
            continue
        kind = type(value)
        compound = kind.__module__ == ast.__name__ and is_dataclass(value) and kind is not ast.Literal
        if kind is not tuple and not compound:
            memo[id(value)] = value
            continue
        children = value if kind is tuple else tuple(getattr(value, field.name) for field in fields(value))
        if not leaving:
            pending.append((value, True))
            pending.extend((child, False) for child in reversed(children))
            continue
        mapped = tuple(memo[id(child)] for child in children)
        if all(left is right for left, right in zip(mapped, children, strict=True)):
            result = value
        elif kind is tuple:
            result = mapped
        else:
            result = replace(value, **{field.name: item for field, item in zip(fields(value), mapped, strict=True)})
        if kind is ast.Query:
            result = _query(result, procedures, outermost=value is statement)
        memo[id(value)] = result
    return memo[id(statement)]
