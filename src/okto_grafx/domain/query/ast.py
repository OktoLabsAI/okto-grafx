"""The abstract syntax of the supported Cypher subset (CONTRACT.md section 8.9, D3).

Every node is a frozen dataclass, which buys three things this component needs. Structural
equality comes for free, and the semantic pass uses it to decide whether an ORDER BY key is one
of the projected items. Hashability comes with it, so a set of expressions is available without a
second identity scheme. And immutability means a parsed statement can be planned twice, cached,
or handed to two callers without either of them being able to change what the other parses.

Nothing here knows about a catalog, a snapshot, a page or a port. A statement is what the text
said; whether it can be answered is the planner's question and needs the catalog to ask.

The tree is bounded by construction: the parser refuses to descend past
:data:`~okto_grafx.domain.query.limits.MAX_EXPRESSION_DEPTH`, so every walk over a tree built
here terminates and the recursive destruction CPython performs on the last reference cannot run
out of stack. :func:`walk` still carries its own bound, because a tree can also be built by hand.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.model.value import Value
from okto_grafx.domain.query.limits import MAX_EXPRESSION_DEPTH

__all__ = [
    "BinaryOperation",
    "CaseAlternative",
    "CaseExpression",
    "ColumnSpec",
    "CreateClause",
    "CreateNodeTableStatement",
    "CreateRelTableStatement",
    "CreateVectorSpaceStatement",
    "DeleteClause",
    "Direction",
    "Expression",
    "FunctionCall",
    "ListExpression",
    "Literal",
    "MapEntry",
    "MapExpression",
    "MatchClause",
    "MergeClause",
    "NamedArgument",
    "NodePattern",
    "NullCheck",
    "Parameter",
    "PatternPath",
    "Property",
    "Query",
    "RelationshipPattern",
    "ReturnClause",
    "ReturnItem",
    "SetClause",
    "SetItem",
    "SortItem",
    "Statement",
    "Subscript",
    "UnaryOperation",
    "UnwindClause",
    "UpdatingClause",
    "Variable",
    "free_variables",
    "walk",
]


class Expression:
    """The base of every expression node, with the one method a walker needs."""

    __slots__ = ()

    def children(self) -> tuple[Expression, ...]:
        """Return the sub-expressions of this node, in the order they were written."""
        return ()

    def describe(self) -> str:
        """Return a short en-US rendering of this expression, for a plan or a message."""
        return type(self).__name__


@dataclass(frozen=True, slots=True)
class Literal(Expression):
    """A constant written in the query."""

    value: Value

    def describe(self) -> str:
        """Return the literal as it would be written back."""
        if self.value is None:
            return "NULL"
        if self.value is True:
            return "true"
        if self.value is False:
            return "false"
        if isinstance(self.value, str):
            return repr(self.value)
        return str(self.value)


@dataclass(frozen=True, slots=True)
class Parameter(Expression):
    """A value supplied at execution time under a name."""

    name: str

    def describe(self) -> str:
        """Return the parameter reference as it was written."""
        return f"${self.name}"


@dataclass(frozen=True, slots=True)
class Variable(Expression):
    """A name a pattern bound to a node or a relationship."""

    name: str

    def describe(self) -> str:
        """Return the variable name."""
        return self.name


@dataclass(frozen=True, slots=True)
class Property(Expression):
    """One property of whatever the subject expression denotes."""

    subject: Expression
    key: str

    def children(self) -> tuple[Expression, ...]:
        """Return the subject this property is read from."""
        return (self.subject,)

    def describe(self) -> str:
        """Return the dotted property reference."""
        return f"{self.subject.describe()}.{self.key}"


@dataclass(frozen=True, slots=True)
class UnaryOperation(Expression):
    """A prefix operator applied to one operand."""

    operator: str
    operand: Expression

    def children(self) -> tuple[Expression, ...]:
        """Return the single operand."""
        return (self.operand,)

    def describe(self) -> str:
        """Return the operator and its operand."""
        separator = " " if self.operator.isalpha() else ""
        return f"{self.operator}{separator}{self.operand.describe()}"


@dataclass(frozen=True, slots=True)
class BinaryOperation(Expression):
    """An infix operator applied to two operands."""

    operator: str
    left: Expression
    right: Expression

    def children(self) -> tuple[Expression, ...]:
        """Return the two operands, left first."""
        return (self.left, self.right)

    def describe(self) -> str:
        """Return the parenthesised operation, so precedence is never ambiguous in a plan."""
        return f"({self.left.describe()} {self.operator} {self.right.describe()})"


@dataclass(frozen=True, slots=True)
class CaseAlternative:
    """One ``WHEN`` expression and the ``THEN`` expression paired with it."""

    condition: Expression
    result: Expression

    def describe(self) -> str:
        """Return this alternative as it is written inside a CASE expression."""
        return f"WHEN {self.condition.describe()} THEN {self.result.describe()}"


@dataclass(frozen=True, slots=True)
class CaseExpression(Expression):
    """A searched or simple ``CASE`` expression.

    ``operand`` is absent for searched CASE and present for simple CASE. ``fallback`` being absent
    records an omitted ELSE rather than manufacturing a literal; execution gives both forms the
    same null result while the AST still describes exactly what the caller wrote.
    """

    operand: Expression | None
    alternatives: tuple[CaseAlternative, ...]
    fallback: Expression | None = None

    def children(self) -> tuple[Expression, ...]:
        """Return every written expression in evaluation order."""
        children: list[Expression] = []
        if self.operand is not None:
            children.append(self.operand)
        for alternative in self.alternatives:
            children.extend((alternative.condition, alternative.result))
        if self.fallback is not None:
            children.append(self.fallback)
        return tuple(children)

    def result_expressions(self) -> tuple[Expression, ...]:
        """Return every explicit result arm, including ELSE when it was written."""
        results = [alternative.result for alternative in self.alternatives]
        if self.fallback is not None:
            results.append(self.fallback)
        return tuple(results)

    def describe(self) -> str:
        """Return a compact unambiguous rendering of this CASE expression."""
        parts = ["CASE"]
        if self.operand is not None:
            parts.append(self.operand.describe())
        parts.extend(alternative.describe() for alternative in self.alternatives)
        if self.fallback is not None:
            parts.extend(("ELSE", self.fallback.describe()))
        parts.append("END")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class Subscript(Expression):
    """A one-based list extraction written as ``subject[index]``."""

    subject: Expression
    index: Expression

    def children(self) -> tuple[Expression, ...]:
        """Return the list expression and then its index."""
        return (self.subject, self.index)

    def describe(self) -> str:
        """Return the postfix list extraction."""
        return f"{self.subject.describe()}[{self.index.describe()}]"


@dataclass(frozen=True, slots=True)
class NullCheck(Expression):
    """The ``IS NULL`` and ``IS NOT NULL`` tests, which are not ordinary comparisons.

    They are their own node because ``x = NULL`` is never true in this language while
    ``x IS NULL`` may be, and folding the two into one comparison operator is the classic way a
    query engine starts returning the wrong rows.
    """

    operand: Expression
    negated: bool = False

    def children(self) -> tuple[Expression, ...]:
        """Return the expression being tested."""
        return (self.operand,)

    def describe(self) -> str:
        """Return the test as it was written."""
        return f"{self.operand.describe()} IS {'NOT ' if self.negated else ''}NULL"


@dataclass(frozen=True, slots=True)
class NamedArgument:
    """One ``name => value`` argument of a function call."""

    name: str
    value: Expression

    def describe(self) -> str:
        """Return the named argument as it was written."""
        return f"{self.name} => {self.value.describe()}"


@dataclass(frozen=True, slots=True)
class FunctionCall(Expression):
    """A call to a named function, which may be an aggregate."""

    name: str
    arguments: tuple[Expression, ...] = ()
    named_arguments: tuple[NamedArgument, ...] = ()
    distinct: bool = False
    star: bool = False

    def children(self) -> tuple[Expression, ...]:
        """Return every positional argument and then every named argument value."""
        return (*self.arguments, *(argument.value for argument in self.named_arguments))

    def named_argument(self, name: str) -> Expression | None:
        """Return the value of one named argument, or None when the call did not carry it."""
        for argument in self.named_arguments:
            if argument.name.lower() == name.lower():
                return argument.value
        return None

    def describe(self) -> str:
        """Return the call as it would be written back."""
        if self.star:
            return f"{self.name}(*)"
        parts = [argument.describe() for argument in self.arguments]
        parts.extend(argument.describe() for argument in self.named_arguments)
        prefix = "DISTINCT " if self.distinct else ""
        return f"{self.name}({prefix}{', '.join(parts)})"


@dataclass(frozen=True, slots=True)
class ListExpression(Expression):
    """A list written out in the query."""

    elements: tuple[Expression, ...] = ()

    def children(self) -> tuple[Expression, ...]:
        """Return the elements in written order."""
        return self.elements

    def describe(self) -> str:
        """Return the list as it would be written back."""
        return f"[{', '.join(element.describe() for element in self.elements)}]"


@dataclass(frozen=True, slots=True)
class MapEntry:
    """One key and value of a map."""

    key: str
    value: Expression

    def describe(self) -> str:
        """Return the entry as it was written."""
        return f"{self.key}: {self.value.describe()}"


@dataclass(frozen=True, slots=True)
class MapExpression(Expression):
    """A map written out in the query, used for properties and for options."""

    entries: tuple[MapEntry, ...] = ()

    def children(self) -> tuple[Expression, ...]:
        """Return the entry values in written order."""
        return tuple(entry.value for entry in self.entries)

    def entry(self, key: str) -> Expression | None:
        """Return the value stored under one key, comparing the key without case."""
        for candidate in self.entries:
            if candidate.key.lower() == key.lower():
                return candidate.value
        return None

    def keys(self) -> tuple[str, ...]:
        """Return every key of this map in written order."""
        return tuple(entry.key for entry in self.entries)

    def describe(self) -> str:
        """Return the map as it would be written back."""
        return "{" + ", ".join(entry.describe() for entry in self.entries) + "}"


class Direction(str, Enum):
    """Which way a relationship pattern points."""

    OUTGOING = "outgoing"
    INCOMING = "incoming"
    UNDIRECTED = "undirected"

    def describe(self) -> str:
        """Return the arrow this direction is written with."""
        if self is Direction.OUTGOING:
            return "-->"
        if self is Direction.INCOMING:
            return "<--"
        return "--"


@dataclass(frozen=True, slots=True)
class NodePattern:
    """One node of a pattern: an optional variable, its labels and its inline properties."""

    variable: str | None = None
    labels: tuple[str, ...] = ()
    properties: MapExpression | None = None

    def describe(self) -> str:
        """Return the node pattern as it would be written back."""
        parts = self.variable or ""
        for label in self.labels:
            parts += f":{label}"
        if self.properties is not None:
            parts += f" {self.properties.describe()}"
        return f"({parts})"


@dataclass(frozen=True, slots=True)
class RelationshipPattern:
    """One relationship of a pattern, with its direction and its hop range."""

    variable: str | None = None
    types: tuple[str, ...] = ()
    direction: Direction = Direction.OUTGOING
    min_hops: int = 1
    max_hops: int = 1
    properties: MapExpression | None = None

    @property
    def variable_length(self) -> bool:
        """Return True when this relationship may match more than one hop."""
        return self.min_hops != 1 or self.max_hops != 1

    def describe(self) -> str:
        """Return the relationship pattern as it would be written back."""
        inner = self.variable or ""
        if self.types:
            inner += ":" + "|".join(self.types)
        if self.variable_length:
            inner += f"*{self.min_hops}..{self.max_hops}"
        if self.properties is not None:
            inner += f" {self.properties.describe()}"
        body = f"[{inner}]"
        if self.direction is Direction.OUTGOING:
            return f"-{body}->"
        if self.direction is Direction.INCOMING:
            return f"<-{body}-"
        return f"-{body}-"


@dataclass(frozen=True, slots=True)
class PatternPath:
    """One connected path: nodes and relationships alternating, starting and ending with a node."""

    nodes: tuple[NodePattern, ...]
    relationships: tuple[RelationshipPattern, ...] = ()

    def describe(self) -> str:
        """Return the whole path as it would be written back."""
        parts = [self.nodes[0].describe()] if self.nodes else []
        for position, relationship in enumerate(self.relationships):
            parts.append(relationship.describe())
            parts.append(self.nodes[position + 1].describe())
        return "".join(parts)


@dataclass(frozen=True, slots=True)
class UnwindClause:
    """One list expanded into rows, each bound to a name the rest of the query reads.

    Pulse sends its batch writes this way: one parameter carrying a list of maps, then a
    MATCH keyed on a field of each element. The clause opens the query -- no frozen form has
    it following anything -- so a plan can treat it as the source of rows rather than as a
    shaping step over rows that already exist.
    """

    expression: Expression
    alias: str

    def describe(self) -> str:
        """Return the clause as it would be written back."""
        return f"UNWIND {self.expression.describe()} AS {self.alias}"


@dataclass(frozen=True, slots=True)
class MatchClause:
    """A MATCH clause and the WHERE predicate that belongs to it."""

    patterns: tuple[PatternPath, ...]
    predicate: Expression | None = None

    def describe(self) -> str:
        """Return the clause as it would be written back."""
        body = ", ".join(pattern.describe() for pattern in self.patterns)
        if self.predicate is None:
            return f"MATCH {body}"
        return f"MATCH {body} WHERE {self.predicate.describe()}"


class UpdatingClause:
    """The base of the clauses that change data, so the parser can order them as a group."""

    __slots__ = ()

    def describe(self) -> str:
        """Return a short en-US rendering of this clause."""
        return type(self).__name__


@dataclass(frozen=True, slots=True)
class CreateClause(UpdatingClause):
    """A CREATE clause that writes the patterns it names."""

    patterns: tuple[PatternPath, ...]

    def describe(self) -> str:
        """Return the clause as it would be written back."""
        return "CREATE " + ", ".join(pattern.describe() for pattern in self.patterns)


@dataclass(frozen=True, slots=True)
class MergeClause(UpdatingClause):
    """A MERGE clause: match the pattern, or create it when nothing matches."""

    pattern: PatternPath

    def describe(self) -> str:
        """Return the clause as it would be written back."""
        return f"MERGE {self.pattern.describe()}"


@dataclass(frozen=True, slots=True)
class SetItem:
    """One assignment of a SET clause."""

    target: Property
    value: Expression

    def describe(self) -> str:
        """Return the assignment as it was written."""
        return f"{self.target.describe()} = {self.value.describe()}"


@dataclass(frozen=True, slots=True)
class SetClause(UpdatingClause):
    """A SET clause and its assignments, applied in written order."""

    items: tuple[SetItem, ...]

    def describe(self) -> str:
        """Return the clause as it would be written back."""
        return "SET " + ", ".join(item.describe() for item in self.items)


@dataclass(frozen=True, slots=True)
class DeleteClause(UpdatingClause):
    """A DELETE clause, optionally detaching the relationships of the rows it removes."""

    targets: tuple[Variable, ...]
    detach: bool = False

    def describe(self) -> str:
        """Return the clause as it would be written back."""
        prefix = "DETACH DELETE " if self.detach else "DELETE "
        return prefix + ", ".join(target.describe() for target in self.targets)


@dataclass(frozen=True, slots=True)
class ReturnItem:
    """One projected item and the name it is returned under."""

    expression: Expression
    alias: str | None = None

    @property
    def name(self) -> str:
        """Return the column name this item produces."""
        return self.alias if self.alias is not None else self.expression.describe()

    def describe(self) -> str:
        """Return the item as it would be written back."""
        if self.alias is None:
            return self.expression.describe()
        return f"{self.expression.describe()} AS {self.alias}"


@dataclass(frozen=True, slots=True)
class SortItem:
    """One ORDER BY key and its direction."""

    expression: Expression
    descending: bool = False

    def describe(self) -> str:
        """Return the key as it would be written back."""
        return f"{self.expression.describe()} {'DESC' if self.descending else 'ASC'}"


@dataclass(frozen=True, slots=True)
class ReturnClause:
    """A RETURN clause with everything that shapes its result."""

    items: tuple[ReturnItem, ...]
    distinct: bool = False
    sort_items: tuple[SortItem, ...] = ()
    skip: Expression | None = None
    limit: Expression | None = None

    def column_names(self) -> tuple[str, ...]:
        """Return the names of the columns this clause produces, in order."""
        return tuple(item.name for item in self.items)

    def describe(self) -> str:
        """Return the clause as it would be written back."""
        parts = ["RETURN"]
        if self.distinct:
            parts.append("DISTINCT")
        parts.append(", ".join(item.describe() for item in self.items))
        if self.sort_items:
            parts.append(
                "ORDER BY " + ", ".join(item.describe() for item in self.sort_items)
            )
        if self.skip is not None:
            parts.append(f"SKIP {self.skip.describe()}")
        if self.limit is not None:
            parts.append(f"LIMIT {self.limit.describe()}")
        return " ".join(parts)


class Statement:
    """The base of everything :func:`~okto_grafx.domain.query.parser.parse` can return."""

    __slots__ = ()

    def describe(self) -> str:
        """Return a short en-US rendering of this statement."""
        return type(self).__name__


@dataclass(frozen=True, slots=True)
class Query(Statement):
    """A reading and updating query: MATCH clauses, then updating clauses, then RETURN."""

    unwind_clause: UnwindClause | None = None
    match_clauses: tuple[MatchClause, ...] = ()
    updating_clauses: tuple[UpdatingClause, ...] = ()
    return_clause: ReturnClause | None = None

    @property
    def writes(self) -> bool:
        """Return True when this query changes anything."""
        return bool(self.updating_clauses)

    def describe(self) -> str:
        """Return the query as it would be written back."""
        parts = [] if self.unwind_clause is None else [self.unwind_clause.describe()]
        parts.extend(clause.describe() for clause in self.match_clauses)
        parts.extend(clause.describe() for clause in self.updating_clauses)
        if self.return_clause is not None:
            parts.append(self.return_clause.describe())
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One column of a table as the DDL declared it.

    ``vector_space`` is the whole reason a vector column is spelled ``VECTOR(space)`` rather than
    with the fixed-list syntax of the reference dialect: SPEC-VEC TR-2 makes the embedding space
    the carrier of the dimension, the metric and the storage precision, so a column that named a
    dimension of its own could contradict the space it points at. The dimension and the dtype are
    read from the catalog at planning time, which is the only place both are known.
    """

    name: str
    type_name: str
    vector_space: str | None = None

    def describe(self) -> str:
        """Return the column as it was written."""
        if self.vector_space is not None:
            return f"{self.name} {self.type_name}({self.vector_space})"
        return f"{self.name} {self.type_name}"


@dataclass(frozen=True, slots=True)
class CreateNodeTableStatement(Statement):
    """A CREATE NODE TABLE statement."""

    name: str
    columns: tuple[ColumnSpec, ...]
    primary_key: str | None = None

    def describe(self) -> str:
        """Return the statement as it would be written back."""
        body = ", ".join(column.describe() for column in self.columns)
        if self.primary_key is not None:
            body += f", PRIMARY KEY({self.primary_key})"
        return f"CREATE NODE TABLE {self.name}({body})"


@dataclass(frozen=True, slots=True)
class CreateRelTableStatement(Statement):
    """A CREATE REL TABLE statement and the two node tables it connects."""

    name: str
    from_table: str
    to_table: str
    columns: tuple[ColumnSpec, ...] = ()

    def describe(self) -> str:
        """Return the statement as it would be written back."""
        parts = [f"FROM {self.from_table} TO {self.to_table}"]
        parts.extend(column.describe() for column in self.columns)
        return f"CREATE REL TABLE {self.name}({', '.join(parts)})"


@dataclass(frozen=True, slots=True)
class CreateVectorSpaceStatement(Statement):
    """A CREATE VECTOR SPACE statement, whose options arrive as an ordinary map.

    The options are carried unresolved on purpose. Every one of them -- the dimension, the
    metric, the normalisation flag, the storage precision -- is validated by the domain model
    when the space is built, and re-validating them here would be a second implementation of a
    rule that already has one (amendment A24).
    """

    name: str
    options: MapExpression = field(default_factory=MapExpression)

    def describe(self) -> str:
        """Return the statement as it would be written back."""
        return f"CREATE VECTOR SPACE {self.name} {self.options.describe()}"


def walk(expression: Expression, *, limit: int = MAX_EXPRESSION_DEPTH) -> Iterator[Expression]:
    """Yield every node of an expression tree, parents before children.

    The walk is iterative and carries an explicit depth bound, so it terminates on any tree it is
    handed -- including one built by hand rather than by the parser, which is the only way a tree
    deeper than the parser allows can exist.
    """
    if not isinstance(expression, Expression):
        raise GrafxPlanError(
            f"A query expression was expected; got {type(expression).__name__}.",
            field="expression",
            value=type(expression).__name__,
        )
    pending: list[tuple[Expression, int]] = [(expression, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > limit:
            raise GrafxPlanError(
                f"A query expression may nest at most {limit} levels deep.",
                field="depth",
                value=limit,
            )
        yield node
        children = node.children()
        for child in reversed(children):
            pending.append((child, depth + 1))


def free_variables(expression: Expression) -> tuple[str, ...]:
    """Return the variable names one expression reads, in first-appearance order."""
    found: list[str] = []
    seen: set[str] = set()
    for node in walk(expression):
        if isinstance(node, Variable) and node.name not in seen:
            seen.add(node.name)
            found.append(node.name)
    return tuple(found)
