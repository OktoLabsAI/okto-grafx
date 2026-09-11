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
        if isinstance(node, ast.Expression):
            pending.extend(node.children())
        elif type(node) is tuple:
            pending.extend(node)
        elif type(node).__module__ == ast.__name__ and is_dataclass(node):
            pending.extend(getattr(node, item.name) for item in fields(node))
    return True
