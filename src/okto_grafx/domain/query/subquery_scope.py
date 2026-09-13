"""Resolve CALL imports without leaking another UNION branch's lexical scope."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.ast import Query, UnionQuery, WithClause, Variable, free_variables


def scope_branches(query: Query | UnionQuery, names: tuple[str, ...], *, persistent: bool) -> Query | UnionQuery:
    """Apply explicit import scopes recursively to every UNION branch."""
    if isinstance(query, UnionQuery):
        return replace(query, left=scope_branches(query.left, names, persistent=persistent),
                       right=scope_branches(query.right, names, persistent=persistent))
    return replace(query, scope_imports=names, global_imports=names if persistent else ())


def resolve_with_imports(query: Query | UnionQuery, available: Collection[str]) -> tuple[Query | UnionQuery, tuple[str, ...]]:
    """Infer only a branch's leading importing WITH; ordinary local WITH stays local."""
    imported = []

    def branch(item: Query | UnionQuery) -> Query | UnionQuery:
        """Resolve each branch's leading WITH imports against its enclosing scope."""
        if isinstance(item, UnionQuery):
            return replace(item, left=branch(item.left), right=branch(item.right))
        assert isinstance(item, Query)
        clauses = item.ordered_clauses()
        names = ()
        if clauses and isinstance(clauses[0], WithClause):
            head = clauses[0]
            expressions = tuple(i.expression for i in head.items) + tuple(i.expression for i in head.sort_items)
            if head.predicate is not None:
                expressions += (head.predicate,)
            expressions += tuple(value for value in (head.skip, head.limit) if value is not None)
            uses_outer = head.include_existing or any(set(free_variables(expr)) & set(available) for expr in expressions)
            if uses_outer:
                if (head.distinct or head.sort_items or head.predicate is not None
                        or head.skip is not None or head.limit is not None
                        or any(not isinstance(i.expression, Variable) or i.alias is not None for i in head.items)):
                    raise GrafxPlanError("Importing WITH allows only direct outer variable references, without modifiers.",
                                         field="imports", reason="invalid_importing_with")
                names = (*available,) if head.include_existing else tuple(i.expression.name for i in head.items)
                if head.include_existing and head.items:
                    raise GrafxPlanError("Importing WITH * cannot repeat explicit names.", field="imports")
        for name in names:
            if name not in imported:
                imported.append(name)
        return replace(item, scope_imports=tuple(names), global_imports=())

    resolved = branch(query)
    return resolved, tuple(imported)


__all__ = [
    'scope_branches',
    'resolve_with_imports',
]
