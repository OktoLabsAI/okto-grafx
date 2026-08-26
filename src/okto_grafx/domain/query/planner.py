"""Turning a statement into an operator tree against a catalog (CONTRACT.md section 8.9).

This is where a query stops being text and becomes a decision about how to answer it, and three
of those decisions are correctness rather than performance.

**Which index may answer a predicate.** An exact index answers EQUALITY on the whole of its key
and nothing else. Using one for a range, or for a prefix of a composite key, would return a subset
of the rows the query asked for -- so the planner takes an index seek only when the predicate
constrains every key column of that index with an equality against a value known before the query
runs. The visibility class the index declares travels into the plan, because CONTRACT.md section
8.7 gives the two classes different guarantees: an exact hit is a CANDIDATE the framework
validates against the heap under the snapshot, a proximity hit is already decided.

**Where the similarity operator sits.** Everything the query filters -- node properties,
relationship properties, traversal -- is planned BELOW the similarity operator, so the rows that
reach it are exactly the candidate set and the two-regime planner of the vector subsystem chooses
its regime from the real cardinality. That is SPEC-VEC BR-6 and AC-7 as a plan shape rather than
as a promise, and :func:`~okto_grafx.domain.query.plan.validate_plan` refuses the shape that
breaks it.

**When a LIMIT may become a k.** Only when nothing above the similarity operator can drop a row:
no filter, no DISTINCT, no aggregate, and an ORDER BY that is exactly the score descending. Fusing
in any other case would ask the vector operator for k rows and then discard some, which is the
under-delivery BR-2 forbids. When the conditions do not hold the operator scores every candidate,
which is slower and always right.

The dialect diverges from the reference engine in three places, deliberately and reported rather
than hidden: a matched node needs exactly one label, a matched relationship needs exactly one
type, and MERGE covers a single node or a single relationship between two already-bound nodes.
Each is a refusal with a message that names the rule, never a silent partial answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from okto_grafx.domain.errors import GrafxEmbeddingSpaceMismatch, GrafxPlanError
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import VECTOR_DTYPES, ValueType
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.query.analysis import QueryAnalysis, SimilarityUse, analyze
from okto_grafx.domain.query.ast import (
    BinaryOperation,
    ColumnSpec,
    CreateClause,
    CreateNodeTableStatement,
    CreateRelTableStatement,
    CreateVectorSpaceStatement,
    DeleteClause,
    Direction,
    Expression,
    FunctionCall,
    Literal,
    MapExpression,
    MatchClause,
    MergeClause,
    NodePattern,
    Parameter,
    PatternPath,
    Property,
    Query,
    RelationshipPattern,
    ReturnClause,
    ReturnItem,
    SetClause,
    SortItem,
    Statement,
    UpdatingClause,
    Variable,
    free_variables,
    walk,
)
from okto_grafx.domain.query.plan import (
    AggregateRows,
    CreatedNode,
    CreatedRelationship,
    CreateNodeTable,
    CreateRelationships,
    CreateRelTable,
    CreateVectorSpace,
    DeleteEntities,
    DistinctRows,
    EagerRows,
    FilterRows,
    IndexSeek,
    LimitRows,
    MergePattern,
    NodeScan,
    PlanNode,
    ProduceResults,
    ProjectRows,
    PropertyAssignment,
    SetProperties,
    SingleRow,
    SkipRows,
    SortRows,
    TraverseRelationship,
    VectorSearch,
    validate_plan,
)
from okto_grafx.domain.query.tokens import SIMILARITY_FUNCTION, SIMILARITY_SCORE_FUNCTION

__all__ = [
    "ANONYMOUS_VARIABLE_PREFIX",
    "COLUMN_VALUE_TYPES",
    "SCORE_COLUMN",
    "THRESHOLD_OPERATORS",
    "PlannedQuery",
    "build_plan",
    "conjuncts_of",
]

ANONYMOUS_VARIABLE_PREFIX: str = "anonymous pattern element "
"""The name an unnamed pattern element is bound under while a query runs.

It carries spaces on purpose. A user variable is an ASCII identifier and can never contain one,
so an anonymous binding can never be shadowed by, or shadow, something the caller wrote.
"""

SCORE_COLUMN: str = "similarity score"
"""The row key the similarity operator writes its score under, for the same reason."""

THRESHOLD_OPERATORS: frozenset[str] = frozenset({">", ">="})
"""The comparisons a similarity threshold may use.

Only the two that put a FLOOR under the score. The vector subsystem returns the best neighbours
first, so a floor can be applied inside the operator without changing which rows it would have
returned; a ceiling would ask it to skip its own best answers, which is a different search.
"""

COLUMN_VALUE_TYPES: dict[str, ValueType] = {
    "INT64": ValueType.INT64,
    "STRING": ValueType.STRING,
    "DOUBLE": ValueType.DOUBLE,
    "BOOL": ValueType.BOOL,
    "BOOLEAN": ValueType.BOOL,
    "BLOB": ValueType.BYTES,
    "UUID": ValueType.UUID,
    "TIMESTAMP": ValueType.TIMESTAMP,
}
"""The scalar column types of the dialect mapped to the stored value types of the domain model."""

_SPACE_OPTIONS: tuple[str, ...] = ("dimension", "metric", "normalized", "storage_dtype")


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    """A plan together with what a caller needs to run and read it."""

    root: PlanNode
    analysis: QueryAnalysis
    columns: tuple[str, ...] = ()
    writes: bool = False

    def describe(self) -> str:
        """Return the operator tree as one block of indented lines."""
        return "\n".join(self.root.render())


def conjuncts_of(predicate: Expression | None) -> tuple[Expression, ...]:
    """Return the top-level AND terms of a predicate, in written order.

    Splitting is what lets one conjunct become an index seek, another a similarity threshold and
    the rest a filter, without any of them being evaluated twice. Only AND splits: an OR term is
    one condition, and taking it apart would drop rows.
    """
    if predicate is None:
        return ()
    pending = [predicate]
    found: list[Expression] = []
    while pending:
        node = pending.pop(0)
        if isinstance(node, BinaryOperation) and node.operator == "AND":
            pending.insert(0, node.right)
            pending.insert(0, node.left)
            continue
        found.append(node)
    return tuple(found)


def _conjoin(terms: Sequence[Expression]) -> Expression | None:
    """Return the conjunction of these terms, or None when there are none."""
    if not terms:
        return None
    combined = terms[0]
    for term in terms[1:]:
        combined = BinaryOperation(operator="AND", left=combined, right=term)
    return combined


def build_plan(
    statement: Statement,
    *,
    catalog: Catalog,
    indexes: Sequence[IndexDefinition] = (),
    analysis: QueryAnalysis | None = None,
) -> PlannedQuery:
    """Return the operator tree that answers one statement against this catalog.

    Only :class:`~okto_grafx.domain.errors.GrafxPlanError` and the typed refusals of the domain
    model leave this door, and the plan it returns has already been checked against the shape
    rules of :func:`~okto_grafx.domain.query.plan.validate_plan`.
    """
    if not isinstance(catalog, Catalog):
        raise GrafxPlanError(
            f"Planning needs the catalog of the database; got {type(catalog).__name__}.",
            field="catalog",
            value=type(catalog).__name__,
        )
    resolved = analysis if analysis is not None else analyze(statement)
    planner = _Planner(catalog=catalog, indexes=tuple(indexes), analysis=resolved)
    return planner.run(statement)


@dataclass(slots=True)
class _Planner:
    """The single-use builder that turns one statement into one operator tree."""

    catalog: Catalog
    indexes: tuple[IndexDefinition, ...]
    analysis: QueryAnalysis
    tables: dict[str, TableDef] = field(default_factory=dict)
    anonymous: int = 0

    # --- entry -------------------------------------------------------------------------------

    def run(self, statement: Statement) -> PlannedQuery:
        """Plan whichever kind of statement this is."""
        if isinstance(statement, CreateNodeTableStatement):
            return self._planned(self._node_table(statement), writes=True)
        if isinstance(statement, CreateRelTableStatement):
            return self._planned(self._rel_table(statement), writes=True)
        if isinstance(statement, CreateVectorSpaceStatement):
            return self._planned(self._vector_space(statement), writes=True)
        if isinstance(statement, Query):
            return self._query(statement)
        raise GrafxPlanError(
            f"A statement of type {type(statement).__name__} cannot be planned.",
            field="statement",
            value=type(statement).__name__,
        )

    def _planned(
        self, root: PlanNode, *, columns: tuple[str, ...] = (), writes: bool = False
    ) -> PlannedQuery:
        """Wrap a root in the value a caller receives, after checking the plan's shape."""
        return PlannedQuery(
            root=validate_plan(root),
            analysis=self.analysis,
            columns=columns,
            writes=writes,
        )

    # --- schema ------------------------------------------------------------------------------

    def _node_table(self, statement: CreateNodeTableStatement) -> PlanNode:
        """Plan a CREATE NODE TABLE statement."""
        columns = self._columns(statement.columns, statement.primary_key)
        if statement.primary_key is not None and statement.primary_key not in {
            column.name for column in columns
        }:
            raise GrafxPlanError(
                f"Table {statement.name!r} names {statement.primary_key!r} as its primary key, "
                "which is not one of its columns.",
                field="primary_key",
                value=statement.primary_key,
            )
        return CreateNodeTable(
            name=statement.name, columns=columns, primary_key=statement.primary_key
        )

    def _rel_table(self, statement: CreateRelTableStatement) -> PlanNode:
        """Plan a CREATE REL TABLE statement, checking that both endpoints are node tables."""
        for role, name in (("from", statement.from_table), ("to", statement.to_table)):
            endpoint = self._table_named(name, role)
            if endpoint.kind != "node":
                raise GrafxPlanError(
                    f"A relationship connects node tables; {name!r} is a {endpoint.kind} table.",
                    field=role,
                    value=name,
                )
        return CreateRelTable(
            name=statement.name,
            from_table=statement.from_table,
            to_table=statement.to_table,
            columns=self._columns(statement.columns, None),
        )

    def _vector_space(self, statement: CreateVectorSpaceStatement) -> PlanNode:
        """Plan a CREATE VECTOR SPACE statement from its options map."""
        options = statement.options
        for key in options.keys():
            if key.lower() not in _SPACE_OPTIONS:
                raise GrafxPlanError(
                    f"An embedding space has no option named {key!r}; it takes "
                    f"{', '.join(_SPACE_OPTIONS)}.",
                    field="option",
                    value=key,
                )
        dimension = self._required_option(options, "dimension", int)
        metric_name = self._required_option(options, "metric", str)
        normalized = self._optional_option(options, "normalized", bool, False)
        storage_dtype = self._optional_option(options, "storage_dtype", str, "float32")
        metric = self._metric(metric_name)
        return CreateVectorSpace(
            name=statement.name,
            dimension=dimension,
            metric=metric,
            normalized=normalized,
            storage_dtype=storage_dtype,
        )

    def _metric(self, name: str) -> DistanceMetric:
        """Return the distance metric of that name, refusing one the port does not offer."""
        for candidate in DistanceMetric:
            if candidate.value == name:
                return candidate
        allowed = ", ".join(repr(candidate.value) for candidate in DistanceMetric)
        raise GrafxPlanError(
            f"An embedding space compares vectors with one of {allowed}; got {name!r}.",
            field="metric",
            value=name,
        )

    def _required_option(
        self, options: MapExpression, key: str, kind: type
    ) -> object:
        """Return one option of an embedding space, refusing a missing or non-constant one."""
        value = options.entry(key)
        if value is None:
            raise GrafxPlanError(
                f"An embedding space must declare its {key}.",
                field=key,
                value=None,
            )
        return self._constant(value, key, kind)

    def _optional_option(
        self, options: MapExpression, key: str, kind: type, fallback: object
    ) -> object:
        """Return one option of an embedding space, or the default when it was not written."""
        value = options.entry(key)
        if value is None:
            return fallback
        return self._constant(value, key, kind)

    def _constant(self, expression: Expression, key: str, kind: type) -> object:
        """Return the constant an option carries, refusing anything decided at run time."""
        if not isinstance(expression, Literal):
            raise GrafxPlanError(
                f"The {key} of an embedding space is written as a constant; got "
                f"{expression.describe()}.",
                field=key,
                value=expression.describe(),
            )
        value = expression.value
        if kind is bool:
            if not isinstance(value, bool):
                raise self._wrong_option(key, "true or false", expression)
            return value
        if kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise self._wrong_option(key, "a whole number", expression)
            return value
        if not isinstance(value, str):
            raise self._wrong_option(key, "a name in quotes", expression)
        return value

    def _wrong_option(self, key: str, wanted: str, expression: Expression) -> GrafxPlanError:
        """Return the refusal for an option written with the wrong kind of constant."""
        return GrafxPlanError(
            f"The {key} of an embedding space is {wanted}; got {expression.describe()}.",
            field=key,
            value=expression.describe(),
        )

    def _columns(
        self, specs: Sequence[ColumnSpec], primary_key: str | None
    ) -> tuple[ColumnDef, ...]:
        """Resolve the declared columns against the catalog, refusing a name written twice."""
        seen: set[str] = set()
        resolved: list[ColumnDef] = []
        for spec in specs:
            if spec.name in seen:
                raise GrafxPlanError(
                    f"The column {spec.name!r} is declared more than once.",
                    field="column",
                    value=spec.name,
                )
            seen.add(spec.name)
            resolved.append(self._column(spec, nullable=spec.name != primary_key))
        return tuple(resolved)

    def _column(self, spec: ColumnSpec, *, nullable: bool) -> ColumnDef:
        """Resolve one declared column, reading a vector column's precision from its space."""
        if spec.vector_space is None:
            value_type = COLUMN_VALUE_TYPES.get(spec.type_name)
            if value_type is None:
                raise GrafxPlanError(
                    f"The column {spec.name!r} declares the type {spec.type_name!r}, which this "
                    "engine does not store.",
                    field="type",
                    value=spec.type_name,
                )
            return ColumnDef(name=spec.name, type=value_type, nullable=nullable)
        space = self._space_named(spec.vector_space)
        return ColumnDef(
            name=spec.name,
            type=VECTOR_DTYPES[space.storage_dtype],
            nullable=nullable,
            vector_space=space.name,
        )

    # --- catalog -----------------------------------------------------------------------------

    def _table_named(self, name: str, role: str) -> TableDef:
        """Return one table by name, refusing a name the catalog does not know."""
        if not self.catalog.has_table(name):
            known = ", ".join(table.name for table in self.catalog.tables()) or "none"
            raise GrafxPlanError(
                f"No table named {name!r} exists; the tables of this database are {known}.",
                field=role,
                value=name,
            )
        return self.catalog.table(name)

    def _space_named(self, name: str) -> EmbeddingSpaceDef:
        """Return one embedding space by name, refusing one the catalog does not know."""
        if not self.catalog.has_space(name):
            known = ", ".join(space.name for space in self.catalog.spaces()) or "none"
            raise GrafxPlanError(
                f"No embedding space named {name!r} exists; the spaces of this database are "
                f"{known}.",
                field="space",
                value=name,
            )
        return self.catalog.space(name)

    def _column_of(self, table: TableDef, key: str) -> ColumnDef | None:
        """Return one column of a table by name, or None when the table has no such column."""
        for column in table.columns:
            if column.name == key:
                return column
        return None

    def _index_for(
        self, table: TableDef, constrained: Sequence[str]
    ) -> tuple[IndexDefinition, tuple[str, ...]] | None:
        """Return the widest index whose WHOLE key is constrained by these columns.

        The whole key and nothing less. A hash index stores the encoding of ALL its key columns
        as one key, so a seek that constrained only some of them would be asking for entries
        under a key that was never written -- an empty answer for a query that has rows, which
        is the wrong-result failure CONTRACT.md section 13 item 2 names.

        The key order comes from the index definition rather than from the order the conditions
        were written in, because the definition is what decided the bytes. Candidates are ranked
        by key width and then by name, so the choice does not depend on the order the caller
        happened to register its indexes in.
        """
        by_position: dict[int, str] = {}
        for name in constrained:
            position = table.column_index(name)
            by_position[position] = name
        candidates: list[tuple[int, str, IndexDefinition, tuple[str, ...]]] = []
        for definition in self.indexes:
            if definition.table_id != table.table_id:
                continue
            if not all(position in by_position for position in definition.positions):
                continue
            columns = tuple(by_position[position] for position in definition.positions)
            candidates.append((-len(columns), definition.name, definition, columns))
        if not candidates:
            return None
        candidates.sort(key=lambda entry: (entry[0], entry[1]))
        chosen = candidates[0]
        return chosen[2], chosen[3]

    # --- queries -----------------------------------------------------------------------------

    def _query(self, statement: Query) -> PlannedQuery:
        """Plan a reading and updating query."""
        pipeline: PlanNode = SingleRow()
        similarity_terms: list[Expression] = []
        for clause in statement.match_clauses:
            pipeline, deferred = self._match_clause(pipeline, clause)
            similarity_terms.extend(deferred)
        pipeline = self._similarity(pipeline, statement, similarity_terms)
        for clause in statement.updating_clauses:
            pipeline = self._updating_clause(pipeline, clause)
        if statement.updating_clauses:
            # Everything that writes is drawn in full before anything above can stop early. A
            # LIMIT truncates what the caller RECEIVES; it must not decide how many rows got
            # written, and without this barrier it does -- silently, and differently depending on
            # whether an ORDER BY happens to sit beside it.
            pipeline = EagerRows(child=pipeline)
        columns: tuple[str, ...] = ()
        if statement.return_clause is not None:
            pipeline = self._result(pipeline, statement.return_clause)
            columns = statement.return_clause.column_names()
        return self._planned(
            ProduceResults(child=pipeline, columns=columns),
            columns=columns,
            writes=statement.writes,
        )

    def _match_clause(
        self, pipeline: PlanNode, clause: MatchClause
    ) -> tuple[PlanNode, list[Expression]]:
        """Plan the patterns of one MATCH clause and place its residual predicate."""
        terms = list(conjuncts_of(clause.predicate))
        for pattern in clause.patterns:
            pipeline, terms = self._pattern(pipeline, pattern, terms)
        residual = [term for term in terms if not self._reads_similarity(term)]
        deferred = [term for term in terms if self._reads_similarity(term)]
        predicate = _conjoin(residual)
        if predicate is not None:
            pipeline = FilterRows(child=pipeline, predicate=predicate)
        return pipeline, deferred

    def _reads_similarity(self, expression: Expression) -> bool:
        """Return True when this term needs the score the similarity operator produces."""
        return any(
            isinstance(node, FunctionCall)
            and node.name.upper() in (SIMILARITY_FUNCTION, SIMILARITY_SCORE_FUNCTION)
            for node in walk(expression)
        )

    # --- patterns ----------------------------------------------------------------------------

    def _pattern(
        self, pipeline: PlanNode, pattern: PatternPath, terms: list[Expression]
    ) -> tuple[PlanNode, list[Expression]]:
        """Plan one connected path, taking index seeks from the predicate where it can."""
        first = pattern.nodes[0]
        pipeline, terms, source = self._match_node(pipeline, first, terms)
        for position, relationship in enumerate(pattern.relationships):
            target_pattern = pattern.nodes[position + 1]
            pipeline, source = self._traverse(pipeline, source, relationship, target_pattern)
            if target_pattern.properties is not None:
                # The inline map on a TARGET node is the same shorthand it is on the first node,
                # and it used to be dropped here: `(x)-[:R]->(y:P {id: 2})` matched every
                # neighbour of x, and a SET or DELETE above it touched all of them (C10 round-2
                # B1). The terms become a filter above the traversal, where the target is bound.
                terms = terms + list(self._property_terms(source, target_pattern.properties))
        return pipeline, terms

    def _match_node(
        self, pipeline: PlanNode, pattern: NodePattern, terms: list[Expression]
    ) -> tuple[PlanNode, list[Expression], str]:
        """Bind one node of a pattern, as a reference, an index seek or a scan."""
        variable = pattern.variable or self._anonymous()
        if pattern.variable is not None and pattern.variable in self.tables:
            if pattern.labels:
                self._require_same_table(pattern.variable, pattern.labels)
            if pattern.properties is not None:
                terms = terms + list(self._property_terms(variable, pattern.properties))
            return pipeline, terms, variable
        table = self._node_table_of(pattern)
        self.tables[variable] = table
        if pattern.properties is not None:
            terms = terms + list(self._property_terms(variable, pattern.properties))
        seek, remaining = self._index_seek(pipeline, variable, table, terms)
        if seek is not None:
            return seek, remaining, variable
        return NodeScan(child=pipeline, variable=variable, table=table), terms, variable

    def _node_table_of(self, pattern: NodePattern) -> TableDef:
        """Return the table a matched node reads, refusing a pattern that names none or many."""
        if len(pattern.labels) != 1:
            raise GrafxPlanError(
                "A matched node names exactly one label, because a row lives in exactly one "
                f"table; got {pattern.describe()}.",
                field="labels",
                value=pattern.describe(),
            )
        table = self._table_named(pattern.labels[0], "label")
        if table.kind != "node":
            raise GrafxPlanError(
                f"The label {table.name!r} names a {table.kind} table, so it cannot match a "
                "node.",
                field="label",
                value=table.name,
            )
        return table

    def _require_same_table(self, variable: str, labels: tuple[str, ...]) -> None:
        """Refuse a second mention of a variable under a different label."""
        bound = self.tables[variable]
        if len(labels) != 1 or labels[0] != bound.name:
            raise GrafxPlanError(
                f"The variable {variable!r} is already bound to {bound.name!r} and is mentioned "
                f"again as {list(labels)}.",
                field="variable",
                value=variable,
            )

    def _property_terms(
        self, variable: str, properties: MapExpression
    ) -> tuple[Expression, ...]:
        """Return the equality terms an inline property map is shorthand for."""
        return tuple(
            BinaryOperation(
                operator="=",
                left=Property(subject=Variable(name=variable), key=entry.key),
                right=entry.value,
            )
            for entry in properties.entries
        )

    def _index_seek(
        self, pipeline: PlanNode, variable: str, table: TableDef, terms: list[Expression]
    ) -> tuple[PlanNode | None, list[Expression]]:
        """Return an index seek for this variable when an index answers the predicate exactly."""
        constrained: dict[str, tuple[Expression, Expression]] = {}
        for term in terms:
            binding = self._equality_on(term, variable, table)
            if binding is None:
                continue
            column, value = binding
            if column not in constrained:
                constrained[column] = (term, value)
        if not constrained:
            return None, terms
        found = self._index_for(table, tuple(constrained))
        if found is None:
            return None, terms
        definition, columns = found
        used = [constrained[column][0] for column in columns]
        remaining = [term for term in terms if term not in used]
        return (
            IndexSeek(
                child=pipeline,
                variable=variable,
                table=table,
                index=definition.name,
                visibility=definition.visibility,
                key_columns=columns,
                key_values=tuple(constrained[column][1] for column in columns),
            ),
            remaining,
        )

    def _equality_on(
        self, term: Expression, variable: str, table: TableDef
    ) -> tuple[str, Expression] | None:
        """Return the column and value an equality on this variable constrains, or None."""
        if not isinstance(term, BinaryOperation) or term.operator != "=":
            return None
        for subject, value in ((term.left, term.right), (term.right, term.left)):
            if not isinstance(subject, Property) or not isinstance(subject.subject, Variable):
                continue
            if subject.subject.name != variable:
                continue
            if free_variables(value):
                continue
            if not isinstance(value, (Literal, Parameter)):
                continue
            if self._column_of(table, subject.key) is None:
                raise GrafxPlanError(
                    f"Table {table.name!r} has no column named {subject.key!r}.",
                    field="column",
                    value=subject.key,
                )
            return subject.key, value
        return None

    def _traverse(
        self,
        pipeline: PlanNode,
        source: str,
        relationship: RelationshipPattern,
        target_pattern: NodePattern,
    ) -> tuple[PlanNode, str]:
        """Plan one relationship hop, or a bounded range of them."""
        if len(relationship.types) != 1:
            raise GrafxPlanError(
                "A matched relationship names exactly one type, because a relationship lives in "
                f"exactly one table; got {relationship.describe()}.",
                field="types",
                value=relationship.describe(),
            )
        table = self._table_named(relationship.types[0], "type")
        if table.kind != "rel":
            raise GrafxPlanError(
                f"The type {table.name!r} names a {table.kind} table, so it cannot match a "
                "relationship.",
                field="type",
                value=table.name,
            )
        if relationship.properties is not None:
            raise GrafxPlanError(
                "A matched relationship carries no inline property map in this dialect; write "
                f"the condition in WHERE instead of {relationship.describe()}.",
                field="properties",
                value=relationship.describe(),
            )
        self._require_endpoint(source, table, relationship.direction)
        if relationship.variable is not None:
            # A matched relationship variable names its table exactly as a written one does in
            # ``_written_pattern``. Without this the table map knew every node of the pattern
            # and none of its edges, so DELETE r refused a variable the traversal had already
            # bound at runtime -- the plan, not the execution, was what lacked the edge.
            self.tables[relationship.variable] = table
        named = target_pattern.variable
        already_bound = named is not None and named in self.tables
        target_variable = named if named is not None else self._anonymous()
        target_table = self._target_table(table, relationship.direction, target_pattern)
        if already_bound and named is not None:
            if target_pattern.labels:
                self._require_same_table(named, target_pattern.labels)
            target_table = self.tables[named]
        elif target_table is not None:
            self.tables[target_variable] = target_table
        return (
            TraverseRelationship(
                child=pipeline,
                source=source,
                target=target_variable,
                relationship=relationship.variable,
                table=table,
                direction=relationship.direction,
                min_hops=relationship.min_hops,
                max_hops=relationship.max_hops,
                target_table=target_table,
                target_bound=already_bound,
            ),
            target_variable,
        )

    def _require_endpoint(self, source: str, table: TableDef, direction: Direction) -> None:
        """Refuse a hop whose start cannot be an endpoint of that relationship table."""
        bound = self.tables.get(source)
        if bound is None:
            return
        allowed: tuple[str, ...]
        if direction is Direction.OUTGOING:
            allowed = (str(table.from_table),)
        elif direction is Direction.INCOMING:
            allowed = (str(table.to_table),)
        else:
            allowed = (str(table.from_table), str(table.to_table))
        if bound.name not in allowed:
            raise GrafxPlanError(
                f"A {table.name!r} relationship written this way starts at "
                f"{' or '.join(allowed)}, and {source!r} is bound to {bound.name!r}, so this "
                "pattern can match nothing.",
                field="from_table",
                value=bound.name,
            )

    def _target_table(
        self, table: TableDef, direction: Direction, target: NodePattern
    ) -> TableDef | None:
        """Return the table the far end of a hop lands in, when the pattern fixes one."""
        if target.labels:
            if len(target.labels) != 1:
                raise GrafxPlanError(
                    "A matched node names exactly one label; got "
                    f"{target.describe()}.",
                    field="labels",
                    value=target.describe(),
                )
            return self._table_named(target.labels[0], "label")
        if direction is Direction.OUTGOING:
            return self._table_named(str(table.to_table), "to")
        if direction is Direction.INCOMING:
            return self._table_named(str(table.from_table), "from")
        if table.from_table == table.to_table:
            return self._table_named(str(table.to_table), "to")
        return None

    def _anonymous(self) -> str:
        """Return a fresh binding name for a pattern element the query did not name."""
        self.anonymous += 1
        return f"{ANONYMOUS_VARIABLE_PREFIX}{self.anonymous}"

    # --- similarity --------------------------------------------------------------------------

    def _similarity(
        self, pipeline: PlanNode, statement: Query, terms: Sequence[Expression]
    ) -> PlanNode:
        """Place the similarity operator above the whole candidate pipeline, if there is one."""
        use = self.analysis.similarity
        if use is None:
            if terms:
                raise GrafxPlanError(
                    "A predicate reads a similarity score, and this query performs no "
                    "similarity search.",
                    field="predicate",
                    value=terms[0].describe(),
                )
            return pipeline
        column_space = self._similarity_space(use)
        threshold_operator, threshold, residual = self._threshold(terms, use.call)
        bound = self._fusible_k(statement, residual)
        search = VectorSearch(
            child=pipeline,
            variable=use.variable,
            space=use.space,
            query_vector=use.query_vector,
            property_key=use.property_key,
            score_column=SCORE_COLUMN,
            column_space=column_space,
            k=bound,
            threshold=threshold,
            threshold_operator=threshold_operator,
        )
        predicate = _conjoin(residual)
        if predicate is None:
            return search
        return FilterRows(child=search, predicate=predicate)

    def _similarity_space(self, use: SimilarityUse) -> str:
        """Return the space the searched column belongs to, refusing a cross-space search.

        SPEC-VEC BR-1 makes a comparison between two embedding spaces a typed refusal rather
        than a meaningless ranking, and the earliest place that refusal can happen is here: a
        query that names a space the column does not belong to is wrong before a single vector
        is read. A space named by a PARAMETER cannot be judged now, so the column's own space
        travels into the plan and the executor makes the same comparison once the value is
        bound -- one rule, checked wherever the name becomes known.
        """
        column_space = self._require_vector_column(use.variable, use.property_key)
        named = use.space
        if not isinstance(named, Literal):
            return column_space
        if not isinstance(named.value, str):
            raise GrafxPlanError(
                f"An embedding space is named by text; got {named.describe()}.",
                field="space",
                value=named.describe(),
            )
        self._space_named(named.value)
        if named.value != column_space:
            raise GrafxEmbeddingSpaceMismatch(
                f"The column {use.variable}.{use.property_key} stores vectors of the embedding "
                f"space {column_space!r}, and this query searches {named.value!r}; comparing "
                "vectors across spaces is refused rather than ranked.",
                field="space",
                value=named.value,
                column_space=column_space,
            )
        return column_space

    def _require_vector_column(self, variable: str, key: str) -> str:
        """Return the space a searched column belongs to, refusing one that stores no vector."""
        table = self.tables.get(variable)
        if table is None:
            raise GrafxPlanError(
                f"The variable {variable!r} used by the similarity search is bound to no table.",
                field="variable",
                value=variable,
            )
        column = self._column_of(table, key)
        if column is None:
            raise GrafxPlanError(
                f"Table {table.name!r} has no column named {key!r}.",
                field="column",
                value=key,
            )
        if not column.is_vector or column.vector_space is None:
            raise GrafxPlanError(
                f"The column {table.name}.{key} stores {column.type.name} and not an embedding, "
                "so a similarity search over it has no meaning.",
                field="column",
                value=key,
            )
        return column.vector_space

    def _threshold(
        self, terms: Sequence[Expression], call: FunctionCall
    ) -> tuple[str | None, Expression | None, list[Expression]]:
        """Take one score floor out of the deferred terms, leaving the rest as a filter."""
        remaining = list(terms)
        for term in terms:
            if not isinstance(term, BinaryOperation):
                continue
            if term.operator not in THRESHOLD_OPERATORS:
                continue
            if term.left != call or self._reads_similarity(term.right):
                continue
            if free_variables(term.right):
                continue
            remaining.remove(term)
            return term.operator, term.right, remaining
        return None, None, remaining

    def _fusible_k(self, statement: Query, residual: Sequence[Expression]) -> Expression | None:
        """Return the neighbour count a LIMIT may become, or None when fusing would drop rows.

        Every condition here is a way a row could be discarded after the vector operator ran. A
        residual filter, DISTINCT and an aggregate each drop rows; a sort by anything other than
        the score means the k best by score are not the k the query wants; a SKIP is fusible only
        by adding it to the limit, because the rows it drops still have to be produced.
        """
        clause = statement.return_clause
        if clause is None or residual or clause.distinct or self.analysis.aggregated:
            return None
        if clause.limit is None or len(clause.sort_items) != 1:
            return None
        key = clause.sort_items[0]
        if not key.descending or not self._sorts_by_score(clause, key):
            return None
        if clause.skip is None:
            return clause.limit
        return BinaryOperation(operator="+", left=clause.limit, right=clause.skip)

    def _sorts_by_score(self, clause: ReturnClause, key: SortItem) -> bool:
        """Return True when this ORDER BY key is the similarity score and nothing else."""
        expression = key.expression
        if isinstance(expression, Variable):
            for item in clause.items:
                if item.alias == expression.name:
                    expression = item.expression
                    break
        if not isinstance(expression, FunctionCall):
            return False
        return expression.name.upper() in (SIMILARITY_FUNCTION, SIMILARITY_SCORE_FUNCTION)

    # --- writes ------------------------------------------------------------------------------

    def _updating_clause(self, pipeline: PlanNode, clause: UpdatingClause) -> PlanNode:
        """Plan one clause that writes."""
        if isinstance(clause, CreateClause):
            for pattern in clause.patterns:
                pipeline = self._create(pipeline, pattern)
            return pipeline
        if isinstance(clause, MergeClause):
            return self._merge(pipeline, clause.pattern)
        if isinstance(clause, SetClause):
            return SetProperties(
                child=pipeline,
                assignments=tuple(
                    PropertyAssignment(target=item.target, value=item.value)
                    for item in clause.items
                ),
            )
        if isinstance(clause, DeleteClause):
            for target in clause.targets:
                if target.name not in self.tables:
                    raise GrafxPlanError(
                        f"The variable {target.name!r} is bound to no table, so DELETE has "
                        "nothing to remove.",
                        field="variable",
                        value=target.name,
                    )
            return DeleteEntities(
                child=pipeline,
                variables=tuple(target.name for target in clause.targets),
                detach=clause.detach,
            )
        raise GrafxPlanError(
            f"A clause of type {type(clause).__name__} cannot be planned.",
            field="clause",
            value=type(clause).__name__,
        )

    def _create(self, pipeline: PlanNode, pattern: PatternPath) -> PlanNode:
        """Plan a CREATE pattern into the nodes and relationships it inserts."""
        nodes, relationships = self._written_pattern(pattern, "CREATE")
        return CreateRelationships(
            child=pipeline, nodes=nodes, relationships=relationships
        )

    def _merge(self, pipeline: PlanNode, pattern: PatternPath) -> PlanNode:
        """Plan a MERGE pattern, which this dialect supports in two shapes."""
        if len(pattern.relationships) > 1:
            raise GrafxPlanError(
                "MERGE covers one node or one relationship between two bound nodes in this "
                f"dialect; got {pattern.describe()}.",
                field="pattern",
                value=pattern.describe(),
            )
        if pattern.relationships:
            for endpoint in (pattern.nodes[0], pattern.nodes[1]):
                if endpoint.variable is None or endpoint.variable not in self.tables:
                    raise GrafxPlanError(
                        "MERGE of a relationship needs both of its nodes already matched; "
                        f"{endpoint.describe()} is not.",
                        field="pattern",
                        value=pattern.describe(),
                    )
        nodes, relationships = self._written_pattern(pattern, "MERGE")
        return MergePattern(child=pipeline, nodes=nodes, relationships=relationships)

    def _written_pattern(
        self, pattern: PatternPath, keyword: str
    ) -> tuple[tuple[CreatedNode, ...], tuple[CreatedRelationship, ...]]:
        """Resolve one written pattern into the nodes and relationships it names."""
        nodes: list[CreatedNode] = []
        names: list[str] = []
        for node in pattern.nodes:
            variable = node.variable or self._anonymous()
            if node.variable is not None and node.variable in self.tables:
                if node.properties is not None:
                    raise GrafxPlanError(
                        f"{keyword} cannot give properties to {node.variable!r}, which an "
                        "earlier clause already bound; use SET instead.",
                        field="properties",
                        value=node.describe(),
                    )
                nodes.append(
                    CreatedNode(variable=variable, table=None, properties=None)
                )
                names.append(variable)
                continue
            table = self._node_table_of(node)
            self.tables[variable] = table
            nodes.append(
                CreatedNode(variable=variable, table=table, properties=node.properties)
            )
            names.append(variable)
        relationships: list[CreatedRelationship] = []
        for position, relationship in enumerate(pattern.relationships):
            table = self._table_named(relationship.types[0], "type")
            if table.kind != "rel":
                raise GrafxPlanError(
                    f"The type {table.name!r} names a {table.kind} table, so it cannot be "
                    "written as a relationship.",
                    field="type",
                    value=table.name,
                )
            left, right = names[position], names[position + 1]
            if relationship.direction is Direction.INCOMING:
                left, right = right, left
            self._require_written_endpoint(left, str(table.from_table), table)
            self._require_written_endpoint(right, str(table.to_table), table)
            if relationship.variable is not None:
                self.tables[relationship.variable] = table
            relationships.append(
                CreatedRelationship(
                    variable=relationship.variable,
                    table=table,
                    source=left,
                    target=right,
                    direction=Direction.OUTGOING,
                    properties=relationship.properties,
                )
            )
        return tuple(nodes), tuple(relationships)

    def _require_written_endpoint(
        self, variable: str, expected: str, table: TableDef
    ) -> None:
        """Refuse a written relationship whose endpoint is not the table the schema declares."""
        bound = self.tables.get(variable)
        if bound is None or bound.name == expected:
            return
        raise GrafxPlanError(
            f"A {table.name!r} relationship connects {table.from_table} to {table.to_table}, "
            f"and {variable!r} is bound to {bound.name!r}.",
            field="endpoint",
            value=bound.name,
        )

    # --- results -----------------------------------------------------------------------------

    def _result(self, pipeline: PlanNode, clause: ReturnClause) -> PlanNode:
        """Shape the rows a query returns: group, project, deduplicate, order and window."""
        if self.analysis.aggregated:
            pipeline = AggregateRows(
                child=pipeline,
                grouping=tuple(
                    clause.items[position] for position in self.analysis.grouping_positions
                ),
                aggregations=self.analysis.aggregations,
            )
        pipeline = ProjectRows(child=pipeline, items=self._projected(clause))
        if clause.distinct:
            pipeline = DistinctRows(child=pipeline)
        if clause.sort_items:
            pipeline = SortRows(child=pipeline, keys=clause.sort_items)
        if clause.skip is not None:
            pipeline = SkipRows(child=pipeline, count=clause.skip)
        if clause.limit is not None:
            pipeline = LimitRows(child=pipeline, count=clause.limit)
        return pipeline

    def _projected(self, clause: ReturnClause) -> tuple[ReturnItem, ...]:
        """Return the projected items, giving every one of them the name it is read under."""
        return tuple(
            ReturnItem(expression=item.expression, alias=item.name) for item in clause.items
        )
