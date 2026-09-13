"""Structural entity provenance proofs; never evaluate expressions or consult rows."""

from __future__ import annotations

from collections.abc import Callable

from .ast import BinaryOperation, CaseExpression, Expression, FunctionCall, ListExpression, ListIteration, ListSlice, Literal, Subscript, Variable

RELATIONSHIP_LIST = "relationship list"
NODE_LIST = "node list"
NULL_BINDING = "null alias"
EMPTY_LIST = "empty list"
ENTITY_KINDS = frozenset(("node", "relationship", "path", NODE_LIST, RELATIONSHIP_LIST, NULL_BINDING, EMPTY_LIST))


def entity_sources(expression: Expression, resolve: Callable[[str], str | None]) -> tuple[str, tuple[str, ...]] | None:
    """Prove compatible entity branches and their existing variable sources."""
    if isinstance(expression, Variable):
        kind = resolve(expression.name)
        return (kind, (expression.name,)) if kind in ENTITY_KINDS else None
    if isinstance(expression, Literal) and expression.value is None:
        return NULL_BINDING, ()
    if (isinstance(expression, FunctionCall) and expression.name.upper() in {"STARTNODE", "ENDNODE"}
            and len(expression.arguments) == 1):
        proof = entity_sources(expression.arguments[0], resolve)
        if proof is not None and proof[0] in ("relationship", NULL_BINDING):
            return ("node", proof[1]) if proof[0] == "relationship" else proof
        return None
    if isinstance(expression, ListExpression) and not expression.elements:
        return EMPTY_LIST, ()
    if isinstance(expression, ListSlice):
        proof = entity_sources(expression.subject, resolve)
        return proof if proof is not None and proof[0] in (NODE_LIST, RELATIONSHIP_LIST, EMPTY_LIST) else None
    if isinstance(expression, ListIteration) and expression.mode == "map":
        source = entity_sources(expression.source, resolve)
        element_kind = ({NODE_LIST: "node", RELATIONSHIP_LIST: "relationship"}.get(source[0])
                        if source is not None else None)
        def local_kind(name: str) -> str | None:
            """Resolve the iteration variable's entity kind before consulting outer scope."""
            return element_kind if name == expression.variable else resolve(name)
        body = entity_sources(expression.body or Variable(expression.variable), local_kind)
        if body is None or body[0] not in ("node", "relationship"):
            return None
        # A local binder is a source element, not an identically named outer entity.
        sources = tuple(dict.fromkeys(name for origin in body[1] for name in (
            source[1] if origin == expression.variable and source is not None else (origin,))))
        return (NODE_LIST if body[0] == "node" else RELATIONSHIP_LIST), sources
    if isinstance(expression, Subscript):
        proof = entity_sources(expression.subject, resolve)
        if proof is not None and proof[0] == EMPTY_LIST:
            return NULL_BINDING, ()
        if proof is not None and proof[0] in (NODE_LIST, RELATIONSHIP_LIST):
            return ("node" if proof[0] == NODE_LIST else "relationship"), proof[1]
        return None
    if isinstance(expression, FunctionCall) and expression.name.upper() == "COLLECT" and len(expression.arguments) == 1:
        proof = entity_sources(expression.arguments[0], resolve)
        if proof is not None and proof[0] in ("node", "relationship"):
            return (NODE_LIST if proof[0] == "node" else RELATIONSHIP_LIST), proof[1]
        return None
    if isinstance(expression, BinaryOperation) and expression.operator == "+":
        left = entity_sources(expression.left, resolve)
        right = entity_sources(expression.right, resolve)
        if left is not None and right is not None:
            if left[0] == EMPTY_LIST and right[0] in (NODE_LIST, RELATIONSHIP_LIST, EMPTY_LIST):
                return right
            if right[0] == EMPTY_LIST and left[0] in (NODE_LIST, RELATIONSHIP_LIST):
                return left
        if left is not None and right is not None and left[0] == right[0] and left[0] in (NODE_LIST, RELATIONSHIP_LIST):
            return left[0], tuple(dict.fromkeys((*left[1], *right[1])))
        return None
    is_list = isinstance(expression, ListExpression)
    if is_list:
        branches = expression.elements
    elif isinstance(expression, FunctionCall) and expression.name.upper() == "COALESCE":
        branches = expression.arguments
    elif isinstance(expression, CaseExpression):
        branches = tuple(alt.result for alt in expression.alternatives) + (
            expression.fallback if expression.fallback is not None else Literal(None),)
    else:
        return None
    proofs = [entity_sources(branch, resolve) for branch in branches]
    if any(proof is None for proof in proofs):
        return None
    kinds = {proof[0] for proof in proofs if proof is not None} - {NULL_BINDING}
    if not is_list and EMPTY_LIST in kinds:
        if not kinds <= {EMPTY_LIST, NODE_LIST, RELATIONSHIP_LIST}:
            return None
        kinds.remove(EMPTY_LIST)
        if not kinds:
            return EMPTY_LIST, ()
    if is_list:
        # Empty/all-NULL lists prove no graph kind. In particular, absence of
        # node evidence must not be misclassified as a relationship collection.
        if kinds not in ({"node"}, {"relationship"}):
            return None
        kind = NODE_LIST if kinds == {"node"} else RELATIONSHIP_LIST
    elif not kinds:
        kind = NULL_BINDING
    elif len(kinds) == 1:
        kind = next(iter(kinds))
    else:
        return None
    sources = tuple(dict.fromkeys(name for proof in proofs if proof is not None for name in proof[1]))
    return kind, sources


__all__ = [
    'RELATIONSHIP_LIST',
    'NODE_LIST',
    'NULL_BINDING',
    'EMPTY_LIST',
    'ENTITY_KINDS',
    'entity_sources',
]
