"""Read-only existential query admission and lexical dependency discovery."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import fields, is_dataclass, replace

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query import ast
from okto_grafx.domain.query.subquery_scope import scope_branches
from okto_grafx.domain.query.binding_inference import NODE_LIST, RELATIONSHIP_LIST, EMPTY_LIST, entity_sources


def element_kind(source: ast.Expression, resolve: Callable[[str], str | None]) -> str | None:
    """Prove a list-local entity kind, never granting authority to external maps."""
    if (isinstance(source, ast.Literal) and source.value is None) or (
        isinstance(source, ast.ListExpression) and all(isinstance(value, ast.Literal) and value.value is None
                                                      for value in source.elements)):
        return "null alias"
    proof = entity_sources(source, resolve)
    if proof is not None:
        return {NODE_LIST: "node", RELATIONSHIP_LIST: "relationship", EMPTY_LIST: "null alias"}.get(proof[0])
    if (isinstance(source, ast.FunctionCall) and len(source.arguments) == 1
            and source.name.upper() in ("NODES", "RELATIONSHIPS")):
        path = entity_sources(source.arguments[0], resolve)
        if path is not None and path[0] == "path":
            return "node" if source.name.upper() == "NODES" else "relationship"
    return None


def referenced_names(query: ast.Query | ast.UnionQuery, available: Iterable[str] = ()) -> set[str]:
    """Collect possible correlations without inspecting literal/parameter payloads.

    The semantic pass rejects shadowed outer names before lowering. Pattern
    declarations can be references to an outer binding, so include them as well.
    """
    names: set[str] = set()
    pending = [(query, frozenset())]
    seen = set()
    while pending:
        value, local = pending.pop()
        marker = (id(value), local)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(value, ast.Variable):
            if value.name not in local:
                names.add(value.name)
        elif isinstance(value, (ast.ReturnClause, ast.WithClause)) and value.include_existing:
            names.update(set(available) - local)
        elif isinstance(value, (ast.NodePattern, ast.RelationshipPattern, ast.PatternPath)):
            if value.variable is not None and value.variable not in local:
                names.add(value.variable)
        if isinstance(value, ast.ListIteration):
            bound = local | {value.variable}
            if value.accumulator is not None:
                bound |= {value.accumulator}
            pending.extend((item, scope) for item, scope in (
                (value.source, local), (value.initial, local), (value.body, bound), (value.predicate, bound))
                if item is not None)
            continue
        if type(value) is tuple:
            pending.extend((item, local) for item in value)
        elif (type(value).__module__ == ast.__name__ and is_dataclass(value)
              and not isinstance(value, (ast.Literal, ast.Parameter))):
            pending.extend((getattr(value, field.name), local) for field in fields(value))
    return names


def read_query(query: ast.Query | ast.UnionQuery, imports: tuple[str, ...]) -> ast.Query | ast.UnionQuery:
    """Normalize omitted RETURN only inside EXISTS; preserve real query cardinality."""
    branches = query.branches() if isinstance(query, ast.UnionQuery) else (query,)
    if any(branch.writes for branch in branches):
        raise GrafxPlanError("EXISTS subqueries cannot write.", field="subquery",
                             reason="existential_write", query_phase="planning")
    returns = {branch.return_clause is not None for branch in branches}
    if len(returns) != 1:
        raise GrafxPlanError("Every EXISTS UNION branch must agree on RETURN presence.",
                             field="subquery", reason="existential_return_mismatch", query_phase="planning")

    def normalize(value: ast.Query | ast.UnionQuery) -> ast.Query | ast.UnionQuery:
        """Normalize existential UNION branches and enforce nonempty read-only bodies."""
        if isinstance(value, ast.UnionQuery):
            return replace(value, left=normalize(value.left), right=normalize(value.right))
        if not value.ordered_clauses() and value.return_clause is None:
            raise GrafxPlanError("An EXISTS body cannot be empty.", field="subquery")
        if value.return_clause is not None:
            return value
        return replace(value, return_clause=ast.ReturnClause(items=(
            ast.ReturnItem(expression=ast.Literal(True), alias="exists"),)))

    return scope_branches(normalize(query), imports, persistent=True)


__all__ = [
    'element_kind',
    'referenced_names',
    'read_query',
]
