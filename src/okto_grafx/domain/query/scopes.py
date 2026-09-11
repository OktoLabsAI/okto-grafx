"""Lexical scope lowering: one internal identity per binding, no runtime compatibility mode."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import TypeVar, cast

from okto_grafx.domain.query import ast

T = TypeVar("T")
__all__ = ["lower_scopes"]


def _rewrite(value: T, names: dict[str, str]) -> T:
    """Rewrite only language-owned nodes; never descend into parameter/literal values."""
    if type(value) is ast.Variable:
        return cast(T, replace(value, name=names.get(value.name, value.name)))
    if type(value) in (ast.Literal, ast.Parameter):
        return value
    if type(value) is tuple:
        return cast(T, tuple(_rewrite(item, names) for item in value))
    if type(value).__module__ != ast.__name__ or not is_dataclass(value):
        return value
    changes = {field.name: _rewrite(getattr(value, field.name), names) for field in fields(value)}
    if type(value) in (ast.NodePattern, ast.RelationshipPattern, ast.PatternPath):
        variable = value.variable
        changes["variable"] = names.get(variable, variable)
    return cast(T, replace(value, **changes))


def _lower_list_locals(query: ast.Query) -> ast.Query:
    """Give each expression-local binder a collision-free identity before clause lowering."""
    reserved: set[str] = set()
    has_local = False
    pending: list[object] = [query]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        has_local = has_local or type(item) is ast.ListIteration
        if type(item) is str:
            reserved.add(item)
        elif type(item) is tuple:
            pending.extend(item)
        elif type(item).__module__ == ast.__name__ and is_dataclass(item) and type(item) not in (ast.Literal, ast.Parameter):
            pending.extend(getattr(item, f.name) for f in fields(item))
    if not has_local:
        return query
    ordinal = 0

    def fresh() -> str:
        """Issue a list-local identity absent from all written names."""
        nonlocal ordinal
        while True:
            ordinal += 1
            candidate = f"\x00list{ordinal}"
            if candidate not in reserved:
                reserved.add(candidate)
                return candidate

    lowered: dict[tuple[int, tuple[tuple[str, str], ...]], object] = {}

    def visit(value: T, names: dict[str, str]) -> T:
        """Memoize lexical rewrites while preserving shared AST identities."""
        # Query metadata and its ordered pipeline share AST objects. Preserve that
        # sharing (and local identities) without duplicating an expression DAG.
        key = (id(value), tuple(sorted(names.items())))
        if key not in lowered:
            lowered[key] = visit_uncached(value, names)
        return cast(T, lowered[key])

    def visit_uncached(value: T, names: dict[str, str]) -> T:
        """Lower one owned AST node, introducing lexical binders only in their bodies."""
        if type(value) is ast.Variable:
            return cast(T, replace(value, name=names.get(value.name, value.name)))
        if type(value) in (ast.Literal, ast.Parameter):
            return value
        if type(value) is ast.ListIteration:
            variable = fresh()
            local = {**names, value.variable: variable}
            accumulator = None
            if value.accumulator is not None:
                accumulator = fresh()
                local[value.accumulator] = accumulator
            return cast(T, replace(value, variable=variable, accumulator=accumulator,
                                   source=visit(value.source, names), initial=visit(value.initial, names),
                                   predicate=visit(value.predicate, local), body=visit(value.body, local)))
        if type(value) is tuple:
            return cast(T, tuple(visit(item, names) for item in value))
        if type(value).__module__ != ast.__name__ or not is_dataclass(value):
            return value
        changes = {f.name: visit(getattr(value, f.name), names) for f in fields(value)}
        if type(value) is ast.ReturnItem:
            changes["alias"] = value.name
        return cast(T, replace(value, **changes))

    return visit(query, {})


def lower_scopes(query: ast.Query, initial: tuple[str, ...] = ()) -> ast.Query:
    """Lower an already analysed query while preserving public output names.

    A WITH evaluates all items in its incoming scope, then publishes a simultaneous new
    scope. Reusing a spelling must not overwrite the type authority of an earlier binding.
    Fresh identities are excluded from every name in the entire input, including quoted names
    and later scopes, rather than assuming an internal-looking prefix cannot be written.
    """
    query = _lower_list_locals(query)
    if not query.with_clauses:
        return query
    scope: dict[str, str] = {name: name for name in initial}
    used: set[str] = set(initial)
    reserved = set(initial)
    pending: list[object] = [query]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if type(value) is str:
            reserved.add(value)
        elif type(value) is tuple:
            pending.extend(value)
        elif (type(value).__module__ == ast.__name__ and is_dataclass(value)
              and type(value) not in (ast.Literal, ast.Parameter) and id(value) not in seen):
            seen.add(id(value))
            pending.extend(getattr(value, field.name) for field in fields(value))
    ordinal = 0

    def fresh(name: str) -> str:
        """Reuse a first spelling or allocate a collision-free shadow identity."""
        nonlocal ordinal
        if name not in used:
            used.add(name)
            return name
        while True:
            ordinal += 1
            candidate = f"\x00scope{ordinal}"
            if candidate not in reserved:
                reserved.add(candidate)
                return candidate

    pipeline = []
    for clause in query.ordered_clauses():
        if isinstance(clause, ast.ProcedureCall):
            arguments = _rewrite(clause.arguments, scope)
            yielded = []
            for item in clause.yields:
                identity = fresh(item.name)
                scope[item.name] = identity
                yielded.append(replace(item, alias=identity))
            pipeline.append(replace(clause, arguments=arguments, yields=tuple(yielded),
                                    predicate=_rewrite(clause.predicate, scope)))
            continue
        if isinstance(clause, ast.SubqueryClause):
            outer_names = tuple(scope[name] for name in clause.imports)
            branches = clause.query.branches() if isinstance(clause.query, ast.UnionQuery) else (clause.query,)
            returned = branches[0].return_clause
            assert returned is not None
            columns = returned.column_names()
            outputs = tuple(fresh(name) for name in columns)
            scope.update(zip(columns, outputs, strict=True))
            pipeline.append(replace(clause, outer_names=outer_names, output_aliases=outputs))
            continue
        if isinstance(clause, ast.WithClause):
            projected: dict[str, str] = {}
            items = []
            carried = tuple(ast.ReturnItem(expression=ast.Variable(name)) for name in scope) if clause.include_existing else ()
            for item in (*carried, *clause.items):
                name = item.name
                expression = _rewrite(item.expression, scope)
                carries = isinstance(item.expression, ast.Variable) and (
                    item.alias is None or item.alias == item.expression.name
                )
                identity = scope[name] if carries else fresh(name)
                projected[name] = identity
                items.append(replace(item, expression=expression, alias=identity))
            scope = projected
            pipeline.append(replace(
                clause, items=tuple(items), include_existing=False, predicate=_rewrite(clause.predicate, scope),
                sort_items=_rewrite(clause.sort_items, scope),
                skip=_rewrite(clause.skip, scope), limit=_rewrite(clause.limit, scope),
            ))
            continue
        if isinstance(clause, ast.UnwindClause):
            expression = _rewrite(clause.expression, scope)
            identity = fresh(clause.alias)
            scope[clause.alias] = identity
            pipeline.append(replace(clause, expression=expression, alias=identity))
            continue
        patterns = (clause.pattern,) if isinstance(clause, ast.MergeClause) else (
            clause.patterns if isinstance(clause, (ast.MatchClause, ast.CreateClause)) else ()
        )
        for pattern in patterns:
            for bound in (*pattern.nodes, *pattern.relationships, pattern):
                if bound.variable is not None and bound.variable not in scope:
                    scope[bound.variable] = fresh(bound.variable)
        pipeline.append(_rewrite(clause, scope))
    returned = query.return_clause
    if returned is not None:
        output_scope = {**scope, **{item.alias: item.alias for item in returned.items
                                  if item.alias is not None}}
        returned = replace(
            returned,
            items=tuple(replace(item, expression=_rewrite(item.expression, scope), alias=item.name)
                        for item in returned.items),
            sort_items=_rewrite(returned.sort_items, output_scope),
            skip=_rewrite(returned.skip, scope), limit=_rewrite(returned.limit, scope),
        )
    return replace(
        query, clause_pipeline=tuple(pipeline),
        unwind_clause=next((c for c in pipeline if isinstance(c, ast.UnwindClause)), None),
        match_clauses=tuple(c for c in pipeline if isinstance(c, ast.MatchClause)),
        with_clauses=tuple(c for c in pipeline if isinstance(c, ast.WithClause)),
        updating_clauses=tuple(c for c in pipeline if isinstance(c, ast.UpdatingClause)),
        return_clause=returned,
    )
