"""Pure effect declarations for parsed query trees and optimization admission."""

from __future__ import annotations

from dataclasses import fields, is_dataclass

from okto_grafx.domain.query import ast
from okto_grafx.domain.query.scalars import NONDETERMINISTIC_SCALARS


def is_deterministic(tree: object) -> bool:
    """Inspect all nested AST clauses without invoking or rendering an expression.

    Registered UDFs retain their existing trusted deterministic contract. This
    predicate complements, not replaces, normal AST/function/permission admission.
    Repeated AST references are visited once, including shared WITH/UNION nodes.
    """
    pending = [tree]
    seen = set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, ast.FunctionCall) and node.name.upper() in NONDETERMINISTIC_SCALARS:
            return False
        if type(node) is ast.ProcedureCall and (node.writes or not node.deterministic):
            return False
        if isinstance(node, ast.Expression):
            pending.extend(node.children())
        elif type(node) is tuple:
            pending.extend(node)
        elif type(node).__module__ == ast.__name__ and is_dataclass(node):
            pending.extend(getattr(node, item.name) for item in fields(node))
    return True


def contains_procedure(tree: object) -> bool:
    """Find CALL at any query/expression depth without inspecting literal payloads."""
    pending = [tree]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if type(node) is ast.ProcedureCall:
            return True
        if type(node) is tuple:
            pending.extend(node)
        elif (type(node).__module__ == ast.__name__ and is_dataclass(node)
              and type(node) is not ast.Literal):
            pending.extend(getattr(node, item.name) for item in fields(node))
    return False


__all__ = [
    'is_deterministic',
    'contains_procedure',
]
