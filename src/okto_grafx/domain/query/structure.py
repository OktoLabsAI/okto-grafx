"""Canonical AST structure admission before any semantic dispatch.

Semantic depth, expression values and resource limits retain their own checks.
This pass rejects impostor classes, mutable inventories and cycles, without
calling user-defined equality, iteration or AST methods. Shared DAGs are allowed.
"""

from dataclasses import fields, is_dataclass
from functools import lru_cache
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query import ast


_CANONICAL_IDS = frozenset(
    id(value) for value in vars(ast).values()
    if isinstance(value, type) and value.__module__ == ast.__name__
    and is_dataclass(value)
)


@lru_cache(maxsize=None)
def _members(kind: type) -> tuple[tuple[str, object], ...]:
    if kind is ast.Literal:
        return ()  # Value payloads have a separate recursive type contract.
    hints = get_type_hints(kind)
    return tuple((field.name, hints[field.name]) for field in fields(kind))


def _matches(value: object, expected: object) -> bool:
    origin = get_origin(expected)
    if origin in (UnionType, Union):
        return any(_matches(value, option) for option in get_args(expected))
    if origin is tuple:
        return type(value) is tuple
    if isinstance(expected, type):
        kind = type(value)
        return (kind is expected and (expected.__module__ != ast.__name__ or id(kind) in _CANONICAL_IDS
                                     or kind is ast.Direction)) or (
            id(kind) in _CANONICAL_IDS and issubclass(kind, expected)
        )
    return False


def validate_structure(statement: object) -> None:
    """Refuse malformed syntax independently of optional supplied analysis."""
    pending = [(statement, ast.Statement, False)]
    active: set[int] = set()
    done: set[tuple[int, object]] = set()
    while pending:
        value, expected, leaving = pending.pop()
        marker = id(value)
        if leaving:
            active.remove(marker)
            done.add((marker, expected))
            continue
        if not _matches(value, expected):
            raise GrafxPlanError("Query syntax requires canonical immutable AST fields.",
                                 field="ast", reason="invalid_ast_structure")
        kind = type(value)
        if id(kind) not in _CANONICAL_IDS and kind is not tuple:
            continue
        if marker in active:
            raise GrafxPlanError("Query syntax cannot contain cycles.",
                                 field="ast", reason="invalid_ast_structure")
        # Validate the edge's expected type even when a DAG node was visited.
        if (marker, expected) in done:
            continue
        active.add(marker)
        pending.append((value, expected, True))
        if kind is tuple:
            options = get_args(expected)
            if len(options) == 2 and options[1] is Ellipsis:
                pending.extend((item, options[0], False) for item in reversed(value))
            elif len(options) == len(value):
                pending.extend((item, option, False) for item, option in
                               reversed(tuple(zip(value, options))))
            else:
                raise GrafxPlanError("Query tuple has an invalid arity.",
                                     field="ast", reason="invalid_ast_structure")
        else:
            try:
                pending.extend(
                    (object.__getattribute__(value, name), annotation, False)
                    for name, annotation in reversed(_members(kind))
                )
            except AttributeError as error:
                raise GrafxPlanError("Query syntax is missing a required AST field.",
                                     field="ast", reason="invalid_ast_structure") from error
