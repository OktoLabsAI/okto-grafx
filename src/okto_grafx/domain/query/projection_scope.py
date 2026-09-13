"""Resolve projected expression references without reopening discarded input rows."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace

from okto_grafx.domain.query import ast


def returned_reference(expression: ast.Expression, items: tuple[ast.ReturnItem, ...]) -> ast.Expression:
    """Resolve an exact result column before applying compound projected references."""
    for item in items:
        if expression == item.expression:
            return ast.Variable(item.name)
    return projected_reference(expression, items)


def returned_type_reference(expression: ast.Expression, items: tuple[ast.ReturnItem, ...]) -> ast.Expression:
    """Expose output alias definitions to type proof, never to execution.

    An inserted definition belongs to the input scope; do not rewrite inside it
    again (especially RETURN n.x AS n). Preserve expression-local shadowing and
    shared producer identities, and never walk literal/parameter payloads.
    """
    definitions = {item.name: item.expression for item in items}
    memo = {}
    def visit(value: object, local: frozenset[str] = frozenset()) -> object:
        """Memoize scoped expression rewrites by object identity and local bindings."""
        key = (id(value), local)
        if key not in memo:
            memo[key] = rewrite(value, local)
        return memo[key]
    def rewrite(value: object, local: frozenset[str]) -> object:
        """Expand projected aliases for type inference without capturing local names."""
        if type(value) is ast.Variable and value.name not in local:
            return definitions.get(value.name, value)
        if type(value) in (ast.Literal, ast.Parameter):
            return value
        if type(value) is ast.ListIteration:
            bound = local | {value.variable}
            if value.accumulator is not None:
                bound |= {value.accumulator}
            return replace(value, source=visit(value.source, local), initial=visit(value.initial, local),
                           predicate=visit(value.predicate, bound), body=visit(value.body, bound))
        if type(value) is ast.PatternComprehension:
            bound = local | frozenset(value.local_names())
            return replace(value, pattern=visit(value.pattern, bound), predicate=visit(value.predicate, bound),
                           projection=visit(value.projection, bound))
        if type(value) is tuple:
            return tuple(visit(item, local) for item in value)
        if type(value).__module__ == ast.__name__ and is_dataclass(value):
            return replace(value, **{field.name: visit(getattr(value, field.name), local) for field in fields(value)})
        return value
    return visit(expression)


def projected_reference(expression: ast.Expression | None, items: tuple[ast.ReturnItem, ...]) -> ast.Expression | None:
    """Reuse a projected value in a modifier, respecting output/local shadowing.

    Substitute largest complete expressions first. A property projection does not
    expose its entire input node; unrelated properties remain unbound after DISTINCT.
    Never descend into literal/parameter data or confuse a list-local binder with
    an identically spelled incoming variable.
    """
    outputs = {item.name for item in items}
    candidates = tuple((item.expression, item.name, frozenset(ast.free_variables(item.expression)))
                       for item in items
                       if not isinstance(item.expression, (ast.Literal, ast.Parameter)))
    memo: dict[tuple[int, frozenset[str]], object] = {}

    def visit(value: object, local: frozenset[str]) -> object:
        """Memoize projection-reference rewrites while retaining local binding scope."""
        key = (id(value), local)
        if key not in memo:
            memo[key] = rewrite(value, local)
        return memo[key]

    def rewrite(value: object, local: frozenset[str]) -> object:
        """Replace reusable projected expressions only when their dependencies remain visible."""
        if isinstance(value, ast.Expression):
            for original, name, dependencies in candidates:
                if not dependencies.intersection(local | outputs) and name not in local and value == original:
                    return ast.Variable(name)
        if type(value) in (ast.Literal, ast.Parameter):
            return value
        if type(value) is ast.ListIteration:
            bound = local | {value.variable}
            if value.accumulator is not None:
                bound |= {value.accumulator}
            return replace(value, source=visit(value.source, local), initial=visit(value.initial, local),
                           predicate=visit(value.predicate, bound), body=visit(value.body, bound))
        if type(value) is ast.PatternComprehension:
            bound = local | frozenset(value.local_names())
            return replace(value, pattern=visit(value.pattern, bound), predicate=visit(value.predicate, bound),
                           projection=visit(value.projection, bound))
        if type(value) is tuple:
            return tuple(visit(item, local) for item in value)
        if type(value).__module__ == ast.__name__ and is_dataclass(value):
            return replace(value, **{field.name: visit(getattr(value, field.name), local) for field in fields(value)})
        return value

    return visit(expression, frozenset())


__all__ = [
    'returned_reference',
    'returned_type_reference',
    'projected_reference',
]
