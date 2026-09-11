"""The operator tree a query is answered by (CONTRACT.md section 8.9, SPEC-VEC BR-6, AC-7).

A plan is a value, not a program: every node is a frozen dataclass carrying what the operator
must do and the operators it draws its rows from. Nothing here executes, opens a page or reads a
snapshot. That is what makes ``explain`` honest -- the tree a caller inspects is the same object
the executor walks, rather than a description of it written by hand somewhere else.

Two invariants are enforced here rather than left to the planner's discipline, because both are
wrong-result or denial-of-service failures rather than untidiness:

**One tree, never an over-fetch followed by a post-filter.** SPEC-VEC BR-6 and AC-7 say the
combination of similarity with node predicates, relationship predicates and traversal is the
PLAN's job and never the caller's. The shape that violates it is specific and checkable: a
:class:`VectorSearch` that carries a top-k bound with a :class:`Filter` above it. That plan asks
the vector operator for k rows and then throws some away, so it silently returns fewer than k --
which is the over-fetch the consumer of the reference engine had to write by hand. A filter above
an UNBOUNDED similarity operator is not that shape and is allowed: nothing was truncated, so
nothing can be missing.

**Every tree is finite and bounded.** :func:`validate_plan` walks with an explicit stack, a
visited set keyed on identity and a depth ceiling, so a plan that somehow became cyclic is
refused rather than walked forever.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.query.analysis import Aggregation
from okto_grafx.domain.query.ast import (
    Direction,
    Expression,
    MapExpression,
    Property,
    ReturnItem,
    SortItem,
)
from okto_grafx.domain.query.limits import MAX_EXPRESSION_DEPTH

__all__ = [
    "MAX_PLAN_DEPTH",
    "AggregateRows",
    "AllNodesScan",
    "ArgumentRows",
    "ApplyRows",
    "SubqueryRows",
    "ProcedureRows",
    "CreateIndex",
    "CreateNodeTable",
    "CreateRelTable",
    "CreateRelationships",
    "CreateVectorSpace",
    "CreatedNode",
    "CreatedRelationship",
    "DeleteEntities",
    "DistinctRows",
    "EagerRows",
    "FilterRows",
    "IndexSeek",
    "LimitRows",
    "MergePattern",
    "NodeMultiKeySeek",
    "NodeScan",
    "OrderedNodeMerge",
    "OptionalRows",
    "PlanNode",
    "ProduceResults",
    "ProjectRows",
    "PropertyAssignment",
    "RelationshipScan",
    "RelationshipIncidentSeek",
    "SetProperties",
    "SingleRow",
    "SkipRows",
    "SortRows",
    "TraverseAnyRelationship",
    "TraverseRelationship",
    "UnionRows",
    "UnwindRows",
    "VectorSearch",
    "WithRows",
    "plan_nodes",
    "validate_plan",
]

MAX_PLAN_DEPTH: int = MAX_EXPRESSION_DEPTH * 4
"""How deep an operator tree may be before it is refused as malformed.

It is a multiple of the expression bound rather than a number of its own: a plan grows one layer
per pattern element and one per result-shaping clause, and both of those are already bounded by
the parser. The ceiling exists to make the walk terminate on a tree nobody built through the
planner, not to constrain a real query.
"""


class PlanNode:
    """The base of every operator, with the two questions a walk asks of one."""

    __slots__ = ()

    @property
    def label(self) -> str:
        """Return the operator name, which is what an explain tree shows."""
        return type(self).__name__

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operators this one draws its rows from, in evaluation order."""
        return ()

    def details(self) -> Mapping[str, object]:
        """Return the settings of this operator, in a fixed order, for inspection."""
        return {}

    def describe(self) -> str:
        """Return one line naming this operator and its settings."""
        settings = ", ".join(f"{key}={value}" for key, value in self.details().items())
        return f"{self.label}({settings})" if settings else self.label

    def traverse(self) -> Iterator[tuple[PlanNode, int]]:
        """Yield every operator with its depth, parents first, over one bounded walk.

        The walk is iterative and carries both guards, and it is the ONE traversal of this
        class: :meth:`walk`, :meth:`render` and :meth:`to_dict` all read it rather than each
        repeating the loop. Three copies of a bound are three places for it to be wrong, and a
        recursive ``to_dict`` in particular would answer a deep tree with ``RecursionError``,
        which is not a ``Grafx*`` type and must never leave a public door.
        """
        pending: list[tuple[PlanNode, int]] = [(self, 0)]
        seen: set[int] = set()
        while pending:
            node, depth = pending.pop()
            if depth > MAX_PLAN_DEPTH:
                raise GrafxPlanError(
                    f"An operator tree may be at most {MAX_PLAN_DEPTH} operators deep.",
                    field="depth",
                    value=MAX_PLAN_DEPTH,
                )
            if id(node) in seen:
                raise GrafxPlanError(
                    f"The operator {node.label} appears twice in one tree, so the plan is a "
                    "graph rather than a tree and a walk over it would visit it twice.",
                    field="operator",
                    value=node.label,
                )
            seen.add(id(node))
            yield node, depth
            for child in reversed(node.children()):
                pending.append((child, depth + 1))

    def walk(self) -> Iterator[PlanNode]:
        """Yield this operator and every operator beneath it, parents first."""
        for node, _ in self.traverse():
            yield node

    def render(self) -> tuple[str, ...]:
        """Return the tree as indented lines, parents before children."""
        return tuple("  " * depth + node.describe() for node, depth in self.traverse())

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly view of this operator and everything beneath it."""
        root: dict[str, object] = {}
        stack: list[tuple[int, dict[str, object]]] = []
        for node, depth in self.traverse():
            entry: dict[str, object] = {
                "operator": node.label,
                "details": dict(node.details()),
                "children": [],
            }
            while stack and stack[-1][0] >= depth:
                stack.pop()
            if stack:
                siblings = stack[-1][1]["children"]
                if isinstance(siblings, list):
                    siblings.append(entry)
            else:
                root = entry
            stack.append((depth, entry))
        return root


# --- sources ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SingleRow(PlanNode):
    """A source of exactly one row with nothing bound, which is what a bare RETURN reads."""

    def details(self) -> Mapping[str, object]:
        """Return nothing; this operator has no settings."""
        return {}


@dataclass(frozen=True, slots=True)
class ArgumentRows(PlanNode):
    """The current outer row of one explicitly correlated apply operator."""

    slot: int


@dataclass(frozen=True, slots=True)
class ApplyRows(PlanNode):
    """Run an inner read per outer row, null-extending its new bindings when empty."""

    child: PlanNode
    inner: PlanNode
    slot: int
    null_variables: tuple[str, ...]

    def children(self) -> tuple[PlanNode, ...]:
        """Expose both plans to budget, shape and explain validation."""
        return self.child, self.inner


@dataclass(frozen=True, slots=True)
class ProcedureRows(PlanNode):
    """A trusted, explicitly authorized tabular call in the normal row pipeline."""

    child: PlanNode
    name: str
    columns: tuple[tuple[str, str], ...]
    max_rows: int
    max_result_bytes: int
    arguments: tuple[Expression, ...]
    yields: tuple[ReturnItem, ...]

    def children(self) -> tuple[PlanNode, ...]:
        """Expose the incoming rows to standard planner and budget validation."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Expose the signature, never callback identity or executable implementation."""
        return {"procedure": self.name, "access": "pure_tabular",
                "columns": tuple(item.name for item in self.yields),
                "max_rows": self.max_rows, "max_result_bytes": self.max_result_bytes}


@dataclass(frozen=True, slots=True)
class SubqueryRows(PlanNode):
    """Apply a returning subquery per input row with explicitly mapped imports/exports."""

    child: PlanNode
    inner: PlanNode
    slot: int
    imports: tuple[tuple[str, str], ...]
    outputs: tuple[str, ...]

    def children(self) -> tuple[PlanNode, ...]:
        """Expose both sides to explain and resource-bound traversal."""
        return self.child, self.inner


@dataclass(frozen=True, slots=True)
class UnwindRows(PlanNode):
    """One row per element of a list, each bound to the name the clause gave it.

    The source of a batch statement. It streams: the executor pulls one element at a time and
    the rest of the plan runs to completion for that element before the next is read, so a
    thousand-element batch never has to exist as a thousand rows at once.
    """

    child: PlanNode
    alias: str
    expression: Expression

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose single row this expansion runs under."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the name bound and the list it was bound from."""
        return {"alias": self.alias, "list": self.expression.describe()}


@dataclass(frozen=True, slots=True)
class AllNodesScan(PlanNode):
    """Every node row of every node table, under one name.

    A pattern that names no label matches a node whatever table it lives in, so this operator
    reads the tables in one pass and binds each row under the same variable. It is ONE operator
    rather than a chain of scans on purpose: chained scans would multiply the tables into a
    product, and a union of scans placed side by side would need a filter of its own to know
    which branch produced a row. Everything above -- the predicate, the grouping, the ordering
    and the window -- therefore applies to the whole set exactly once, which is what a caller
    counting all nodes or paging through them is asking for.

    The tables are fixed when the plan is built and they are read in table_id order, so a plan
    says which tables it will read and two runs of one plan read them in one order.
    """

    child: PlanNode
    variable: str
    tables: tuple[TableDef, ...]

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows drive this scan."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the name bound and the tables the scan unites, in the order it reads them."""
        return {
            "variable": self.variable,
            "tables": ", ".join(table.name for table in self.tables) or "none",
        }


@dataclass(frozen=True, slots=True)
class OrderedNodeMerge(PlanNode):
    """Bounded descending merge of one exact ordered generation per node table.

    ``fallback`` is the complete canonical scan/filter pipeline. It is used only before an
    ordered certificate is opened when the runtime collaborator cannot discharge the planned
    capability. Once iteration starts, any drift or damage propagates rather than mixing a
    partially produced ordered prefix with a scan.
    """

    fallback: PlanNode
    variable: str
    tables: tuple[TableDef, ...]
    indexes: tuple[str, ...]
    timestamp_column: str
    string_column: str
    limit: Expression
    predicate: Expression | None = None
    upper_timestamp: Expression | None = None
    upper_string: Expression | None = None

    def children(self) -> tuple[PlanNode, ...]:
        """Expose the canonical fallback as the operator's proof-preserving child."""
        return (self.fallback,)

    def details(self) -> Mapping[str, object]:
        """Return the exact order, participating generations and optional bound."""
        return {
            "variable": self.variable,
            "tables": ", ".join(table.name for table in self.tables) or "none",
            "indexes": ", ".join(self.indexes) or "none",
            "order": f"{self.timestamp_column} DESC, {self.string_column} DESC",
            "upper_bound": (
                "none"
                if self.upper_timestamp is None or self.upper_string is None
                else (
                    f"({self.upper_timestamp.describe()}, "
                    f"{self.upper_string.describe()})"
                )
            ),
            "limit": self.limit.describe(),
        }


@dataclass(frozen=True, slots=True)
class NodeScan(PlanNode):
    """Every version of one node table the snapshot can see, once per incoming row.

    The child is what makes a comma-separated pattern one tree rather than two queries:
    ``MATCH (a:A), (b:B)`` is a scan of B driven by each row of a scan of A, which is the product
    the language means, expressed as nesting.
    """

    child: PlanNode
    variable: str
    table: TableDef

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows drive this scan."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the variable this scan binds and the table it reads."""
        return {"variable": self.variable, "table": self.table.name}


@dataclass(frozen=True, slots=True)
class IndexSeek(PlanNode):
    """The rows one secondary index offers for one key, under the contract it declares.

    ``visibility`` is carried in the plan because the two contracts differ in a way a caller can
    get wrong: an EXACT hit is a CANDIDATE that the framework validates against the heap under
    the snapshot, while a PROXIMITY hit is already decided by the entry's own birth stamp and
    tombstone (CONTRACT.md section 8.7, SD-3). The executor never chooses between them -- it asks
    the index manager, which owns that dispatch -- but a plan that did not record which contract
    it was built for could not be reviewed for having assumed the wrong one.
    """

    child: PlanNode
    variable: str
    table: TableDef
    index: str
    visibility: IndexVisibility
    key_columns: tuple[str, ...]
    key_values: tuple[Expression, ...]

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows drive this seek."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the index, its contract and the key this seek was built for."""
        return {
            "variable": self.variable,
            "table": self.table.name,
            "index": self.index,
            "visibility": self.visibility.value,
            "key": ", ".join(
                f"{column} = {value.describe()}"
                for column, value in zip(self.key_columns, self.key_values)
            ),
        }


# --- shaping ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TraverseRelationship(PlanNode):
    """One hop, or a bounded range of hops, from rows the child already produced."""

    child: PlanNode
    source: str
    target: str
    relationship: str | None
    table: TableDef
    direction: Direction
    min_hops: int
    max_hops: int
    target_table: TableDef | None = None
    target_bound: bool = False
    path_variable: str | None = None
    """The named walk this segment captures."""
    path_append: bool = False
    """Continue a previous segment of this same named pattern."""
    relationship_list: bool = False
    """A written range binds a list even when its bounds are exactly 1..1."""

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator this traversal expands from."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the endpoints, the relationship table and the hop range."""
        details: dict[str, object] = {
            "source": self.source,
            "target": self.target,
            "table": self.table.name,
            "direction": self.direction.value,
            "hops": f"{self.min_hops}..{self.max_hops}",
            "target_bound": self.target_bound,
        }
        if self.path_variable is not None:
            details["path"] = self.path_variable
        if self.path_append:
            details["path_append"] = True
        if self.relationship_list:
            details["relationship_list"] = True
        return details


@dataclass(frozen=True, slots=True)
class TraverseAnyRelationship(PlanNode):
    """One incident hop across eligible tables, optionally null-extending each anchor.

    A typed hop names its table and :class:`TraverseRelationship` walks it. An untyped hop names
    none, and the honest answer is not to pick one: it is every eligible relationship table.
    Correlated optional hops additionally support incoming/undirected or typed expansion,
    a target label and a predicate evaluated before per-anchor null extension.

    The tables are ordered by ``table_id``, which is the order the catalog assigned them and the
    only order that does not depend on how a name happens to sort. Multiplicity is preserved
    both ways -- a row appears once per edge, and parallel edges between the same two nodes are
    separate rows -- because the pair a caller asked for is the edge, not the neighbour.

    One operator rather than a union of traversals, so the rows it produces meet the
    intermediate-row budget at ONE admission point and the child is drawn once.
    """

    child: PlanNode
    source: str
    target: str
    relationship: str
    tables: tuple[TableDef, ...]
    direction: Direction = Direction.OUTGOING
    optional: bool = False
    source_table: str | None = None
    target_table: str | None = None
    relationship_polymorphic: bool = False
    predicate: Expression | None = None

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator this traversal expands from."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the endpoints and the tables this hop may live in, in walk order."""
        details = {
            "source": self.source,
            "target": self.target,
            "tables": ", ".join(table.name for table in self.tables),
        }
        if self.optional:
            details["optional"] = "true"
            details["direction"] = self.direction.value
            if self.predicate is not None:
                details["predicate"] = self.predicate.describe()
        return details


@dataclass(frozen=True, slots=True)
class RelationshipScan(PlanNode):
    """One pass over a relationship table, binding both endpoints of each surviving edge.

    The edge-first shape of ST-1 (b): when neither end of a single typed hop is seekable and
    the residual predicate reads only the relationship, driving the hop from a node scan pays
    a whole node-table walk for information the edge rows already carry. This operator scans
    the relationship table ONCE, evaluates the r-only predicate on a binding that holds
    nothing but the edge, and resolves the two endpoints only for the rows that survive --
    the same owner overlay, ended-row rule and landing-visibility rule the traversal applies.
    """

    child: PlanNode
    from_variable: str
    to_variable: str
    relationship: str | None
    table: TableDef
    from_table: TableDef
    to_table: TableDef
    predicate: Expression | None = None

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows drive this scan."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the endpoints, the relationship table and the consumed predicate."""
        details: dict[str, object] = {
            "from": self.from_variable,
            "to": self.to_variable,
            "table": self.table.name,
        }
        if self.relationship is not None:
            details["relationship"] = self.relationship
        if self.predicate is not None:
            details["predicate"] = self.predicate.describe()
        return details


@dataclass(frozen=True, slots=True)
class NodeMultiKeySeek(PlanNode):
    """Resolve a closed primary-key list of one node table through its exact multi-key index.

    The operator is admitted only for a standalone labelled node whose first local term is
    ``n.<primary key> IN $parameter``.  ``fallback`` is the canonical scan of the same table for
    the same statement, and the predicate itself stays above this operator, replayed over every
    row either path produces: a missing/stale capability, a probe that is not a list or that the
    durable key cannot represent, or a table this transaction has already written change only
    the access path, never the answer or the refusal.  The runtime still validates every
    exact-index candidate against the heap under the transaction snapshot before yielding it.
    """

    fallback: PlanNode
    variable: str
    table: TableDef
    keys: Expression
    key_position: int
    index: str

    def children(self) -> tuple[PlanNode, ...]:
        """Expose the exact canonical fallback retained by this access path."""
        return (self.fallback,)

    def details(self) -> Mapping[str, object]:
        """Describe the closed predicate and the exact index it needs."""
        return {
            "variable": self.variable,
            "table": self.table.name,
            "index": self.index,
            "predicate": (
                f"{self.variable}.{self.table.primary_key} IN {self.keys.describe()}"
            ),
        }


@dataclass(frozen=True, slots=True)
class RelationshipIncidentSeek(PlanNode):
    """Resolve a bounded set of incident edges through exact multi-key indexes.

    The operator is admitted only for the closed predicate
    ``from.pk IN keys OR to.pk IN keys`` over one directed typed hop. ``fallback`` is the full
    canonical edge-first scan and predicate tree for the same statement: a missing/stale capability
    therefore changes only the access path, never the answer. The runtime still validates every
    exact-index candidate against the heap under the transaction snapshot and resolves both
    endpoint rows before yielding an edge.
    """

    fallback: PlanNode
    from_variable: str
    to_variable: str
    relationship: str | None
    table: TableDef
    from_table: TableDef
    to_table: TableDef
    from_keys: Expression
    to_keys: Expression
    from_key_position: int
    to_key_position: int
    from_index: str
    to_index: str
    relationship_from_index: str
    relationship_to_index: str

    def children(self) -> tuple[PlanNode, ...]:
        """Expose the exact canonical fallback retained by this access path."""
        return (self.fallback,)

    def details(self) -> Mapping[str, object]:
        """Describe the closed predicate and the four exact indexes it needs."""
        return {
            "from": self.from_variable,
            "to": self.to_variable,
            "table": self.table.name,
            "relationship": self.relationship or "",
            "predicate": (
                f"{self.from_variable}.{self.from_table.primary_key} IN "
                f"{self.from_keys.describe()} OR "
                f"{self.to_variable}.{self.to_table.primary_key} IN "
                f"{self.to_keys.describe()}"
            ),
            "indexes": ", ".join(
                (
                    self.from_index,
                    self.to_index,
                    self.relationship_from_index,
                    self.relationship_to_index,
                )
            ),
        }


@dataclass(frozen=True, slots=True)
class FilterRows(PlanNode):
    """Rows of the child that satisfy a predicate."""

    child: PlanNode
    predicate: Expression

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator this filter reads."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the predicate as it was written."""
        return {"predicate": self.predicate.describe()}


@dataclass(frozen=True, slots=True)
class VectorSearch(PlanNode):
    """The similarity operator over the candidate set denoted by its child (SPEC-VEC FR-4).

    The child is the candidate set -- everything the node predicates, the relationship predicates
    and the traversal left standing -- and it is a CHILD rather than a separate query because
    BR-6 says the combination belongs to the plan. The record identifiers of those rows become
    the candidate filter the two-regime planner of the vector subsystem reads, so the regime is
    chosen from the real filtered cardinality rather than from a guess.

    A runtime may recognise the exact ``NodeScan(SingleRow)`` whole-table shape as an access-path
    opportunity.  It may bypass physical child materialisation only after proving that the vector
    index's count belongs to the transaction snapshot's durable frontier and to the exact planned
    table/column pair, and must fall back to this child for every filtered, correlated, historical,
    custom-snapshot or otherwise uncertain case.  An owner-dirty table retains the executor's
    separate fail-closed RYOW refusal.  The child therefore remains both the semantic authority and
    the inspectable fallback in the one plan.

    ``k`` is present only when the query asked for a top-k and nothing above this operator can
    discard a row. When it is None the operator scores every candidate and returns them all,
    which cannot under-deliver and is what a threshold query wants.
    """

    child: PlanNode
    variable: str
    space: Expression
    query_vector: Expression
    property_key: str
    score_column: str
    column_space: str = ""
    k: Expression | None = None
    threshold: Expression | None = None
    threshold_operator: str | None = None

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator that produces the candidate set."""
        return (self.child,)

    @property
    def bounded(self) -> bool:
        """Return True when this operator returns at most a fixed number of neighbours."""
        return self.k is not None

    def details(self) -> Mapping[str, object]:
        """Return the space, the reference vector, the bound and the threshold."""
        settings: dict[str, object] = {
            "variable": self.variable,
            "property": self.property_key,
            "space": self.space.describe(),
            "column_space": self.column_space,
            "query": self.query_vector.describe(),
            "score": self.score_column,
            "k": "all candidates" if self.k is None else self.k.describe(),
        }
        if self.threshold is not None and self.threshold_operator is not None:
            settings["threshold"] = (
                f"{self.threshold_operator} {self.threshold.describe()}"
            )
        return settings


@dataclass(frozen=True, slots=True)
class AggregateRows(PlanNode):
    """One row per group, with the aggregates of each group."""

    child: PlanNode
    grouping: tuple[ReturnItem, ...]
    aggregations: tuple[Aggregation, ...]
    # WITH carries matched entities to downstream graph operators; RETURN can
    # detach them to public values and retain its compact spill representation.
    preserve_group_bindings: bool = False

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows are grouped."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the grouping keys and the aggregates."""
        return {
            "grouping": ", ".join(item.describe() for item in self.grouping) or "none",
            "aggregates": ", ".join(item.describe() for item in self.aggregations),
        }


@dataclass(frozen=True, slots=True)
class WithRows(PlanNode):
    """The rows one WITH stage projects, carrying only the names that stage named.

    Every item is evaluated against the row that ARRIVED, so the items of one stage cannot read
    each other: they are produced together or not at all. What leaves is bound to exactly the
    projected names -- a matched row carried under its own name keeps the binding it had, so
    the clauses below still read its properties and can still write it, and a computed item
    arrives as the value it evaluated to.

    It streams one row in, one row out. The stage narrows what a row carries; it never holds
    rows back, which is why a WHERE above it filters as early as the projection allows.
    """

    child: PlanNode
    items: tuple[ReturnItem, ...]

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows this stage projects."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the projected items as they were written."""
        return {"items": ", ".join(item.describe() for item in self.items)}


@dataclass(frozen=True, slots=True)
class ProjectRows(PlanNode):
    """The projected columns of each row."""

    child: PlanNode
    items: tuple[ReturnItem, ...]

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows are projected."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the projected items as they were written."""
        return {"items": ", ".join(item.describe() for item in self.items)}


@dataclass(frozen=True, slots=True)
class EagerRows(PlanNode):
    """Draw every row of the child before yielding the first one.

    The operator exists for one reason and it is a correctness reason. Every other operator here
    is a lazy generator, so an operator ABOVE that stops early stops the ones below it too. That
    is harmless when the work below only reads, and it is data loss when the work below WRITES:
    ``MATCH (p:Person) CREATE (:Copy {...}) RETURN 1 LIMIT 1`` would perform one write of the
    five the MATCH asked for, and adding an ORDER BY -- which changes nothing about which rows
    are written -- would silently change the answer to five, because a sort has to draw its whole
    input anyway.

    A window limits the rows a caller RECEIVES. It does not limit the writes a statement
    performs, and no reading of the language says otherwise: LIMIT 0 performing exactly one write
    is neither "write for every row" nor "write for none". So the planner puts this between the
    clauses that write and the clauses that shape the result, and the barrier is visible in the
    plan rather than being an invisible property of how the executor happens to iterate.
    """

    child: PlanNode
    publish_read_phase: bool = False

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows are drawn in full."""
        return (self.child,)


@dataclass(frozen=True, slots=True)
class OptionalRows(PlanNode):
    """Rows of the child, or one row binding a name to null when the child had none.

    The operator sits ABOVE every filter the clause carries, deferred similarity terms
    included, because the WHERE belongs to the OPTIONAL MATCH. A query that matched rows and
    then filtered all of them away matched nothing, and has to answer the same single null row
    as one whose scan was empty; wrapping the scan instead would let a filter above delete the
    extension it was supposed to protect.

    Binding the name to null rather than leaving it unbound is what makes the extension
    readable: ``v.prop`` and ``label(v)`` already answer null for a null subject, an unbound
    name refuses instead, and the difference between those two is the whole point of the
    clause.
    """

    child: PlanNode
    alias: str

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows are extended when it produces none."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the name bound to null when nothing matched."""
        return {"alias": self.alias}


@dataclass(frozen=True, slots=True)
class UnionRows(PlanNode):
    """The rows of two pipelines, one after the other, under the left branch's column names.

    The only operator here with two children, and the reason the tree stays a tree: each branch
    was planned as a whole query and then had its own ProduceResults taken off, so what arrives
    is two streams of projected columns that no longer know they were separate.

    Renaming happens HERE rather than in either branch. The right branch may have written
    different aliases -- a caller reading the result never sees them -- and rewriting its
    projection to use the left branch's names would make the branch a lie about the text that
    produced it. The operator maps position by position instead, which is also the only reading
    that works when both branches return the same name for different things.

    Deduplication is NOT here. Unless UNION ALL is selected, a single DistinctRows sits above, because the pair is one result
    and duplicates across branches are duplicates: removing them inside each branch would leave
    a row that appears once on each side appearing twice.
    """

    left: PlanNode
    right: PlanNode
    columns: tuple[str, ...]

    def children(self) -> tuple[PlanNode, ...]:
        """Return the two pipelines, left branch first, which is evaluation order."""
        return (self.left, self.right)

    def details(self) -> Mapping[str, object]:
        """Return the column names the pair publishes."""
        return {"columns": ", ".join(self.columns)}


@dataclass(frozen=True, slots=True)
class DistinctRows(PlanNode):
    """Rows of the child with duplicates removed, keeping the first of each."""

    child: PlanNode

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows are deduplicated."""
        return (self.child,)


@dataclass(frozen=True, slots=True)
class SortRows(PlanNode):
    """Rows of the child in the order the query asked for.

    ``retained_limit`` is a physical bound, not another semantic window.  When present, the
    executor may keep only ``retained_skip + retained_limit`` rows while it orders the child;
    the ordinary :class:`SkipRows` and :class:`LimitRows` above this node still apply the query's
    window.  Keeping both expressions separate avoids turning two valid INT64 parameters into
    an overflowing query-language addition merely to communicate an execution bound.
    """

    child: PlanNode
    keys: tuple[SortItem, ...]
    retained_limit: Expression | None = None
    retained_skip: Expression | None = None

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows are ordered."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the sort keys and their directions."""
        details: dict[str, object] = {
            "keys": ", ".join(key.describe() for key in self.keys)
        }
        if self.retained_limit is not None:
            limit = self.retained_limit.describe()
            details["retains"] = (
                limit
                if self.retained_skip is None
                else f"{self.retained_skip.describe()} + {limit}"
            )
        return details


@dataclass(frozen=True, slots=True)
class SkipRows(PlanNode):
    """Rows of the child after the first few are dropped."""

    child: PlanNode
    count: Expression

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose leading rows are dropped."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return how many rows are dropped."""
        return {"count": self.count.describe()}


@dataclass(frozen=True, slots=True)
class LimitRows(PlanNode):
    """At most that many rows of the child."""

    child: PlanNode
    count: Expression

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows are truncated."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the row ceiling."""
        return {"count": self.count.describe()}


@dataclass(frozen=True, slots=True)
class ProduceResults(PlanNode):
    """The root of every plan: the columns the caller receives."""

    child: PlanNode
    columns: tuple[str, ...] = ()

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows are handed to the caller."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the column names of the result."""
        return {"columns": ", ".join(self.columns) or "none"}


# --- writes -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreatedNode:
    """One node a write operator must insert, or reuse when the variable is already bound."""

    variable: str | None
    table: TableDef | None
    properties: MapExpression | None

    def describe(self) -> str:
        """Return the node as it would be written back."""
        table = self.table.name if self.table is not None else "bound"
        return f"({self.variable or ''}:{table})"


@dataclass(frozen=True, slots=True)
class CreatedRelationship:
    """One relationship a write operator must insert, between two of its nodes."""

    variable: str | None
    table: TableDef
    source: str
    target: str
    direction: Direction
    properties: MapExpression | None

    def describe(self) -> str:
        """Return the relationship as it would be written back."""
        return f"({self.source})-[:{self.table.name}]->({self.target})"


@dataclass(frozen=True, slots=True)
class CreateRelationships(PlanNode):
    """Insert the nodes and relationships one CREATE clause names, once per incoming row."""

    child: PlanNode
    nodes: tuple[CreatedNode, ...]
    relationships: tuple[CreatedRelationship, ...] = ()

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows drive the insertion."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return what this operator inserts."""
        return {
            "nodes": ", ".join(node.describe() for node in self.nodes),
            "relationships": ", ".join(item.describe() for item in self.relationships)
            or "none",
        }


@dataclass(frozen=True, slots=True)
class MergePattern(PlanNode):
    """Match one pattern, and insert it exactly once when nothing matches."""

    child: PlanNode
    nodes: tuple[CreatedNode, ...]
    relationships: tuple[CreatedRelationship, ...] = ()

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows drive the merge."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return what this operator merges."""
        return {
            "nodes": ", ".join(node.describe() for node in self.nodes),
            "relationships": ", ".join(item.describe() for item in self.relationships)
            or "none",
        }


@dataclass(frozen=True, slots=True)
class PropertyAssignment:
    """One property a SET clause writes."""

    target: Property
    value: Expression

    def describe(self) -> str:
        """Return the assignment as it was written."""
        return f"{self.target.describe()} = {self.value.describe()}"


@dataclass(frozen=True, slots=True)
class SetProperties(PlanNode):
    """Write the properties one SET clause names, once per incoming row."""

    child: PlanNode
    assignments: tuple[PropertyAssignment, ...]

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows are updated."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the assignments."""
        return {"assignments": ", ".join(item.describe() for item in self.assignments)}


@dataclass(frozen=True, slots=True)
class DeleteEntities(PlanNode):
    """Remove the rows the named variables are bound to, once per incoming row."""

    child: PlanNode
    variables: tuple[str, ...]
    detach: bool = False

    def children(self) -> tuple[PlanNode, ...]:
        """Return the operator whose rows name what is deleted."""
        return (self.child,)

    def details(self) -> Mapping[str, object]:
        """Return the variables deleted and whether relationships go with them."""
        return {"variables": ", ".join(self.variables), "detach": self.detach}


# --- schema ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateIndex(PlanNode):
    """Build and publish one custom exact index over a committed node table."""

    name: str
    table: TableDef
    positions: tuple[int, ...]
    bucket_count: int
    expected_cardinality: int | None
    layout: IndexLayout
    key_derivation: str

    def details(self) -> Mapping[str, object]:
        """Return the fully resolved logical definition and sizing intent."""
        return {
            "index": self.name,
            "table": self.table.name,
            "positions": ", ".join(str(position) for position in self.positions),
            "bucket_count": self.bucket_count,
            "expected_cardinality": self.expected_cardinality or "none",
            "layout": self.layout.value,
            "key_derivation": self.key_derivation,
        }


@dataclass(frozen=True, slots=True)
class CreateNodeTable(PlanNode):
    """Install one node table in the catalog.

    The columns arrive already resolved against the catalog, because a vector column takes its
    stored precision from the embedding space it names and only the catalog knows that. What the
    plan deliberately does NOT carry is the numeric table identity: it is assigned when the
    statement runs, so a plan built now cannot install a table under an identity another
    statement has taken in the meantime.
    """

    name: str
    columns: tuple[ColumnDef, ...]
    primary_key: str | None

    def details(self) -> Mapping[str, object]:
        """Return the table this operator installs."""
        return {
            "table": self.name,
            "columns": ", ".join(
                f"{column.name} {column.type.name}" for column in self.columns
            ),
            "primary_key": self.primary_key or "none",
        }


@dataclass(frozen=True, slots=True)
class CreateRelTable(PlanNode):
    """Install one relationship table in the catalog."""

    name: str
    from_table: str
    to_table: str
    columns: tuple[ColumnDef, ...]

    def details(self) -> Mapping[str, object]:
        """Return the table this operator installs and the tables it connects."""
        return {
            "table": self.name,
            "from": self.from_table,
            "to": self.to_table,
            "columns": ", ".join(
                f"{column.name} {column.type.name}" for column in self.columns
            )
            or "none",
        }


@dataclass(frozen=True, slots=True)
class CreateVectorSpace(PlanNode):
    """Install one embedding space in the catalog.

    The numeric identity and the creation stamp are absent for the same reason the table identity
    is: one is assigned at execution and the other is a clock reading, and a pure plan holds
    neither a counter nor a clock.
    """

    name: str
    dimension: int
    metric: DistanceMetric
    normalized: bool
    storage_dtype: str

    def details(self) -> Mapping[str, object]:
        """Return the space this operator installs."""
        return {
            "space": self.name,
            "dimension": self.dimension,
            "metric": self.metric.value,
            "normalized": self.normalized,
            "storage_dtype": self.storage_dtype,
        }


def plan_nodes(root: PlanNode) -> tuple[PlanNode, ...]:
    """Return every operator of a plan, parents before children."""
    return tuple(root.walk())


def validate_plan(root: PlanNode) -> PlanNode:
    """Return the plan after checking the invariants a wrong plan would break.

    The one that matters is SPEC-VEC BR-6 and AC-7: a bounded similarity operator with a filter
    above it is an over-fetch followed by a post-filter, and it silently returns fewer rows than
    the caller asked for. It is refused here rather than in the planner so that a plan built by
    any other route -- a test, a future rewrite rule -- meets the same rule.
    """
    if not isinstance(root, PlanNode):
        raise GrafxPlanError(
            f"A plan is an operator tree; got {type(root).__name__}.",
            field="plan",
            value=type(root).__name__,
        )
    _refuse_unmatched_sort_retention(root)
    _refuse_post_filtered_search(root)
    return root


def _refuse_unmatched_sort_retention(root: PlanNode) -> None:
    """Refuse a physical top-N bound that is not the exact semantic window above it.

    A retained sort intentionally discards rows.  It is correct only when the immediately
    enclosing SKIP/LIMIT operators discard those same rows semantically; accepting a forged or
    future rewrite with any other shape would turn a performance hint into silent under-delivery.
    """
    ancestors: list[PlanNode] = []
    for node, depth in root.traverse():
        del ancestors[depth:]
        if (
            isinstance(node, SortRows)
            and node.retained_limit is None
            and node.retained_skip is not None
        ):
            raise GrafxPlanError(
                "A bounded sort cannot retain SKIP without an accompanying LIMIT.",
                field="operator",
                value=node.label,
                reason="unmatched_retention",
            )
        if isinstance(node, SortRows) and node.retained_limit is not None:
            parent = ancestors[-1] if ancestors else None
            if node.retained_skip is None:
                matched = (
                    isinstance(parent, LimitRows)
                    and parent.count == node.retained_limit
                )
            else:
                grandparent = ancestors[-2] if len(ancestors) >= 2 else None
                matched = (
                    isinstance(parent, SkipRows)
                    and parent.count == node.retained_skip
                    and isinstance(grandparent, LimitRows)
                    and grandparent.count == node.retained_limit
                )
            if not matched:
                raise GrafxPlanError(
                    "A bounded sort must be enclosed by the exact SKIP/LIMIT window whose "
                    "rows it retains.",
                    field="operator",
                    value=node.label,
                    reason="unmatched_retention",
                )
        ancestors.append(node)


def _refuse_post_filtered_search(root: PlanNode) -> None:
    """Refuse a plan in which a filter can discard rows a bounded similarity operator returned.

    Reachability is what the rule is about, not adjacency: a filter three operators above a
    bounded search discards its rows just as surely as one directly above it. The traversal
    below is the class's own bounded walk, so a malformed tree is refused by the same guard
    everything else uses; the state it carries is one flag per branch, which is whether anything
    on the way down from the root can drop a row.
    """
    discards_above: list[bool] = []
    for node, depth in root.traverse():
        del discards_above[depth:]
        above = discards_above[-1] if discards_above else False
        if isinstance(node, VectorSearch) and node.bounded and above:
            raise GrafxPlanError(
                "This plan asks the similarity operator for a bounded number of neighbours and "
                "then discards some of them, which returns fewer rows than the query asked for. "
                "The predicate belongs below the similarity operator, as one tree.",
                field="operator",
                value=node.label,
                space=node.space.describe(),
            )
        discards_above.append(above or isinstance(node, FilterRows))
